#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
"""Checks for the network-free parts of gsecrets.favicon.

Covers candidate discovery in a page's <head> and the image conversion.
Standalone rather than a pytest module, like the browser checks; needs
GdkPixbuf but no display and no network:

    tests/favicon_check.py

Exits non-zero if any check fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import gi  # noqa: E402

gi.require_version("GdkPixbuf", "2.0")

from gi.repository import GdkPixbuf  # noqa: E402

from gsecrets import favicon  # noqa: E402

PAGE = """
<!doctype html>
<html><head>
  <title>Example</title>
  <link rel="stylesheet" href="/style.css">
  <link rel="icon" href="/favicon-32.png" sizes="32x32">
  <link rel="shortcut icon" href="favicon.ico">
  <link rel="apple-touch-icon" sizes="180x180" href="/apple.png">
  <link rel="icon" href="data:image/png;base64,AAAA">
  <link rel="ICON" href="//cdn.example.net/plain.png">
  <link rel="icon" href="/dup.png"><link rel="icon" href="/dup.png">
</head><body>
  <link rel="icon" href="/in-body.png">
</body></html>
"""

failures = 0


def check(condition: bool, message: str) -> None:
    global failures  # noqa: PLW0603
    if condition:
        print(f"ok   {message}")
    else:
        failures += 1
        print(f"FAIL {message}")


def main() -> int:
    candidates = favicon.icon_candidates(PAGE, "https://www.example.com/login/")

    check(candidates[0] == "https://www.example.com/apple.png", "largest declared icon first")
    check(
        candidates.index("https://cdn.example.net/plain.png")
        < candidates.index("https://www.example.com/favicon-32.png"),
        "undeclared size ranks above a small declared one",
    )
    check(
        "https://www.example.com/login/favicon.ico" in candidates,
        "relative href resolves against the page",
    )
    check(candidates[-1] == "https://www.example.com/favicon.ico", "root favicon.ico is the last resort")
    check(not any(c.startswith("data:") for c in candidates), "data: URIs are ignored")
    check("https://www.example.com/style.css" not in candidates, "stylesheets are not icons")
    check("https://www.example.com/in-body.png" not in candidates, "links after <head> are ignored")
    check(candidates.count("https://www.example.com/dup.png") == 1, "duplicates collapse")

    empty = favicon.icon_candidates("", "https://example.org")
    check(empty == ["https://example.org/favicon.ico"], "a page with no links still tries favicon.ico")

    # Conversion: a large image comes back as a PNG no bigger than MAX_SIZE.
    big = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 300, 200)
    big.fill(0xFF67EFFF)
    ok, raw = big.save_to_bufferv("png", [], [])
    check(ok, "test image encodes")

    png = favicon.to_png(bytes(raw))
    check(png is not None and png.startswith(b"\x89PNG"), "conversion yields PNG")
    if png is not None:
        loader = GdkPixbuf.PixbufLoader()
        loader.write(png)
        loader.close()
        result = loader.get_pixbuf()
        check(
            result.get_width() == favicon.MAX_SIZE and result.get_height() == 85,
            "scaled to MAX_SIZE on the long side, aspect kept",
        )

    small = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, 16, 16)
    small.fill(0x1C71D8FF)
    ok, raw = small.save_to_bufferv("png", [], [])
    png = favicon.to_png(bytes(raw))
    if png is not None:
        loader = GdkPixbuf.PixbufLoader()
        loader.write(png)
        loader.close()
        check(loader.get_pixbuf().get_width() == 16, "small icons are not upscaled")

    check(favicon.to_png(b"<html>not an image</html>") is None, "garbage converts to None")

    print()
    if failures:
        print(f"{failures} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
