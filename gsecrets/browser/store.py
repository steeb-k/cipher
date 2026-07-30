# SPDX-License-Identifier: GPL-3.0-only
"""Persistence for browser client associations.

Associations live in the database's Meta/CustomData rather than in an entry,
which is where KeePassXC keeps them too. That placement matters: entries are
returned by searches and by get-logins, so storing association keys as an
entry would leak them into credential results and clutter the user's view.

pykeepass has no CustomData API, so this manipulates the parsed XML tree
directly.
"""

from __future__ import annotations

import hashlib

from lxml import etree
from pykeepass import PyKeePass

# Namespaced under the app so a database shared with KeePassXC keeps both sets
# of associations independently.
KEY_PREFIX = "Cipher_Browser_"


def _custom_data(db: PyKeePass, create: bool = False) -> etree._Element | None:
    meta = db.tree.find("Meta")
    if meta is None:
        return None

    custom_data = meta.find("CustomData")
    if custom_data is None and create:
        custom_data = etree.SubElement(meta, "CustomData")

    return custom_data


def _items(db: PyKeePass) -> dict[str, etree._Element]:
    custom_data = _custom_data(db)
    if custom_data is None:
        return {}

    found = {}
    for item in custom_data.findall("Item"):
        key = item.find("Key")
        if key is not None and key.text:
            found[key.text] = item

    return found


def get_association(db: PyKeePass, name: str) -> str | None:
    """Return the stored public key for association `name`, if any."""
    item = _items(db).get(KEY_PREFIX + name)
    if item is None:
        return None

    value = item.find("Value")
    return value.text if value is not None else None


def set_association(db: PyKeePass, name: str, public_key: str) -> None:
    """Store `public_key` under association `name`, replacing any existing one."""
    full_key = KEY_PREFIX + name
    existing = _items(db).get(full_key)

    if existing is not None:
        value = existing.find("Value")
        if value is None:
            value = etree.SubElement(existing, "Value")
        value.text = public_key
        return

    custom_data = _custom_data(db, create=True)
    if custom_data is None:
        raise RuntimeError("database has no Meta element")

    item = etree.SubElement(custom_data, "Item")
    etree.SubElement(item, "Key").text = full_key
    etree.SubElement(item, "Value").text = public_key


def list_associations(db: PyKeePass) -> dict[str, str]:
    """Return every stored association as {name: public key}."""
    found = {}
    for key, item in _items(db).items():
        if not key.startswith(KEY_PREFIX):
            continue

        value = item.find("Value")
        if value is not None and value.text:
            found[key[len(KEY_PREFIX) :]] = value.text

    return found


def remove_association(db: PyKeePass, name: str) -> bool:
    """Delete association `name`. Returns whether it existed."""
    item = _items(db).get(KEY_PREFIX + name)
    if item is None:
        return False

    item.getparent().remove(item)
    return True


def database_hash(db: PyKeePass) -> str:
    """A stable identifier for this database.

    Derived from the root group UUID, matching how KeePassXC computes the hash
    it hands to the extension. It identifies the database across sessions
    without revealing anything about its contents or location.
    """
    return hashlib.sha256(db.root_group.uuid.hex.encode("utf-8")).hexdigest()
