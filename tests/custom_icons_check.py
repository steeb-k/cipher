#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
"""Checks for gsecrets.custom_icons against a real KDBX file.

Standalone rather than a pytest module, like the browser checks, so it runs
with nothing but python and pykeepass:

    tests/custom_icons_check.py

Exits non-zero if any check fails.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pykeepass import PyKeePass, create_database  # noqa: E402

from gsecrets import custom_icons  # noqa: E402

PASSWORD = "check"

# Two distinct, valid-looking payloads. The layer stores bytes and never
# decodes them, so they need not be real PNGs.
ICON_A = b"\x89PNG\r\n\x1a\nA" * 4
ICON_B = b"\x89PNG\r\n\x1a\nB" * 4

failures = 0


def check(condition: bool, message: str) -> None:
    global failures  # noqa: PLW0603
    if condition:
        print(f"ok   {message}")
    else:
        failures += 1
        print(f"FAIL {message}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "icons.kdbx")
        kp = create_database(path, password=PASSWORD)
        # pykeepass writes no IconID unless asked; KeePassXC always does. One
        # entry of each kind, since the reference is placed relative to it.
        entry = kp.add_entry(kp.root_group, "Example", "user", "secret", icon="0")
        other = kp.add_entry(kp.root_group, "Other", "user", "secret")

        check(custom_icons.list_icons(kp) == {}, "new database has no custom icons")
        check(custom_icons.element_icon(entry) is None, "new entry has no custom icon")

        uuid_a = custom_icons.add_icon(kp, ICON_A, name="example.com")
        check(custom_icons.get_icon(kp, uuid_a) == ICON_A, "added icon reads back")
        check(custom_icons.add_icon(kp, ICON_A) == uuid_a, "identical data is stored once")
        uuid_b = custom_icons.add_icon(kp, ICON_B)
        check(uuid_b != uuid_a, "different data gets a different uuid")
        check(len(custom_icons.list_icons(kp)) == 2, "two icons listed")

        custom_icons.set_element_icon(entry, uuid_a)
        check(custom_icons.element_icon(entry) == uuid_a, "entry points at icon")
        check(entry.icon == "0", "built-in icon number is untouched")

        # Beside IconID, the way KeePass writes it.
        tags = [child.tag for child in entry._element]  # noqa: SLF001
        check(
            tags.index("CustomIconUUID") == tags.index("IconID") + 1,
            "reference sits next to IconID",
        )

        custom_icons.set_element_icon(entry, None)
        check(custom_icons.element_icon(entry) is None, "reference can be cleared")
        custom_icons.set_element_icon(entry, uuid_a)
        custom_icons.set_element_icon(entry, uuid_b)
        check(
            len(entry._element.findall("CustomIconUUID")) == 1,  # noqa: SLF001
            "re-pointing replaces rather than duplicates the reference",
        )

        check(custom_icons.referenced_icons(kp) == {uuid_b}, "only the current reference counts")
        removed = custom_icons.remove_unreferenced(kp)
        check(removed == 1, "one orphan removed")
        check(custom_icons.get_icon(kp, uuid_a) is None, "orphan is gone")
        check(custom_icons.get_icon(kp, uuid_b) == ICON_B, "referenced icon survives")

        # Round trip through the file: the reader has to find what the
        # writer put in, or none of the above matters.
        kp.save()
        reopened = PyKeePass(path, password=PASSWORD)
        found = reopened.find_entries(title="Example", first=True)
        check(custom_icons.element_icon(found) == uuid_b, "reference survives save and reload")
        check(custom_icons.get_icon(reopened, uuid_b) == ICON_B, "icon data survives save and reload")

        if reopened.version >= custom_icons.NAMED_ICONS_VERSION:
            icons = reopened._xpath("/KeePassFile/Meta/CustomIcons/Icon")  # noqa: SLF001
            check(
                all(icon.find("LastModificationTime") is not None for icon in icons),
                "KDBX 4.1 icons carry a modification time",
            )

        check(custom_icons.element_icon(other) is None, "untouched entry stays untouched")

        custom_icons.set_element_icon(other, uuid_b)
        check(
            custom_icons.element_icon(other) == uuid_b,
            "an entry without IconID can still point at an icon",
        )

    print()
    if failures:
        print(f"{failures} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
