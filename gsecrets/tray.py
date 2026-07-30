# SPDX-License-Identifier: GPL-3.0-only
"""A StatusNotifierItem tray icon.

Implemented directly on Gio.DBusConnection rather than through
libayatana-appindicator, which is GTK3 and cannot be loaded into a GTK4 process
alongside GTK4 itself.

Only useful while running in the background: the icon exists so a hidden window
can be brought back, so it follows the run-in-background setting rather than
having one of its own.

The item is advertised both by icon name and as raw pixels. A host resolves the
name through the icon theme, which fails for a build installed somewhere
non-standard -- a development prefix, for instance -- and the result is an item
that is registered but invisible. Sending the pixels too means it shows up
regardless.
"""

from __future__ import annotations

import logging
import os
import typing

from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk

from gsecrets import const

if typing.TYPE_CHECKING:
    from gi.repository import Adw

WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
WATCHER_INTERFACE = "org.kde.StatusNotifierWatcher"

ITEM_PATH = "/StatusNotifierItem"
ITEM_INTERFACE = "org.kde.StatusNotifierItem"

# Size to hand over as pixels. Hosts scale as needed; 64 is enough for any
# panel without making the message large.
PIXMAP_SIZE = 64

INTERFACE_XML = f"""
<node>
  <interface name="{ITEM_INTERFACE}">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="s" direction="in"/>
    </method>
    <signal name="NewIcon"/>
    <signal name="NewStatus">
      <arg name="status" type="s"/>
    </signal>
    <signal name="NewToolTip"/>
  </interface>
</node>
"""


def _load_pixmap() -> list[tuple[int, int, bytes]]:
    """Load the application icon as ARGB32 pixel data for the tray protocol.

    Returns an empty list if the icon cannot be found, which is not fatal: the
    icon name is still advertised, so a host with the theme installed shows the
    right thing anyway.
    """
    display = Gdk.Display.get_default()
    if display is None:
        return []

    theme = Gtk.IconTheme.get_for_display(display)
    paintable = theme.lookup_icon(
        const.APP_ID, None, PIXMAP_SIZE, 1, Gtk.TextDirection.NONE, 0
    )
    gfile = paintable.get_file() if paintable else None
    if gfile is None or gfile.get_path() is None:
        logging.debug("No icon file for %s; sending no pixmap", const.APP_ID)
        return []

    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(
            gfile.get_path(), PIXMAP_SIZE, PIXMAP_SIZE
        )
    except GLib.Error as err:
        logging.debug("Could not load tray icon pixels: %s", err)
        return []

    if not pixbuf.get_has_alpha():
        pixbuf = pixbuf.add_alpha(False, 0, 0, 0)

    width, height = pixbuf.get_width(), pixbuf.get_height()
    stride, source = pixbuf.get_rowstride(), pixbuf.get_pixels()

    # The protocol wants ARGB32 in network byte order. GdkPixbuf gives RGBA
    # rows that may be padded, so both the channel order and the padding have
    # to be dealt with rather than passing the buffer straight through.
    argb = bytearray(width * height * 4)
    out = 0
    for y in range(height):
        row = y * stride
        for x in range(width):
            r, g, b, a = source[row + x * 4 : row + x * 4 + 4]
            argb[out] = a
            argb[out + 1] = r
            argb[out + 2] = g
            argb[out + 3] = b
            out += 4

    return [(width, height, bytes(argb))]


class TrayIcon:
    """Publishes a StatusNotifierItem for the application."""

    def __init__(self, application: Gio.Application) -> None:
        self._application = application
        self._connection: Gio.DBusConnection | None = None
        self._registration_id = 0
        self._owner_id = 0
        self._pixmap: list[tuple[int, int, bytes]] | None = None

        # The documented naming convention. Hosts also accept a unique name, but
        # a well-known one is what every other implementation registers.
        self._bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"

    @property
    def active(self) -> bool:
        return self._registration_id != 0

    # -- lifecycle -------------------------------------------------------

    def start(self) -> bool:
        """Publish the item and register it with the watcher."""
        if self.active:
            return True

        try:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as err:
            logging.warning("No session bus for the tray icon: %s", err)
            return False

        self._connection = connection
        node = Gio.DBusNodeInfo.new_for_xml(INTERFACE_XML)

        try:
            self._registration_id = connection.register_object(
                ITEM_PATH,
                node.interfaces[0],
                self._on_method_call,
                self._on_get_property,
                None,
            )
        except GLib.Error as err:
            logging.warning("Could not export the tray icon: %s", err)
            self._connection = None
            return False

        self._owner_id = Gio.bus_own_name_on_connection(
            connection,
            self._bus_name,
            Gio.BusNameOwnerFlags.NONE,
            None,
            None,
        )

        self._register_with_watcher()
        logging.info("Tray icon published as %s", self._bus_name)
        return True

    def stop(self) -> None:
        """Withdraw the item. Safe to call when it was never started."""
        if self._connection is None:
            return

        if self._registration_id:
            self._connection.unregister_object(self._registration_id)
            self._registration_id = 0

        if self._owner_id:
            Gio.bus_unown_name(self._owner_id)
            self._owner_id = 0

        self._connection = None
        logging.info("Tray icon withdrawn")

    def _register_with_watcher(self) -> None:
        """Tell the watcher about the item, if one is running.

        Failure is not an error worth surfacing: on a desktop with no tray at
        all there is simply nothing to register with, and the application must
        carry on regardless.
        """
        if self._connection is None:
            return

        def on_registered(connection: Gio.DBusConnection, result: Gio.AsyncResult) -> None:
            try:
                connection.call_finish(result)
            except GLib.Error as err:
                logging.info("No status notifier host available: %s", err.message)
            else:
                logging.debug("Registered with %s", WATCHER_NAME)

        self._connection.call(
            WATCHER_NAME,
            WATCHER_PATH,
            WATCHER_INTERFACE,
            "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (self._bus_name,)),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            on_registered,
        )

    # -- properties ------------------------------------------------------

    def _icon_pixmap(self) -> GLib.Variant:
        if self._pixmap is None:
            self._pixmap = _load_pixmap()

        return GLib.Variant("a(iiay)", self._pixmap)

    def _on_get_property(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _interface: str,
        name: str,
    ) -> GLib.Variant | None:
        if name == "Category":
            return GLib.Variant("s", "ApplicationStatus")
        if name == "Id":
            return GLib.Variant("s", const.APP_ID)
        if name == "Title":
            return GLib.Variant("s", const.NAME)
        if name == "Status":
            return GLib.Variant("s", "Active")
        if name == "IconName":
            return GLib.Variant("s", const.APP_ID)
        if name == "IconPixmap":
            return self._icon_pixmap()
        if name in ("OverlayIconName", "AttentionIconName"):
            return GLib.Variant("s", "")
        if name == "ToolTip":
            return GLib.Variant(
                "(sa(iiay)ss)", (const.APP_ID, [], const.NAME, "")
            )
        if name == "ItemIsMenu":
            # No menu is exported, so a click must be delivered as Activate
            # rather than being swallowed as a menu request.
            return GLib.Variant("b", False)
        if name == "Menu":
            # Required to be a valid object path even with no menu.
            return GLib.Variant("o", "/NO_DBUSMENU")

        return None

    # -- methods ---------------------------------------------------------

    def _present(self) -> None:
        window = self._application.get_active_window() or next(
            iter(self._application.get_windows()), None
        )
        if window is None:
            self._application.activate()
            return

        window.present()

    def _on_method_call(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _interface: str,
        method: str,
        _params: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        if method in ("Activate", "SecondaryActivate", "ContextMenu"):
            # ContextMenu is treated as Activate for now: a real menu means
            # implementing com.canonical.dbusmenu, and presenting the window is
            # more useful than doing nothing.
            self._present()
            invocation.return_value(None)
            return

        if method == "Scroll":
            invocation.return_value(None)
            return

        invocation.return_error_literal(
            Gio.DBusError.quark(),
            Gio.DBusError.UNKNOWN_METHOD,
            f"Unknown method {method}",
        )
