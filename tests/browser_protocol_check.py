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

from gsecrets.browser import crypto, store  # noqa: E402
from gsecrets.browser.server import BrowserServer  # noqa: E402

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

    def get_database(self):
        return self.db

    async def confirm_association(self, key_id: str) -> str | None:
        return self.association_name if self.approve else None

    async def save(self) -> None:
        self.saved += 1


class Client:
    """The browser half of the protocol."""

    def __init__(self, reader, writer) -> None:
        self.reader, self.writer = reader, writer
        self.secret = PrivateKey.generate()
        self.client_id = crypto.b64encode(nacl_random(24))
        self.box: Box | None = None
        self.id_key = crypto.b64encode(bytes(PrivateKey.generate().public_key))
        self.association_id: str | None = None

    async def _roundtrip(self, request: dict) -> dict:
        self.writer.write(json.dumps(request).encode())
        await self.writer.drain()
        raw = await asyncio.wait_for(self.reader.read(65536), timeout=5)
        return json.loads(raw.decode())

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

    async def send(self, action: str, payload: dict) -> dict:
        """Encrypt, send, and decrypt one action, verifying nonce handling."""
        assert self.box is not None, "handshake must run first"

        nonce = nacl_random(24)
        ciphertext = self.box.encrypt(json.dumps(payload).encode(), nonce).ciphertext
        response = await self._roundtrip({
            "action": action,
            "message": crypto.b64encode(ciphertext),
            "nonce": crypto.b64encode(nonce),
            "clientID": self.client_id,
        })

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
