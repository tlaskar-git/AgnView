"""AgnView as a macOS desktop app: one window, a menu bar icon, and an option
to start at login.

The same shell as the Windows app in ``app.py``, with the Mac parts swapped
in. The window is pywebview on WebKit over the hub on loopback. The menu bar
icon is a native NSStatusItem made with PyObjC on the main thread, inside the
Cocoa run loop pywebview already runs. pystray is not used on macOS, because
it wants to own the main thread and its run loop as well, and only one of the
two can. See docs/adr/ADR-MACOS-DESKTOP.md.

Only macOS imports this module. The GUI imports stay inside functions so a
test run on any platform can still import ``agent_relay.desktop``.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional

from . import app as desktop
from . import macos

logger = logging.getLogger("agnview.desktop")

APP_NAME = desktop.APP_NAME
MENU_OPEN = "Open AgnView"
MENU_AUTOSTART = "Start at login"
MENU_LAN = "Allow phones on my network"
MENU_QUIT = "Quit AgnView"

# NSApplicationActivationPolicy values. Regular shows a Dock icon, Accessory
# does not.
POLICY_REGULAR = 0
POLICY_ACCESSORY = 1
# NSApplicationTerminateReply
TERMINATE_NOW = 1


def _on_main(func, *args) -> None:
    """Run func on the main thread, where AppKit wants every UI call."""
    from PyObjCTools import AppHelper

    AppHelper.callAfter(func, *args)


def alert(text: str) -> None:
    """A modal error message, the Mac version of the Windows message box."""

    def show():
        try:
            import AppKit

            AppKit.NSApplication.sharedApplication()
            box = AppKit.NSAlert.alloc().init()
            box.setMessageText_(APP_NAME)
            box.setInformativeText_(text)
            box.setAlertStyle_(2)  # NSAlertStyleCritical
            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            box.runModal()
        except Exception:
            logger.exception("Could not show the message: %s", text)

    if threading.current_thread() is threading.main_thread():
        show()
    else:
        _on_main(show)


def menu_bar_icon_path() -> Path:
    # The transparent logo. As a template image macOS draws it in the menu
    # bar's own colour, light or dark.
    return Path(desktop.__file__).resolve().parent.parent / "web" / "static" / "agnview-icon.png"


_target_class = None


def _menu_target_class():
    """The Objective-C class that receives menu clicks and app events. Made
    once, because the Objective-C runtime refuses a second class of the same
    name."""
    global _target_class
    if _target_class is not None:
        return _target_class

    import AppKit
    import objc

    class AgnViewMenuTarget(AppKit.NSObject):
        # Every method catches its own errors. An exception that escapes into
        # AppKit ends the app.

        def openApp_(self, _sender):
            self._call("show")

        def toggleAutostart_(self, _sender):
            self._call("toggle_autostart")
            self._call("refresh_menu")

        def toggleLan_(self, _sender):
            self._call("toggle_lan")

        def quitApp_(self, _sender):
            self._call("quit")

        def menuWillOpen_(self, _menu):
            self._call("refresh_menu")

        def applicationShouldTerminate_(self, _app):
            # Cmd+Q, Quit in the Dock and logging out all end up here.
            try:
                return self.owner.should_terminate()
            except Exception:
                logger.exception("Shutdown failed")
                return TERMINATE_NOW

        def applicationShouldHandleReopen_hasVisibleWindows_(self, _app, _visible):
            # Opening AgnView from Finder or Launchpad while it runs.
            self._call("show")
            return True

        def applicationSupportsSecureRestorableState_(self, _app):
            return True

        @objc.python_method
        def _call(self, name):
            try:
                getattr(self.owner, name)()
            except Exception:
                logger.exception("Menu action %s failed", name)

    _target_class = AgnViewMenuTarget
    return _target_class


class MacDesktopApp(desktop.DesktopApp):
    """The Windows DesktopApp with a menu bar icon in place of the tray.

    Settings, the hub restart for Allow phones on my network and the Start at
    login switch are the shared code in app.py. The Dock icon shows while the
    window is open and goes with it, so a hidden AgnView lives in the menu bar
    only, as it lives in the tray on Windows.
    """

    def __init__(self, hub, settings: dict, start_hidden: bool, instance: Optional[macos.SingleInstance] = None):
        super().__init__(hub, settings, start_hidden)
        self.instance = instance
        self.status_item = None
        self.menu_target = None
        self.autostart_item = None
        self.lan_item = None
        self._shut_down = False

    # --- window --------------------------------------------------------------

    def show(self) -> None:
        if self.window is None:
            return
        _on_main(self._set_policy, POLICY_REGULAR)
        self.window.show()

    def hide(self) -> None:
        if self.window is None:
            return
        self.window.hide()
        _on_main(self._set_policy, POLICY_ACCESSORY)

    def on_closing(self):
        # The close button hides the window and the hub keeps running. The
        # menu bar icon brings it back, and Quit AgnView in its menu quits.
        if self.quitting:
            return True
        self.hide()
        if not self.told_about_tray:
            self.told_about_tray = True
            self.notify(
                "AgnView is still running. Its icon in the menu bar opens it again, "
                "and Quit AgnView in that menu closes it."
            )
        return False

    def notify(self, message: str) -> None:
        # Best effort. A build without a signed bundle can have no
        # notification centre, and a missing notice is no reason to fail.
        try:
            import Foundation

            center = Foundation.NSUserNotificationCenter.defaultUserNotificationCenter()
            if center is None:
                return
            note = Foundation.NSUserNotification.alloc().init()
            note.setTitle_(APP_NAME)
            note.setInformativeText_(message)
            center.deliverNotification_(note)
        except Exception:
            logger.info("No notification shown: %s", message)

    @staticmethod
    def _set_policy(policy: int) -> None:
        import AppKit

        AppKit.NSApplication.sharedApplication().setActivationPolicy_(policy)

    # --- quitting ------------------------------------------------------------

    def quit(self, _icon=None, _item=None) -> None:
        """Quit AgnView from the menu. Goes through the normal Cocoa quit, so
        Cmd+Q, the Dock and the menu all stop the hub the same way."""
        import AppKit

        self.quitting = True
        AppKit.NSApplication.sharedApplication().terminate_(None)

    def should_terminate(self) -> int:
        self.quitting = True
        self.shutdown()
        # AppKit ends the process straight after this, with no Python
        # clean-up, so the log is flushed here.
        logging.shutdown()
        return TERMINATE_NOW

    def shutdown(self) -> None:
        """Stop the hub and let go of the single-instance lock. Safe to call
        more than once."""
        if self._shut_down:
            return
        self._shut_down = True
        desktop._lan_change_handler = None
        try:
            self.hub.stop()
        except Exception:
            logger.exception("Could not stop the hub")
        if self.instance is not None:
            self.instance.release()
        logger.info("AgnView closed")

    # --- menu bar icon -------------------------------------------------------

    def refresh_menu(self) -> None:
        if self.autostart_item is not None:
            self.autostart_item.setState_(1 if desktop.autostart_enabled() else 0)
        if self.lan_item is not None:
            self.lan_item.setState_(1 if desktop.lan_enabled() else 0)

    def install_menu_bar(self) -> None:
        """Create the menu bar icon. Runs on the main thread once the Cocoa
        run loop is going."""
        import AppKit

        target = _menu_target_class().alloc().init()
        target.owner = self

        menu = AppKit.NSMenu.alloc().initWithTitle_(APP_NAME)
        menu.setAutoenablesItems_(False)
        menu.setDelegate_(target)

        def add(title: str, action: str, key: str = ""):
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
            item.setTarget_(target)
            menu.addItem_(item)
            return item

        add(MENU_OPEN, "openApp:")
        self.autostart_item = add(MENU_AUTOSTART, "toggleAutostart:")
        self.lan_item = add(MENU_LAN, "toggleLan:")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        add(MENU_QUIT, "quitApp:", "q")

        status_item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        button = status_item.button()
        image = AppKit.NSImage.alloc().initWithContentsOfFile_(str(menu_bar_icon_path()))
        if image is not None:
            image.setSize_((18, 18))
            image.setTemplate_(True)
            button.setImage_(image)
        else:
            button.setTitle_(APP_NAME)
        button.setToolTip_(APP_NAME)
        status_item.setMenu_(menu)

        # Held here, or Python frees them and the icon disappears.
        self.status_item = status_item
        self.menu_target = target
        self.refresh_menu()

        AppKit.NSApplication.sharedApplication().setDelegate_(target)
        if self.start_hidden:
            self._set_policy(POLICY_ACCESSORY)
        logger.info("Menu bar icon ready")

    def _schedule_menu_bar(self) -> None:
        # pywebview calls this on a worker thread as its run loop starts.
        _on_main(self.install_menu_bar)

    def run(self) -> None:
        import webview

        self.window = webview.create_window(
            APP_NAME,
            self.hub.url,
            width=1320,
            height=880,
            min_size=(960, 640),
            hidden=self.start_hidden,
        )
        self.window.events.closing += self.on_closing
        if self.instance is not None:
            self.instance.listen(self.show)
        webview.start(
            func=self._schedule_menu_bar,
            gui="cocoa",
            private_mode=False,
            storage_path=str(desktop.DATA_DIR / "webview"),
        )


# --- Entry point -------------------------------------------------------------------

def sync_login_item(settings: dict) -> None:
    """Honour the saved Start at login choice at every start, so an app moved
    to another folder still starts from the right place."""
    from ..core import autostart

    wanted = bool(settings.get("autostart", False))
    stale = not wanted and macos.LAUNCH_AGENT_PATH.exists()
    if wanted != macos.login_item_registered() or stale or autostart.MACOS_PLIST_PATH.exists():
        desktop.set_autostart(wanted)


def main(args) -> int:
    desktop.configure_logging()

    instance = macos.SingleInstance()
    if not instance.claim():
        logger.info("AgnView is already running, so this launch showed its window")
        return 0

    settings = desktop.load_settings()
    if args.port:
        settings["port"] = args.port
    desktop.save_settings(settings)

    try:
        sync_login_item(settings)
    except OSError:
        logger.exception("Could not update the Start at login registration")

    port = desktop.choose_port(int(settings["port"]))
    if port is None:
        alert(
            f"Ports {settings['port']} to {int(settings['port']) + desktop.PORT_SEARCH_SPAN - 1} are all in use, "
            "so AgnView cannot start its hub.\n\n"
            "Close an older AgnView started with `agnview serve`, then start AgnView again."
        )
        return 1
    if port != int(settings["port"]):
        logger.warning("Port %s is in use, so this run serves on %s", settings["port"], port)

    hub = desktop.Hub(port, desktop.bind_host(bool(settings.get("lan", False))))
    try:
        hub.start()
    except Exception as exc:
        logger.exception("Hub failed to start")
        alert(f"AgnView could not start: {exc}\n\nLog: {desktop.LOG_PATH}")
        return 1
    logger.info("Hub serving on %s", hub.url)

    desktop_app = MacDesktopApp(hub, settings, start_hidden=args.minimized, instance=instance)
    desktop._lan_change_handler = desktop_app.set_lan
    try:
        desktop_app.run()
    finally:
        desktop_app.shutdown()
        logging.shutdown()
        # Worker threads started by the hub, such as the iroh transport, must
        # not keep a closed app alive in the background.
        os._exit(0)
