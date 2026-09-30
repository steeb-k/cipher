#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
"""End-to-end check of the browser protocol server.

Speaks the client half of the protocol over a real Unix socket, the way the
extension's proxy does. Standalone rather than a pytest module so it runs with
nothing but python and PyNaCl:

    tests/browser_protocol_check.py

Exits non-zero if any check fails.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _ensure_const() -> None:
    """Provide gsecrets.const, which meson generates only at build time.

    Installed unconditionally rather than after an `import gsecrets.const`
    probe: gsecrets has no __init__.py, so it is a namespace package whose
    __path__ merges this checkout with any installed copy, and the probe would
    silently pick up the installed application's APP_ID.

    The checks always pass an explicit socket path, so this value only needs to
    be distinct enough that a stray default can never touch a real socket.
    """
    module = types.ModuleType("gsecrets.const")
    module.APP_ID = "io.github.steeb_k.Cipher.SelfTest"
    module.VERSION = "test"
    sys.modules["gsecrets.const"] = module


_ensure_const()

from nacl.public import Box, PrivateKey, PublicKey  # noqa: E402
from nacl.utils import random as nacl_random  # noqa: E402
from pykeepass import create_database  # noqa: E402
from pyotp import TOTP  # noqa: E402

# Base32, so pyotp accepts it; the expected code is computed with pyotp too.
TOTP_SECRET = "JBSWY3DPEHPK3PXP"

from gsecrets.browser import crypto, store  # noqa: E402
from gsecrets.browser.server import BrowserServer  # noqa: E402

# Unsolicited messages the server may push at any time.
SIGNAL_ACTIONS = {"database-locked", "database-unlocked"}

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name)
    mark = "PASS" if condition else "FAIL"
    suffix = f" -- {detail}" if detail and not condition else ""
    print(f"  [{mark}] {name}{suffix}")


class FakeBackend:
    """Stands in for the GTK application."""

    def __init__(self, db) -> None:
        self.db = db
        self.association_name = "cipher-bridge-test"
        self.approve = True
        self.saved = 0
        self.locked = False
        self.icon_colour = "pink"
        # Awaited directly here. The application cannot: its notification comes
        # from a GObject property change, so it schedules a task instead.
        self.on_state_change = None
        self.unlock_requests = 0

    def get_database(self):
        return self.db

    def icon_color(self) -> str:
        """Carried in the handshake, so the toolbar icon matches the tray."""
        return self.icon_colour

    async def request_unlock(self) -> None:
        self.unlock_requests += 1

    async def confirm_association(self, key_id: str) -> str | None:
        return self.association_name if self.approve else None

    async def save(self) -> None:
        self.saved += 1

    async def set_login(
        self,
        *,
        url: str,
        login: str,
        password: str,
        title: str,
        uuid: str | None = None,
        group_uuid: str | None = None,
        download_favicon: bool = False,
    ) -> bool:
        """Write through pykeepass; the real backend goes via the UI model."""
        # Accepted and ignored: fetching an icon is the real backend's business
        # and needs the network, which these checks must never touch.
        del download_favicon
        if uuid:
            for entry in self.db.entries:
                if entry.uuid.hex == uuid:
                    entry.username = login
                    entry.password = password
                    return False
            raise RuntimeError(f"no entry with uuid {uuid}")

        group = self.db.root_group
        if group_uuid:
            for candidate in self.db.groups:
                if candidate.uuid.hex == group_uuid:
                    group = candidate
                    break

        self.db.add_entry(group, title, login, password, url=url)
        return True

    async def create_group(self, path: str) -> tuple[str, str]:
        """Create a group via pykeepass; the application uses the UI model."""
        parts = [part for part in path.split("/") if part.strip()]
        if not parts:
            raise RuntimeError(f"no usable group name in {path!r}")

        group = self.db.root_group
        for part in parts:
            existing = next(
                (c for c in group.subgroups if (c.name or "") == part), None
            )
            group = existing if existing is not None else self.db.add_group(group, part)

        return group.name, group.uuid.hex


    async def generate_password(self) -> str:
        """Uses the real generator; only reading its settings needs a schema."""
        from gsecrets import password_generator

        return password_generator.generate(20, True, True, True, False)

    async def lock(self) -> None:
        """Locking is modelled as the database becoming unavailable.

        Signals only on a real transition, mirroring notify::locked, which
        GObject emits only when the value actually changes. The application gets
        that for free; a fake has to be careful, and a spurious signal is not
        harmless -- each one makes the extension re-query the database.
        """
        if self.locked:
            return

        self.db = None
        self.locked = True
        if self.on_state_change is not None:
            await self.on_state_change("database-locked")

    async def unlock(self, db) -> None:
        """Only reachable from the checks; the protocol cannot unlock a safe."""
        if not self.locked:
            return

        self.db = db
        self.locked = False
        if self.on_state_change is not None:
            await self.on_state_change("database-unlocked")


class Client:
    """The browser half of the protocol."""

    def __init__(self, reader, writer) -> None:
        self.reader, self.writer = reader, writer
        self.secret = PrivateKey.generate()
        self.client_id = crypto.b64encode(nacl_random(24))
        self.box: Box | None = None
        self.id_key = crypto.b64encode(bytes(PrivateKey.generate().public_key))
        self.association_id: str | None = None
        self._buffer = ""
        self.signals: list[str] = []

    async def _read_message(self) -> dict:
        """Read one JSON object, buffering the way the proxy does.

        Not a single read() per message: the server may coalesce a signal and a
        reply into one write, and json.loads() on two concatenated objects
        fails.
        """
        decoder = json.JSONDecoder()
        while True:
            stripped = self._buffer.lstrip()
            self._buffer = stripped
            if stripped:
                try:
                    obj, end = decoder.raw_decode(stripped)
                except json.JSONDecodeError:
                    pass  # incomplete; read more
                else:
                    self._buffer = stripped[end:]
                    return obj

            chunk = await asyncio.wait_for(self.reader.read(65536), timeout=5)
            if not chunk:
                raise ConnectionError("server closed the connection")
            self._buffer += chunk.decode()

    def _is_signal(self, message: dict) -> bool:
        return (
            message.get("action") in SIGNAL_ACTIONS
            and "message" not in message
            and "error" not in message
        )

    async def _roundtrip(self, request: dict) -> dict:
        self.writer.write(json.dumps(request).encode())
        await self.writer.drain()

        while True:
            message = await self._read_message()
            # Signals are unsolicited and may arrive before the reply to the
            # request just sent. The extension routes them separately in
            # onNativeMessage(), so a client must not mistake one for a reply.
            if self._is_signal(message):
                self.signals.append(message["action"])
                continue
            return message

    async def wait_for_signal(self, timeout: float = 3.0) -> str | None:
        """Wait for a signal without having sent anything."""
        try:
            while True:
                message = await asyncio.wait_for(self._read_message(), timeout)
                if self._is_signal(message):
                    self.signals.append(message["action"])
                    return message["action"]
        except (TimeoutError, ConnectionError):
            return None

    async def change_public_keys(self) -> tuple[dict, bytes]:
        """Run the key exchange, returning the response and expected nonce.

        The caller checks the nonce because the extension's verifyKeyResponse()
        does: it validates the reply nonce's length and value even though this
        exchange is unencrypted, and rejects the handshake if either is wrong.
        """
        nonce = nacl_random(24)
        response = await self._roundtrip({
            "action": "change-public-keys",
            "publicKey": crypto.b64encode(bytes(self.secret.public_key)),
            "nonce": crypto.b64encode(nonce),
            "clientID": self.client_id,
        })
        if "publicKey" in response:
            self.box = Box(
                self.secret, PublicKey(crypto.b64decode(response["publicKey"]))
            )
        return response, crypto.increment_nonce(nonce)

    async def send(
        self, action: str, payload: dict, trigger_unlock: bool = False
    ) -> dict:
        """Encrypt, send, and decrypt one action, verifying nonce handling."""
        assert self.box is not None, "handshake must run first"

        nonce = nacl_random(24)
        ciphertext = self.box.encrypt(json.dumps(payload).encode(), nonce).ciphertext
        envelope = {
            "action": action,
            "message": crypto.b64encode(ciphertext),
            "nonce": crypto.b64encode(nonce),
            "clientID": self.client_id,
        }
        if trigger_unlock:
            # On the outer envelope, in plaintext, as buildRequest() puts it.
            envelope["triggerUnlock"] = "true"

        response = await self._roundtrip(envelope)

        if "error" in response:
            return response

        expected = crypto.increment_nonce(nonce)
        if crypto.b64decode(response["nonce"]) != expected:
            return {"error": "envelope nonce mismatch", "errorCode": -1}

        plaintext = self.box.decrypt(
            crypto.b64decode(response["message"]), crypto.b64decode(response["nonce"])
        )
        decoded = json.loads(plaintext)

        if crypto.b64decode(decoded.get("nonce", "")) != expected:
            return {"error": "payload nonce mismatch", "errorCode": -1}

        return decoded


def build_database(path: Path):
    """A database whose URLs exercise the interesting matching cases."""
    db = create_database(str(path), password="test-password")
    add = db.add_entry
    add(db.root_group, "GitHub", "octocat", "hunter2", url="https://github.com/login")
    add(db.root_group, "Sub", "subuser", "subpass", url="https://gist.github.com")
    add(db.root_group, "Example", "alice", "pw-example", url="https://example.com")
    add(db.root_group, "Lookalike", "mallory", "pw-evil", url="https://notgithub.com")
    add(db.root_group, "NoUrl", "nobody", "pw-nourl")

    # A known base32 secret, so the expected code can be computed independently.
    otp_entry = add(db.root_group, "WithTotp", "totpuser", "totppass",
                    url="https://totp.example")
    otp_entry.otp = TOTP_SECRET

    # A malformed secret must not break lookups for the site it shares.
    broken = add(db.root_group, "BrokenTotp", "brokenuser", "brokenpass",
                 url="https://broken.example")
    broken.otp = "not!valid!base32"

    # Nested groups, so the tree returned by get-database-groups has depth to
    # check rather than a bare root.
    work = db.add_group(db.root_group, "Work")
    db.add_group(work, "Internal")
    db.add_group(db.root_group, "Personal")

    db.save()
    return db


async def run_checks(tmpdir: Path) -> None:
    db = build_database(tmpdir / "test.kdbx")
    backend = FakeBackend(db)
    server = BrowserServer(backend, path=tmpdir / "BrowserServer")
    await server.start()

    print(f"\nserver: {server.path}\n")
    reader, writer = await asyncio.open_unix_connection(str(server.path))
    client = Client(reader, writer)

    print("Handshake")
    response, expected_nonce = await client.change_public_keys()
    check("change-public-keys returns a host public key", "publicKey" in response)
    check("handshake reports success", response.get("success") == "true")

    # Everything verifyKeyResponse() checks, in the order it checks it.
    reply_nonce = response.get("nonce")
    check("handshake reply carries a nonce", bool(reply_nonce), f"got {response}")
    check(
        "handshake nonce is 24 bytes",
        bool(reply_nonce) and len(crypto.b64decode(reply_nonce)) == 24,
    )
    check(
        "handshake nonce is the request nonce incremented",
        bool(reply_nonce) and crypto.b64decode(reply_nonce) == expected_nonce,
    )

    print("\nDatabase identity")
    response = await client.send("get-databasehash", {"action": "get-databasehash"})
    check(
        "get-databasehash matches the computed hash",
        response.get("hash") == store.database_hash(db),
    )

    print("\nAccess control before association")
    response = await client.send(
        "get-logins", {"action": "get-logins", "url": "https://github.com", "keys": []}
    )
    check(
        "get-logins is refused without an association",
        response.get("errorCode") == 8,
        f"got {response}",
    )

    print("\nAssociation")
    response = await client.send("associate", {
        "action": "associate",
        "key": crypto.b64encode(bytes(client.secret.public_key)),
        "idKey": client.id_key,
    })
    client.association_id = response.get("id")
    check("associate succeeds", response.get("success") == "true", f"got {response}")
    check("associate returns the chosen name", client.association_id == "cipher-bridge-test")
    check("association was persisted", backend.saved == 1)
    check(
        "association key is stored in Meta/CustomData",
        store.get_association(db, "cipher-bridge-test") == client.id_key,
    )

    print("\nAssociation verification")
    response = await client.send("test-associate", {
        "action": "test-associate", "id": client.association_id, "key": client.id_key,
    })
    check("test-associate accepts the stored key", response.get("success") == "true")

    response = await client.send("test-associate", {
        "action": "test-associate",
        "id": client.association_id,
        "key": crypto.b64encode(bytes(PrivateKey.generate().public_key)),
    })
    check(
        "test-associate rejects a forged key",
        response.get("errorCode") == 8,
        f"got {response}",
    )

    keys = [{"id": client.association_id, "key": client.id_key}]

    print("\nCredential retrieval")
    response = await client.send("get-logins", {
        "action": "get-logins", "url": "https://github.com/login", "keys": keys,
    })
    entries = response.get("entries", [])
    check("get-logins returns the matching entry", len(entries) == 1, f"got {entries}")
    if entries:
        check("password is delivered", entries[0].get("password") == "hunter2")
        check("username is delivered", entries[0].get("login") == "octocat")
    check("count is a string, as the extension expects", response.get("count") == "1")

    print("\nURL matching")
    response = await client.send("get-logins", {
        "action": "get-logins", "url": "https://gist.github.com/x", "keys": keys,
    })
    names = sorted(e["name"] for e in response.get("entries", []))
    check("subdomain matches its parent-domain entry", names == ["GitHub", "Sub"], f"got {names}")

    response = await client.send("get-logins", {
        "action": "get-logins", "url": "https://notgithub.com", "keys": keys,
    })
    names = sorted(e["name"] for e in response.get("entries", []))
    check("lookalike domain does not match github.com", names == ["Lookalike"], f"got {names}")

    response = await client.send("get-logins", {
        "action": "get-logins", "url": "https://nosuchsite.invalid", "keys": keys,
    })
    check("unmatched URL reports no logins", response.get("errorCode") == 15, f"got {response}")

    print("\nForged association key")
    response = await client.send("get-logins", {
        "action": "get-logins",
        "url": "https://github.com",
        "keys": [{
            "id": client.association_id,
            "key": crypto.b64encode(bytes(PrivateKey.generate().public_key)),
        }],
    })
    check(
        "get-logins rejects an unrecognised key",
        response.get("errorCode") == 10,
        f"got {response}",
    )

    print("\nPassword generation")
    response = await client.send(
        "generate-password", {"action": "generate-password"}
    )
    generated = response.get("password")
    check("generate-password returns a password in the 'password' field",
          isinstance(generated, str) and len(generated) == 20,
          f"got {response}")
    check("the reported version advertises the generator",
          response.get("version") == "2.7.0", f"got {response.get('version')}")

    second = (await client.send(
        "generate-password", {"action": "generate-password"}
    )).get("password")
    check("successive calls differ, so it is generating rather than echoing",
          generated != second, f"{generated!r} vs {second!r}")

    print("\nGroup tree")
    response = await client.send(
        "get-database-groups", {"action": "get-database-groups"}
    )
    # The extension reads response.groups.groups, not response.groups, so a flat
    # array here would make it report an empty result and silently fall back.
    outer = response.get("groups")
    check(
        "groups is nested the way the extension unwraps it",
        isinstance(outer, dict) and isinstance(outer.get("groups"), list),
        f"got {outer!r}",
    )
    tree = (outer or {}).get("groups") or []
    check("exactly one root is returned", len(tree) == 1, f"got {len(tree)}")
    root = tree[0] if tree else {}
    check("root carries a name and uuid", bool(root.get("name")) and bool(root.get("uuid")))
    names = sorted(c["name"] for c in root.get("children", []))
    check("root's children are listed", names == ["Personal", "Work"], f"got {names}")
    work = next((c for c in root.get("children", []) if c["name"] == "Work"), {})
    check(
        "nesting is recursive, not one level deep",
        [c["name"] for c in work.get("children", [])] == ["Internal"],
        f"got {work.get('children')}",
    )

    print("\nCreating groups")
    response = await client.send("create-new-group", {
        "action": "create-new-group", "groupName": "Browser",
    })
    check("create-new-group returns a name and uuid",
          response.get("name") == "Browser" and bool(response.get("uuid")),
          f"got {response}")
    new_group_uuid = response.get("uuid")
    check("the group exists in the database",
          any(g.name == "Browser" for g in db.groups))

    response = await client.send("create-new-group", {
        "action": "create-new-group", "groupName": "Browser",
    })
    check(
        "creating the same group again reuses it rather than duplicating",
        response.get("uuid") == new_group_uuid
        and len([g for g in db.groups if g.name == "Browser"]) == 1,
        f"got {response}, {len([g for g in db.groups if g.name == 'Browser'])} groups",
    )

    response = await client.send("create-new-group", {
        "action": "create-new-group", "groupName": "Work/Internal/Deep",
    })
    check("a path creates only the missing levels",
          response.get("name") == "Deep"
          and len([g for g in db.groups if g.name == "Internal"]) == 1,
          f"got {response}")

    response = await client.send("create-new-group", {
        "action": "create-new-group", "groupName": "   ",
    })
    check("a blank group name is refused",
          response.get("errorCode") == 17, f"got {response}")

    print("\nSaving into a chosen group")
    response = await client.send("set-login", {
        "action": "set-login", "id": client.association_id,
        "url": "https://grouped.example/login", "login": "grouped", "password": "gpass",
        "group": "Browser", "groupUuid": new_group_uuid,
    })
    check("set-login into a group succeeds", response.get("success") == "true",
          f"got {response}")
    placed = [e for e in db.entries if e.username == "grouped"]
    check("the entry landed in the requested group",
          len(placed) == 1 and placed[0].group.name == "Browser",
          f"got {[(e.title, e.group.name) for e in placed]}")

    print("\nOne-time passwords")
    response = await client.send("get-logins", {
        "action": "get-logins", "url": "https://totp.example", "keys": keys,
    })
    entries = response.get("entries", [])
    totp_uuid = entries[0]["uuid"] if entries else None
    check(
        "get-logins advertises TOTP, which is what makes the extension ask",
        bool(entries) and entries[0].get("totp"),
        f"got {entries}",
    )

    response = await client.send("get-totp", {"action": "get-totp", "uuid": totp_uuid})
    expected = TOTP(TOTP_SECRET).now()
    check(
        "get-totp returns the current code",
        response.get("totp") == expected,
        f"got {response.get('totp')!r}, expected {expected!r}",
    )

    response = await client.send("get-logins", {
        "action": "get-logins", "url": "https://example.com", "keys": keys,
    })
    entries = response.get("entries", [])
    check(
        "an entry without TOTP advertises an empty string",
        bool(entries) and entries[0].get("totp") == "",
        f"got {entries}",
    )

    print("\nMalformed TOTP secrets")
    response = await client.send("get-logins", {
        "action": "get-logins", "url": "https://broken.example", "keys": keys,
    })
    entries = response.get("entries", [])
    check(
        "an unparseable secret does not break the credential lookup",
        len(entries) == 1 and entries[0].get("password") == "brokenpass",
        f"got {response}",
    )
    check("the broken entry advertises no TOTP",
          bool(entries) and entries[0].get("totp") == "")

    response = await client.send("get-totp", {
        "action": "get-totp", "uuid": entries[0]["uuid"] if entries else "",
    })
    check(
        "get-totp on an unusable secret reports an error rather than a bad code",
        response.get("errorCode") == 0,
        f"got {response}",
    )

    response = await client.send("get-totp", {
        "action": "get-totp", "uuid": "00000000000000000000000000000000",
    })
    check("get-totp for an unknown uuid is refused",
          response.get("errorCode") == 18, f"got {response}")

    print("\nCreating a login")
    response = await client.send("set-login", {
        "action": "set-login", "id": client.association_id,
        "url": "https://newsite.example/login", "submitUrl": "https://newsite.example/login",
        "login": "freshuser", "password": "freshpass",
    })
    check("set-login succeeds", response.get("success") == "true", f"got {response}")
    check(
        "error field is empty, so the extension reports success",
        response.get("error") == "",
        f"got {response.get('error')!r}",
    )
    check("the entry was written to the database", any(
        e.username == "freshuser" and e.password == "freshpass" for e in db.entries
    ))
    check("the new entry is titled by host", any(
        e.title == "newsite.example" for e in db.entries
    ), f"titles: {[e.title for e in db.entries]}")

    print("\nThe created login is retrievable")
    response = await client.send("get-logins", {
        "action": "get-logins", "url": "https://newsite.example/login", "keys": keys,
    })
    entries = response.get("entries", [])
    check(
        "get-logins returns what set-login stored",
        len(entries) == 1 and entries[0].get("password") == "freshpass",
        f"got {entries}",
    )
    created_uuid = entries[0]["uuid"] if entries else None

    print("\nUpdating an existing login")
    response = await client.send("set-login", {
        "action": "set-login", "id": client.association_id,
        "url": "https://newsite.example/login", "submitUrl": "https://newsite.example/login",
        "login": "freshuser", "password": "rotatedpass", "uuid": created_uuid,
    })
    check("update succeeds", response.get("success") == "true", f"got {response}")
    matches = [e for e in db.entries if e.uuid.hex == created_uuid]
    check(
        "the password was rotated in place, not duplicated",
        len(matches) == 1 and matches[0].password == "rotatedpass",
        f"got {[(e.title, e.password) for e in matches]}",
    )

    print("\nset-login refuses bad input")
    response = await client.send("set-login", {
        "action": "set-login", "id": client.association_id,
        "url": "https://newsite.example/login", "login": "x", "password": "",
    })
    check(
        "an empty password is refused",
        response.get("errorCode") == 0,
        f"got {response}",
    )

    response = await client.send("set-login", {
        "action": "set-login", "id": client.association_id,
        "url": "https://newsite.example/login", "login": "x", "password": "y",
        "uuid": "00000000000000000000000000000000",
    })
    check(
        "updating a deleted entry is refused rather than recreated",
        response.get("errorCode") == 0,
        f"got {response}",
    )

    print("\nset-login requires a verified association")
    # A second client completes the handshake but never proves it holds the
    # association key, which is all set-login itself would demand.
    reader2, writer2 = await asyncio.open_unix_connection(str(server.path))
    intruder = Client(reader2, writer2)
    await intruder.change_public_keys()
    response = await intruder.send("set-login", {
        "action": "set-login", "id": client.association_id,
        "url": "https://victim.example/login", "login": "attacker", "password": "pwned",
    })
    check(
        "an unverified client cannot write entries",
        response.get("errorCode") == 8,
        f"got {response}",
    )
    check("no entry was created by the unverified client", not any(
        e.username == "attacker" for e in db.entries
    ))

    response = await intruder.send("get-totp", {
        "action": "get-totp", "uuid": totp_uuid,
    })
    check(
        "an unverified client cannot read one-time passwords",
        response.get("errorCode") == 8,
        f"got {response}",
    )

    response = await intruder.send(
        "get-database-groups", {"action": "get-database-groups"}
    )
    check(
        "an unverified client cannot enumerate groups",
        response.get("errorCode") == 8,
        f"got {response}",
    )

    response = await intruder.send("create-new-group", {
        "action": "create-new-group", "groupName": "Intruder",
    })
    check(
        "an unverified client cannot create groups",
        response.get("errorCode") == 8,
        f"got {response}",
    )
    check("no group was created by the unverified client",
          not any(g.name == "Intruder" for g in db.groups))

    response = await intruder.send(
        "generate-password", {"action": "generate-password"}
    )
    check(
        "an unverified client cannot use the generator",
        response.get("errorCode") == 8,
        f"got {response}",
    )
    writer2.close()

    print("\nLocked database")
    backend.db = None
    response = await client.send("get-databasehash", {"action": "get-databasehash"})
    check(
        "locked database reports DATABASE_NOT_OPENED",
        response.get("errorCode") == 1,
        f"got {response}",
    )
    backend.db = db

    print("\nMalformed input")
    response = await client.send("no-such-action", {"action": "no-such-action"})
    check("unknown action is rejected", response.get("errorCode") == 12, f"got {response}")

    # Last, because it makes everything above unavailable.
    print("\nLocking and signals")
    # A second connection that never asks for anything, standing in for another
    # browser: it must still be told the safe was locked.
    reader3, writer3 = await asyncio.open_unix_connection(str(server.path))
    observer = Client(reader3, writer3)
    await observer.change_public_keys()

    backend.on_state_change = server.broadcast

    response = await client.send("lock-database", {"action": "lock-database"})
    check(
        "lock-database reports success as DATABASE_NOT_OPENED, as the protocol requires",
        response.get("errorCode") == 1,
        f"got {response}",
    )
    check("the backend was actually asked to lock", backend.locked is True)

    response = await client.send("get-databasehash", {"action": "get-databasehash"})
    check(
        "the safe really is locked afterwards",
        response.get("errorCode") == 1,
        f"got {response}",
    )
    check(
        "a plain request against a locked safe does not summon the window",
        backend.unlock_requests == 0,
        f"got {backend.unlock_requests}",
    )

    print("\nSummoning the unlock window")
    response = await client.send(
        "get-databasehash", {"action": "get-databasehash"}, trigger_unlock=True
    )
    check(
        "triggerUnlock asks the application to bring up its window",
        backend.unlock_requests == 1,
        f"got {backend.unlock_requests}",
    )
    check(
        "the request is still answered rather than waiting for the unlock",
        response.get("errorCode") == 1,
        f"got {response}",
    )

    # Nothing to verify against once locked, so this must not raise either.
    response = await client.send("lock-database", {"action": "lock-database"})
    check(
        "locking an already locked safe is harmless",
        response.get("errorCode") == 1,
        f"got {response}",
    )

    check(
        "the client that locked was signalled",
        "database-locked" in client.signals,
        f"got {client.signals}",
    )
    check(
        "a redundant lock does not signal twice",
        client.signals.count("database-locked") == 1,
        f"got {client.signals}",
    )
    observed = await observer.wait_for_signal()
    check(
        "a client that asked for nothing was signalled too",
        observed == "database-locked",
        f"got {observed!r}",
    )

    # The protocol has no unlock action; this exercises the push path for the
    # transition the application reports when the user unlocks in the UI.
    await backend.unlock(db)
    unlocked = await observer.wait_for_signal()
    check(
        "unlocking is signalled as well",
        unlocked == "database-unlocked",
        f"got {unlocked!r}",
    )

    response = await observer.send("get-databasehash", {"action": "get-databasehash"})
    check(
        "the safe is usable again after the unlock signal",
        bool(response.get("hash")),
        f"got {response}",
    )

    before = backend.unlock_requests
    await observer.send(
        "get-databasehash", {"action": "get-databasehash"}, trigger_unlock=True
    )
    check(
        "triggerUnlock against an open safe summons nothing",
        backend.unlock_requests == before,
        f"got {backend.unlock_requests}, was {before}",
    )

    writer3.close()
    await asyncio.sleep(0.1)
    delivered = await server.broadcast("database-locked")
    check(
        "a disconnected client is dropped rather than breaking the broadcast",
        delivered <= 1,
        f"delivered to {delivered} clients",
    )

    writer.close()
    await server.stop()
    check("socket is removed on shutdown", not server.path.exists())


def main() -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="cipher-browser-check-"))
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
