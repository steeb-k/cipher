# SPDX-License-Identifier: GPL-3.0-only
"""Custom icons stored inside the database.

KDBX keeps them in the file itself: Meta/CustomIcons lists each icon as a
UUID and PNG data (KDBX 4.1 adds a name and modification time), and an entry
or group points at one through a CustomIconUUID element, which takes
precedence over its built-in IconID. So icons travel and sync with the safe,
and KeePassXC, KeePass and KeePassDX all show them.

pykeepass exposes none of this, but it round-trips the whole XML tree, so
icons already in a file survive a save and the rest is written through the
tree directly. Everything here is plain lxml on pykeepass objects, with no
GTK, so it can be checked without a display (tests/custom_icons_check.py).
"""

from __future__ import annotations

import base64
import binascii
import typing
from datetime import UTC, datetime
from uuid import UUID, uuid4

from lxml.builder import E

if typing.TYPE_CHECKING:
    from lxml.etree import _Element
    from pykeepass import PyKeePass
    from pykeepass.baseelement import BaseElement

META_PATH = "/KeePassFile/Meta"
ICONS_TAG = "CustomIcons"
REFERENCE_TAG = "CustomIconUUID"

# KDBX version from which an icon carries a name and modification time.
NAMED_ICONS_VERSION = (4, 1)


def _encode_uuid(value: UUID) -> str:
    return base64.b64encode(value.bytes).decode("ascii")


def _decode_uuid(text: str | None) -> UUID | None:
    if not text:
        return None
    try:
        return UUID(bytes=base64.b64decode(text))
    except (binascii.Error, ValueError):
        return None


def _icons_element(kp: PyKeePass, *, create: bool) -> _Element | None:
    meta = kp._xpath(META_PATH, first=True)  # noqa: SLF001
    icons = meta.find(ICONS_TAG)
    if icons is None and create:
        icons = E.CustomIcons()
        # Where KeePass writes it. Readers do not depend on the order, but a
        # file that looks like the one KeePass would have written is easier to
        # compare against.
        anchor = meta.find("MemoryProtection")
        if anchor is not None:
            anchor.addnext(icons)
        else:
            meta.append(icons)
    return icons


def _icon_data(icon: _Element) -> bytes | None:
    data = icon.findtext("Data")
    if not data:
        return None
    try:
        return base64.b64decode(data)
    except binascii.Error:
        return None


def list_icons(kp: PyKeePass) -> dict[UUID, bytes]:
    """Every custom icon in the database, by UUID."""
    icons = _icons_element(kp, create=False)
    if icons is None:
        return {}

    out: dict[UUID, bytes] = {}
    for icon in icons.findall("Icon"):
        icon_uuid = _decode_uuid(icon.findtext("UUID"))
        data = _icon_data(icon)
        if icon_uuid is not None and data is not None:
            out[icon_uuid] = data
    return out


def get_icon(kp: PyKeePass, icon_uuid: UUID) -> bytes | None:
    """The PNG data of one icon, or None if the database has no such icon."""
    icons = _icons_element(kp, create=False)
    if icons is None:
        return None

    wanted = _encode_uuid(icon_uuid)
    for icon in icons.findall("Icon"):
        if icon.findtext("UUID") == wanted:
            return _icon_data(icon)
    return None


def find_icon(kp: PyKeePass, data: bytes) -> UUID | None:
    """The UUID of an icon with exactly this data, if one is stored already."""
    for icon_uuid, existing in list_icons(kp).items():
        if existing == data:
            return icon_uuid
    return None


def add_icon(kp: PyKeePass, data: bytes, name: str | None = None) -> UUID:
    """Store PNG data as a custom icon and return its UUID.

    Identical data is stored once: the same favicon saved for a dozen entries
    on one site should be one icon referenced a dozen times, which is also
    what KeePassXC does.
    """
    existing = find_icon(kp, data)
    if existing is not None:
        return existing

    icon_uuid = uuid4()
    icon = E.Icon(
        E.UUID(_encode_uuid(icon_uuid)),
        E.Data(base64.b64encode(data).decode("ascii")),
    )
    if kp.version >= NAMED_ICONS_VERSION:
        if name:
            icon.append(E.Name(name))
        icon.append(
            E.LastModificationTime(kp._encode_time(datetime.now(UTC)))  # noqa: SLF001
        )

    _icons_element(kp, create=True).append(icon)
    return icon_uuid


def referenced_icons(kp: PyKeePass) -> set[UUID]:
    """UUIDs referenced by any entry or group, history included."""
    return {
        icon_uuid
        for elem in kp._xpath(f"/KeePassFile/Root//{REFERENCE_TAG}")  # noqa: SLF001
        if (icon_uuid := _decode_uuid(elem.text)) is not None
    }


def remove_unreferenced(kp: PyKeePass) -> int:
    """Drop icons nothing points at any more, returning how many went.

    Not called on every change: an icon a user just replaced may still be
    referenced from the entry's history, and dropping it would leave that
    history entry with a dangling reference. Meant for a deliberate cleanup.
    """
    icons = _icons_element(kp, create=False)
    if icons is None:
        return 0

    keep = referenced_icons(kp)
    removed = 0
    for icon in list(icons.findall("Icon")):
        icon_uuid = _decode_uuid(icon.findtext("UUID"))
        if icon_uuid is None or icon_uuid not in keep:
            icons.remove(icon)
            removed += 1
    return removed


def element_icon(element: BaseElement) -> UUID | None:
    """The custom icon an entry or group points at, if any."""
    return _decode_uuid(element._element.findtext(REFERENCE_TAG))  # noqa: SLF001


def set_element_icon(element: BaseElement, icon_uuid: UUID | None) -> None:
    """Point an entry or group at a custom icon, or at none.

    Only the reference changes; the built-in IconID is left alone, which is
    what the element falls back to when the reference is cleared.
    """
    node = element._element  # noqa: SLF001
    existing = node.find(REFERENCE_TAG)
    if existing is not None:
        node.remove(existing)

    if icon_uuid is None:
        return

    reference = getattr(E, REFERENCE_TAG)(_encode_uuid(icon_uuid))
    # Beside IconID, where KeePass puts it.
    anchor = node.find("IconID")
    if anchor is not None:
        anchor.addnext(reference)
    else:
        node.append(reference)
