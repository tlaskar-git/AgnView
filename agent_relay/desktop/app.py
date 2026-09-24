"""AgnView as a Windows desktop app: one window, a tray icon, and an option to
start at sign-in.

The hub still serves its API on loopback, because paired phones and the agent
CLIs talk to it. Nobody opens a browser any more: the window hosts the
dashboard in WebView2. The close button, or Quit AgnView in the tray menu,
closes the app and stops the hub. The minimise button keeps it on the taskbar.
With Start with Windows on, it starts hidden in the tray at sign-in.
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
# The desktop app's own port, away from 8765, which `agnview serve` uses by
# default. Sharing a port meant an old browser-only hub started at sign-in
# took it first and the desktop app then refused to start.
DEFAULT_PORT = 18845
# When the saved port is taken, the next free one in this many is used.
PORT_SEARCH_SPAN = 20
DESKTOP_ENV = "AGNVIEW_DESKTOP"

# One instance per signed-in user. A second launch signals the first to show
# its window and then exits, so a double-click on the shortcut never starts a
# second hub fighting for the same port.
MUTEX_NAME = "Local\\AgnViewDesktop"
SHOW_EVENT_NAME = "Local\\AgnViewDesktopShow"
ERROR_ALREADY_EXISTS = 183

logger = logging.getLogger("agnview.desktop")


# --- Settings ------------------------------------------------------------------

def load_settings() -> dict:
    # Start with Windows is off until a person turns it on. It used to be on
    # by default, so a first start registered a sign-in task nobody asked for.
    settings = {"port": DEFAULT_PORT, "autostart": False}
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


def autostart_enabled() -> bool:
    """The saved Start with Windows choice. The tray menu and the dashboard
    both read it from the settings file, so they can never disagree."""
    return bool(load_settings().get("autostart", False))


def change_autostart(enabled: bool) -> None:
    """Turn Start with Windows on or off and save the choice. Raises OSError
    when Task Scheduler refuses, and then saves nothing."""
    set_autostart(enabled)
    settings = load_settings()
    settings["autostart"] = bool(enabled)
    save_settings(settings)


# --- Start with Windows --------------------------------------------------------
#
# Start with Windows is a Task Scheduler task with a sign-in trigger, not a Run
# key value. Explorer skipped the Run value after an unexpected restart with
# nothing in any log, while it still started the other Run entries. The task
# is started by the Task Scheduler service itself, waits a few seconds for the
# desktop, and retries if the app fails to start. Creating a sign-in task for
# your own account needs no admin rights.

TASK_NAME = "AgnView"
SIGN_IN_DELAY = "PT15S"
CREATE_NO_WINDOW = 0x08000000


def launch_command() -> tuple[str, str]:
    """The program and arguments that start AgnView at sign-in. The window
    stays hidden in the tray, since nobody asked to see it yet."""
    if getattr(sys, "frozen", False):
        return sys.executable, "--minimized"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else Path(sys.executable)
    return str(interpreter), "-m agent_relay.desktop --minimized"


def current_user() -> str:
    domain = os.environ.get("USERDOMAIN")
    user = os.environ.get("USERNAME", "")
    return f"{domain}\\{user}" if domain else user


def task_xml(program: str, arguments: str, user: str) -> str:
    from xml.sax.saxutils import escape

    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Starts AgnView hidden in the tray at sign-in</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
      <Delay>{SIGN_IN_DELAY}</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(program)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str):
    import subprocess

    return subprocess.run(
        ["schtasks", *args],
        capture_output=True,
        text=True,
        creationflags=CREATE_NO_WINDOW,
    )


def autostart_registered() -> bool:
    """True when the sign-in task exists and starts this install."""
    result = _schtasks("/Query", "/TN", TASK_NAME, "/XML")
    if result.returncode != 0:
        return False
    program, _arguments = launch_command()
    from xml.sax.saxutils import escape

    return escape(program).lower() in result.stdout.lower()


def remove_run_values() -> None:
    """Remove the Run key value from older AgnView versions. It shares the
    name `agnview serve` registers, so this also stops the old browser-only
    hub from starting at sign-in beside the desktop app."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
    except FileNotFoundError:
        pass


def set_autostart(enabled: bool) -> None:
    """Create or remove the sign-in task."""
    if enabled:
        import tempfile

        program, arguments = launch_command()
        xml = task_xml(program, arguments, current_user())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "agnview-task.xml"
            path.write_text(xml, encoding="utf-16")
            result = _schtasks("/Create", "/TN", TASK_NAME, "/XML", str(path), "/F")
        if result.returncode != 0:
            raise OSError(result.stderr.strip() or result.stdout.strip() or "schtasks failed")
    else:
        result = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
        if result.returncode != 0 and autostart_registered():
            raise OSError(result.stderr.strip() or "schtasks failed")

    remove_run_values()

    from ..core import autostart

    # The desktop app owns this choice now. Keep `agnview serve` from putting
    # its own registration back behind it.
    autostart.OPT_OUT_MARKER.parent.mkdir(parents=True, exist_ok=True)
    autostart.OPT_OUT_MARKER.write_text("")


def run_value_present() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


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

def choose_port(preferred: int) -> Optional[int]:
    """The saved port, or the next free one after it.

    A busy port used to stop the app with a message, so one stray process
    kept AgnView from starting at all. The saved port is left as it is, so the
    next start tries it again first.
    """
    for port in range(preferred, preferred + PORT_SEARCH_SPAN):
        if not port_in_use(port):
            return port
    return None


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
        # Tells the dashboard's autostart switch to drive this app's Start with
        # Windows setting, not the Run key the browser-only hub uses.
        os.environ[DESKTOP_ENV] = "1"

        app = create_app(port=self.port)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_config=None,
            log_level="info",
            timeout_graceful_shutdown=3,
        )
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
        # The close button closes AgnView, as in any Windows app, and the
        # minimise button keeps it on the taskbar. Minimising on close left
        # people with no way to close the window short of the tray menu.
        self.quitting = True
        return True

    def toggle_autostart(self, _icon=None, _item=None) -> None:
        enabled = not autostart_enabled()
        try:
            change_autostart(enabled)
        except OSError as exc:
            logger.exception("Could not change the Start with Windows setting")
            message_box(f"Could not change the Start with Windows setting: {exc}")

    def quit(self, _icon=None, _item=None) -> None:
        """Quit from the tray menu. Destroying the window ends webview.start,
        and main() then stops the tray and the hub."""
        self.quitting = True
        if self.window is not None:
            self.window.destroy()

    def stop_tray(self) -> None:
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                logger.exception("Could not remove the tray icon")

    def start_tray(self) -> None:
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem("Open AgnView", lambda: self.show(), default=True),
            pystray.MenuItem("Start with Windows", self.toggle_autostart, checked=lambda _item: autostart_enabled()),
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
        try:
            webview.start(
                gui="edgechromium",
                private_mode=False,
                storage_path=str(DATA_DIR / "webview"),
            )
        finally:
            self.stop_tray()


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
        wanted = bool(settings.get("autostart", False))
        if wanted != autostart_registered() or run_value_present():
            set_autostart(wanted)
    except OSError:
        logger.exception("Could not update the Start with Windows registration")

    port = choose_port(int(settings["port"]))
    if port is None:
        message_box(
            f"Ports {settings['port']} to {int(settings['port']) + PORT_SEARCH_SPAN - 1} are all in use, "
            "so AgnView cannot start its hub.\n\n"
            "Close an older AgnView started with `agnview serve`, then start AgnView again."
        )
        return 1
    if port != int(settings["port"]):
        logger.warning("Port %s is in use, so this run serves on %s", settings["port"], port)

    hub = Hub(port)
    try:
        hub.start()
    except Exception as exc:
        logger.exception("Hub failed to start")
        message_box(f"AgnView could not start: {exc}\n\nLog: {LOG_PATH}")
        return 1

    try:
        DesktopApp(hub, settings, start_hidden=args.minimized).run()
    finally:
        hub.stop()
        logger.info("AgnView closed")
        logging.shutdown()
        # Worker threads started by the hub, such as the iroh transport, must
        # not keep a closed app alive in the background.
        os._exit(0)
