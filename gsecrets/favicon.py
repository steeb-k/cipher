# SPDX-License-Identifier: GPL-3.0-only
"""Fetching a website's icon for an entry.

Fetched from the site itself and nowhere else. Public favicon services have a
better hit rate, but every lookup hands a third party the name of a site the
user holds an account on, which is not a trade a password manager should
make on their behalf.

Order of preference: the icons the page declares in its <head>, largest
first, then favicon.ico at the root. Whatever loads first is scaled down to
MAX_SIZE and stored as PNG, the one format every KDBX reader shows.

Blocking, so call it from a thread: entry_page.py does so directly and the
browser backend goes through asyncio.to_thread(). Only icon_candidates() and
to_png() need no network, and those are what tests/favicon_check.py covers.
"""

from __future__ import annotations

import logging
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import gi
import requests

gi.require_version("GdkPixbuf", "2.0")

from gi.repository import GdkPixbuf, GLib  # noqa: E402

from gsecrets.browser.matching import hostname  # noqa: E402

# Longest side of a stored icon, in pixels. Big enough for any list or header
# the application draws, small enough that a safe full of icons stays small.
MAX_SIZE = 128

# Caps on what is read from the network. A page is only read far enough to
# find its <head>; an icon larger than this is not a favicon.
MAX_PAGE_BYTES = 512 * 1024
MAX_ICON_BYTES = 2 * 1024 * 1024

TIMEOUT = 10  # seconds, per request

# Some sites answer a bare client with a block page or nothing at all, and a
# password manager fetching one icon is not what those rules exist for.
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Cipher"

# rel values that name an icon, lower-cased. apple-touch-icon is included
# because it is usually the largest and cleanest one a site offers.
ICON_RELS = {
    "icon",
    "shortcut icon",
    "apple-touch-icon",
    "apple-touch-icon-precomposed",
}

# What an undeclared size is assumed to be when ordering candidates: a plain
# <link rel="icon"> is more often the real favicon than a tiny one, so it
# ranks above a declared 16x16 and below a declared 180x180.
ASSUMED_SIZE = 48


class _LinkParser(HTMLParser):
    """Collects icon links from a page's <head>."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, int]] = []
        self.done = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if self.done:
            return
        if tag == "body":
            self.done = True
            return
        if tag != "link":
            return

        attributes = {key: (value or "") for key, value in attrs}
        rel = " ".join(attributes.get("rel", "").lower().split())
        href = attributes.get("href", "").strip()
        if rel not in ICON_RELS or not href or href.startswith("data:"):
            return

        self.links.append((href, _declared_size(attributes.get("sizes", ""))))

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self.done = True


def _declared_size(sizes: str) -> int:
    """The largest side declared in a sizes attribute, or 0 if none is."""
    largest = 0
    for token in sizes.lower().split():
        width, _, height = token.partition("x")
        if width.isdigit() and height.isdigit():
            largest = max(largest, int(width), int(height))
    return largest


def icon_candidates(html: str, page_url: str) -> list[str]:
    """Icon URLs a page declares, best first, followed by /favicon.ico."""
    parser = _LinkParser()
    parser.feed(html)

    ranked = sorted(
        parser.links,
        key=lambda link: -(link[1] or ASSUMED_SIZE),
    )

    candidates: list[str] = []
    for href, _ in ranked:
        absolute = urljoin(page_url, href)
        if urlsplit(absolute).scheme in ("http", "https") and absolute not in candidates:
            candidates.append(absolute)

    root = urljoin(page_url, "/favicon.ico")
    if root not in candidates:
        candidates.append(root)
    return candidates


def to_png(data: bytes) -> bytes | None:
    """Decode any image GdkPixbuf understands and re-encode it as a small PNG.

    ICO, PNG, SVG, GIF and JPEG all arrive in the wild as favicons; PNG is
    what KDBX readers expect. Returns None for anything that does not decode.
    """
    loader = GdkPixbuf.PixbufLoader()
    try:
        loader.write(data)
        loader.close()
    except GLib.Error:
        return None

    pixbuf = loader.get_pixbuf()
    if pixbuf is None:
        return None

    width, height = pixbuf.get_width(), pixbuf.get_height()
    longest = max(width, height)
    if longest > MAX_SIZE:
        scale = MAX_SIZE / longest
        pixbuf = pixbuf.scale_simple(
            max(1, round(width * scale)),
            max(1, round(height * scale)),
            GdkPixbuf.InterpType.BILINEAR,
        )
        if pixbuf is None:
            return None

    ok, png = pixbuf.save_to_bufferv("png", [], [])
    return bytes(png) if ok else None


def _read_capped(response: requests.Response, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=16 * 1024):
        chunks.append(chunk)
        total += len(chunk)
        if total >= limit:
            break
    return b"".join(chunks)[:limit]


def fetch(url: str) -> bytes | None:
    """The icon of the site `url` points at, as PNG, or None if none loads."""
    host = hostname(url)
    if not host:
        return None

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    page_url = f"https://{host}/"
    candidates: list[str] = []
    try:
        response = session.get(page_url, stream=True, timeout=TIMEOUT)
        page = _read_capped(response, MAX_PAGE_BYTES)
        # Where the page really lives: www redirects and the like, which is
        # where relative icon links have to be resolved against.
        page_url = response.url or page_url
        candidates = icon_candidates(page.decode(response.encoding or "utf-8", "replace"), page_url)
    except requests.RequestException as err:
        logging.debug("No page for %s: %s", host, err)

    # The plain root icon, even if the page could not be read at all.
    for fallback in (urljoin(page_url, "/favicon.ico"), f"https://{host}/favicon.ico"):
        if fallback not in candidates:
            candidates.append(fallback)

    for candidate in candidates:
        try:
            response = session.get(candidate, stream=True, timeout=TIMEOUT)
            if response.status_code != requests.codes.ok:
                continue
            data = _read_capped(response, MAX_ICON_BYTES)
        except requests.RequestException as err:
            logging.debug("Could not fetch %s: %s", candidate, err)
            continue

        png = to_png(data)
        if png is not None:
            logging.debug("Icon for %s from %s", host, candidate)
            return png

    logging.info("No icon found for %s", host)
    return None
