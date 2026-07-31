# SPDX-License-Identifier: GPL-3.0-only
from __future__ import annotations

import asyncio
import logging
from gettext import gettext as _

from gi.events import GLibEventLoopPolicy
from gi.repository import Adw, Gio, GLib, Gtk, GtkSource

from gsecrets import autostart, config_manager, const
from gsecrets.browser.backend import ApplicationBackend
from gsecrets.browser.server import BrowserServer
from gsecrets.password_generator_dialog import PasswordGeneratorDialog
from gsecrets.recent_manager import RecentManager
from gsecrets.tray import TrayIcon
from gsecrets.widgets.mod import load_widgets
from gsecrets.widgets.window import Window


class Application(Adw.Application):
    development_mode = const.IS_DEVEL
    application_id = const.APP_ID
    settings = Gio.Settings.new(application_id)

    def __init__(self, *args, **_kwargs):
        super().__init__(
            *args,
            application_id=self.application_id,
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
            resource_base_path="/org/gnome/World/Secrets",
            register_session=True,
        )

        asyncio.set_event_loop_policy(GLibEventLoopPolicy())

        self._browser_server: BrowserServer | None = None
        # Held alongside the server only so its listeners can be taken off when
        # the server goes away; every other use of it is the server's.
        self._browser_backend: ApplicationBackend | None = None
        self._tray_icon: TrayIcon | None = None

        # debug level logging option
        self.add_main_option(
            "debug",
            ord("d"),
            GLib.OptionFlags.NONE,
            GLib.OptionArg.NONE,
            _("Enable debug logging"),
            None,
        )

    def do_startup(self):  # pylint: disable=arguments-differ
        Adw.Application.do_startup(self)

        GtkSource.init()

        Gtk.Window.set_default_icon_name(const.APP_ID)

        self.setup_actions()
        self.add_global_accelerators()

        load_widgets()

        recents = RecentManager()
        self.create_asyncio_task(recents.clean_non_existent())

        self.settings.connect(
            f"changed::{config_manager.BROWSER_INTEGRATION}",
            self._on_browser_integration_changed,
        )
        if config_manager.get_browser_integration():
            self.create_asyncio_task(self._start_browser_server())

        # The tray icon follows run-in-background rather than having a setting
        # of its own: its whole purpose is to bring back a hidden window, so it
        # is meaningless when closing the window quits.
        self.settings.connect(
            f"changed::{config_manager.RUN_IN_BACKGROUND}",
            self._on_run_in_background_changed,
        )
        self._update_tray_icon()

        # The extension is told the colour during the handshake, so a browser
        # that connects later needs nothing here; this is only for the one
        # already connected when the setting changes.
        self.settings.connect(
            f"changed::{config_manager.ICON_COLOR}",
            self._on_icon_color_changed,
        )

        self.settings.connect(
            f"changed::{config_manager.AUTOSTART}",
            self._on_autostart_changed,
        )
        # Reconcile on every start: the desktop entry is a file the user can
        # delete from their own autostart settings, and the setting would then
        # disagree with reality until it was toggled.
        autostart.set_enabled(config_manager.get_autostart())

    def _on_icon_color_changed(self, _settings: Gio.Settings, _key: str) -> None:
        if server := self._browser_server:
            self.create_asyncio_task(
                server.broadcast("icon-color", value=config_manager.get_icon_color())
            )

    def _on_autostart_changed(self, _settings: Gio.Settings, _key: str) -> None:
        autostart.set_enabled(config_manager.get_autostart())

    def _on_run_in_background_changed(
        self, _settings: Gio.Settings, _key: str
    ) -> None:
        self._update_tray_icon()

    def _update_tray_icon(self) -> None:
        wanted = config_manager.get_run_in_background()
        if wanted and self._tray_icon is None:
            tray = TrayIcon(self)
            if tray.start():
                self._tray_icon = tray
        elif not wanted and (tray := self._tray_icon):
            self._tray_icon = None
            tray.stop()

    def do_shutdown(self):  # pylint: disable=arguments-differ
        if tray := self._tray_icon:
            self._tray_icon = None
            tray.stop()

        if server := self._browser_server:
            # Synchronous by necessity: an asyncio task scheduled here never
            # runs, because the event loop is already being torn down, and the
            # socket would survive the process.
            self._browser_server = None
            self._stop_watching_databases()
            server.close()

        Adw.Application.do_shutdown(self)

    def _on_browser_integration_changed(
        self, _settings: Gio.Settings, _key: str
    ) -> None:
        """Start or stop the browser server when the setting is toggled."""
        enabled = config_manager.get_browser_integration()
        if enabled and self._browser_server is None:
            self.create_asyncio_task(self._start_browser_server())
        elif not enabled and (server := self._browser_server):
            self._browser_server = None
            self._stop_watching_databases()
            self.create_asyncio_task(server.stop())

    def _stop_watching_databases(self) -> None:
        if backend := self._browser_backend:
            self._browser_backend = None
            backend.stop_watching()

    async def _start_browser_server(self) -> None:
        if self._browser_server is not None:
            return

        backend = ApplicationBackend(self)
        server = BrowserServer(backend)

        # Push lock-state changes to connected browsers. Without this, locking
        # from Cipher's own UI goes unnoticed until the extension's next request
        # fails. Scheduled as a task because the notification arrives from a
        # GObject property change, which cannot await.
        backend.on_state_change = lambda action: self.create_asyncio_task(
            server.broadcast(action)
        )

        try:
            await server.start()
        except (OSError, RuntimeError) as err:
            # Most likely another instance already holds the socket. Log and
            # carry on: failing to serve a browser must not stop the safe from
            # being usable.
            logging.warning("Could not start browser integration: %s", err)
            backend.stop_watching()
            return

        self._browser_server = server
        self._browser_backend = backend

    def do_open(self, gfile_list, _n_files, _hint):  # pylint: disable=arguments-differ
        for gfile in gfile_list:
            if not gfile.query_exists():
                logging.error("Error: File %s does not exist", gfile.get_path())
                if self.get_windows() is None:
                    self.quit()

            if self.is_safe_open(gfile.get_path()):
                logging.error("Error: Safe %s is already open", gfile.get_path())
            else:
                window = self.new_window()
                window.present()
                window.start_database_opening_routine(gfile.get_path())

    def new_window(self) -> Window:
        """Create a new window.

        The new window will be placed in its own group. This is done
        so that modal windows don't make other windows insensitive.
        """
        window_group = Gtk.WindowGroup()
        window = Window(application=self)

        window_group.add_window(window)
        return window

    def is_safe_open(self, filepath: str) -> bool:
        for window in self.get_windows():
            database = window.unlocked_db
            if not database:
                continue

            if database.database_manager.path == filepath:
                return True

        return False

    def do_handle_local_options(  # pylint: disable=arguments-differ
        self,
        options: GLib.VariantDict,
    ) -> int:
        """Handle cli arguments.

        :returns int: If you have handled your options and want to exit
        the process, return a non-negative option, 0 for success, and
        a positive value for failure. To continue, return -1 to let
        the default option processing continue.
        """
        #  convert GVariantDict -> GVariant -> dict
        options = options.end().unpack()

        # set up logging depending on the verbosity level
        loglevel = logging.INFO
        if self.development_mode or "debug" in options:
            loglevel = logging.DEBUG
            # Don't clutter our log output with debug msg of the
            # pykeepass module it is very noisy.
            pykeepass_logger = logging.getLogger("pykeepass")
            pykeepass_logger.setLevel(logging.INFO)

        logging.basicConfig(
            format="%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%d-%m-%y %H:%M:%S",
            level=loglevel,
        )

        return -1

    def do_activate(self):  # pylint: disable=arguments-differ
        Adw.Application.do_activate(self)

        if window := self.get_active_window():
            window.present()
            return

        window = self.new_window()
        window.invoke_initial_screen()

        # Only the first activation of the process reaches this point -- every
        # later one returns above with an existing window -- so starting
        # minimized cannot swallow a deliberate request to open the window,
        # whether that comes from the tray or from launching the app again.
        #
        # Gated on run-in-background at runtime as well as in preferences: the
        # tray icon follows that setting, so without it there would be no window
        # and nothing to summon one with. The window is still created, just not
        # presented, which is what keeps the application from quitting.
        if (
            config_manager.get_start_minimized()
            and config_manager.get_run_in_background()
        ):
            logging.debug("Starting minimized to the tray")
            return

        window.present()

    def setup_actions(self):
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", self.on_quit_action)
        self.add_action(quit_action)

        # An application action rather than a window one: the generator needs
        # no safe, so it is offered whether or not anything is unlocked.
        password_generator_action = Gio.SimpleAction.new("password_generator", None)
        password_generator_action.connect("activate", self.on_password_generator_action)
        self.add_action(password_generator_action)

        new_window_action = Gio.SimpleAction.new("new-window", None)
        new_window_action.connect("activate", self.on_new_window_action)
        self.add_action(new_window_action)

    def on_quit_action(self, _action: Gio.Action, _param: GLib.Variant) -> None:
        for window in self.get_windows():
            # Not close(): with running in the background enabled that would be
            # taken as a request to hide, leaving no way to quit.
            window.close_for_quit()

    def on_password_generator_action(
        self,
        _action: Gio.Action,
        _param: GLib.Variant,
    ) -> None:
        if window := self.get_active_window():
            PasswordGeneratorDialog().present(window)

    def on_new_window_action(self, _action: Gio.Action, _param: GLib.Variant) -> None:
        window = self.new_window()
        window.invoke_initial_screen()
        window.present()

    def add_global_accelerators(self):
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("app.new-window", ["<Control><Shift>n"])
        self.set_accels_for_action("win.settings", ["<Control>comma"])
        self.set_accels_for_action("win.open_database('')", ["<Control>o"])
        self.set_accels_for_action("win.new_database", ["<Control>n"])
        self.set_accels_for_action("win.db.save", ["<Control>s"])
        self.set_accels_for_action("win.db.search", ["<Control>f"])
        self.set_accels_for_action("win.db.lock", ["<Control>l"])
        self.set_accels_for_action("win.db.add_entry", ["<Control>e"])
        self.set_accels_for_action("win.db.add_group", ["<Control>p"])
        self.set_accels_for_action("win.go_back", ["Escape"])
        self.set_accels_for_action("window.close", ["<Control>w"])
