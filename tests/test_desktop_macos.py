"""The macOS desktop app's logic that needs no GUI: the Start at login
LaunchAgent, the single-instance lock, the port fallback and the dashboard
wording. Runs on Linux, macOS and Windows alike."""

import plistlib
import socket
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from agent_relay.desktop import app as desktop
from agent_relay.desktop import macos
from agent_relay.desktop import macos_app


# --- LaunchAgent ------------------------------------------------------------------

def test_the_launch_agent_starts_the_app_hidden_once_at_login():
    data = plistlib.loads(macos.launch_agent_plist(["/Applications/AgnView.app/Contents/MacOS/AgnView", "--minimized"]))
    assert data["Label"] == macos.LAUNCH_AGENT_LABEL
    assert data["ProgramArguments"] == ["/Applications/AgnView.app/Contents/MacOS/AgnView", "--minimized"]
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is False
    assert data["LimitLoadToSessionType"] == "Aqua"


def test_the_launch_agent_escapes_awkward_paths():
    awkward = "/Users/Test User/Apps/A & B <x>/AgnView.app/Contents/MacOS/AgnView"
    data = plistlib.loads(macos.launch_agent_plist([awkward, "--minimized"]))
    assert data["ProgramArguments"][0] == awkward


def test_the_desktop_label_differs_from_the_serve_label():
    from agent_relay.core import autostart

    assert macos.LAUNCH_AGENT_LABEL != autostart.MACOS_LABEL


def test_a_built_app_registers_its_own_executable(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/Applications/AgnView.app/Contents/MacOS/AgnView")
    assert macos.launch_arguments() == ["/Applications/AgnView.app/Contents/MacOS/AgnView", "--minimized"]


def test_a_source_install_registers_the_module(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert macos.launch_arguments()[1:] == ["-m", "agent_relay.desktop", "--minimized"]


def test_the_login_item_is_written_and_removed(tmp_path):
    path = tmp_path / "LaunchAgents" / "agent.plist"
    assert macos.login_item_registered(path) is False

    macos.set_login_item(True, path)
    assert path.exists()
    assert macos.login_item_registered(path) is True
    assert macos.registered_arguments(path) == macos.launch_arguments()

    macos.set_login_item(False, path)
    assert not path.exists()
    macos.set_login_item(False, path)


def test_a_login_item_for_another_install_is_not_ours(tmp_path):
    path = tmp_path / "agent.plist"
    path.write_bytes(macos.launch_agent_plist(["/elsewhere/AgnView", "--minimized"]))
    assert macos.login_item_registered(path) is False


def test_a_corrupt_login_item_reads_as_none(tmp_path):
    path = tmp_path / "agent.plist"
    path.write_text("not a plist")
    assert macos.registered_arguments(path) is None


def _mac_paths(tmp_path, monkeypatch):
    from agent_relay.core import autostart

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(macos, "LAUNCH_AGENT_PATH", tmp_path / "LaunchAgents" / "com.agnview.desktop.plist")
    monkeypatch.setattr(autostart, "MACOS_PLIST_PATH", tmp_path / "LaunchAgents" / "com.agnview.hub.plist")
    monkeypatch.setattr(autostart, "OPT_OUT_MARKER", tmp_path / "opt-out")
    monkeypatch.setattr(desktop, "SETTINGS_PATH", tmp_path / "desktop.json")
    return autostart


def test_start_at_login_on_macos_writes_the_launch_agent(tmp_path, monkeypatch):
    autostart = _mac_paths(tmp_path, monkeypatch)
    autostart.MACOS_PLIST_PATH.parent.mkdir(parents=True)
    autostart.MACOS_PLIST_PATH.write_text("<plist/>")

    desktop.change_autostart(True)

    assert macos.LAUNCH_AGENT_PATH.exists()
    # The browser-only hub no longer starts at login beside the app, and
    # agnview serve leaves the choice to the app.
    assert not autostart.MACOS_PLIST_PATH.exists()
    assert autostart.OPT_OUT_MARKER.exists()
    assert desktop.autostart_enabled() is True

    desktop.change_autostart(False)
    assert not macos.LAUNCH_AGENT_PATH.exists()
    assert desktop.autostart_enabled() is False


def test_the_saved_choice_is_applied_at_start(tmp_path, monkeypatch):
    _mac_paths(tmp_path, monkeypatch)
    macos.LAUNCH_AGENT_PATH.parent.mkdir(parents=True)
    macos.LAUNCH_AGENT_PATH.write_bytes(macos.launch_agent_plist(["/old/AgnView", "--minimized"]))

    macos_app.sync_login_item({"autostart": False})
    assert not macos.LAUNCH_AGENT_PATH.exists()

    macos_app.sync_login_item({"autostart": True})
    assert macos.login_item_registered() is True


def test_the_dashboard_switch_is_start_at_login_on_macos(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from agent_relay.api.app import create_app

    _mac_paths(tmp_path, monkeypatch)
    monkeypatch.setenv(desktop.DESKTOP_ENV, "1")
    client = TestClient(create_app(db_path=str(tmp_path / "hub.db")))

    state = client.get("/api/system/autostart").json()
    assert state["enabled"] is False
    assert state["label"] == "Start at login"

    answer = client.post("/api/system/autostart", json={"enable": True}).json()
    assert answer["enabled"] is True
    assert answer["message"] == "Start at login is on."
    assert macos.LAUNCH_AGENT_PATH.exists()

    assert "macOS" in client.get("/api/system/lan").json()["hint"]


def test_windows_keeps_its_own_wording(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    text = desktop.platform_text()
    assert text["autostart_label"] == "Start with Windows"
    assert "firewall" in text["lan_hint"]


def test_linux_still_has_no_desktop_app(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    assert desktop.main([]) == 1


# --- Port ----------------------------------------------------------------------------

def test_a_port_in_real_use_moves_to_a_free_one():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(16)
        busy = held.getsockname()[1]
        assert desktop.port_in_use(busy) is True
        chosen = desktop.choose_port(busy)
    assert chosen is not None and busy < chosen < busy + desktop.PORT_SEARCH_SPAN


def test_the_mac_app_uses_the_windows_port():
    assert desktop.DEFAULT_PORT == 18845


# --- Single instance --------------------------------------------------------------------

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="flock and Unix sockets are POSIX only")


@pytest.fixture
def short_dir():
    # Unix socket paths are limited to about 100 characters, and pytest's
    # tmp_path on macOS is longer than that.
    folder = Path(tempfile.mkdtemp(prefix="agv", dir="/tmp" if sys.platform != "win32" else None))
    yield folder
    for item in folder.iterdir():
        item.unlink()
    folder.rmdir()


@posix_only
def test_a_second_instance_wakes_the_first_and_gives_up(short_dir):
    first = macos.SingleInstance(short_dir / "desktop.lock", short_dir / "desktop.sock")
    assert first.claim() is True
    shown = threading.Event()
    first.listen(shown.set)

    second = macos.SingleInstance(short_dir / "desktop.lock", short_dir / "desktop.sock")
    assert second.claim() is False
    assert shown.wait(5)

    first.release()
    third = macos.SingleInstance(short_dir / "desktop.lock", short_dir / "desktop.sock")
    assert third.claim() is True
    third.release()


@posix_only
def test_waking_with_nobody_listening_is_harmless(short_dir):
    instance = macos.SingleInstance(short_dir / "desktop.lock", short_dir / "absent.sock")
    assert instance.wake_first() is False


@posix_only
def test_a_stale_socket_file_is_replaced(short_dir):
    (short_dir / "desktop.sock").write_text("")
    instance = macos.SingleInstance(short_dir / "desktop.lock", short_dir / "desktop.sock")
    assert instance.claim() is True
    instance.listen(lambda: None)
    instance.release()
    assert not (short_dir / "desktop.sock").exists()


# --- Close hides the window --------------------------------------------------------------

def test_the_close_button_hides_the_mac_window_and_says_so_once(monkeypatch):
    class Window:
        hidden = 0

        def hide(self):
            self.hidden += 1

    monkeypatch.setattr(macos_app, "_on_main", lambda func, *args: None)
    notes = []
    app = macos_app.MacDesktopApp(hub=None, settings={}, start_hidden=False)
    monkeypatch.setattr(app, "notify", notes.append)
    app.window = Window()

    assert app.on_closing() is False
    assert app.on_closing() is False
    assert app.window.hidden == 2
    assert len(notes) == 1 and "menu bar" in notes[0]

    app.quitting = True
    assert app.on_closing() is True


def test_shutting_down_twice_stops_the_hub_once():
    class Hub:
        stops = 0

        def stop(self):
            self.stops += 1

    app = macos_app.MacDesktopApp(hub=Hub(), settings={}, start_hidden=False)
    app.shutdown()
    app.shutdown()
    assert app.hub.stops == 1


def test_the_menu_bar_icon_ships_with_the_package():
    assert macos_app.menu_bar_icon_path().exists()


# --- Newer copies take over -------------------------------------------------------

class _App:
    def __init__(self, pid):
        self._pid = pid

    def processIdentifier(self):
        return self._pid


def test_a_running_older_copy_is_taken_over():
    ended = []
    result = macos_app.take_over_older_copy(
        {"pid": 4242, "version": "0.1.7"}, "0.1.8", find_pids=lambda: [], end=lambda pid: ended.append(pid) or True
    )
    assert result is True
    assert ended == [4242]


def test_a_same_or_newer_copy_is_left_running():
    def refuse(pid):
        raise AssertionError("must not end a same or newer copy")

    assert macos_app.take_over_older_copy({"pid": 1, "version": "0.1.8"}, "0.1.8", end=refuse) is False
    assert macos_app.take_over_older_copy({"pid": 1, "version": "0.2.0"}, "0.1.8", end=refuse) is False


def test_a_copy_without_a_record_is_found_by_bundle_and_taken_over():
    ended = []
    result = macos_app.take_over_older_copy(
        None, "0.1.8", find_pids=lambda: [11, 12], end=lambda pid: ended.append(pid) or True
    )
    assert result is True
    assert ended == [11, 12]


def test_no_record_and_no_process_found_means_no_take_over():
    assert macos_app.take_over_older_copy(None, "0.1.8", find_pids=lambda: [], end=lambda pid: True) is False


def test_a_copy_that_will_not_close_is_not_taken_over():
    assert macos_app.take_over_older_copy(
        {"pid": 7, "version": "0.1.0"}, "0.1.8", end=lambda pid: False
    ) is False


def test_older_copies_are_found_by_bundle_without_this_process():
    apps = [_App(100), _App(200), _App(300), _App(0)]
    assert macos.older_copy_pids(me=200, running_apps=apps) == [100, 300]
    assert macos.older_copy_pids(me=1, running_apps=[]) == []


def _ender(alive_until_kill):
    state = {"now": 0.0, "alive": True, "sent": []}

    def send(pid, sig):
        state["sent"].append(sig)
        # One signal ends it, or only the second one does.
        if not alive_until_kill or len(state["sent"]) == 2:
            state["alive"] = False

    def sleep(seconds):
        state["now"] += seconds

    return state, dict(alive=lambda pid: state["alive"], send=send, sleep=sleep, clock=lambda: state["now"])


def test_end_process_sends_sigterm_and_stops_when_it_exits():
    import signal

    state, hooks = _ender(alive_until_kill=False)
    assert macos.end_process(5, **hooks) is True
    assert state["sent"] == [signal.SIGTERM]


def test_end_process_falls_back_to_sigkill_after_the_timeout():
    import signal

    state, hooks = _ender(alive_until_kill=True)
    assert macos.end_process(5, timeout=1.0, **hooks) is True
    assert state["sent"][0] == signal.SIGTERM
    assert len(state["sent"]) == 2


def test_end_process_reports_a_process_that_never_goes():
    state, hooks = _ender(alive_until_kill=True)
    hooks["send"] = lambda pid, sig: None
    assert macos.end_process(5, timeout=1.0, kill_after=1.0, **hooks) is False


def test_end_process_on_a_gone_process_is_done():
    assert macos.end_process(5, alive=lambda pid: False, send=lambda pid, sig: 1 / 0) is True


def test_a_leftover_launch_agent_is_removed_when_start_at_login_is_off(tmp_path):
    plist = tmp_path / "Library" / "LaunchAgents" / "com.example.test.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(macos.launch_agent_plist(["/opt/old/AgnView", "--minimized"]))
    assert macos.leftover_login_item(False, plist) is True
    assert macos.leftover_login_item(True, plist) is False
    macos.set_login_item(False, plist)
    assert not plist.exists()
    assert macos.leftover_login_item(False, plist) is False


def test_pid_alive_sees_this_process_and_not_a_missing_one():
    import os

    assert macos.pid_alive(os.getpid()) is True
    assert macos.pid_alive(2**22 + 12345) is False
