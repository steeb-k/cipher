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

from gsecrets.browser.backend import ApplicationBackend  # noqa: E402
from gsecrets.database_manager import DatabaseManager  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f" -- {detail}" if detail and not condition else ""))


class StubWindow:
    def __init__(self, unlocked_db) -> None:
        self.unlocked_db = unlocked_db


class StubUnlockedDatabase:
    def __init__(self, database_manager) -> None:
        self.database_manager = database_manager


class StubApplication:
    """Stands in for Gio.Application, exposing only what the backend uses."""

    def __init__(self, windows) -> None:
        self._windows = windows

    def get_windows(self):
        return self._windows

    def get_active_window(self):
        return self._windows[0] if self._windows else None


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
    check("safe starts unlocked", manager.props.locked is False)
    await backend.lock()
    check("lock() sets the property the application's own lock action sets",
          manager.props.locked is True)
    check("a locked safe reports no database", backend.get_database() is None)

    await backend.lock()
    check("locking an already locked safe does not raise", manager.props.locked is True)

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
