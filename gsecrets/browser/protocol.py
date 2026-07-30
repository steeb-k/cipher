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

    async def confirm_association(self, key_id: str) -> str | None:
        """Ask the user to approve a new association.

        Returns the name to store it under, or None if declined. Asynchronous
        because the real implementation presents a dialog and the extension
        expects a single blocking reply; the alternative -- returning "pending"
        and having the client retry -- is not something the protocol provides
        for.
        """

    async def save(self) -> None:
        """Persist pending database changes."""

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
        """Create or update a login, returning True if one was created.

        Owned by the backend rather than done here through pykeepass, because
        the application keeps a parallel model of the database that drives its
        UI. Writing entries behind that model's back leaves the visible list
        stale until the safe is reopened.
        """


class RequestHandler:
    """Handles decrypted requests for a single client connection."""

    def __init__(self, backend: Backend) -> None:
        self._backend = backend
        self._sessions: dict[str, ClientSession] = {}
        # Association names this connection has proven it holds the key for,
        # per client. See _require_verified().
        self._verified: dict[str, set[str]] = {}

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

    def _mark_verified(self, client_id: str, name: str) -> None:
        self._verified.setdefault(client_id, set()).add(name)

    def _require_verified(self, client_id: str, name: str) -> None:
        """Require that this client proved it holds the key for `name`.

        set-login sends only the association id, never the key, so unlike
        get-logins it carries no proof of its own. An id alone is guessable,
        and the encrypted channel proves nothing about identity because any
        local process can complete a key exchange. Without this check, any such
        process could write entries into the open safe.

        Requiring the id to have been verified earlier on this same connection
        closes that: verification happens in associate and test-associate, both
        of which check the key. The extension's updateCredentials() always calls
        testAssociation() first, and that always makes a round trip, so this
        costs nothing in practice.
        """
        if name not in self._verified.get(client_id, ()):
            raise ProtocolError(
                errors.ASSOCIATION_FAILED,
                "association has not been verified on this connection",
            )

    # -- dispatch --------------------------------------------------------

    async def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
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

        result = await handler(self, payload, client_id)

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

    async def _get_databasehash(
        self, _payload: dict[str, Any], _client_id: str
    ) -> dict[str, Any]:
        return {"action": "hash", "hash": store.database_hash(self._database())}

    async def _associate(
        self, payload: dict[str, Any], client_id: str
    ) -> dict[str, Any]:
        db = self._database()

        id_key = payload.get("idKey")
        if not id_key:
            raise ProtocolError(errors.ASSOCIATION_FAILED, "missing idKey")

        name = await self._backend.confirm_association(id_key)
        if not name:
            raise ProtocolError(
                errors.ACTION_CANCELLED_OR_DENIED, "association declined"
            )

        store.set_association(db, name, id_key)
        await self._backend.save()
        self._mark_verified(client_id, name)

        return {"hash": store.database_hash(db), "id": name}

    async def _test_associate(
        self, payload: dict[str, Any], client_id: str
    ) -> dict[str, Any]:
        db = self._database()

        name, key = payload.get("id"), payload.get("key")
        if not name or not key:
            raise ProtocolError(errors.ASSOCIATION_FAILED, "missing id or key")

        if store.get_association(db, name) != key:
            raise ProtocolError(
                errors.ASSOCIATION_FAILED, "association not recognised"
            )

        # Comparing the key above is what makes this proof, so record it as the
        # credential set-login relies on.
        self._mark_verified(client_id, name)

        return {"hash": store.database_hash(db), "id": name}

    async def _get_logins(
        self, payload: dict[str, Any], client_id: str
    ) -> dict[str, Any]:
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

    async def _set_login(
        self, payload: dict[str, Any], client_id: str
    ) -> dict[str, Any]:
        db = self._database()

        name = payload.get("id")
        if not name:
            raise ProtocolError(errors.ASSOCIATION_FAILED, "missing association id")

        self._require_verified(client_id, name)

        url = payload.get("url")
        if not url:
            raise ProtocolError(errors.NO_URL_PROVIDED, "missing url")

        login = payload.get("login") or ""
        password = payload.get("password") or ""
        if not password:
            # Saving a blank password silently would be worse than refusing:
            # it looks like a successful save and overwrites a real one.
            raise ProtocolError(
                errors.UNKNOWN_ERROR, "refusing to store an empty password"
            )

        uuid = payload.get("uuid") or None
        group_uuid = payload.get("groupUuid") or None

        created = await self._backend.set_login(
            url=url,
            login=login,
            password=password,
            # Entries are titled by host, which is what the user recognises in
            # the safe and what a bare URL does not give them.
            title=matching.hostname(url) or url,
            uuid=uuid,
            group_uuid=group_uuid,
        )
        await self._backend.save()

        logging.info(
            "Browser %s a login for %s", "created" if created else "updated", url
        )

        # count and entries are unused for this action but the extension's
        # response shape includes them; error must be present and empty, since
        # updateCredentials() reads it to decide between reporting "created"
        # and surfacing it as a failure message.
        return {
            "count": None,
            "entries": None,
            "error": "",
            "hash": store.database_hash(db),
        }

    async def _lock_database(
        self, _payload: dict[str, Any], _client_id: str
    ) -> dict[str, Any]:
        raise ProtocolError(
            errors.INCORRECT_ACTION, "lock-database is not implemented yet"
        )

    _HANDLERS = {
        "get-databasehash": _get_databasehash,
        "associate": _associate,
        "test-associate": _test_associate,
        "get-logins": _get_logins,
        "set-login": _set_login,
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
