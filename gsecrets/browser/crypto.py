# SPDX-License-Identifier: GPL-3.0-only
"""NaCl box encryption for the browser protocol.

Wire-compatible with keepassxc-browser: X25519 crypto_box with 24 byte nonces
and base64 encoded payloads. Responses carry the request nonce incremented by
one, using libsodium's little-endian sodium_increment() semantics.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from nacl.public import Box, PrivateKey, PublicKey
from nacl.utils import random as nacl_random

from gsecrets.browser.errors import CryptoError

NONCE_SIZE = 24


def b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64decode(data: str) -> bytes:
    return base64.b64decode(data, validate=True)


def random_nonce() -> bytes:
    return nacl_random(NONCE_SIZE)


def increment_nonce(nonce: bytes) -> bytes:
    """Increment `nonce` little-endian, matching libsodium's sodium_increment()."""
    out = bytearray(nonce)
    carry = 1
    for i in range(len(out)):
        carry += out[i]
        out[i] = carry & 0xFF
        carry >>= 8
    return bytes(out)


class ClientSession:
    """Per-client encryption state.

    A fresh host key pair is generated for every key exchange, so a client
    reconnecting or renegotiating never reuses an old shared secret.
    """

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self._secret_key = PrivateKey.generate()
        self._box: Box | None = None

    @property
    def public_key(self) -> str:
        """This session's host public key, base64 encoded."""
        return b64encode(bytes(self._secret_key.public_key))

    @property
    def established(self) -> bool:
        return self._box is not None

    def exchange_keys(self, client_public_key: str) -> str:
        """Adopt the client's public key and return ours.

        Regenerates the host key pair so that renegotiation cannot resurrect a
        previously established shared secret.
        """
        self._secret_key = PrivateKey.generate()
        try:
            peer = PublicKey(b64decode(client_public_key))
        except Exception as exc:
            raise CryptoError(f"invalid client public key: {exc}") from exc

        self._box = Box(self._secret_key, peer)
        return self.public_key

    def decrypt(self, message: str, nonce: str) -> dict[str, Any]:
        if self._box is None:
            raise CryptoError("no key exchange has taken place")

        try:
            plaintext = self._box.decrypt(b64decode(message), b64decode(nonce))
        except Exception as exc:
            raise CryptoError(f"cannot decrypt message: {exc}") from exc

        try:
            decoded = json.loads(plaintext)
        except json.JSONDecodeError as exc:
            raise CryptoError(f"decrypted payload is not JSON: {exc}") from exc

        if not isinstance(decoded, dict):
            raise CryptoError("decrypted payload is not a JSON object")

        return decoded

    def encrypt(self, payload: dict[str, Any], nonce: bytes) -> str:
        if self._box is None:
            raise CryptoError("no key exchange has taken place")

        raw = json.dumps(payload).encode("utf-8")
        try:
            return b64encode(self._box.encrypt(raw, nonce).ciphertext)
        except Exception as exc:
            raise CryptoError(f"cannot encrypt message: {exc}") from exc
