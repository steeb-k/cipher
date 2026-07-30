#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
#
# Rasterise the application icon from data/icons/appIcon.png into the hicolor
# sizes meson installs. Re-run after replacing that file.
#
# The icon is a PNG rather than the SVG upstream ships, so it has to be provided
# at each size: an icon theme picks the closest available raster size, and a
# single large PNG scaled down by the theme loader looks worse than one resized
# properly here.

set -euo pipefail

ICONS="$(cd "$(dirname "$0")/../data/icons" && pwd)"
SOURCE="$ICONS/appIcon.png"

# Sizes an icon theme actually looks for. 512 is the largest worth shipping;
# the source is 810px, so every one of these is a downscale.
SIZES=(16 24 32 48 64 128 256 512)

command -v magick >/dev/null || { echo "ImageMagick (magick) is required" >&2; exit 1; }
[ -f "$SOURCE" ] || { echo "missing $SOURCE" >&2; exit 1; }

echo "Rasterising $(basename "$SOURCE")"
for size in "${SIZES[@]}"; do
    dir="$ICONS/hicolor/${size}x${size}/apps"
    mkdir -p "$dir"
    # -filter Lanczos keeps the keyhole edge crisp at small sizes; the default
    # is softer and the glyph turns to mush by 24px.
    magick "$SOURCE" -filter Lanczos -resize "${size}x${size}" \
        -strip "$dir/cipher.png"
    echo "  ${size}x${size}"
done

echo
echo "Done. data/icons/meson.build installs these renamed to the application ID."
