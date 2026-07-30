# SPDX-License-Identifier: GPL-3.0-only
"""Unix socket server carrying the browser protocol.

Framing matches keepassxc-proxy: bare JSON objects written back to back with
no length prefix or delimiter, so the reader accumulates bytes and decodes
successive objects out of the buffer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from gsecrets import const
from gsecrets.browser.errors import ProtocolError
from gsecrets.browser.protocol import Backend, RequestHandler, error_response

SOCKET_NAME = "BrowserServer"

# Guards against a malfunctioning or hostile peer growing the buffer without
# ever completing a JSON object.
MAX_BUFFER = 1024 * 1024


def socket_directory() -> Path:
    """Runtime directory for this application's sockets.

    Derived from the application ID so that a Devel build and a stable build
    can run at the same time without fighting over one socket.
    """
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        runtime_dir = f"/run/user/{os.getuid()}"

    return Path(runtime_dir) / const.APP_ID


def socket_path() -> Path:
    return socket_directory() / SOCKET_NAME


class BrowserServer:
    """Accepts browser connections and dispatches protocol requests."""

    def __init__(self, backend: Backend, path: Path | None = None) -> None:
        self._backend = backend
        self._path = path or socket_path()
        self._server: asyncio.AbstractServer | None = None
        # Live connections, so state changes can be pushed to them. Everything
        # else in this protocol is request/response, where a reply goes back
        # down the connection that asked; signals have no request to answer.
        self._connections: set[asyncio.StreamWriter] = set()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def running(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        if self._server is not None:
            return

        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        # The socket carries credentials, so keep it owner-only.
        directory.chmod(0o700)

        self._remove_stale_socket()

        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=str(self._path)
        )
        self._path.chmod(0o600)

        logging.info("Browser server listening on %s", self._path)

    async def stop(self) -> None:
        if self._server is None:
            self._path.unlink(missing_ok=True)
            return

        self._server.close()
        await self._server.wait_closed()
        self._server = None

        self._path.unlink(missing_ok=True)
        logging.info("Browser server stopped")

    def close(self) -> None:
        """Stop listening and remove the socket, without awaiting anything.

        For use from application shutdown. Scheduling stop() as a task there
        does not work: the event loop is already being torn down, so the task
        never runs and the socket is left behind for the next run to find.
        AbstractServer.close() is synchronous; only wait_closed() is not, and
        waiting for connections to drain is pointless at process exit.
        """
        if self._server is not None:
            self._server.close()
            self._server = None

        self._path.unlink(missing_ok=True)
        logging.info("Browser server closed")

    def _remove_stale_socket(self) -> None:
        """Clear a socket left behind by a previous run.

        Only removes it if nothing is listening, so a second instance cannot
        silently steal a live socket from the first.
        """
        if not self._path.exists():
            return

        import socket

        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(self._path))
        except OSError:
            logging.debug("Removing stale socket at %s", self._path)
            self._path.unlink(missing_ok=True)
        else:
            probe.close()
            raise RuntimeError(f"another instance is listening on {self._path}")
        finally:
            probe.close()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        handler = RequestHandler(self._backend)
        decoder = json.JSONDecoder()
        buffer = ""

        self._connections.add(writer)
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break

                buffer += chunk.decode("utf-8", errors="replace")
                if len(buffer) > MAX_BUFFER:
                    logging.warning("Browser client exceeded buffer limit")
                    break

                buffer = await self._drain_buffer(handler, decoder, buffer, writer)
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            logging.exception("Unhandled error on browser connection")
        finally:
            self._connections.discard(writer)
            writer.close()

    async def broadcast(self, action: str) -> int:
        """Push an unsolicited signal to every connected client.

        Signals are plaintext: the extension's onNativeMessage() dispatches on
        response.action alone, before any decryption, and they carry no secret
        beyond the fact that the state changed. That also avoids the question of
        which client's session key to encrypt an unsolicited message with.

        Best-effort by design. A client that has gone away must not stop the
        others from being told, and cannot be allowed to propagate an error into
        whatever changed the lock state.
        """
        if not self._connections:
            return 0

        payload = json.dumps({"action": action}).encode("utf-8")
        delivered = 0

        for writer in list(self._connections):
            try:
                writer.write(payload)
                await writer.drain()
                delivered += 1
            except (ConnectionResetError, BrokenPipeError, OSError) as err:
                logging.debug("Dropping browser connection during signal: %s", err)
                self._connections.discard(writer)

        logging.debug("Signalled %s to %s client(s)", action, delivered)
        return delivered

    async def _drain_buffer(
        self,
        handler: RequestHandler,
        decoder: json.JSONDecoder,
        buffer: str,
        writer: asyncio.StreamWriter,
    ) -> str:
        """Decode and dispatch every complete JSON object in `buffer`."""
        while buffer:
            stripped = buffer.lstrip()
            if not stripped:
                return ""

            try:
                request, end = decoder.raw_decode(stripped)
            except json.JSONDecodeError:
                # An incomplete object; wait for more bytes.
                return stripped

            buffer = stripped[end:]
            response = await self._dispatch(handler, request)
            if response is not None:
                writer.write(json.dumps(response).encode("utf-8"))
                await writer.drain()

        return buffer

    async def _dispatch(self, handler: RequestHandler, request: dict) -> dict | None:
        action = request.get("action") if isinstance(request, dict) else None
        try:
            if not isinstance(request, dict):
                raise ProtocolError(0, "request is not a JSON object")
            return await handler.handle(request)
        except ProtocolError as exc:
            return error_response(action, exc)
        except Exception as exc:  # noqa: BLE001 - never kill the connection
            logging.exception("Error handling browser request")
            return error_response(action, ProtocolError(0, str(exc)))
