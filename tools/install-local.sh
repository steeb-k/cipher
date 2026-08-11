#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
#
# Install Cipher into ~/.local for everyday use, from this checkout.
#
#   tools/install-local.sh              # build and install
#   tools/install-local.sh --rebuild    # discard the build directory first
#   tools/install-local.sh --uninstall  # remove what was installed
#
# ~/.local rather than a throwaway prefix like tools/dev-run.sh uses, because
# several things only work from a real user prefix:
#
#   * ~/.local/share is XDG_DATA_HOME, which icon themes and GSettings always
#     search. The tray icon resolves by name from here, which it cannot do from
#     /tmp no matter what XDG_DATA_DIRS the launcher sets -- the panel is a
#     separate process and never sees that variable.
#   * ~/.local/lib/pythonX.Y/site-packages is Python's per-user site directory,
#     so the package is importable with no PYTHONPATH.
#   * The desktop entry is picked up, so Cipher appears in the app grid and can
#     own .kdbx files.
#
# This does not touch anything outside ~/.local, so the distribution's own
# secrets package is left alone.

set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="${CIPHER_PREFIX:-$HOME/.local}"
BUILD_DIR="${CIPHER_BUILD:-$SOURCE_DIR/_build-local}"

DATA_DIR="$PREFIX/share"
SCHEMA_DIR="$DATA_DIR/glib-2.0/schemas"

app_id() {
    grep '^APP_ID' "$BUILD_DIR/gsecrets/const.py" | cut -d'"' -f2
}

# The application icon used to be installed as a PNG at eight sizes, and is a
# single scalable SVG now. ninja only knows how to uninstall what the build it
# was configured from installs, so an install made before that change leaves
# those PNGs behind -- and a leftover raster wins the icon theme's lookup at
# exactly the size it sits in, which shows up as the old artwork appearing at
# some sizes and the new one at others. Clear them out by name.
remove_stale_raster_icons() {
    local id="$1" dir removed=0

    for dir in "$DATA_DIR"/icons/hicolor/*x*/apps; do
        [ -f "$dir/$id.png" ] || continue
        rm -f "$dir/$id.png"
        rmdir -p --ignore-fail-on-non-empty "$dir" 2>/dev/null || true
        removed=1
    done

    [ "$removed" = 0 ] || echo "Removed raster icons left by an earlier install"
}

if [ "${1:-}" = "--uninstall" ]; then
    if [ ! -f "$BUILD_DIR/build.ninja" ]; then
        echo "no build directory at $BUILD_DIR; nothing to uninstall" >&2
        exit 1
    fi
    echo "Uninstalling from $PREFIX"
    ninja -C "$BUILD_DIR" uninstall >/dev/null
    remove_stale_raster_icons "$(app_id)"
    glib-compile-schemas "$SCHEMA_DIR" 2>/dev/null || true
    echo "Done."
    exit 0
fi

if [ "${1:-}" = "--rebuild" ]; then
    rm -rf "$BUILD_DIR"
fi

for tool in meson ninja glib-compile-schemas; do
    command -v "$tool" >/dev/null || { echo "$tool is required" >&2; exit 1; }
done

if [ ! -f "$BUILD_DIR/build.ninja" ]; then
    echo "Configuring into $BUILD_DIR (prefix $PREFIX)"
    meson setup "$BUILD_DIR" "$SOURCE_DIR" --prefix="$PREFIX" >/dev/null
fi

echo "Building and installing"
ninja -C "$BUILD_DIR" install >/dev/null

APP_ID="$(app_id)"

# meson's post-install steps are skipped when it installs into a prefix it
# considers already current, so run the ones that matter directly. All three
# are cheap and idempotent.
glib-compile-schemas "$SCHEMA_DIR"

remove_stale_raster_icons "$APP_ID"

if command -v gtk4-update-icon-cache >/dev/null; then
    gtk4-update-icon-cache -qtf "$DATA_DIR/icons/hicolor" 2>/dev/null || true
fi

if command -v update-desktop-database >/dev/null; then
    update-desktop-database -q "$DATA_DIR/applications" 2>/dev/null || true
fi

echo
BINARY="$(grep '^binary_name' "$SOURCE_DIR/meson.build" | cut -d"'" -f2)"

echo "Installed $APP_ID into $PREFIX"
echo "  binary   $PREFIX/bin/$BINARY"
echo "  desktop  $DATA_DIR/applications/$APP_ID.desktop"
echo "  icons    $DATA_DIR/icons/hicolor/*/apps/$APP_ID*.svg"

if ! printf '%s' ":$PATH:" | grep -q ":$PREFIX/bin:"; then
    echo
    echo "NOTE: $PREFIX/bin is not on PATH. Add it, or run the binary by full path."
fi

echo
echo "Browser integration is off by default. Turn it on with:"
echo "  gsettings set $APP_ID browser-integration true"
