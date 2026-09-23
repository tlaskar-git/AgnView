"""AgnView as a Windows desktop app: one window, a tray icon, and an option to
start at sign-in.

The hub still serves its API on loopback, because paired phones and the agent
CLIs talk to it. Nobody opens a browser any more: the window hosts the
dashboard in WebView2, closing the window hides it to the tray, and Quit in
the tray menu stops the hub.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Optional

APP_NAME = "AgnView"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
DATA_DIR = Path.home() / ".agnview"
SETTINGS_PATH = DATA_DIR / "desktop.json"
LOG_PATH = DATA_DIR / "logs" / "desktop.log"
DEFAULT_PORT = 8765

# One instance per signed-in user. A second launch signals the first to show
# its window and then exits, so a double-click on the shortcut never starts a
# second hub fighting for the same port.
MUTEX_NAME = "Local\\AgnViewDesktop"
SHOW_EVENT_NAME = "Local\\AgnViewDesktopShow"
ERROR_ALREADY_EXISTS = 183

logger = logging.getLogger("agnview.desktop")


# --- Settings ------------------------------------------------------------------

def load_settings() -> dict:
    settings = {"port": DEFAULT_PORT, "autostart": True}
    try:
        stored = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            settings.update({k: stored[k] for k in ("port", "autostart") if k in stored})
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


# --- Start with Windows --------------------------------------------------------

def launch_command() -> str:
    """The command the Run key starts at sign-in. The window stays hidden in
    the tray, since nobody asked to see it yet."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --minimized'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{interpreter}" -m agent_relay.desktop --minimized'


def autostart_registered() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            value, _kind = winreg.QueryValueEx(key, APP_NAME)
            return value == launch_command()
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    """Write or remove the Run value. It shares its name with the one
    `agnview serve` registers, so the desktop app replaces the old
    browser-only hub at sign-in instead of starting a second one."""
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass

    from ..core import autostart

    # The desktop app owns this choice now. Keep `agnview serve` from putting
    # its own registration back behind it.
    autostart.OPT_OUT_MARKER.parent.mkdir(parents=True, exist_ok=True)
    autostart.OPT_OUT_MARKER.write_text("")


# --- Single instance -----------------------------------------------------------

def claim_single_instance() -> bool:
    """Return True for the first instance. A later one wakes the first and
    returns False."""
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if kernel32.GetLastError() != ERROR_ALREADY_EXISTS:
        return True
    EVENT_MODIFY_STATE = 0x0002
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, SHOW_EVENT_NAME)
    if handle:
        kernel32.SetEvent(handle)
        kernel32.CloseHandle(handle)
    return False


def watch_for_show_requests(on_show) -> None:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateEventW(None, False, False, SHOW_EVENT_NAME)

    def loop():
        INFINITE = 0xFFFFFFFF
        while True:
            kernel32.WaitForSingleObject(handle, INFINITE)
            on_show()

    threading.Thread(target=loop, name="agnview-show-watch", daemon=True).start()


def message_box(text: str) -> None:
    MB_ICONERROR = 0x10
    ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, MB_ICONERROR)


# --- Hub -----------------------------------------------------------------------

def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class Hub:
    """The FastAPI hub on loopback, run by uvicorn on a background thread."""

    def __init__(self, port: int):
        self.port = port
        self.server = None
        self.thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def start(self, timeout: float = 30.0) -> None:
        import uvicorn
        from ..api.app import create_app
        from ..core.network import BIND_MODE_ENV
        from ..core.pairing import get_or_create_pairing_token

        os.environ["AGENT_RELAY_TOKEN"] = os.environ.get("AGENT_RELAY_TOKEN") or get_or_create_pairing_token()
        os.environ[BIND_MODE_ENV] = "loopback"

        app = create_app(port=self.port)
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_config=None, log_level="info")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="agnview-hub", daemon=True)
        self.thread.start()

        deadline = time.monotonic() + timeout
        while not self.server.started:
            if not self.thread.is_alive():
                raise RuntimeError("The AgnView hub stopped while starting. See the log for details.")
            if time.monotonic() > deadline:
                raise RuntimeError("The AgnView hub did not start in time.")
            time.sleep(0.1)

    def stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None:
            self.thread.join(timeout=10)


# --- Window and tray -----------------------------------------------------------

def load_icon_image():
    from PIL import Image

    static = Path(__file__).resolve().parent.parent / "web" / "static"
    return Image.open(static / "agnview-app-icon-dark.png")


class DesktopApp:
    def __init__(self, hub: Hub, settings: dict, start_hidden: bool):
        self.hub = hub
        self.settings = settings
        self.start_hidden = start_hidden
        self.window = None
        self.tray = None
        self.quitting = False

    def show(self) -> None:
        if self.window is None:
            return
        self.window.show()
        self.window.restore()

    def on_closing(self):
        # Closing the window hides it. The hub keeps running for paired phones
        # and agent CLIs until Quit is chosen in the tray.
        if self.quitting:
            return True
        self.window.hide()
        return False

    def toggle_autostart(self, _icon=None, _item=None) -> None:
        enabled = not autostart_registered()
        try:
            set_autostart(enabled)
        except OSError as exc:
            logger.exception("Could not change the Start with Windows setting")
            message_box(f"Could not change the Start with Windows setting: {exc}")
            return
        self.settings["autostart"] = enabled
        save_settings(self.settings)

    def quit(self, _icon=None, _item=None) -> None:
        self.quitting = True
        if self.tray is not None:
            self.tray.stop()
        self.hub.stop()
        if self.window is not None:
            self.window.destroy()

    def start_tray(self) -> None:
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem("Open AgnView", lambda: self.show(), default=True),
            pystray.MenuItem("Start with Windows", self.toggle_autostart, checked=lambda _item: autostart_registered()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit AgnView", self.quit),
        )
        self.tray = pystray.Icon(APP_NAME, load_icon_image(), APP_NAME, menu)
        self.tray.run_detached()

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
        watch_for_show_requests(self.show)
        self.start_tray()
        webview.start(
            gui="edgechromium",
            private_mode=False,
            storage_path=str(DATA_DIR / "webview"),
        )


# --- Entry point ---------------------------------------------------------------

def configure_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # A windowed build has no console. Anything printed must still land
    # somewhere instead of raising on a missing stream.
    if sys.stdout is None or sys.stderr is None:
        stream = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="AgnView", description="AgnView desktop app for Windows")
    parser.add_argument("--minimized", action="store_true", help="Start hidden in the tray (used at sign-in)")
    parser.add_argument("--port", type=int, help="Loopback port for the hub, saved for later starts")
    args = parser.parse_args(argv)

    if sys.platform != "win32":
        print("The AgnView desktop app runs on Windows. Use `agnview serve` elsewhere.", file=sys.stderr)
        return 1

    configure_logging()

    if not claim_single_instance():
        return 0

    settings = load_settings()
    if args.port:
        settings["port"] = args.port
    save_settings(settings)

    # Honour the saved choice at every start, so an install moved to a new
    # folder still starts from the right place.
    try:
        if settings.get("autostart", True) != autostart_registered():
            set_autostart(bool(settings.get("autostart", True)))
    except OSError:
        logger.exception("Could not update the Start with Windows registration")

    port = int(settings["port"])
    if port_in_use(port):
        message_box(
            f"Port {port} is already in use, so AgnView cannot start its hub.\n\n"
            "Stop the other program, or close an older AgnView started with `agnview serve`, then start AgnView again."
        )
        return 1

    hub = Hub(port)
    try:
        hub.start()
    except Exception as exc:
        logger.exception("Hub failed to start")
        message_box(f"AgnView could not start: {exc}\n\nLog: {LOG_PATH}")
        return 1

    DesktopApp(hub, settings, start_hidden=args.minimized).run()
    hub.stop()
    return 0
