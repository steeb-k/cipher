# SPDX-License-Identifier: GPL-3.0-only
"""Matching stored entries against a requested URL.

This is deliberately conservative: it matches on host, never on path, and
requires an exact host match or a parent-domain match. Loosening it is the
main tuning knob for browser integration quality, so keep the rules here
rather than spreading them through the protocol handlers.
"""

from __future__ import annotations

from urllib.parse import urlsplit


def hostname(url: str) -> str | None:
    """Extract a lowercase hostname from `url`, tolerating a missing scheme."""
    if not url:
        return None

    candidate = url.strip()
    if "//" not in candidate:
        # urlsplit treats a bare "example.com/path" as a path, not a host.
        candidate = "//" + candidate

    try:
        host = urlsplit(candidate).hostname
    except ValueError:
        return None

    return host.lower() if host else None


def is_parent_domain(parent: str, child: str) -> bool:
    """Whether `child` is `parent` or a subdomain of it.

    The dot-boundary check is what stops "notexample.com" from matching an
    entry stored for "example.com".
    """
    return child == parent or child.endswith("." + parent)


def entry_matches(entry_url: str, request_host: str) -> bool:
    """Whether an entry storing `entry_url` should be offered for `request_host`."""
    entry_host = hostname(entry_url)
    if not entry_host or not request_host:
        return False

    return is_parent_domain(entry_host, request_host)


def find_matching(entries, request_url: str):
    """Return the entries whose URL matches `request_url`.

    `entries` is any iterable of pykeepass entries.
    """
    request_host = hostname(request_url)
    if not request_host:
        return []

    return [
        entry
        for entry in entries
        if entry.url and entry_matches(entry.url, request_host)
    ]
