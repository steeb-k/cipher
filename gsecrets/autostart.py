# SPDX-License-Identifier: GPL-3.0-only
"""Starting Cipher when the user logs in.

Implemented as a desktop entry in the XDG autostart directory, which every
desktop environment reads. There is a portal for this
(org.freedesktop.portal.Background) but it exists to let sandboxed applications
ask permission; for a normally installed one it adds a dependency and a
permission prompt to do exactly what writing this file does.

The entry is generated rather than copied from the installed one, because it
needs an absolute Exec path: a login session does not necessarily have
~/.local/bin on PATH, and an entry that silently fails to launch is worse than
no entry at all.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from gi.repository import GLib

from gsecrets import const

ENTRY_TEMPLATE = """\
[Desktop Entry]
Type=Application
Name={name}
Comment=Start {name} when you log in
Icon={app_id}
Exec={exec_path}
Terminal=false
X-GNOME-Autostart-enabled=true
"""


def _autostart_dir() -> Path:
    return Path(GLib.get_user_config_dir()) / "autostart"


def _entry_path() -> Path:
    return _autostart_dir() / f"{const.APP_ID}.desktop"


BINARY = "cipher"


def _executable() -> str:
    """Absolute path to the installed binary, falling back to its name.

    Deliberately not const.SHORT_NAME: that is the gettext package, still
    "secrets" for translation continuity, and looking it up on PATH finds
    upstream GNOME Secrets at /usr/bin/secrets. An autostart entry pointing at
    a different password manager is a bad way to start a session.
    """
    # PATH here is the one this process was launched with, a better guess than
    # the login session's, but resolve it so the entry depends on neither.
    return shutil.which(BINARY) or BINARY


def is_enabled() -> bool:
    return _entry_path().is_file()


def set_enabled(enabled: bool) -> None:
    """Create or remove the autostart entry. Never raises."""
    path = _entry_path()

    try:
        if not enabled:
            path.unlink(missing_ok=True)
            logging.debug("Autostart disabled")
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            ENTRY_TEMPLATE.format(
                name=const.NAME,
                app_id=const.APP_ID,
                exec_path=_executable(),
            ),
        )
        logging.debug("Autostart enabled: %s", path)
    except OSError as err:
        # A read-only or missing config directory is not worth failing over;
        # the setting simply will not take effect.
        logging.warning("Could not update the autostart entry: %s", err)
