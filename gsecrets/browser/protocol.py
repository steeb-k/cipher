# SPDX-License-Identifier: GPL-3.0-only
"""Request dispatch for the browser protocol.

This module is deliberately free of GTK and GLib imports so it can be
exercised headlessly. Everything it needs from the application arrives
through a `Backend`.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from gsecrets.browser import crypto, errors, matching, store
from gsecrets.browser.crypto import ClientSession
from gsecrets.browser.errors import ProtocolError

# Reported to the extension, which treats it as a capability declaration
# rather than a version string: updateFeaturesList() enables optional actions
# purely by comparing against it. So this must name the highest level we
# actually implement, not Cipher's own version.
#
# 2.6.0 is the extension's minimum supported version and enables nothing
# optional. Raise it as actions land:
#   2.6.1  -> get-totp
#   2.7.0  -> generate-password, favicon download after set-login
#   2.7.7  -> passkeys-get, passkeys-register
#   2.7.10 -> passkeys default group
# Claiming a level we do not implement makes the extension attempt those
# actions on real sites and get INCORRECT_ACTION back.
PROTOCOL_VERSION = "2.6.0"


class Backend(Protocol):
    """The application surface the protocol handlers depend on."""

    def get_database(self):
        """Return the unlocked PyKeePass database, or None if unavailable."""

    def confirm_association(self, key_id: str) -> str | None:
        """Ask the user to approve a new association.

        Returns the name to store it under, or None if declined.
        """

    def save(self) -> None:
        """Persist pending database changes."""


class RequestHandler:
    """Handles decrypted requests for a single client connection."""

    def __init__(self, backend: Backend) -> None:
        self._backend = backend
        self._sessions: dict[str, ClientSession] = {}

    # -- session helpers -------------------------------------------------

    def _session(self, client_id: str) -> ClientSession:
        session = self._sessions.get(client_id)
        if session is None:
            raise ProtocolError(
                errors.CLIENT_PUBLIC_KEY_NOT_RECEIVED,
                "no key exchange for this client",
            )
        return session

    def _database(self):
        db = self._backend.get_database()
        if db is None:
            raise ProtocolError(
                errors.DATABASE_NOT_OPENED, "no unlocked database available"
            )
        return db

    def _verify_keys(self, db, keys: list[dict[str, str]]) -> None:
        """Check that the client presented a key we previously associated.

        Without this any local process that completed a key exchange could
        read credentials; association is what binds a client to this database.
        """
        if not keys:
            raise ProtocolError(
                errors.ASSOCIATION_FAILED, "no association keys presented"
            )

        for item in keys:
            name, key = item.get("id"), item.get("key")
            if not name or not key:
                continue
            if store.get_association(db, name) == key:
                return

        raise ProtocolError(
            errors.ENCRYPTION_KEY_UNRECOGNIZED, "no recognised association key"
        )

    # -- dispatch --------------------------------------------------------

    def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Handle one request envelope and return the response envelope."""
        action = request.get("action")
        if not action:
            raise ProtocolError(errors.INCORRECT_ACTION, "missing action")

        # The key exchange is the only unencrypted exchange.
        if action == "change-public-keys":
            return self._change_public_keys(request)

        client_id = request.get("clientID")
        if not client_id:
            raise ProtocolError(errors.INCORRECT_ACTION, "missing clientID")

        message, nonce = request.get("message"), request.get("nonce")
        if not message or not nonce:
            raise ProtocolError(errors.EMPTY_MESSAGE_RECEIVED, "missing payload")

        session = self._session(client_id)
        payload = session.decrypt(message, nonce)

        handler = self._HANDLERS.get(action)
        if handler is None:
            raise ProtocolError(
                errors.INCORRECT_ACTION, f"unsupported action: {action}"
            )

        result = handler(self, payload)

        # Responses echo the request nonce incremented by one, both in the
        # envelope and inside the encrypted payload; the extension checks both.
        response_nonce = crypto.increment_nonce(crypto.b64decode(nonce))
        result.setdefault("success", "true")
        result.setdefault("version", PROTOCOL_VERSION)
        result["nonce"] = crypto.b64encode(response_nonce)

        return {
            "action": action,
            "message": session.encrypt(result, response_nonce),
            "nonce": crypto.b64encode(response_nonce),
        }

    # -- handlers --------------------------------------------------------

    def _change_public_keys(self, request: dict[str, Any]) -> dict[str, Any]:
        client_id = request.get("clientID")
        public_key = request.get("publicKey")
        nonce = request.get("nonce")
        if not client_id or not public_key:
            raise ProtocolError(
                errors.CLIENT_PUBLIC_KEY_NOT_RECEIVED,
                "missing clientID or publicKey",
            )

        if not nonce:
            raise ProtocolError(
                errors.CLIENT_PUBLIC_KEY_NOT_RECEIVED, "missing nonce"
            )

        session = ClientSession(client_id)
        host_key = session.exchange_keys(public_key)
        self._sessions[client_id] = session

        # This exchange is unencrypted, but the reply must still carry the
        # incremented nonce: the extension's verifyKeyResponse() checks both
        # its length and its value, and rejects the exchange otherwise. The
        # protocol documentation omits this field from the documented
        # response, so follow the implementation rather than the document.
        response_nonce = crypto.increment_nonce(crypto.b64decode(nonce))

        return {
            "action": "change-public-keys",
            "version": PROTOCOL_VERSION,
            "publicKey": host_key,
            "nonce": crypto.b64encode(response_nonce),
            "success": "true",
        }

    def _get_databasehash(self, _payload: dict[str, Any]) -> dict[str, Any]:
        return {"action": "hash", "hash": store.database_hash(self._database())}

    def _associate(self, payload: dict[str, Any]) -> dict[str, Any]:
        db = self._database()

        id_key = payload.get("idKey")
        if not id_key:
            raise ProtocolError(errors.ASSOCIATION_FAILED, "missing idKey")

        name = self._backend.confirm_association(id_key)
        if not name:
            raise ProtocolError(
                errors.ACTION_CANCELLED_OR_DENIED, "association declined"
            )

        store.set_association(db, name, id_key)
        self._backend.save()

        return {"hash": store.database_hash(db), "id": name}

    def _test_associate(self, payload: dict[str, Any]) -> dict[str, Any]:
        db = self._database()

        name, key = payload.get("id"), payload.get("key")
        if not name or not key:
            raise ProtocolError(errors.ASSOCIATION_FAILED, "missing id or key")

        if store.get_association(db, name) != key:
            raise ProtocolError(
                errors.ASSOCIATION_FAILED, "association not recognised"
            )

        return {"hash": store.database_hash(db), "id": name}

    def _get_logins(self, payload: dict[str, Any]) -> dict[str, Any]:
        db = self._database()

        url = payload.get("url")
        if not url:
            raise ProtocolError(errors.NO_URL_PROVIDED, "missing url")

        self._verify_keys(db, payload.get("keys", []))

        matches = matching.find_matching(db.entries, url)
        if not matches:
            raise ProtocolError(errors.NO_LOGINS_FOUND, "no matching entries")

        entries = [
            {
                "login": entry.username or "",
                "name": entry.title or "",
                "password": entry.password or "",
                "uuid": entry.uuid.hex,
                "group": entry.group.name if entry.group else "",
            }
            for entry in matches
        ]

        return {
            "count": str(len(entries)),
            "entries": entries,
            "hash": store.database_hash(db),
        }

    def _lock_database(self, _payload: dict[str, Any]) -> dict[str, Any]:
        raise ProtocolError(
            errors.INCORRECT_ACTION, "lock-database is not implemented yet"
        )

    _HANDLERS = {
        "get-databasehash": _get_databasehash,
        "associate": _associate,
        "test-associate": _test_associate,
        "get-logins": _get_logins,
        "lock-database": _lock_database,
    }


def error_response(action: str | None, exc: ProtocolError) -> dict[str, Any]:
    """Build the unencrypted error envelope the extension expects."""
    logging.debug("browser protocol error (%s): %s", action, exc)
    return {
        "action": action or "",
        "error": str(exc),
        "errorCode": exc.code,
    }
