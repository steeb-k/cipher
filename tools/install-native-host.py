#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
"""Install the native messaging host manifest for Cipher Bridge.

The manifest tells the browser which executable to launch for our host name,
and restricts that to our extension ID alone. It is written to a file named
after our host, so it never touches a manifest belonging to another password
manager -- notably KeePassXC's org.keepassxc.keepassxc_browser.json.

Prints the manifest and its destination by default; pass --install to write it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HOST_NAME = "io.github.steeb_k.cipher_bridge"
EXTENSION_ID = "cipher-bridge@steeb-k.github.io"

# Where each browser family looks for host manifests.
TARGETS = {
    "firefox": Path.home() / ".mozilla" / "native-messaging-hosts",
    "librewolf": Path.home() / ".librewolf" / "native-messaging-hosts",
    "chromium": Path.home() / ".config" / "chromium" / "NativeMessagingHosts",
}

# Firefox keys the allowlist by extension ID; Chromium keys it by origin.
CHROMIUM_ORIGIN = "chrome-extension://REPLACE_WITH_CHROMIUM_EXTENSION_ID/"


def build_manifest(proxy: Path, browser: str) -> dict:
    manifest = {
        "name": HOST_NAME,
        "description": "Cipher Bridge native messaging host",
        "path": str(proxy),
        "type": "stdio",
    }

    if browser == "chromium":
        manifest["allowed_origins"] = [CHROMIUM_ORIGIN]
    else:
        manifest["allowed_extensions"] = [EXTENSION_ID]

    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--browser", choices=sorted(TARGETS), default="firefox",
    )
    parser.add_argument(
        "--proxy",
        type=Path,
        default=Path(__file__).resolve().parent / "cipher-proxy",
        help="path to the cipher-proxy executable",
    )
    parser.add_argument("--install", action="store_true", help="write the manifest")
    args = parser.parse_args()

    proxy = args.proxy.resolve()
    if not proxy.exists():
        print(f"proxy not found: {proxy}", file=sys.stderr)
        return 1
    if not proxy.stat().st_mode & 0o111:
        print(f"proxy is not executable: {proxy}", file=sys.stderr)
        return 1

    manifest = build_manifest(proxy, args.browser)
    destination = TARGETS[args.browser] / f"{HOST_NAME}.json"

    print(f"destination: {destination}")
    print(json.dumps(manifest, indent=4))

    if args.browser == "chromium":
        print(
            "\nNote: Chromium allowlists by extension ID, which is assigned at "
            f"load time. Replace {CHROMIUM_ORIGIN} before use.",
            file=sys.stderr,
        )

    if not args.install:
        print("\n(dry run -- pass --install to write it)")
        return 0

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(manifest, indent=4) + "\n")
    print(f"\nwrote {destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
