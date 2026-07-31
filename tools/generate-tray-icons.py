#!/usr/bin/python3
# SPDX-License-Identifier: GPL-3.0-only
"""Generate the per-colour tray icons from the default one.

The geometry lives in data/icons/hicolor/scalable/apps/cipher-tray.svg and is
documented there; this only re-paints it. Reading the template rather than
re-declaring the shapes means the mark cannot drift between colours, which is
the failure mode a second copy of the path data would invite.

The colours match tools/generate-icons.py in the Cipher Bridge tree, so a tray
icon set to Blue is the same blue as the browser toolbar icon. They are
duplicated rather than shared because the two are separately packaged, and the
application is the one that owns the choice -- it is what sends the colour to
the extension.

    tools/generate-tray-icons.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ICONS = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "icons"
    / "hicolor"
    / "scalable"
    / "apps"
)
TEMPLATE = ICONS / "cipher-tray.svg"

# The fill in the template, and so the string being replaced. Also the brand
# pink, which is why "pink" below repeats it.
DEFAULT = "#ff67ef"

# Every value except the pink is libadwaita's own named palette (the -4 shades),
# which is what the application already uses for entry labels. Sharing them
# means the icon colours and the label colours agree exactly.
COLOURS = {
    "pink": DEFAULT,
    "blue": "#1c71d8",
    "green": "#2ec27e",
    "yellow": "#f5c211",
    "orange": "#e66100",
    "red": "#c01c28",
    "purple": "#813d9c",
    "brown": "#865e3c",
    # A tray pixmap is sent as pixels and never recoloured by the panel, so
    # monochrome has to commit to one shade rather than adapting like a symbolic
    # icon would. White, because a dark panel is overwhelmingly the common case
    # and the keyhole is cut out of the shape -- on a light panel the mark reads
    # as an outline rather than disappearing.
    "monochrome": "#fcfcfc",
}


def main() -> int:
    template = TEMPLATE.read_text()
    if DEFAULT not in template:
        print(f"{TEMPLATE} no longer contains {DEFAULT}", file=sys.stderr)
        return 1

    for name, colour in COLOURS.items():
        path = ICONS / f"cipher-tray-{name}.svg"
        path.write_text(template.replace(DEFAULT, colour))
        print(f"{path.name}: {colour}")

    print(f"\n{len(COLOURS)} files written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
