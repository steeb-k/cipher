# SPDX-License-Identifier: GPL-3.0-only
"""Starting Cipher when the user logs in.

Implemented as a desktop entry in the XDG autostart directory, which every
desktop environment reads. The entry is generated rather than copied from the
installed one, because it needs an absolute Exec path: a login session does not
necessarily have ~/.local/bin on PATH, and an entry that silently fails to
launch is worse than no entry at all.

Under Flatpak that file cannot work, and not because writing it fails: the
config directory is inside the sandbox, so the entry is written successfully to
somewhere no login session will ever read. There the entry has to be created on
our behalf by org.freedesktop.portal.Background, which is what that portal
exists for. It costs a permission prompt, which is why it is not used for a
normally installed copy that can just write the file.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from gi.repository import Gio, GLib

from gsecrets import config_manager, const

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
BACKGROUND_INTERFACE = "org.freedesktop.portal.Background"
REQUEST_INTERFACE = "org.freedesktop.portal.Request"

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
    """Whether an autostart entry exists.

    Only meaningful when we wrote the entry ourselves. The portal offers no way
    to query what it created, so under Flatpak the setting that drove the
    request is the only record of it, and callers should read that instead.
    """
    if const.IS_FLATPAK:
        return config_manager.get_autostart()

    return _entry_path().is_file()


# Distinguishes concurrent requests from this process, since the portal derives
# the reply's object path from the token and a stale one would collide.
_request_serial = 0


def _set_enabled_portal(enabled: bool) -> None:
    """Ask the background portal to create or remove the entry. Never raises."""
    global _request_serial  # noqa: PLW0603

    try:
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as err:
        logging.warning("No session bus for the background portal: %s", err)
        return

    _request_serial += 1
    token = f"cipher_{os.getpid()}_{_request_serial}"

    # Subscribe before asking, on the path the portal derives from our unique
    # name and the token. Waiting for the returned handle instead would race
    # against a reply that needs no user interaction, which is exactly the case
    # when the permission has already been granted.
    sender = connection.get_unique_name()[1:].replace(".", "_")
    request_path = f"{PORTAL_PATH}/request/{sender}/{token}"

    subscription = 0

    def on_response(
        _connection: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _interface: str,
        _signal: str,
        parameters: GLib.Variant,
    ) -> None:
        connection.signal_unsubscribe(subscription)
        response, results = parameters.unpack()
        if response != 0:
            # 1 is the user cancelling, 2 is anything else. Neither is ours to
            # fix, and the setting simply will not take effect.
            logging.warning(
                "The background portal did not grant autostart (response %s)",
                response,
            )
            return

        logging.debug("Autostart via portal: %s", results.get("autostart", False))

    subscription = connection.signal_subscribe(
        PORTAL_BUS,
        REQUEST_INTERFACE,
        "Response",
        request_path,
        None,
        Gio.DBusSignalFlags.NONE,
        on_response,
    )

    def on_requested(_connection: Gio.DBusConnection, result: Gio.AsyncResult) -> None:
        try:
            connection.call_finish(result)
        except GLib.Error as err:
            connection.signal_unsubscribe(subscription)
            logging.warning("Could not reach the background portal: %s", err.message)

    options = {
        "handle_token": GLib.Variant("s", token),
        "reason": GLib.Variant("s", f"Start {const.NAME} when you log in"),
        "autostart": GLib.Variant("b", enabled),
        # The command the entry runs, interpreted inside the sandbox, so it is
        # the plain binary name rather than the absolute path _executable()
        # resolves for a host entry.
        "commandline": GLib.Variant("as", [BINARY]),
    }

    connection.call(
        PORTAL_BUS,
        PORTAL_PATH,
        BACKGROUND_INTERFACE,
        "RequestBackground",
        GLib.Variant("(sa{sv})", ("", options)),
        GLib.VariantType("(o)"),
        Gio.DBusCallFlags.NONE,
        -1,
        None,
        on_requested,
    )


def set_enabled(enabled: bool) -> None:
    """Create or remove the autostart entry. Never raises."""
    if const.IS_FLATPAK:
        _set_enabled_portal(enabled)
        return

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
