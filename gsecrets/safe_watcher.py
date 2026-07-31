# SPDX-License-Identifier: GPL-3.0-only
"""Tells interested parts of the application when a safe opens or closes.

Three things need this and none of them can get it from a single signal: the
browser integration pushes the state to connected extensions, the tray icon
paints itself by it, and the suspend guard locks by it. There is no one place
that knows, either -- a safe can appear when a window is unlocked, disappear
when a window is closed, and flip either way when the lock property changes --
so the answer is assembled from all three and reported when it actually moves.
"""

from __future__ import annotations

import logging
import typing
import weakref

from gi.repository import Gio, Gtk

if typing.TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from gsecrets.database_manager import DatabaseManager


def iter_managers(application: Gio.Application) -> Iterator[DatabaseManager]:
    """Yield every DatabaseManager the open windows hold, locked or not.

    Defensive per window: this is reached from a D-Bus callback while the
    machine is suspending and from a tray repaint, and in neither case should
    one window in a strange state hide the safe in the next one.
    """
    for window in application.get_windows():
        try:
            unlocked_db = getattr(window, "unlocked_db", None)
            if unlocked_db is None:
                continue

            database_manager = unlocked_db.database_manager
        except Exception:  # noqa: BLE001
            logging.exception("Could not inspect a window's safe")
            continue

        if database_manager is not None:
            yield database_manager


def any_unlocked(application: Gio.Application) -> bool:
    """Whether any window currently holds a safe that can be read."""
    for database_manager in iter_managers(application):
        try:
            if not database_manager.props.locked and database_manager.db is not None:
                return True
        except Exception:  # noqa: BLE001
            logging.exception("Could not read a safe's lock state")

    return False


class SafeWatcher:
    """Calls back when the application goes from having an open safe to not.

    Aggregated over every window rather than reported per safe: with two windows
    open, one locking does not mean there is nothing to reach, and a consumer
    told otherwise would show a locked icon over a safe that is still readable.
    Deduplicated, so the several paths into a refresh -- a lock, a window
    closing, a safe swapped for another -- cannot between them report the same
    state twice.
    """

    def __init__(
        self,
        application: Gio.Application,
        on_change: Callable[[bool], None] | None = None,
    ) -> None:
        self._application = application

        # Called with True when a safe becomes reachable and False when the last
        # one stops being. Assignable after construction, since a consumer is
        # often built before the thing it reports to.
        self.on_change = on_change

        # Windows and managers already connected to, mapped to the handler
        # attached, so a second pass does not add a duplicate listener and
        # stop() can take them all off again. Weak keys, for two reasons:
        # watching something must not keep it alive, and an entry that outlived
        # its object would be a hazard -- an id()-keyed set could match a later
        # object allocated at the same address and then never watch it at all.
        self._windows: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._managers: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._application_handlers: list[int] = []

        self._unlocked: bool | None = None

    @property
    def unlocked(self) -> bool:
        """The last observed state, without re-deriving it."""
        return bool(self._unlocked)

    def managers(self) -> Iterator[DatabaseManager]:
        return iter_managers(self._application)

    def start(self) -> None:
        """Follow windows as they appear and disappear.

        Doing this from whatever request happens to arrive instead -- the shape
        this replaces -- means state is only ever discovered while something
        else is being answered. That misses the first unlock outright, since
        nothing could have discovered the safe beforehand.
        """
        if self._application_handlers:
            return

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

        # And safes that are already open in them, which have no notify left to
        # catch either. Without this a consumer started while a safe is unlocked
        # -- the tray, when the setting is switched on mid-session -- would never
        # hear it lock.
        self.watch_managers()

        # Seed the state without announcing it. There has been no change yet;
        # a consumer that needs the starting point reads `unlocked`.
        self._refresh(notify=False)

    def stop(self) -> None:
        """Take every listener back off again.

        A consumer that is switched off -- browser integration, the tray --
        would otherwise stay alive on the strength of its own connections, and
        go on recomputing state for something that has gone away.
        """
        for handler_id in self._application_handlers:
            self._application.disconnect(handler_id)
        self._application_handlers.clear()

        for watched in (self._windows, self._managers):
            for obj, handler_id in list(watched.items()):
                obj.disconnect(handler_id)
            watched.clear()

        self.on_change = None

    def watch_managers(self) -> None:
        """Attach a lock listener to any manager not yet watched.

        Managers are watched whether locked or not, so both directions are
        reported: a safe locked from the interface, and one unlocked again by
        password, quick unlock or fingerprint. Public because a consumer built
        after a safe was already open has no notify to have caught.
        """
        for database_manager in self.managers():
            if database_manager in self._managers:
                continue

            self._managers[database_manager] = database_manager.connect(
                "notify::locked", self._on_locked_changed
            )

    def refresh(self) -> None:
        """Report the current state, if it differs from the last one."""
        self._refresh(notify=True)

    def _refresh(self, *, notify: bool) -> None:
        unlocked = any_unlocked(self._application)
        if unlocked == self._unlocked:
            return

        self._unlocked = unlocked

        if notify and self.on_change is not None:
            self.on_change(unlocked)

    # -- listeners -------------------------------------------------------

    def _watch_window(self, window: Gtk.Window) -> None:
        if window in self._windows:
            return

        self._windows[window] = window.connect(
            "notify::unlocked-db", self._on_unlocked_db_changed
        )

    def _on_window_added(self, _application: Gio.Application, window: Gtk.Window):
        self._watch_window(window)
        # Ordinarily a new window has no safe in it yet and this finds nothing,
        # but a window is not required to arrive empty.
        self.watch_managers()
        self.refresh()

    def _on_window_removed(self, _application: Gio.Application, _window: Gtk.Window):
        # A window closing takes its safe with it, so what could be reached a
        # moment ago no longer can be.
        self.refresh()

    def _on_unlocked_db_changed(self, _window: Gtk.Window, _pspec: object) -> None:
        self.watch_managers()
        self.refresh()

    def _on_locked_changed(self, _manager: DatabaseManager, _pspec: object) -> None:
        self.refresh()
