# SPDX-License-Identifier: GPL-3.0-only
"""The colours the tray and browser icons can be set to.

Values are the nicks of the icon-color enum in the settings schema, and are also
the directory names the icons are installed under -- data/icons/meson.build for
the tray, icons/toolbar/ in Cipher Bridge for the browser. One string all the
way through, so a colour cannot be renamed in one place and not another.

Declaration order is the order the swatches appear. It has no bearing on what is
stored, since the nick is what gets written; the schema's own numbering is
independent and only matters to GSettings.
"""

from __future__ import annotations

from enum import Enum
from gettext import gettext as _


class IconColor(Enum):
    PINK = "pink"
    BLUE = "blue"
    GREEN = "green"
    YELLOW = "yellow"
    ORANGE = "orange"
    RED = "red"
    PURPLE = "purple"
    BROWN = "brown"
    MONOCHROME = "monochrome"

    def to_translatable(self):  # pylint: disable=too-many-return-statements
        match self:
            case IconColor.PINK:
                return _("Pink")
            case IconColor.BLUE:
                return _("Blue")
            case IconColor.GREEN:
                return _("Green")
            case IconColor.YELLOW:
                return _("Yellow")
            case IconColor.ORANGE:
                return _("Orange")
            case IconColor.RED:
                return _("Red")
            case IconColor.PURPLE:
                return _("Purple")
            case IconColor.BROWN:
                return _("Brown")
            case IconColor.MONOCHROME:
                return _("Monochrome")

    def css_class(self):
        """The swatch style, shared with the entry colour palette.

        Everything but the pink is one of the classes data/style.css already
        defines for entry labels, which is the point: a colour chosen here is
        the same colour as the label of that name.

        Monochrome has a swatch of its own, since it is not one colour to show:
        it is white on a dark panel and black on a light one, and the swatch
        says so by showing both.
        """
        return self.value
