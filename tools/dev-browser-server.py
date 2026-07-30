#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
"""Serve a database over the browser protocol without launching the GUI.

This exists so browser-side problems can be diagnosed independently of the
GTK integration. It opens a database directly with pykeepass and answers
protocol requests from the real server implementation.

    tools/dev-browser-server.py path/to/database.kdbx

The password is read from the CIPHER_DEV_PASSWORD environment variable, or
prompted for interactively. Association requests are approved from the
terminal, standing in for the dialog the application will eventually show.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _app_id_from_meson() -> str:
    """Read project_id out of meson.build so this tracks the real build."""
    for line in (REPO_ROOT / "meson.build").read_text().splitlines():
        if line.startswith("project_id"):
            return line.split("=", 1)[1].strip().strip("'\"")

    raise RuntimeError("could not find project_id in meson.build")


def _ensure_const() -> None:
    """Provide gsecrets.const, which meson generates only at build time.

    Deliberately does not attempt `import gsecrets.const` as a probe. gsecrets
    ships no __init__.py, so it is a namespace package: its __path__ merges
    this checkout with any installed copy, and the probe would silently
    resolve against the *installed* application's const.py. That bound the
    dev socket under the installed app's ID rather than this fork's.
    """
    if (REPO_ROOT / "gsecrets" / "const.py").exists():
        # A generated const.py is present in the checkout; let it be imported.
        return

    module = types.ModuleType("gsecrets.const")
    module.APP_ID = _app_id_from_meson()
    module.VERSION = "dev"
    sys.modules["gsecrets.const"] = module


_ensure_const()

from pykeepass import PyKeePass  # noqa: E402

from gsecrets.browser import store  # noqa: E402
from gsecrets.browser.server import BrowserServer  # noqa: E402


class DevBackend:
    """Backend that talks to the terminal instead of a GTK window."""

    def __init__(self, db: PyKeePass, auto_approve: bool = False) -> None:
        self._db = db
        self._auto_approve = auto_approve
        self._locked = False
        # Set by run(); the application wires the equivalent in application.py.
        self.on_state_change = None

    def get_database(self) -> PyKeePass | None:
        return None if self._locked else self._db

    async def request_unlock(self) -> None:
        print("  browser asked to unlock (the application would show its window)")

    async def confirm_association(self, key_id: str) -> str | None:
        print("\n--- association request ---")
        print(f"  client key: {key_id[:16]}...")

        if self._auto_approve:
            print("  auto-approved as 'cipher-bridge'")
            return "cipher-bridge"

        def prompt() -> str | None:
            try:
                return input("  name to save it as (blank to deny): ").strip()
            except (EOFError, KeyboardInterrupt):
                return None

        # In a thread so a blocking read on stdin does not stall the event loop
        # and with it every other socket connection.
        name = await asyncio.to_thread(prompt)
        if not name:
            print("  denied")
            return None

        return name

    async def save(self) -> None:
        await asyncio.to_thread(self._db.save)
        print("  database saved")

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
        """Write through pykeepass; the application backend uses the UI model."""
        if uuid:
            for entry in self._db.entries:
                if entry.uuid.hex == uuid:
                    entry.username = login
                    entry.password = password
                    print(f"  updated entry {entry.title!r} ({login})")
                    return False
            raise RuntimeError(f"no entry with uuid {uuid}")

        group = self._db.root_group
        if group_uuid:
            for candidate in self._db.groups:
                if candidate.uuid.hex == group_uuid:
                    group = candidate
                    break

        self._db.add_entry(group, title, login, password, url=url)
        print(f"  created entry {title!r} ({login}) for {url}")
        return True

    async def create_group(self, path: str) -> tuple[str, str]:
        """Create a group via pykeepass; the application uses the UI model."""
        parts = [part for part in path.split("/") if part.strip()]
        if not parts:
            raise RuntimeError(f"no usable group name in {path!r}")

        group = self._db.root_group
        for part in parts:
            existing = next(
                (c for c in group.subgroups if (c.name or "") == part), None
            )
            group = existing if existing is not None else self._db.add_group(group, part)

        return group.name, group.uuid.hex


    async def generate_password(self) -> str:
        """Uses the real generator; only reading its settings needs a schema."""
        from gsecrets import password_generator

        return password_generator.generate(20, True, True, True, False)

    async def lock(self) -> None:
        # Only signal on a real transition, mirroring notify::locked, which
        # GObject emits only when the value changes.
        if self._locked:
            return

        self._locked = True
        print("  safe locked; further requests will be refused")

        if self.on_state_change is not None:
            await self.on_state_change("database-locked")



async def run(path: Path, password: str, auto_approve: bool) -> int:
    try:
        db = PyKeePass(str(path), password=password)
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator
        print(f"cannot open {path}: {exc}", file=sys.stderr)
        return 1

    backend = DevBackend(db, auto_approve=auto_approve)
    server = BrowserServer(backend)
    backend.on_state_change = server.broadcast

    try:
        await server.start()
    except RuntimeError as exc:
        # Almost always the application already holding the socket. Say so
        # unmistakably: quietly talking to a different server than intended
        # produces results that look like protocol bugs.
        print(f"\nERROR: cannot start server: {exc}", file=sys.stderr)
        print(
            "Another server owns this socket. Quit the running Cipher (or dev "
            "server) first;\nanything you test meanwhile reaches that one, not "
            "this one.",
            file=sys.stderr,
        )
        return 1

    print(f"database : {path}")
    print(f"entries  : {len(db.entries)}")
    print(f"hash     : {store.database_hash(db)}")
    print(f"socket   : {server.path}")

    existing = store.list_associations(db)
    print(f"assoc.   : {', '.join(existing) if existing else '(none)'}")
    print("\nReady. Ctrl+C to stop.")

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        await server.stop()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="approve association requests without prompting",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    # Line-buffer stdout. Otherwise every status line below -- including the
    # socket path and the association prompt -- is invisible whenever output is
    # piped or captured, while logging still appears because it goes to stderr
    # unbuffered. That made a failure to start easy to overlook.
    sys.stdout.reconfigure(line_buffering=True)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    password = os.environ.get("CIPHER_DEV_PASSWORD")
    if password is None:
        password = getpass.getpass(f"Password for {args.database.name}: ")

    try:
        return asyncio.run(run(args.database, password, args.auto_approve))
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
