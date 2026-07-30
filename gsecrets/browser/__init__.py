# SPDX-License-Identifier: GPL-3.0-only
"""Browser integration for Cipher.

Implements the server half of the keepassxc-browser wire protocol so that a
browser extension can request credentials from the running application.

The transport is a Unix domain socket whose path is derived from the
application ID (see `server.socket_path`), which keeps parallel installs --
notably a Devel build running alongside a stable one -- from colliding.
"""
