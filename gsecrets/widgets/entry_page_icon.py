# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import typing
from gettext import gettext as _

from gi.repository import Gdk, GLib, Gtk

if typing.TYPE_CHECKING:
    from uuid import UUID

# The name the custom icon's picker child carries, where the built-in
# children carry their icon number. Cannot collide: numbers are digits.
CUSTOM_ICON_NAME = "website"

# Textures decoded from icon data, by icon UUID. An icon's data never changes
# under its UUID -- new data gets a new UUID -- so nothing here goes stale, and
# a safe with a hundred entries on one site decodes that site's icon once.
_textures: dict[UUID, Gdk.Texture] = {}


def custom_icon_texture(icon_uuid: UUID, data: bytes) -> Gdk.Texture:
    texture = _textures.get(icon_uuid)
    if texture is None:
        texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
        _textures[icon_uuid] = texture
    return texture


@Gtk.Template(resource_path="/org/gnome/World/Secrets/gtk/entry_page_icon.ui")
class EntryPageIcon(Gtk.FlowBoxChild):
    __gtype_name__ = "EntryPageIcon"

    image = Gtk.Template.Child()

    def __init__(self, icon_name, icon_number):
        super().__init__()

        self.image.props.icon_name = icon_name
        self.props.tooltip_text = icon_name
        self.set_name(icon_number)


class EntryPageCustomIcon(Gtk.FlowBoxChild):
    """The entry's own icon, offered beside the built-in ones.

    Shown only when the entry has one. Picking any built-in icon instead
    clears it, which is how the user gets rid of an icon they do not want.
    """

    __gtype_name__ = "EntryPageCustomIcon"

    def __init__(self, texture: Gdk.Texture) -> None:
        super().__init__()

        image = Gtk.Image.new_from_paintable(texture)
        image.props.pixel_size = 24
        for side in ("start", "end", "top", "bottom"):
            image.set_property(f"margin-{side}", 6)
        image.props.accessible_role = Gtk.AccessibleRole.PRESENTATION
        self.set_child(image)

        self.props.tooltip_text = _("Website Icon")
        self.set_name(CUSTOM_ICON_NAME)
