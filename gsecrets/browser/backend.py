# SPDX-License-Identifier: GPL-3.0-only
"""The application-facing half of the browser integration.

Everything that needs GTK, GLib or knowledge of Cipher's widget tree lives
here, so that gsecrets.browser.protocol stays testable without a display.
"""

from __future__ import annotations

import asyncio
import logging
import typing
import weakref
from gettext import gettext as _

from gi.repository import Adw, Gio, GLib, Gtk

from gsecrets import config_manager, password_generator
from gsecrets.safe_element import SafeGroup

# Shortest gap between honouring two unlock requests, in microseconds to match
# GLib's monotonic clock.
UNLOCK_REQUEST_INTERVAL = 3 * 1000 * 1000

if typing.TYPE_CHECKING:
    from collections.abc import Callable

    from pykeepass import PyKeePass

    from gsecrets.database_manager import DatabaseManager


class ApplicationBackend:
    """Serves protocol requests from the application's unlocked database."""

    def __init__(self, application: Gio.Application) -> None:
        self._application = application

        # Called with "database-locked" or "database-unlocked" when the safe's
        # state changes, so the server can tell connected browsers. Set by
        # whoever owns the server, since the backend is built first.
        self.on_state_change: Callable[[str], None] | None = None

        # Windows and DatabaseManagers already connected to, mapped to the
        # handler that was attached, so a second pass does not add a duplicate
        # listener and stop_watching() can take them all off again. Weak keys,
        # for two reasons: watching something must not keep it alive, and an
        # entry that outlived its object would be a hazard -- the id()-keyed set
        # this replaces could match a later object allocated at the same address
        # and then never watch it at all.
        self._watched_windows: weakref.WeakKeyDictionary = (
            weakref.WeakKeyDictionary()
        )
        self._watched_managers: weakref.WeakKeyDictionary = (
            weakref.WeakKeyDictionary()
        )
        self._application_handlers: list[int] = []

        # The last state reported, so that a change which does not alter the
        # answer is not announced as news. None until the first observation.
        self._unlocked: bool | None = None

        self._last_unlock_request = 0

        self._watch_application()

    # -- database access -------------------------------------------------

    def _iter_managers(self):
        """Yield every DatabaseManager the open windows hold, locked or not."""
        for window in self._application.get_windows():
            unlocked_db = getattr(window, "unlocked_db", None)
            if unlocked_db is None:
                continue

            database_manager = unlocked_db.database_manager
            if database_manager is not None:
                yield database_manager

    # -- state watching --------------------------------------------------

    def _watch_application(self) -> None:
        """Follow windows as they appear and disappear.

        This used to be done opportunistically, from _database_manager(), which
        meant state was only ever discovered while answering a request. That is
        backwards: the whole point of the signal is to spare the browser from
        having to ask. It also missed the first unlock outright, since nothing
        could have discovered the manager beforehand, and missed a lock
        entirely if the browser had connected without yet sending anything.
        """
        for signal, handler in (
            ("window-added", self._on_window_added),
            ("window-removed", self._on_window_removed),
        ):
            self._application_handlers.append(
                self._application.connect(signal, handler)
            )

        # Windows that already exist; window-added has been and gone for these.
        for window in self._application.get_windows():
            self._watch_window(window)

        # Record the starting point without announcing it. Nothing is listening
        # yet -- on_state_change is assigned by the server's owner after this
        # returns -- and a browser that connects later is told the state by the
        # handshake anyway.
        self._refresh_state()

    def _watch_window(self, window: Gtk.Window) -> None:
        """Watch a window for a safe being unlocked in it."""
        if window in self._watched_windows:
            return

        self._watched_windows[window] = window.connect(
            "notify::unlocked-db", self._on_unlocked_db_changed
        )

    def _on_window_added(self, _application: Gio.Application, window: Gtk.Window):
        self._watch_window(window)
        # Ordinarily a new window has no safe in it yet and this finds nothing,
        # but a window is not required to arrive empty.
        self._watch_managers()
        self._refresh_state()

    def _on_window_removed(self, _application: Gio.Application, _window: Gtk.Window):
        # A window closing takes its safe with it, so the browser can no longer
        # reach what it could a moment ago.
        self._refresh_state()

    def _on_unlocked_db_changed(self, _window: Gtk.Window, _pspec: object) -> None:
        self._watch_managers()
        self._refresh_state()

    def _watch_managers(self) -> None:
        """Attach a lock-state listener to any manager not yet watched.

        Managers are watched whether locked or not, so that both directions are
        reported: a safe locked from Cipher's own interface, and one unlocked
        again by password, quick unlock or fingerprint.

        Still called from _database_manager() as well as from the window
        watcher, which costs nothing and covers a manager that was already in
        place before this backend existed -- browser integration can be
        switched on at any time, including with a safe already open.
        """
        for database_manager in self._iter_managers():
            if database_manager in self._watched_managers:
                continue

            self._watched_managers[database_manager] = database_manager.connect(
                "notify::locked", self._on_locked_changed
            )

    def stop_watching(self) -> None:
        """Take every listener back off again.

        Switching browser integration off and on builds a fresh backend, but the
        application and its windows outlive both. Left connected, the old
        backend stays alive on the strength of its own signal connections and
        goes on recomputing state for a server that has stopped.
        """
        for handler_id in self._application_handlers:
            self._application.disconnect(handler_id)
        self._application_handlers.clear()

        for watched in (self._watched_windows, self._watched_managers):
            for obj, handler_id in list(watched.items()):
                obj.disconnect(handler_id)
            watched.clear()

        self.on_state_change = None

    def _on_locked_changed(
        self, _database_manager: DatabaseManager, _pspec: object
    ) -> None:
        self._refresh_state()

    def _refresh_state(self) -> None:
        """Report the safe's state, but only when it has actually changed.

        Aggregated over every window rather than reported per manager: with two
        windows open the browser can still reach an unlocked safe after one of
        them locks, and saying otherwise would have it show a locked icon over a
        safe it can read. Deduplicating here also means the several paths into
        this method -- a lock, a window closing, a safe being swapped for
        another -- cannot between them announce the same state twice.
        """
        unlocked = any(
            not database_manager.props.locked and database_manager.db is not None
            for database_manager in self._iter_managers()
        )

        if unlocked == self._unlocked:
            return

        self._unlocked = unlocked

        if self.on_state_change is None:
            return

        self.on_state_change("database-unlocked" if unlocked else "database-locked")

    def _database_manager(self) -> DatabaseManager | None:
        """Find an unlocked database among the open windows.

        Returns None when nothing is unlocked, which the protocol layer turns
        into DATABASE_NOT_OPENED. Never unlocks anything itself; bringing up the
        window is request_unlock()'s job, and only when the browser asked for it.
        """
        self._watch_managers()

        for database_manager in self._iter_managers():
            if database_manager.props.locked or database_manager.db is None:
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

    async def request_unlock(self) -> None:
        """Bring up the window so the user can unlock the safe.

        Sent by the extension as triggerUnlock when the user actively asked for
        credentials against a locked safe. Returns without waiting: the request
        that prompted it is answered with DATABASE_NOT_OPENED, and the browser
        finds out the safe is open from the database-unlocked signal that the
        window watcher emits once the unlock goes through.

        Rate limited. Any process on the socket can ask for this, including
        before it has associated, since get-databasehash needs no association.
        Without a limit that is a way to flood the desktop with window
        activations, and to train a user into typing their password at an
        unexpected prompt.
        """
        now = GLib.get_monotonic_time()
        if now - self._last_unlock_request < UNLOCK_REQUEST_INTERVAL:
            logging.debug("Ignoring unlock request; one was just made")
            return

        self._last_unlock_request = now

        # Mirrors do_activate(): present what is there, otherwise create a
        # window on the initial screen. Deliberately does not choose a safe or
        # prefill anything -- the user does that.
        window = self._application.get_active_window()
        if window is None:
            self._application.activate()
            logging.info("Browser asked to unlock; opened a window")
            return

        window.present()
        logging.info("Browser asked to unlock; presented the window")

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

    def icon_color(self) -> str:
        return config_manager.get_icon_color()

    async def generate_password(self) -> str:
        """Generate a password using the application's own generator settings.

        The same settings the built-in generator uses, so a password produced
        for the browser matches one produced in the interface. Synchronous and
        fast; it is bounded internally, so an unsatisfiable combination of
        requirements cannot stall the loop this runs on.
        """
        return password_generator.generate(
            config_manager.get_generator_length(),
            config_manager.get_generator_use_uppercase(),
            config_manager.get_generator_use_lowercase(),
            config_manager.get_generator_use_numbers(),
            config_manager.get_generator_use_symbols(),
        )

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
