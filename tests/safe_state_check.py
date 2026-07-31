#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
"""Check how the application tracks and reacts to its safes' lock state.

Covers the three consumers of that state: locking on suspend, the tray icon's
colour, and the shared watcher underneath both.

Suspending used to leave a safe open indefinitely: the inactivity timer runs on
the monotonic clock, which stops while the machine is down, and locking on
session lock depends on something else locking the screen. SleepLock watches
logind's PrepareForSleep instead.

The D-Bus half is not exercised here -- there is no logind to talk to, and no
way to make a machine suspend from a check -- so the connection is left unset,
which makes the inhibitor calls no-ops. What is exercised is everything that
decides what happens when the signal arrives.

    tests/safe_state_check.py
"""

from __future__ import annotations

import atexit
import itertools
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

APP_ID = "io.github.steeb_k.Cipher.SelfTest"


def _install_schema() -> None:
    """Compile this application's GSettings schema into a temporary directory.

    Importing gsecrets.config_manager calls Gio.Settings.new(APP_ID) at module
    level, which aborts the process outright if the schema is not installed.
    """
    schema_dir = Path(tempfile.mkdtemp(prefix="cipher-sleep-schema-"))
    atexit.register(shutil.rmtree, schema_dir, True)

    template = (REPO_ROOT / "data" / "org.gnome.World.Secrets.gschema.xml.in").read_text()
    schema = (
        template.replace("@APP_ID@", APP_ID)
        .replace("@APP_PATH@", "/" + APP_ID.replace(".", "/") + "/")
        .replace("@GETTEXT_PACKAGE@", "secrets")
    )
    (schema_dir / f"{APP_ID}.gschema.xml").write_text(schema)

    subprocess.run(
        ["glib-compile-schemas", str(schema_dir)], check=True, capture_output=True
    )
    os.environ["GSETTINGS_SCHEMA_DIR"] = str(schema_dir)


_install_schema()

module = types.ModuleType("gsecrets.const")
module.APP_ID = APP_ID
module.VERSION = "test"
module.IS_DEVEL = False
module.NAME = "Cipher"
sys.modules["gsecrets.const"] = module

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gsecrets import config_manager  # noqa: E402
from gsecrets.database_manager import DatabaseManager  # noqa: E402
from gsecrets.sleep_lock import SleepLock  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f" -- {detail}" if detail and not condition else ""))


class StubSignals:
    """Connectable the way a GObject is, for the watcher to attach to."""

    _ids = itertools.count(1)

    def __init__(self) -> None:
        self._handlers: dict[str, list] = {}

    def connect(self, signal, handler):
        handler_id = next(self._ids)
        self._handlers.setdefault(signal, []).append((handler_id, handler))
        return handler_id

    def disconnect(self, handler_id) -> None:
        for signal, handlers in self._handlers.items():
            self._handlers[signal] = [h for h in handlers if h[0] != handler_id]

    def emit(self, signal, *args) -> None:
        for _handler_id, handler in list(self._handlers.get(signal, [])):
            handler(self, *args)


class StubUnlockedDatabase:
    def __init__(self, database_manager) -> None:
        self.database_manager = database_manager


class StubWindow(StubSignals):
    def __init__(self, unlocked_db) -> None:
        super().__init__()
        self.unlocked_db = unlocked_db


class StubApplication(StubSignals):
    """Exposes only what SleepLock and SafeWatcher reach for."""

    def __init__(self, windows) -> None:
        super().__init__()
        self._windows = windows
        self.tasks = 0

    def get_windows(self):
        return self._windows

    def create_asyncio_task(self, coro):
        # SleepLock schedules the inhibitor refresh on resume. Close the
        # coroutine rather than running it; there is no bus to talk to.
        self.tasks += 1
        coro.close()


def manager(tmpdir: Path, name: str) -> DatabaseManager:
    """A manager with no database opened; only its properties are read here."""
    return DatabaseManager(None, str(tmpdir / name))


def sleep_signal(going_to_sleep: bool):
    """The argument tuple a PrepareForSleep subscription is called with."""
    return (None, ":1.0", "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager", "PrepareForSleep",
            (going_to_sleep,), None)


def run_checks(tmpdir: Path) -> None:
    config_manager.setting.set_boolean("lock-on-suspend", True)

    print("Setup")
    check("lock-on-suspend defaults to on",
          config_manager.setting.get_default_value("lock-on-suspend").get_boolean())

    first, second = manager(tmpdir, "a.kdbx"), manager(tmpdir, "b.kdbx")
    app = StubApplication([
        StubWindow(StubUnlockedDatabase(first)),
        StubWindow(StubUnlockedDatabase(second)),
    ])
    guard = SleepLock(app)
    check("both safes start unlocked",
          not first.props.locked and not second.props.locked)

    print("\nSuspending locks every open safe")
    guard._on_prepare_for_sleep(*sleep_signal(True))  # noqa: SLF001
    check("the first safe is locked", first.props.locked is True)
    check("the second safe is locked too, not just the first",
          second.props.locked is True)

    print("\nA safe already locked is left alone")
    locked_count = guard._lock_all()  # noqa: SLF001
    check("nothing left to lock", locked_count == 0)

    print("\nWindows without a safe are skipped")
    empty = SleepLock(StubApplication([StubWindow(None)]))
    check("a window with no safe locks nothing", empty._lock_all() == 0)  # noqa: SLF001

    print("\nThe setting is honoured")
    third = manager(tmpdir, "c.kdbx")
    opted_out = SleepLock(StubApplication([StubWindow(StubUnlockedDatabase(third))]))
    config_manager.setting.set_boolean("lock-on-suspend", False)
    opted_out._on_prepare_for_sleep(*sleep_signal(False))  # noqa: SLF001
    opted_out._on_prepare_for_sleep(*sleep_signal(True))  # noqa: SLF001
    check("a safe is left open when the setting is off",
          third.props.locked is False)

    config_manager.setting.set_boolean("lock-on-suspend", True)
    opted_out._on_prepare_for_sleep(*sleep_signal(True))  # noqa: SLF001
    check("and locked once it is back on", third.props.locked is True)

    print("\nResuming is not a lock")
    fourth = manager(tmpdir, "d.kdbx")
    resumed = StubApplication([StubWindow(StubUnlockedDatabase(fourth))])
    guard_resume = SleepLock(resumed)
    guard_resume._on_prepare_for_sleep(*sleep_signal(False))  # noqa: SLF001
    check("coming back up leaves the safe open", fourth.props.locked is False)
    check("and re-arms the inhibitor for the next suspend", resumed.tasks == 1)

    print("\nOne safe failing does not leave the others open")

    class Stubborn(DatabaseManager):
        """Refuses to lock, the way a broken property setter would."""

        @property
        def props(self):
            raise RuntimeError("cannot lock")

    fifth, sixth = Stubborn(None, str(tmpdir / "e.kdbx")), manager(tmpdir, "f.kdbx")
    mixed = SleepLock(StubApplication([
        StubWindow(StubUnlockedDatabase(fifth)),
        StubWindow(StubUnlockedDatabase(sixth)),
    ]))
    try:
        mixed._on_prepare_for_sleep(*sleep_signal(True))  # noqa: SLF001
        raised = False
    except Exception:  # noqa: BLE001
        raised = True

    check("the failure does not escape into the D-Bus callback", not raised)
    check("the healthy safe was still locked", sixth.props.locked is True)

    print("\nPending saves are what the inhibitor waits on")
    seventh = manager(tmpdir, "g.kdbx")
    waiting = SleepLock(StubApplication([StubWindow(StubUnlockedDatabase(seventh))]))
    check("a clean safe has nothing pending", waiting._saves_pending() is False)  # noqa: SLF001

    seventh.is_dirty = True
    check("an unsaved safe is pending", waiting._saves_pending() is True)  # noqa: SLF001

    seventh.is_dirty = False
    seventh.save_running = True
    check("a save in flight is pending too, not only a dirty flag",
          waiting._saves_pending() is True)  # noqa: SLF001

    seventh.save_running = False
    check("and settles once the write lands",
          waiting._saves_pending() is False)  # noqa: SLF001

    print("\nTeardown is safe without a bus")
    guard.stop()
    check("stopping without ever connecting does not raise", True)

    print("\nThe tray icon greys out once everything is locked")
    from gsecrets.tray import LOCKED_COLOR, tray_icon_color

    # The colour rather than the resolved icon name: resolving consults the icon
    # theme, which has nothing installed under the self-test application ID, so
    # every name would fall back to the unsuffixed icon and the check would pass
    # for the wrong reason.
    config_manager.setting.set_string("icon-color", "pink")
    check("an open safe keeps the chosen accent colour",
          tray_icon_color(True) == "pink", f"got {tray_icon_color(True)}")
    check("a locked safe goes grey instead",
          tray_icon_color(False) == LOCKED_COLOR, f"got {tray_icon_color(False)}")

    config_manager.setting.set_string("icon-color", "blue")
    check("the accent is followed while unlocked",
          tray_icon_color(True) == "blue", f"got {tray_icon_color(True)}")
    check("but locked is grey whatever the accent",
          tray_icon_color(False) == LOCKED_COLOR, f"got {tray_icon_color(False)}")

    config_manager.setting.set_string("icon-color", LOCKED_COLOR)
    check("choosing monochrome collapses the two, as it must",
          tray_icon_color(True) == tray_icon_color(False))
    config_manager.setting.set_string("icon-color", "pink")

    print("\nThe shared watcher reports what the tray paints by")
    from gsecrets.safe_watcher import SafeWatcher, any_unlocked

    eighth = manager(tmpdir, "h.kdbx")
    eighth.db = object()  # any_unlocked() wants a database, not just a manager
    tray_app = StubApplication([StubWindow(StubUnlockedDatabase(eighth))])
    seen: list[bool] = []
    watcher = SafeWatcher(tray_app, seen.append)

    check("an unlocked safe reads as unlocked", any_unlocked(tray_app) is True)
    check("a manager with no database open does not count",
          any_unlocked(StubApplication([
              StubWindow(StubUnlockedDatabase(manager(tmpdir, "i.kdbx")))
          ])) is False)

    watcher.start()
    watcher.watch_managers()
    check("starting does not report the state it found",
          seen == [], f"got {seen}")
    check("but it is available to paint by", watcher.unlocked is True)

    eighth.props.locked = True
    check("locking is reported once", seen == [False], f"got {seen}")
    check("and the watcher agrees", watcher.unlocked is False)

    eighth.props.locked = False
    check("unlocking is reported too", seen == [False, True], f"got {seen}")

    watcher.stop()
    eighth.props.locked = True
    check("a stopped watcher reports nothing further",
          seen == [False, True], f"got {seen}")


def main() -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="cipher-sleep-check-"))
    try:
        run_checks(tmpdir)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("failures: " + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
