#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
#
# Build, install and launch Cipher from this checkout without touching the
# system. Everything goes into a throwaway prefix; delete it and nothing
# remains.
#
#   tools/dev-run.sh                 # build, install, launch
#   tools/dev-run.sh --rebuild       # discard the build directory first
#
# Uses the default profile deliberately, not development. tools/cipher-proxy
# looks for the socket of the default application ID, so a Devel build would
# listen somewhere the browser never looks, and the extension would fail with
# nothing obviously wrong.

set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="${CIPHER_PREFIX:-/tmp/cipher-run}"
BUILD_DIR="${CIPHER_BUILD:-/tmp/cipher-run-build}"

if [ "${1:-}" = "--rebuild" ]; then
    rm -rf "$BUILD_DIR" "$PREFIX"
    shift  # so it is not forwarded to the application
fi

if [ ! -f "$BUILD_DIR/build.ninja" ]; then
    echo "Configuring into $BUILD_DIR (prefix $PREFIX)"
    meson setup "$BUILD_DIR" "$SOURCE_DIR" --prefix="$PREFIX" >/dev/null
fi

echo "Building and installing"
ninja -C "$BUILD_DIR" install >/dev/null

SCHEMA_DIR="$PREFIX/share/glib-2.0/schemas"
# meson's post-install compiles these, but not when DESTDIR-style installs are
# skipped as unchanged, so make sure the cache exists.
glib-compile-schemas "$SCHEMA_DIR"

# Locate the installed package directory rather than assuming a Python version.
PYTHON_DIR="$(find "$PREFIX/lib" -maxdepth 2 -name 'site-packages' -type d | head -1)"
if [ -z "$PYTHON_DIR" ]; then
    echo "could not find site-packages under $PREFIX/lib" >&2
    exit 1
fi

export GSETTINGS_SCHEMA_DIR="$SCHEMA_DIR"
export XDG_DATA_DIRS="$PREFIX/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
export PYTHONPATH="$PYTHON_DIR"

APP_ID="$(grep '^APP_ID' "$BUILD_DIR/gsecrets/const.py" | cut -d'"' -f2)"

# Browser integration is off by default; this is a test build, so turn it on.
gsettings set "$APP_ID" browser-integration true

SOCKET="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/$APP_ID/BrowserServer"
# A leftover socket needs no handling here: the server probes it on startup and
# removes it only if nothing is listening, refusing to steal a live one.

echo
echo "application : $APP_ID"
echo "prefix      : $PREFIX"
echo "socket      : $SOCKET"
echo "integration : $(gsettings get "$APP_ID" browser-integration)"
echo
echo "Launching. Unlock a safe, then press Connect in Cipher Bridge."
echo

exec "$PREFIX/bin/secrets" --debug "$@"
