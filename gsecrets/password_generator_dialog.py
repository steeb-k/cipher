# SPDX-License-Identifier: GPL-3.0-only
"""A password generator that does not belong to an entry.

The generator in an entry's credentials group is a GtkPopover anchored to a
button, so it only exists while an entry is being edited and cannot be opened on
its own. This dialog wraps the same generator functions so a password can be
produced from the main menu without touching a safe.

Options are read from the saved generator preferences rather than repeated here,
so this and the popover cannot disagree about what was asked for. They are
changed in Preferences.
"""

from __future__ import annotations

from gettext import gettext as _

from gi.repository import Adw, Gdk, Gio, Gtk

import gsecrets.config_manager as config
from gsecrets.passphrase_generator import Passphrase
from gsecrets.password_generator import generate as generate_pwd


@Gtk.Template(
    resource_path="/org/gnome/World/Secrets/gtk/password_generator_dialog.ui",
)
class PasswordGeneratorDialog(Adw.Dialog):
    __gtype_name__ = "PasswordGeneratorDialog"

    _copy_button = Gtk.Template.Child()
    _passphrase_row = Gtk.Template.Child()
    _password_label = Gtk.Template.Child()
    _regenerate_button = Gtk.Template.Child()
    _toast_overlay = Gtk.Template.Child()

    def __init__(self) -> None:
        super().__init__()

        self._password = ""

        self._passphrase_row.connect("notify::active", self._on_mode_changed)
        self._regenerate_button.connect("clicked", self._on_regenerate_clicked)
        self._copy_button.connect("clicked", self._on_copy_clicked)

        self.generate()

    def generate(self) -> None:
        """Produce a new secret using the saved generator preferences."""
        if self._passphrase_row.props.active:
            # Passphrases are generated off the main thread and arrive by
            # signal, so this cannot simply return a value like the other path.
            passphrase = Passphrase()
            passphrase.connect("generated", self._on_passphrase_generated)
            passphrase.generate(
                config.get_generator_words(),
                config.get_generator_separator(),
            )
            return

        self._set_password(
            generate_pwd(
                config.get_generator_length(),
                config.get_generator_use_uppercase(),
                config.get_generator_use_lowercase(),
                config.get_generator_use_numbers(),
                config.get_generator_use_symbols(),
            ),
        )

    def _on_passphrase_generated(self, _generator, passphrase: str) -> None:
        self._set_password(passphrase)

    def _set_password(self, password: str) -> None:
        self._password = password
        self._password_label.props.label = password

    def _on_mode_changed(self, *_args) -> None:
        self.generate()

    def _on_regenerate_clicked(self, _button: Gtk.Button) -> None:
        self.generate()

    def _on_copy_clicked(self, _button: Gtk.Button) -> None:
        if not self._password:
            return

        # Prefer the safe's own clipboard handling when one is open, so a
        # password copied from here is cleared on the same schedule as one
        # copied from an entry. Falling back to the plain clipboard matters
        # because this dialog is reachable while everything is still locked,
        # when there is no safe to ask.
        unlocked_db = getattr(self.get_root(), "unlocked_db", None)
        if unlocked_db is None:
            # get_root() gives the presenting window while the dialog is shown
            # inline, but not when it is floated into a window of its own. Ask
            # the application in that case rather than quietly skipping the
            # clipboard timer.
            app = Gio.Application.get_default()
            window = app.get_active_window() if app else None
            unlocked_db = getattr(window, "unlocked_db", None)

        if unlocked_db is not None:
            unlocked_db.send_to_clipboard(
                self._password,
                _("Password copied"),
                self._toast_overlay,
            )
            return

        if display := Gdk.Display.get_default():
            display.get_clipboard().set(self._password)

        self._toast_overlay.add_toast(Adw.Toast.new(_("Password copied")))
