# SPDX-License-Identifier: GPL-3.0-only
"""Cipher, a KeePass client for GNOME.

This file exists to make gsecrets a regular package rather than a namespace
package, and should not be removed.

Without it, Python merges every gsecrets directory it finds on sys.path into
one package. Running this fork from a source checkout on a machine that also
has Secrets or Cipher installed therefore mixes the two: modules present in the
checkout are used, and anything missing from it -- most notably const.py, which
meson only generates at build time -- silently resolves against the installed
copy instead. The result runs, but with another build's application ID, which
sends sockets, GSettings and D-Bus names to the wrong place.

With this file present, the first gsecrets on sys.path wins outright.
"""
