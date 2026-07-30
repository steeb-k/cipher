# SPDX-License-Identifier: GPL-3.0-only
"""Protocol error codes.

These values are fixed by the browser extension, which maps them to localised
strings on its side. They must not be renumbered.
"""

from __future__ import annotations

UNKNOWN_ERROR = 0
DATABASE_NOT_OPENED = 1
DATABASE_HASH_NOT_RECEIVED = 2
CLIENT_PUBLIC_KEY_NOT_RECEIVED = 3
CANNOT_DECRYPT_MESSAGE = 4
TIMEOUT_OR_NOT_CONNECTED = 5
ACTION_CANCELLED_OR_DENIED = 6
PUBLIC_KEY_NOT_FOUND = 7
ASSOCIATION_FAILED = 8
KEY_CHANGE_FAILED = 9
ENCRYPTION_KEY_UNRECOGNIZED = 10
NO_SAVED_DATABASES_FOUND = 11
INCORRECT_ACTION = 12
EMPTY_MESSAGE_RECEIVED = 13
NO_URL_PROVIDED = 14
NO_LOGINS_FOUND = 15
NO_GROUPS_FOUND = 16
CANNOT_CREATE_NEW_GROUP = 17
NO_VALID_UUID_PROVIDED = 18
ACCESS_TO_ALL_ENTRIES_DENIED = 19


class ProtocolError(Exception):
    """An error that should be reported to the client as an error response."""

    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(message or f"protocol error {code}")
        self.code = code


class CryptoError(ProtocolError):
    """Encryption or decryption failed."""

    def __init__(self, message: str, code: int = CANNOT_DECRYPT_MESSAGE) -> None:
        super().__init__(code, message)
