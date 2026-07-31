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
from gettext import gettext as _

from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk

from gsecrets import config_manager, const
from gsecrets.safe_watcher import SafeWatcher

if typing.TYPE_CHECKING:
    from gi.repository import Adw

WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
WATCHER_INTERFACE = "org.kde.StatusNotifierWatcher"

ITEM_PATH = "/StatusNotifierItem"
ITEM_INTERFACE = "org.kde.StatusNotifierItem"

MENU_PATH = "/MenuBar"
MENU_INTERFACE = "com.canonical.dbusmenu"

# Item ids used in the exported menu. 0 is reserved for the root by the spec.
MENU_ID_SHOW = 1
MENU_ID_SEPARATOR = 2
MENU_ID_QUIT = 3

# Size to hand over as pixels. Hosts scale as needed; 64 is enough for any
# panel without making the message large.
PIXMAP_SIZE = 64

# The flat mark rather than the application icon. appIcon.png has a soft
# gradient background that blurs into a wash at panel size, so the tray gets the
# same simplified artwork the browser extension uses. Installed by
# data/icons/meson.build under this name, once plain and once per colour.
TRAY_ICON_BASE = f"{const.APP_ID}-tray"


# The variant a locked safe gets, whatever accent is configured. Its own icon
# rather than one of the selectable colours: the obvious candidate, monochrome,
# is white, which would have made "locked" white instead of grey for every
# accent -- and indistinguishable from the icon itself for anyone who had chosen
# Monochrome. Painted in the same dull grey the browser extension uses.
LOCKED_COLOR = "locked"


def tray_icon_color(unlocked: bool) -> str:
    """The palette the tray icon takes for this lock state.

    Grey once everything is locked, so the accent colour carries a meaning
    rather than being decoration: it says there is a safe open right now. That
    is the same division the browser extension draws with its own locked icon.

    A safe left open is the state worth noticing, which is why it is the one
    that keeps the colour. The locked state has an icon of its own rather than
    borrowing a palette entry, so it stays distinct even when the chosen accent
    is Monochrome -- which it did not, when this borrowed that palette: both
    states came out the same white, and the icon never appeared to change.

    Split from tray_icon_name() so the choice can be checked without a display
    and an installed icon theme, which is what the name resolution needs.
    """
    return config_manager.get_icon_color() if unlocked else LOCKED_COLOR


def tray_icon_name(unlocked: bool) -> str:
    """The icon name for the chosen colour and the current lock state.

    Falls back to the unsuffixed icon when the colour names a set this build
    does not ship, which is what happens when the settings schema is newer than
    the installed icons -- an upgrade that replaced one but not the other. An
    icon in the wrong colour beats no icon at all.
    """
    name = f"{TRAY_ICON_BASE}-{tray_icon_color(unlocked)}"

    display = Gdk.Display.get_default()
    if display is None:
        return name

    theme = Gtk.IconTheme.get_for_display(display)
    if not theme.has_icon(name):
        logging.debug("No tray icon named %s; using %s", name, TRAY_ICON_BASE)
        return TRAY_ICON_BASE

    return name

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

# The subset of com.canonical.dbusmenu a host needs to render a static menu.
# The spec is much larger, but the rest describes features this menu does not
# use: no icons, no submenus, no items that change while shown.
MENU_XML = f"""
<node>
  <interface name="{MENU_INTERFACE}">
    <property name="Version" type="u" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg type="i" name="parentId" direction="in"/>
      <arg type="i" name="recursionDepth" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="u" name="revision" direction="out"/>
      <arg type="(ia{{sv}}av)" name="layout" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="as" name="propertyNames" direction="in"/>
      <arg type="a(ia{{sv}})" name="properties" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="name" direction="in"/>
      <arg type="v" name="value" direction="out"/>
    </method>
    <method name="Event">
      <arg type="i" name="id" direction="in"/>
      <arg type="s" name="eventId" direction="in"/>
      <arg type="v" name="data" direction="in"/>
      <arg type="u" name="timestamp" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg type="a(isvu)" name="events" direction="in"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg type="i" name="id" direction="in"/>
      <arg type="b" name="needUpdate" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg type="ai" name="ids" direction="in"/>
      <arg type="ai" name="updatesNeeded" direction="out"/>
      <arg type="ai" name="idErrors" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg type="a(ia{{sv}})" name="updatedProps" direction="out"/>
      <arg type="a(ias)" name="removedProps" direction="out"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg type="u" name="revision" direction="out"/>
      <arg type="i" name="parent" direction="out"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg type="i" name="id" direction="out"/>
      <arg type="u" name="timestamp" direction="out"/>
    </signal>
  </interface>
</node>
"""


def _load_pixmap(name: str) -> list[tuple[int, int, bytes]]:
    """Load the given icon as ARGB32 pixel data for the tray protocol.

    Returns an empty list if the icon cannot be found, which is not fatal: the
    icon name is still advertised, so a host with the theme installed shows the
    right thing anyway.
    """
    display = Gdk.Display.get_default()
    if display is None:
        return []

    theme = Gtk.IconTheme.get_for_display(display)
    paintable = theme.lookup_icon(
        name, None, PIXMAP_SIZE, 1, Gtk.TextDirection.NONE, 0
    )
    gfile = paintable.get_file() if paintable else None
    if gfile is None or gfile.get_path() is None:
        logging.debug("No icon file for %s; sending no pixmap", name)
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
        self._menu_registration_id = 0
        self._owner_id = 0
        self._pixmap: list[tuple[int, int, bytes]] | None = None
        self._settings = Gio.Settings.new(const.APP_ID)
        self._color_handler = 0

        # The icon is grey while everything is locked, so it has to be repainted
        # when that changes and not only when the colour setting does.
        self._watcher = SafeWatcher(application, self._on_lock_state_changed)

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

        # A failure here is not fatal: without the menu the item still works,
        # it just falls back to presenting the window on right-click.
        try:
            menu_node = Gio.DBusNodeInfo.new_for_xml(MENU_XML)
            self._menu_registration_id = connection.register_object(
                MENU_PATH,
                menu_node.interfaces[0],
                self._on_menu_method_call,
                self._on_menu_get_property,
                None,
            )
        except GLib.Error as err:
            logging.warning("Could not export the tray menu: %s", err)

        self._owner_id = Gio.bus_own_name_on_connection(
            connection,
            self._bus_name,
            Gio.BusNameOwnerFlags.NONE,
            None,
            None,
        )

        self._color_handler = self._settings.connect(
            f"changed::{config_manager.ICON_COLOR}",
            self._on_icon_color_changed,
        )

        self._watcher.start()

        self._register_with_watcher()
        logging.info("Tray icon published as %s", self._bus_name)
        return True

    def _on_icon_color_changed(self, _settings: Gio.Settings, _key: str) -> None:
        self._repaint()

    def _on_lock_state_changed(self, _unlocked: bool) -> None:
        """Repaint when the last safe locks, or the first one opens."""
        self._repaint()

    def _repaint(self) -> None:
        """Drop the cached pixels and tell the host to read the icon again.

        The pixels are cached, so they have to be dropped before anything is
        announced; a host that reads IconPixmap in response to NewIcon would
        otherwise be handed the previous icon and cache it as the new one.
        """
        self._pixmap = None

        if self._connection is None:
            return

        try:
            self._connection.emit_signal(
                None, ITEM_PATH, ITEM_INTERFACE, "NewIcon", None
            )
        except GLib.Error as err:
            logging.debug("Could not announce the new tray icon: %s", err)

    def stop(self) -> None:
        """Withdraw the item. Safe to call when it was never started."""
        self._watcher.stop()

        if self._color_handler:
            self._settings.disconnect(self._color_handler)
            self._color_handler = 0

        if self._connection is None:
            return

        if self._registration_id:
            self._connection.unregister_object(self._registration_id)
            self._registration_id = 0

        if self._menu_registration_id:
            self._connection.unregister_object(self._menu_registration_id)
            self._menu_registration_id = 0

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
            self._pixmap = _load_pixmap(tray_icon_name(self._watcher.unlocked))

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
            # The item's identity, not an icon: hosts key their own per-item
            # state off this, so it stays the application ID.
            return GLib.Variant("s", const.APP_ID)
        if name == "Title":
            return GLib.Variant("s", const.NAME)
        if name == "Status":
            return GLib.Variant("s", "Active")
        if name == "IconName":
            return GLib.Variant("s", tray_icon_name(self._watcher.unlocked))
        if name == "IconPixmap":
            return self._icon_pixmap()
        if name in ("OverlayIconName", "AttentionIconName"):
            return GLib.Variant("s", "")
        if name == "ToolTip":
            return GLib.Variant(
                # First field is an icon name, so it follows the tray icon too.
                "(sa(iiay)ss)",
                (tray_icon_name(self._watcher.unlocked), [], const.NAME, ""),
            )
        if name == "ItemIsMenu":
            # False even though a menu is exported: the item is not menu-only,
            # so a left click still has to arrive as Activate and present the
            # window rather than opening the menu.
            return GLib.Variant("b", False)
        if name == "Menu":
            return GLib.Variant("o", MENU_PATH)

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

    # -- exported menu ---------------------------------------------------

    def _menu_item(self, item_id: int, label: str = "", separator: bool = False):
        props: dict[str, GLib.Variant] = {}
        if separator:
            props["type"] = GLib.Variant("s", "separator")
        else:
            props["label"] = GLib.Variant("s", label)
            props["enabled"] = GLib.Variant("b", True)
            props["visible"] = GLib.Variant("b", True)

        return GLib.Variant("(ia{sv}av)", (item_id, props, []))

    def _menu_layout(self) -> tuple:
        """The root layout, as a plain tuple.

        Not a GLib.Variant: this is nested inside GetLayout's return signature,
        and PyGObject builds nested structs from raw Python values. Handing it
        an already-built variant fails at construction time. Only the `av`
        children and the `sv` property values have to be variants, because
        those slots are variant-typed in the signature itself.
        """
        children = [
            self._menu_item(MENU_ID_SHOW, _("Show Window")),
            self._menu_item(MENU_ID_SEPARATOR, separator=True),
            self._menu_item(MENU_ID_QUIT, _("Quit")),
        ]
        root = {"children-display": GLib.Variant("s", "submenu")}
        return (0, root, children)

    def _menu_item_properties(self, item_id: int) -> dict[str, GLib.Variant]:
        for child in (MENU_ID_SHOW, MENU_ID_SEPARATOR, MENU_ID_QUIT):
            if child != item_id:
                continue
            if child == MENU_ID_SEPARATOR:
                return {"type": GLib.Variant("s", "separator")}
            label = _("Show Window") if child == MENU_ID_SHOW else _("Quit")
            return {
                "label": GLib.Variant("s", label),
                "enabled": GLib.Variant("b", True),
                "visible": GLib.Variant("b", True),
            }
        return {}

    def _activate_menu_item(self, item_id: int) -> None:
        if item_id == MENU_ID_SHOW:
            self._present()
        elif item_id == MENU_ID_QUIT:
            # Goes through the application action rather than Gio.Application
            # .quit(), so it takes the same path as the menu item and the
            # accelerator -- which is what marks the quit as deliberate and
            # stops the window hiding itself instead.
            self._application.activate_action("quit", None)

    def _on_menu_get_property(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _interface: str,
        name: str,
    ) -> GLib.Variant | None:
        if name == "Version":
            return GLib.Variant("u", 3)
        if name == "Status":
            return GLib.Variant("s", "normal")
        if name == "TextDirection":
            direction = Gtk.Widget.get_default_direction()
            ltr = direction != Gtk.TextDirection.RTL
            return GLib.Variant("s", "ltr" if ltr else "rtl")
        if name == "IconThemePath":
            return GLib.Variant("as", [])

        return None

    def _on_menu_method_call(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _path: str,
        _interface: str,
        method: str,
        params: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        if method == "GetLayout":
            invocation.return_value(
                GLib.Variant("(u(ia{sv}av))", (1, self._menu_layout()))
            )
            return

        if method == "GetGroupProperties":
            ids = params[0]
            wanted = ids or [MENU_ID_SHOW, MENU_ID_SEPARATOR, MENU_ID_QUIT]
            entries = [(i, self._menu_item_properties(i)) for i in wanted]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (entries,)))
            return

        if method == "GetProperty":
            item_id, name = params[0], params[1]
            value = self._menu_item_properties(item_id).get(name)
            invocation.return_value(
                GLib.Variant("(v)", (value or GLib.Variant("s", ""),))
            )
            return

        if method == "Event":
            item_id, event_id = params[0], params[1]
            if event_id == "clicked":
                self._activate_menu_item(item_id)
            invocation.return_value(None)
            return

        if method == "EventGroup":
            for event in params[0]:
                if event[1] == "clicked":
                    self._activate_menu_item(event[0])
            invocation.return_value(GLib.Variant("(ai)", ([],)))
            return

        if method == "AboutToShow":
            # The menu never changes, so it never needs rebuilding first.
            invocation.return_value(GLib.Variant("(b)", (False,)))
            return

        if method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
            return

        invocation.return_error_literal(
            Gio.DBusError.quark(),
            Gio.DBusError.UNKNOWN_METHOD,
            f"Unknown method {method}",
        )

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
            # ContextMenu still presents the window. A host that renders the
            # exported menu never calls it -- it reads the Menu property and
            # draws the menu itself -- so the only callers left are hosts with
            # no dbusmenu support, for which doing something beats doing
            # nothing.
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
