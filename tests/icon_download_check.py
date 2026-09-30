#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
"""Checks for gsecrets.icon_download with a fake fetch and a GLib main loop.

No network, no GTK, no display:

    tests/icon_download_check.py

Exits non-zero if any check fails.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from gi.repository import GLib  # noqa: E402

from gsecrets.icon_download import BulkIconDownload  # noqa: E402

failures = 0


def check(condition: bool, message: str) -> None:
    global failures  # noqa: PLW0603
    if condition:
        print(f"ok   {message}")
    else:
        failures += 1
        print(f"FAIL {message}")


def run(jobs, fetch, timeout=5.0):
    """Drive one download to completion on a main loop; return what it reported."""
    loop = GLib.MainLoop()
    results: list[tuple[object, bytes | None]] = []
    threads: set[int] = set()
    done: list[tuple[int, int]] = []

    def on_result(item, data):
        threads.add(threading.get_ident())
        results.append((item, data))

    def on_done(found, missing):
        threads.add(threading.get_ident())
        done.append((found, missing))
        loop.quit()

    download = BulkIconDownload(fetch, on_result, on_done)
    GLib.timeout_add(int(timeout * 1000), loop.quit)
    download.start(jobs)
    loop.run()
    return results, done, threads, download


def main() -> int:
    main_thread = threading.get_ident()

    def fetch(url):
        time.sleep(0.01)
        if "none" in url:
            return None
        if "boom" in url:
            raise RuntimeError("simulated failure")
        return b"PNG:" + url.encode()

    jobs = [("a", "https://a.example"), ("b", "https://none.example"),
            ("c", "https://boom.example"), ("d", "https://d.example")]
    results, done, threads, download = run(jobs, fetch)

    check(len(results) == 4, "every job is reported")
    check(dict(results)["a"] == b"PNG:https://a.example", "data reaches the item it was fetched for")
    check(dict(results)["b"] is None, "a site without an icon reports None")
    check(dict(results)["c"] is None, "a failing fetch reports None rather than raising")
    check(done == [(2, 2)], "done carries found and missing counts")
    check(threads == {main_thread}, "callbacks all run on the main loop thread")
    check(download.running is False, "run is over afterwards")

    # Cancelling stops reports, and done is never called.
    started = threading.Event()

    def slow_fetch(url):
        started.set()
        time.sleep(0.2)
        return b"late"

    loop = GLib.MainLoop()
    late: list = []
    download = BulkIconDownload(slow_fetch, lambda *a: late.append(a), lambda *a: late.append(("done", a)))
    download.start([("x", "https://x.example")])
    started.wait(2)
    download.cancel()
    GLib.timeout_add(600, loop.quit)
    loop.run()
    check(late == [], "nothing is reported after cancel")

    # A second start on a running download is refused.
    download = BulkIconDownload(slow_fetch, lambda *a: None, lambda *a: None)
    download.start([("y", "https://y.example")])
    try:
        download.start([("z", "https://z.example")])
    except RuntimeError:
        check(True, "a second concurrent start is refused")
    else:
        check(False, "a second concurrent start is refused")
    download.cancel()

    print()
    if failures:
        print(f"{failures} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
