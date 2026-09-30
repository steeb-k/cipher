# SPDX-License-Identifier: GPL-3.0-only
"""Fetching website icons for many entries at once.

Runs the fetches on a small pool of threads and hands each result back on
the main loop, one at a time, so the caller can write to the model without
locking and the list updates as icons arrive rather than all at the end.

No GTK here, only GLib, so tests/icon_download_check.py can drive it with a
fake fetch and a plain main loop.
"""

from __future__ import annotations

import logging
import threading
import typing
from concurrent.futures import ThreadPoolExecutor, as_completed

from gi.repository import GLib

if typing.TYPE_CHECKING:
    from collections.abc import Callable

# Enough to hide one slow site behind the others, few enough that a safe with
# hundreds of entries does not open hundreds of connections at once.
WORKERS = 4


class BulkIconDownload:
    """One run over a list of (item, url) pairs.

    `fetch` is called on a worker thread with each URL and returns PNG bytes
    or None. `on_result(item, data)` is called on the main loop for every
    item, including the ones that yielded nothing, and `on_done(found,
    missing)` once after the last of them.
    """

    def __init__(
        self,
        fetch: Callable[[str], bytes | None],
        on_result: Callable[[object, bytes | None], None],
        on_done: Callable[[int, int], None],
    ) -> None:
        self._fetch = fetch
        self._on_result = on_result
        self._on_done = on_done
        self._cancelled = threading.Event()
        self.running = False

    def start(self, jobs: list[tuple[object, str]]) -> None:
        if self.running:
            raise RuntimeError("download already running")

        self.running = True
        thread = threading.Thread(
            target=self._run, args=(list(jobs),), daemon=True
        )
        thread.start()

    def cancel(self) -> None:
        """Stop reporting results. Fetches in flight finish on their own."""
        self._cancelled.set()

    def _run(self, jobs: list[tuple[object, str]]) -> None:
        found = 0
        missing = 0

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {
                pool.submit(self._fetch_quietly, url): item for item, url in jobs
            }
            for future in as_completed(futures):
                if self._cancelled.is_set():
                    break

                data = future.result()
                if data is None:
                    missing += 1
                else:
                    found += 1
                GLib.idle_add(self._report, futures[future], data)

        self.running = False
        if not self._cancelled.is_set():
            GLib.idle_add(self._finish, found, missing)

    def _fetch_quietly(self, url: str) -> bytes | None:
        try:
            return self._fetch(url)
        except Exception:
            logging.exception("Fetching an icon for %s failed", url)
            return None

    def _report(self, item: object, data: bytes | None) -> bool:
        if not self._cancelled.is_set():
            self._on_result(item, data)
        return GLib.SOURCE_REMOVE

    def _finish(self, found: int, missing: int) -> bool:
        self._on_done(found, missing)
        return GLib.SOURCE_REMOVE
