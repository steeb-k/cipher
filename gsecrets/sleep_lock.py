# SPDX-License-Identifier: GPL-3.0-only
"""Lock every open safe before the system suspends.

Suspending used to leave a safe open indefinitely. Neither existing guard covers
it:

  * The inactivity timer is a GLib timeout, and those run on the monotonic
    clock, which does not advance while the machine is suspended. A safe with
    four minutes left on a five minute timer still has four minutes left when
    the machine wakes, however many hours later.
  * Locking on session lock depends on something else locking the screen. Most
    desktops do that around a suspend, but it is a separate setting in a
    separate component, and when it is off -- or when the screen locks only
    after the resume -- nothing ever tells the safe anything.

So this watches for the suspend itself, on logind's PrepareForSleep, which is
broadcast before the machine goes down whatever the desktop does about screens.
"""

from __future__ import annotations

import logging
import os

from gi.repository import Gio, GLib

from gsecrets import config_manager, const
from gsecrets.safe_watcher import iter_managers

DBUS_TIMEOUT = 500  # In milliseconds

LOGIN1_BUS = "org.freedesktop.login1"
LOGIN1_PATH = "/org/freedesktop/login1"
LOGIN1_MANAGER = "org.freedesktop.login1.Manager"

# How often to look at whether the save that locking kicked off has landed, and
# how long to keep looking. The budget stays under logind's InhibitDelayMaxSec,
# five seconds by default: past that the suspend proceeds regardless, so waiting
# longer would only mean holding a lock that no longer delays anything.
SAVE_POLL_INTERVAL = 100  # In milliseconds
SAVE_WAIT_BUDGET = 4000  # In milliseconds


class SleepLock:
    """Locks open safes when logind announces a suspend.

    The lock itself is instant -- assigning the locked property is what shuts
    the safe, and it takes effect before anything can await -- so the safe is
    closed no matter how abruptly the machine goes down. The delay inhibitor is
    for what follows: locking kicks off the automatic save, and with no lock
    held the system carries on suspending while that write is still in flight.
    Holding one buys logind's grace period to finish it.

    A delay inhibitor is not a block: it postpones a suspend only for as long as
    the holder takes to release, and logind caps that. Dropping it is what says
    we are done, so it is dropped the moment the save settles rather than being
    sat on for the full budget.
    """

    def __init__(self, application: Gio.Application) -> None:
        self._application = application
        self._connection: Gio.DBusConnection | None = None
        self._subscription = 0
        self._fd = -1
        self._waiting = False

    async def start(self) -> None:
        """Subscribe to PrepareForSleep and take the inhibitor."""
        if self._subscription:
            return

        try:
            self._connection = await Gio.bus_get(Gio.BusType.SYSTEM, None)
            self._subscription = self._connection.signal_subscribe(
                LOGIN1_BUS,
                LOGIN1_MANAGER,
                "PrepareForSleep",
                LOGIN1_PATH,
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_prepare_for_sleep,
                None,
            )
        except GLib.Error as err:
            # Best effort. logind may be absent, and a safe that does not lock
            # on suspend is a great deal better than one that will not open.
            logging.warning("Could not watch for suspend: %s", err)
            return

        logging.debug("Watching for suspend")
        await self.sync_inhibitor()

    def stop(self) -> None:
        """Stop watching and drop the inhibitor.

        Synchronous, so it is usable from application shutdown, where the event
        loop is already going away and a scheduled task would never run.
        """
        if self._subscription and self._connection is not None:
            self._connection.signal_unsubscribe(self._subscription)

        self._subscription = 0
        self._drop_inhibitor()

    # -- inhibitor -------------------------------------------------------

    async def sync_inhibitor(self) -> None:
        """Hold the inhibitor while the setting asks for it, and not otherwise.

        Held for as long as Cipher runs rather than only while a safe is open.
        Releasing is prompt, so the cost to a suspend with nothing to lock is
        the microseconds it takes to notice that; tying it to the safes instead
        would mean tracking their state here purely to save that.
        """
        if config_manager.get_lock_on_suspend():
            await self._take_inhibitor()
        else:
            self._drop_inhibitor()

    async def _take_inhibitor(self) -> None:
        if self._fd != -1 or self._connection is None:
            return

        try:
            reply, fd_list = await self._connection.call_with_unix_fd_list(
                LOGIN1_BUS,
                LOGIN1_PATH,
                LOGIN1_MANAGER,
                "Inhibit",
                GLib.Variant(
                    "(ssss)",
                    (
                        "sleep",
                        const.NAME,
                        "Locking the open safe",
                        # Delay, never block. A block would let a password
                        # manager refuse to let the machine sleep at all.
                        "delay",
                    ),
                ),
                GLib.VariantType("(h)"),
                Gio.DBusCallFlags.NONE,
                DBUS_TIMEOUT,
                None,
                None,
            )
        except GLib.Error as err:
            # The safe still locks on PrepareForSleep; only the save that
            # follows is now racing the suspend.
            logging.debug("Could not take the sleep inhibitor: %s", err)
            return

        if fd_list is not None:
            self._fd = fd_list.get(reply[0])

    def _drop_inhibitor(self) -> None:
        if self._fd == -1:
            return

        fd, self._fd = self._fd, -1
        try:
            os.close(fd)
        except OSError as err:
            logging.debug("Could not drop the sleep inhibitor: %s", err)

    # -- locking ---------------------------------------------------------

    def _managers(self):
        """Yield every DatabaseManager the open windows hold, locked or not."""
        return iter_managers(self._application)

    def _on_prepare_for_sleep(self, *args) -> None:
        # (connection, sender, path, interface, signal, parameters, user_data)
        going_to_sleep = args[5][0]

        if not going_to_sleep:
            # Coming back up. The inhibitor was dropped on the way down, so take
            # it again ready for the next suspend.
            self._application.create_asyncio_task(self.sync_inhibitor())
            return

        if not config_manager.get_lock_on_suspend():
            self._drop_inhibitor()
            return

        locked = self._lock_all()
        if not locked:
            self._drop_inhibitor()
            return

        # Let the save that locking started finish before releasing logind.
        self._wait_for_saves()

    def _lock_all(self) -> int:
        """Lock every open safe, returning how many were shut.

        Assigning the property is the whole of it: the notify handler saves,
        tears the view down and returns the window to its unlock screen, exactly
        as it does for a lock from the menu or the inactivity timer.
        """
        locked = 0
        for database_manager in list(self._managers()):
            # Reading the state is inside the guard as well as writing it.
            # Never let one safe's failure leave the others open, and never
            # raise out of a D-Bus callback while the machine is going down,
            # where the only result is a traceback nobody can read.
            try:
                if database_manager.props.locked:
                    continue

                database_manager.props.locked = True
            except Exception:  # noqa: BLE001
                logging.exception("Could not lock a safe before suspend")
                continue

            locked += 1

        if locked:
            logging.info("Locked %s safe(s) before suspend", locked)

        return locked

    def _saves_pending(self) -> bool:
        for database_manager in self._managers():
            try:
                if database_manager.props.is_dirty or database_manager.save_running:
                    return True
            except Exception:  # noqa: BLE001
                # Runs from a timeout while the machine is going down; a safe
                # that cannot be asked is not a reason to keep logind waiting.
                logging.exception("Could not check a safe for pending saves")

        return False

    def _wait_for_saves(self) -> None:
        """Drop the inhibitor once the safes have finished writing."""
        if self._waiting:
            return

        if not self._saves_pending():
            self._drop_inhibitor()
            return

        self._waiting = True
        deadline = GLib.get_monotonic_time() + SAVE_WAIT_BUDGET * 1000

        def poll() -> int:
            if self._saves_pending() and GLib.get_monotonic_time() < deadline:
                return GLib.SOURCE_CONTINUE

            if self._saves_pending():
                logging.warning("Suspending with a save still unfinished")

            self._waiting = False
            self._drop_inhibitor()
            return GLib.SOURCE_REMOVE

        GLib.timeout_add(SAVE_POLL_INTERVAL, poll)
