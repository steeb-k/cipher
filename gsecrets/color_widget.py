# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import typing

from gi.repository import Adw, Gio, GObject, Gtk

import gsecrets.config_manager as config
from gsecrets.const import APP_ID
from gsecrets.icon_color import IconColor
from gsecrets.safe_element import EntryColor

if typing.TYPE_CHECKING:
    from gsecrets.database_manager import DatabaseManager
    from gsecrets.safe_element import SafeEntry
    from gsecrets.unlocked_database import UnlockedDatabase


@Gtk.Template(resource_path="/org/gnome/World/Secrets/gtk/color_button.ui")
class ColorButton(Gtk.FlowBoxChild):
    __gtype_name__ = "ColorButton"

    _button = Gtk.Template.Child()

    def __init__(self, color: EntryColor | IconColor, is_selected: bool):
        """RadioButton to select a color.

        Takes anything offering a value, a css_class and a to_translatable --
        entry label colours and icon colours both qualify, and the swatch is the
        same in either case.

        :param color: color of the button
        :param bool selected: True if the color is selected
        """
        super().__init__()

        self._is_selected = False
        self._color: EntryColor | IconColor = color

        self.props.is_selected = is_selected

        self._button.add_css_class(self._color.css_class())
        self._button.props.tooltip_text = color.to_translatable()

    @property
    def color(self) -> str:
        """Color of the widget."""
        return self._color.value

    @GObject.Property(type=bool, default=False)
    def is_selected(self) -> bool:
        return self._is_selected

    @is_selected.setter  # type: ignore
    def is_selected(self, is_selected: bool) -> None:
        self._is_selected = is_selected
        if is_selected:
            self._button.props.icon_name = "checkmark-symbolic"
        else:
            self._button.props.icon_name = ""

    def connect_clicked(self, callback):
        def cb_wrapper(_button, color_button):
            callback(color_button)

        self._button.connect("clicked", cb_wrapper, self)


@Gtk.Template(resource_path="/org/gnome/World/Secrets/gtk/color_entry_row.ui")
class ColorEntryRow(Adw.PreferencesGroup):
    __gtype_name__ = "ColorEntryRow"

    _flowbox = Gtk.Template.Child()

    def __init__(self, unlocked_database: UnlockedDatabase, safe_entry: SafeEntry):
        """Widget to select the color of an entry.

        :param UnlockedDatabase unlocked_database: unlocked database
        :param SafeEntry safe_entry: the safe entry
        """
        super().__init__()

        self._unlocked_database: UnlockedDatabase = unlocked_database
        self._db_manager: DatabaseManager = unlocked_database.database_manager
        self._safe_entry: SafeEntry = safe_entry

        for color in EntryColor:
            is_selected: bool = safe_entry.props.color == color.value
            color_button: ColorButton = ColorButton(color, is_selected)
            self._flowbox.append(color_button)
            color_button.connect_clicked(self._on_color_clicked)

    def _on_color_clicked(
        self,
        selected_child: ColorButton,
    ) -> None:
        if selected_child.props.is_selected:
            return

        for child in self._flowbox:  # pylint: disable=not-an-iterable
            selected: bool = child == selected_child
            child.props.is_selected = selected

        self._safe_entry.props.color = selected_child.color


@Gtk.Template(resource_path="/org/gnome/World/Secrets/gtk/icon_color_row.ui")
class IconColorRow(Adw.PreferencesGroup):
    """Picks the colour of the tray and browser extension icons.

    The same swatches as the entry colour palette, because they are the same
    colours: choosing Blue here gives an icon in exactly the blue a Blue label
    is drawn in. A combo row would have listed the names without showing any of
    them, which is a poor way to choose a colour.
    """

    __gtype_name__ = "IconColorRow"

    _flowbox = Gtk.Template.Child()

    def __init__(self):
        super().__init__()

        # No arguments, so this can be instantiated straight from the settings
        # dialog's own template rather than being built and inserted by hand.
        current = config.get_icon_color()

        for color in IconColor:
            button = ColorButton(color, color.value == current)
            self._flowbox.append(button)
            button.connect_clicked(self._on_color_clicked)

        # Follow the setting as well as driving it, so a second window's
        # settings dialog cannot sit there showing a colour that is no longer
        # set. Writing the value already stored is a no-op, so the two
        # directions settle instead of driving each other.
        self._settings = Gio.Settings.new(APP_ID)
        self._settings.connect(
            f"changed::{config.ICON_COLOR}",
            self._on_setting_changed,
        )

    def _select(self, color: str) -> None:
        for child in self._flowbox:  # pylint: disable=not-an-iterable
            child.props.is_selected = child.color == color

    def _on_setting_changed(self, _settings: Gio.Settings, _key: str) -> None:
        self._select(config.get_icon_color())

    def _on_color_clicked(self, selected_child: ColorButton) -> None:
        if selected_child.props.is_selected:
            return

        self._select(selected_child.color)
        config.set_icon_color(selected_child.color)
