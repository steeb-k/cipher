# SPDX-License-Identifier: GPL-3.0-only
"""The application-facing half of the browser integration.

Everything that needs GTK, GLib or knowledge of Cipher's widget tree lives
here, so that gsecrets.browser.protocol stays testable without a display.
"""

from __future__ import annotations

import asyncio
import logging
import typing
from gettext import gettext as _

from gi.repository import Adw, Gio, Gtk

from gsecrets.safe_element import SafeGroup

if typing.TYPE_CHECKING:
    from pykeepass import PyKeePass

    from gsecrets.database_manager import DatabaseManager


class ApplicationBackend:
    """Serves protocol requests from the application's unlocked database."""

    def __init__(self, application: Gio.Application) -> None:
        self._application = application

    # -- database access -------------------------------------------------

    def _database_manager(self) -> DatabaseManager | None:
        """Find an unlocked database among the open windows.

        Returns None when nothing is unlocked, which the protocol layer turns
        into DATABASE_NOT_OPENED. Deliberately never triggers an unlock: a
        request arriving from a browser must not be able to raise a password
        prompt, or a hostile local process could induce the user to unlock.
        """
        for window in self._application.get_windows():
            unlocked_db = getattr(window, "unlocked_db", None)
            if unlocked_db is None:
                continue

            database_manager = unlocked_db.database_manager
            if database_manager is None or database_manager.props.locked:
                continue

            if database_manager.db is None:
                continue

            return database_manager

        return None

    def get_database(self) -> PyKeePass | None:
        database_manager = self._database_manager()
        return database_manager.db if database_manager else None

    # -- user interaction ------------------------------------------------

    def _active_window(self) -> Gtk.Window | None:
        return self._application.get_active_window() or next(
            iter(self._application.get_windows()), None
        )

    async def confirm_association(self, key_id: str) -> str | None:
        """Ask the user to name and approve a new browser association."""
        window = self._active_window()
        if window is None:
            logging.warning("Association requested with no window to ask in")
            return None

        entry = Adw.EntryRow(title=_("Name"))
        entry.set_text(_("Browser"))

        group = Adw.PreferencesGroup()
        group.add(entry)

        dialog = Adw.AlertDialog(
            heading=_("Allow Browser Access?"),
            body=_(
                "A browser extension is asking to read passwords from this "
                "safe. Only allow this if you started the request yourself."
            ),
            extra_child=group,
        )
        dialog.add_response("deny", _("Deny"))
        dialog.add_response("allow", _("Allow"))
        dialog.set_response_appearance("allow", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("deny")
        dialog.set_close_response("deny")

        # Bring the window forward: the request originates in another
        # application, so the dialog would otherwise be easy to miss.
        window.present()

        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
            if not future.done():
                future.set_result(response)

        dialog.connect("response", on_response)
        dialog.present(window)

        response = await future
        if response != "allow":
            logging.info("Browser association declined")
            return None

        name = entry.get_text().strip()
        if not name:
            logging.info("Browser association declined: no name given")
            return None

        return name

    # -- writing ---------------------------------------------------------

    def _find_entry(self, database_manager: DatabaseManager, uuid: str):
        """Locate a SafeEntry by the hex UUID handed out in get-logins."""
        entries = database_manager.entries
        for index in range(entries.get_n_items()):
            entry = entries.get_item(index)
            if entry.uuid.hex == uuid:
                return entry

        return None

    def _find_group(self, database_manager: DatabaseManager, uuid: str | None):
        """Locate a SafeGroup by hex UUID, falling back to the root group."""
        if uuid:
            groups = database_manager.groups
            for index in range(groups.get_n_items()):
                group = groups.get_item(index)
                if group.uuid.hex == uuid:
                    return group

            logging.warning("Browser asked for unknown group %s; using root", uuid)

        return SafeGroup.get_root(database_manager)

    async def set_login(
        self,
        *,
        url: str,
        login: str,
        password: str,
        title: str,
        uuid: str | None = None,
        group_uuid: str | None = None,
    ) -> bool:
        """Create or update a login through the model the UI observes.

        Uses SafeEntry/SafeGroup rather than pykeepass directly so that the
        visible entry list updates and the safe is marked dirty: both fall out
        of SafeElement.updated(), which the property setters call.
        """
        database_manager = self._database_manager()
        if database_manager is None:
            raise RuntimeError("no unlocked database available")

        if uuid:
            entry = self._find_entry(database_manager, uuid)
            if entry is None:
                # The extension sends a UUID it was given earlier, so this means
                # the entry has since been deleted. Creating a replacement would
                # resurrect something the user removed, so refuse instead.
                raise RuntimeError(f"no entry with uuid {uuid}")

            entry.props.username = login
            entry.props.password = password
            return False

        group = self._find_group(database_manager, group_uuid)
        entry = group.new_entry(title, login, password)
        entry.props.url = url
        return True

    async def create_group(self, path: str) -> tuple[str, str]:
        """Create a group by name or slash-separated path.

        Existing levels are reused rather than duplicated, so repeating a
        request is harmless. Goes through SafeGroup.new_subgroup() for the same
        reason set_login does: it registers the group with the model that drives
        the UI and marks the safe dirty.
        """
        database_manager = self._database_manager()
        if database_manager is None:
            raise RuntimeError("no unlocked database available")

        parts = [part for part in path.split("/") if part.strip()]
        if not parts:
            raise RuntimeError(f"no usable group name in {path!r}")

        group = SafeGroup.get_root(database_manager)
        for part in parts:
            existing = next(
                (
                    child
                    for child in group.group.subgroups
                    if (child.name or "") == part
                ),
                None,
            )
            if existing is not None:
                group = SafeGroup(database_manager, existing)
                continue

            group = group.new_subgroup(part)

        return group.props.name, group.uuid.hex

    async def lock(self) -> None:
        """Lock the safe, as UnlockedDatabase.lock_safe() does.

        Setting the property is the whole of it; the application's own lock
        action does no more, and everything else -- clearing the view, the
        automatic save loop -- hangs off notify::locked.
        """
        database_manager = self._database_manager()
        if database_manager is None:
            # Already locked, or nothing open. Nothing to do, and not an error:
            # the caller wanted the safe locked and it is.
            return

        database_manager.props.locked = True

    # -- persistence -----------------------------------------------------

    async def save(self) -> None:
        """Write the database out after a protocol handler modified it."""
        database_manager = self._database_manager()
        if database_manager is None:
            logging.warning("Asked to save with no unlocked database")
            return

        # Required: _save_task() returns early when is_dirty is false, so
        # without this the write is silently skipped and the association is
        # lost when the safe is locked.
        database_manager.is_dirty = True

        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        def on_saved(manager: DatabaseManager, result: Gio.AsyncResult) -> None:
            try:
                saved = manager.save_finish(result)
            except Exception as exc:  # noqa: BLE001 - reported, never raised on
                logging.error("Saving after a browser request failed: %s", exc)
                if not future.done():
                    future.set_result(False)
                return

            if not future.done():
                future.set_result(saved)

        database_manager.save_async(on_saved)
        await future
