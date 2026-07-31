#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
"""Check the application backend's writes go through the UI model.

The protocol-level checks in browser_protocol_check.py use a backend that
writes with pykeepass directly. That is not what the application does, and the
difference matters: the application keeps SafeEntry/SafeGroup wrappers in
Gio.ListStores that drive the visible entry list, so a write which bypasses
them leaves the UI showing stale contents and the safe not marked dirty.

This exercises ApplicationBackend against a real DatabaseManager without
needing a display or an unlocked GUI.

    tests/browser_backend_check.py
"""

from __future__ import annotations

import asyncio
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
    level, which aborts the process outright if the schema is not installed. So
    the schema has to exist before the imports below, which means generating it
    here the way meson does rather than depending on an installed build.
    """
    schema_dir = Path(tempfile.mkdtemp(prefix="cipher-check-schema-"))
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
sys.modules["gsecrets.const"] = module

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import GLib  # noqa: E402
from pykeepass import PyKeePass, create_database  # noqa: E402

from gsecrets.browser.backend import (  # noqa: E402
    UNLOCK_REQUEST_INTERVAL,
    ApplicationBackend,
)
from gsecrets.database_manager import DatabaseManager  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f" -- {detail}" if detail and not condition else ""))


class StubSignals:
    """Records connections the way a GObject would accept them.

    The backend watches windows for notify::unlocked-db and the application for
    window-added/window-removed. Nothing here emits them -- the checks drive
    state directly -- but they have to be connectable, and emit() lets a check
    stand in for the application when it wants to.
    """

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

    def connected(self) -> int:
        return sum(len(handlers) for handlers in self._handlers.values())

    def emit(self, signal, *args) -> None:
        for _handler_id, handler in list(self._handlers.get(signal, [])):
            handler(self, *args)


class StubWindow(StubSignals):
    def __init__(self, unlocked_db, presented=None) -> None:
        super().__init__()
        self.unlocked_db = unlocked_db
        self._presented = presented if presented is not None else []

    def set_unlocked_db(self, unlocked_db) -> None:
        """Assign the safe and notify, as the real GObject property does."""
        self.unlocked_db = unlocked_db
        self.emit("notify::unlocked-db", None)

    def present(self) -> None:
        self._presented.append("present")


class StubUnlockedDatabase:
    def __init__(self, database_manager) -> None:
        self.database_manager = database_manager


class StubApplication(StubSignals):
    """Stands in for Gio.Application, exposing only what the backend uses."""

    def __init__(self, windows) -> None:
        super().__init__()
        self._windows = windows
        self.presented: list[str] = []

    def get_windows(self):
        return self._windows

    def add_window(self, window) -> None:
        self._windows.append(window)
        self.emit("window-added", window)

    def remove_window(self, window) -> None:
        self._windows.remove(window)
        self.emit("window-removed", window)

    def get_active_window(self):
        window = self._windows[0] if self._windows else None
        if window is not None:
            window._presented = self.presented  # noqa: SLF001
        return window

    def activate(self) -> None:
        self.presented.append("activate")


def store_items(store):
    return [store.get_item(i) for i in range(store.get_n_items())]


def build_manager(path: Path, password: str) -> DatabaseManager:
    """Open a database and populate the model stores the UI reads."""
    manager = DatabaseManager(None, str(path))
    manager.db = PyKeePass(str(path), password=password)

    # Normally driven by idle callbacks after unlock; run them to completion.
    while manager._load_groups(0.0) == GLib.SOURCE_CONTINUE:  # noqa: SLF001
        pass
    while manager._load_entries(0.0) == GLib.SOURCE_CONTINUE:  # noqa: SLF001
        pass

    manager.is_dirty = False
    return manager


async def run_checks(tmpdir: Path) -> None:
    path = tmpdir / "backend.kdbx"
    db = create_database(str(path), password="pw")
    db.add_entry(db.root_group, "Existing", "olduser", "oldpass", url="https://old.example")
    db.save()

    manager = build_manager(path, "pw")
    backend = ApplicationBackend(
        StubApplication([StubWindow(StubUnlockedDatabase(manager))])
    )

    print("Setup")
    check("model stores were populated", manager.entries.get_n_items() == 1)
    check("database starts clean", manager.is_dirty is False)
    check("backend finds the unlocked database", backend.get_database() is not None)

    print("\nCreating an entry")
    created = await backend.set_login(
        url="https://new.example/login",
        login="newuser",
        password="newpass",
        title="new.example",
    )
    check("reports that it created", created is True)
    check("entry exists in pykeepass", any(
        e.username == "newuser" for e in manager.db.entries
    ))
    check(
        "entry also appears in the model store the UI reads",
        any(e.props.username == "newuser" for e in store_items(manager.entries)),
        f"store has {[e.props.name for e in store_items(manager.entries)]}",
    )
    check("url was set on the new entry", any(
        e.props.url == "https://new.example/login" for e in store_items(manager.entries)
    ))
    check("database was marked dirty, so a save will not be skipped",
          manager.is_dirty is True)

    print("\nUpdating an entry")
    manager.is_dirty = False
    existing = next(e for e in store_items(manager.entries) if e.props.name == "Existing")
    updated = await backend.set_login(
        url="https://old.example",
        login="rotateduser",
        password="rotatedpass",
        title="old.example",
        uuid=existing.uuid.hex,
    )
    check("reports that it updated", updated is False)
    check("username was changed in place", existing.props.username == "rotateduser")
    check("password was changed in place", existing.props.password == "rotatedpass")
    check("no duplicate entry was created", len(
        [e for e in manager.db.entries if e.url == "https://old.example"]
    ) == 1)
    check("update marked the database dirty", manager.is_dirty is True)

    print("\nRefusals")
    try:
        await backend.set_login(
            url="https://x.example", login="a", password="b", title="x.example",
            uuid="00000000000000000000000000000000",
        )
        check("unknown uuid raises rather than creating a new entry", False,
              "no exception raised")
    except RuntimeError:
        check("unknown uuid raises rather than creating a new entry", True)

    check("nothing was created by the refused update", not any(
        e.username == "a" for e in manager.db.entries
    ))

    print("\nCreating groups")
    manager.is_dirty = False
    groups_before = manager.groups.get_n_items()
    name, uuid = await backend.create_group("Browser")
    check("returns the created name", name == "Browser")
    check("returns a uuid", bool(uuid))
    check("group exists in pykeepass", any(
        g.name == "Browser" for g in manager.db.groups
    ))
    check(
        "group also appears in the model store the UI reads",
        manager.groups.get_n_items() == groups_before + 1,
        f"{groups_before} -> {manager.groups.get_n_items()}",
    )
    check("creating a group marked the database dirty", manager.is_dirty is True)

    same_name, same_uuid = await backend.create_group("Browser")
    check("an existing group is reused, not duplicated",
          same_uuid == uuid and len(
              [g for g in manager.db.groups if g.name == "Browser"]
          ) == 1,
          f"got {same_name}/{same_uuid}")

    deep_name, _ = await backend.create_group("Browser/Nested/Deeper")
    check("a path creates the missing levels", deep_name == "Deeper")
    check("intermediate levels were reused, not duplicated", len(
        [g for g in manager.db.groups if g.name == "Browser"]
    ) == 1)

    print("\nSaving into a created group")
    _, group_uuid = await backend.create_group("Saved")
    await backend.set_login(
        url="https://grouped.example/login",
        login="grouped",
        password="gpass",
        title="grouped.example",
        group_uuid=group_uuid,
    )
    placed = [e for e in manager.db.entries if e.username == "grouped"]
    check("the entry landed in the requested group",
          len(placed) == 1 and placed[0].group.name == "Saved",
          f"got {[(e.title, e.group.name) for e in placed]}")

    print("\nLocking through the backend")
    signals: list[str] = []
    backend.on_state_change = signals.append

    # This window was already holding the safe when the backend was built, so
    # notify::unlocked-db has been and gone; _database_manager() is the path
    # that picks it up.
    backend.get_database()

    check("safe starts unlocked", manager.props.locked is False)
    await backend.lock()
    check("lock() sets the property the application's own lock action sets",
          manager.props.locked is True)
    check("a locked safe reports no database", backend.get_database() is None)
    check("locking emitted database-locked",
          signals == ["database-locked"], f"got {signals}")

    await backend.lock()
    check("locking an already locked safe does not raise", manager.props.locked is True)
    check(
        "a redundant lock emits no second signal, since notify::locked "
        "only fires on a change",
        signals == ["database-locked"],
        f"got {signals}",
    )

    print("\nUnlocking is signalled")
    # Locking is the only transition the protocol can cause; unlocking happens
    # in the UI, so drive it the same way the application does.
    manager.props.locked = False
    check("unlocking emitted database-unlocked",
          signals == ["database-locked", "database-unlocked"], f"got {signals}")
    check("the database is available again", backend.get_database() is not None)

    await backend.lock()
    check("relocking signals again",
          signals[-1] == "database-locked", f"got {signals}")

    print("\nAn unlock with no request behind it is still reported")
    # The gap the old opportunistic watcher left. Nothing has asked the backend
    # anything, so nothing would have discovered this safe, and the browser
    # would have gone on showing a locked icon over a safe it could read.
    app = backend._application  # noqa: SLF001
    second_window = StubWindow(None, presented=app.presented)
    before = len(signals)
    app.add_window(second_window)
    check("an added window with no safe in it says nothing",
          len(signals) == before, f"got {signals[before:]}")

    second_path = tmpdir / "second.kdbx"
    create_database(str(second_path), password="pw").save()
    second = build_manager(second_path, "pw")
    second_window.set_unlocked_db(StubUnlockedDatabase(second))
    check("unlocking a safe emits database-unlocked without a request first",
          signals[-1] == "database-unlocked" and len(signals) == before + 1,
          f"got {signals[before:]}")

    print("\nState is aggregated over windows, not reported per safe")
    before = len(signals)
    manager.props.locked = False
    check("a second safe unlocking is not news; one was already reachable",
          len(signals) == before, f"got {signals[before:]}")

    second.props.locked = True
    check("one safe locking while another stays open is not a lock",
          len(signals) == before, f"got {signals[before:]}")

    manager.props.locked = True
    check("the last safe locking is",
          signals[-1] == "database-locked" and len(signals) == before + 1,
          f"got {signals[before:]}")

    print("\nClosing a window takes its safe with it")
    second.props.locked = False
    check("reopening reports unlocked", signals[-1] == "database-unlocked",
          f"got {signals}")

    before = len(signals)
    app.remove_window(second_window)
    check("closing the only window holding an unlocked safe reports locked",
          signals[-1] == "database-locked" and len(signals) == before + 1,
          f"got {signals[before:]}")
    check("and the safe is genuinely no longer reachable",
          backend.get_database() is None)

    print("\nPassword generation follows the application's settings")
    from gsecrets import config_manager

    config_manager.setting.set_int("generator-length", 24)
    config_manager.setting.set_boolean("generator-use-uppercase", True)
    config_manager.setting.set_boolean("generator-use-lowercase", True)
    config_manager.setting.set_boolean("generator-use-numbers", True)
    config_manager.setting.set_boolean("generator-use-symbols", False)

    generated = await backend.generate_password()
    check(f"length follows the setting ({len(generated)} == 24)",
          len(generated) == 24)
    check("no symbols, as configured",
          all(c.isalnum() for c in generated), f"got {generated!r}")
    check("contains every enabled class",
          any(c.isupper() for c in generated)
          and any(c.islower() for c in generated)
          and any(c.isdigit() for c in generated),
          f"got {generated!r}")

    config_manager.setting.set_int("generator-length", 8)
    check("a changed setting is picked up on the next call",
          len(await backend.generate_password()) == 8)

    # The pathological case the generator used to hang on, now reachable from a
    # browser request.
    config_manager.setting.set_int("generator-length", 1)
    config_manager.setting.set_boolean("generator-use-symbols", True)
    short = await backend.generate_password()
    check("an unsatisfiable request returns instead of hanging",
          len(short) == 1, f"got {short!r}")

    print("\nSummoning the unlock window")
    presented = backend._application.presented  # noqa: SLF001
    await backend.request_unlock()
    check("request_unlock presents a window", presented == ["present"], f"got {presented}")

    await backend.request_unlock()
    check(
        "a second request straight away is ignored, so it cannot be used to "
        "flood the desktop",
        presented == ["present"],
        f"got {presented}",
    )

    # Reach past the interval rather than sleeping through it.
    backend._last_unlock_request -= UNLOCK_REQUEST_INTERVAL + 1  # noqa: SLF001
    await backend.request_unlock()
    check("a request after the interval is honoured again",
          presented == ["present", "present"], f"got {presented}")

    print("\nSwitching browser integration off detaches everything")
    check("the application is being listened to", app.connected() > 0)
    backend.stop_watching()
    check("no listeners are left on the application", app.connected() == 0)
    check("none on its windows either",
          all(w.connected() == 0 for w in app.get_windows()))

    before = len(signals)
    backend.on_state_change = signals.append
    manager.props.locked = False
    check("a detached backend reports nothing",
          len(signals) == before, f"got {signals[before:]}")
    manager.props.locked = True
    backend.on_state_change = None

    print("\nWrites are refused once locked")
    for description, call in (
        ("set_login", backend.set_login(
            url="https://after.example", login="x", password="y",
            title="after.example")),
        ("create_group", backend.create_group("AfterLock")),
    ):
        try:
            await call
            check(f"{description} is refused while locked", False, "no exception")
        except RuntimeError:
            check(f"{description} is refused while locked", True)

    check("nothing was written while locked", not any(
        e.username == "x" for e in manager.db.entries
    ) and not any(g.name == "AfterLock" for g in manager.db.groups))


def main() -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="cipher-backend-check-"))
    try:
        asyncio.run(run_checks(tmpdir))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("failures: " + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
