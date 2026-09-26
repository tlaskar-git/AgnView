"""The Windows desktop app keeps its settings in ~/.agnview/desktop.json and
registers a command that starts it hidden in the tray at sign-in."""

import json
import logging
import os
import sys
import types
from pathlib import Path

import pytest

from agent_relay.desktop import app as desktop


def test_settings_default_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "SETTINGS_PATH", tmp_path / "desktop.json")
    assert desktop.load_settings() == {"port": desktop.DEFAULT_PORT, "autostart": False, "lan": False}


def test_settings_keep_known_keys_only(tmp_path, monkeypatch):
    path = tmp_path / "desktop.json"
    path.write_text(json.dumps({"port": 9100, "autostart": False, "other": 1}))
    monkeypatch.setattr(desktop, "SETTINGS_PATH", path)
    assert desktop.load_settings() == {"port": 9100, "autostart": False, "lan": False}


def test_settings_survive_a_corrupt_file(tmp_path, monkeypatch):
    path = tmp_path / "desktop.json"
    path.write_text("{not json")
    monkeypatch.setattr(desktop, "SETTINGS_PATH", path)
    assert desktop.load_settings()["port"] == desktop.DEFAULT_PORT


def test_settings_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "SETTINGS_PATH", tmp_path / "sub" / "desktop.json")
    desktop.save_settings({"port": 9200, "autostart": True})
    assert desktop.load_settings() == {"port": 9200, "autostart": True, "lan": False}


def test_frozen_build_registers_itself_minimized(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\AgnView\AgnView.exe")
    assert desktop.launch_command() == (r"C:\Program Files\AgnView\AgnView.exe", "--minimized")


def test_source_install_registers_the_module_minimized(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    _program, arguments = desktop.launch_command()
    assert arguments == "-m agent_relay.desktop --minimized"


def test_sign_in_task_starts_hidden_after_a_delay_and_retries():
    xml = desktop.task_xml(r"C:\Apps\AgnView & Co\AgnView.exe", "--minimized", r"HOST\user")
    assert "<LogonTrigger>" in xml
    assert r"<UserId>HOST\user</UserId>" in xml
    assert f"<Delay>{desktop.SIGN_IN_DELAY}</Delay>" in xml
    assert "<RestartOnFailure>" in xml
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml
    assert r"<Command>C:\Apps\AgnView &amp; Co\AgnView.exe</Command>" in xml
    assert "<Arguments>--minimized</Arguments>" in xml


def test_start_with_windows_is_off_until_turned_on(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "SETTINGS_PATH", tmp_path / "desktop.json")
    assert desktop.autostart_enabled() is False


def test_changing_start_with_windows_saves_the_choice(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "SETTINGS_PATH", tmp_path / "desktop.json")
    desktop.save_settings({"port": 9300, "autostart": False})
    calls = []
    monkeypatch.setattr(desktop, "set_autostart", lambda enabled: calls.append(enabled))

    desktop.change_autostart(True)

    assert calls == [True]
    assert desktop.load_settings() == {"port": 9300, "autostart": True, "lan": False}
    assert desktop.autostart_enabled() is True


def test_a_refused_change_saves_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "SETTINGS_PATH", tmp_path / "desktop.json")

    def refuse(enabled):
        raise OSError("Access is denied.")

    monkeypatch.setattr(desktop, "set_autostart", refuse)
    try:
        desktop.change_autostart(True)
    except OSError:
        pass
    assert desktop.autostart_enabled() is False


def test_the_close_button_hides_to_the_tray_and_says_so_once():
    class Window:
        hidden = 0

        def hide(self):
            self.hidden += 1

    class Tray:
        def __init__(self):
            self.messages = []

        def notify(self, message, title):
            self.messages.append(message)

    app = desktop.DesktopApp(hub=None, settings={}, start_hidden=False)
    app.window = Window()
    app.tray = Tray()

    assert app.on_closing() is False
    assert app.on_closing() is False
    assert app.window.hidden == 2
    assert len(app.tray.messages) == 1
    assert "Quit AgnView" in app.tray.messages[0]


def test_quit_from_the_tray_closes_the_app():
    app = desktop.DesktopApp(hub=None, settings={}, start_hidden=False)
    app.quitting = True
    assert app.on_closing() is True


def test_the_dashboard_switch_drives_the_desktop_setting(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from agent_relay.api.app import create_app

    monkeypatch.setattr(desktop, "SETTINGS_PATH", tmp_path / "desktop.json")
    monkeypatch.setattr(desktop, "set_autostart", lambda enabled: None)
    monkeypatch.setenv(desktop.DESKTOP_ENV, "1")
    client = TestClient(create_app(db_path=str(tmp_path / "hub.db")))

    assert client.get("/api/system/autostart").json()["enabled"] is False
    assert client.post("/api/system/autostart", json={"enable": True}).json()["enabled"] is True
    assert desktop.autostart_enabled() is True
    assert client.post("/api/system/autostart", json={"enable": False}).json()["enabled"] is False


def test_the_desktop_app_has_its_own_port():
    # 8765 is what `agnview serve` uses by default.
    assert desktop.DEFAULT_PORT != 8765


def test_a_busy_port_moves_to_the_next_free_one(monkeypatch):
    busy = {18845, 18846}
    monkeypatch.setattr(desktop, "port_in_use", lambda port: port in busy)
    assert desktop.choose_port(18845) == 18847


def test_no_free_port_in_range_gives_none(monkeypatch):
    monkeypatch.setattr(desktop, "port_in_use", lambda port: True)
    assert desktop.choose_port(18845) is None


def test_agnview_serve_leaves_sign_in_to_the_desktop_app(tmp_path, monkeypatch):
    from agent_relay.core import autostart

    marker = tmp_path / "desktop.json"
    marker.write_text("{}")
    monkeypatch.setattr(autostart, "DESKTOP_SETTINGS", marker)
    monkeypatch.setattr(autostart, "OPT_OUT_MARKER", tmp_path / "absent")
    monkeypatch.setattr(autostart, "status", lambda: (_ for _ in ()).throw(AssertionError("checked")))
    monkeypatch.setattr(autostart, "enable", lambda args=(): (_ for _ in ()).throw(AssertionError("registered")))
    assert autostart.ensure_enabled_by_default(["--port", "8845"]) is None


def test_phones_on_the_network_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "SETTINGS_PATH", tmp_path / "desktop.json")
    assert desktop.lan_enabled() is False
    assert desktop.bind_host(False) == "127.0.0.1"
    assert desktop.bind_host(True) == "0.0.0.0"


def test_no_running_app_means_no_lan_change(monkeypatch):
    monkeypatch.setattr(desktop, "_lan_change_handler", None)
    assert desktop.request_lan_change(True) is False


def test_the_pairing_screen_switch_asks_the_desktop_app(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from agent_relay.api.app import create_app

    monkeypatch.setattr(desktop, "SETTINGS_PATH", tmp_path / "desktop.json")
    monkeypatch.setenv(desktop.DESKTOP_ENV, "1")
    asked = []
    monkeypatch.setattr(desktop, "request_lan_change", lambda enabled: asked.append(enabled) or True)
    client = TestClient(create_app(db_path=str(tmp_path / "hub.db")))

    state = client.get("/api/system/lan").json()
    assert state["available"] is True and state["enabled"] is False

    answer = client.post("/api/system/lan", json={"enable": True}).json()
    assert answer["restarting"] is True
    assert asked == [True]


def test_outside_the_desktop_app_the_switch_is_not_offered(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from agent_relay.api.app import create_app

    monkeypatch.delenv(desktop.DESKTOP_ENV, raising=False)
    client = TestClient(create_app(db_path=str(tmp_path / "hub.db")))
    assert client.get("/api/system/lan").json()["available"] is False
    assert client.post("/api/system/lan", json={"enable": True}).status_code == 400


def test_versions_compare_as_numbers():
    assert desktop.version_tuple("0.1.10") > desktop.version_tuple("0.1.9")
    assert desktop.version_tuple("0.2.0") > desktop.version_tuple("0.1.7")
    assert desktop.version_tuple("0.1.7") == desktop.version_tuple("0.1.7")


def test_a_newer_copy_takes_over_an_older_one():
    assert desktop.should_take_over({"pid": 1, "version": "0.1.7"}, "0.1.8") is True


def test_a_same_or_newer_running_copy_is_kept():
    assert desktop.should_take_over({"pid": 1, "version": "0.1.8"}, "0.1.8") is False
    assert desktop.should_take_over({"pid": 1, "version": "0.2.0"}, "0.1.8") is False


def test_a_copy_without_a_record_is_older_by_definition():
    # Every copy from before instance records existed.
    assert desktop.should_take_over(None, "0.1.8") is True


def test_a_record_for_a_dead_process_is_ignored(tmp_path, monkeypatch):
    path = tmp_path / "desktop-instance.json"
    path.write_text('{"pid": 12345, "version": "0.1.7"}')
    monkeypatch.setattr(desktop, "INSTANCE_PATH", path)
    monkeypatch.setattr(desktop, "process_alive", lambda pid: False)
    assert desktop.read_instance_record() is None


def test_a_record_for_a_live_process_is_read(tmp_path, monkeypatch):
    path = tmp_path / "desktop-instance.json"
    path.write_text('{"pid": 12345, "version": "0.1.7"}')
    monkeypatch.setattr(desktop, "INSTANCE_PATH", path)
    monkeypatch.setattr(desktop, "process_alive", lambda pid: True)
    assert desktop.read_instance_record()["version"] == "0.1.7"


# --- A failure must leave a trace ---------------------------------------------------

def test_a_window_error_is_logged_and_shown_not_swallowed(monkeypatch, caplog):
    # main() ends with os._exit, which discards an exception in flight. Before
    # the fix, a window or tray error ended the app with code 0, no message and
    # no log line, so a double-click seemed to do nothing.
    shown = []
    monkeypatch.setattr(desktop, "message_box", shown.append)

    class Broken:
        quitting = False

        def run(self):
            raise RuntimeError("the window library failed to load")

    with caplog.at_level(logging.ERROR, logger="agnview.desktop"):
        assert desktop.run_window(Broken()) == 1
    assert "the window library failed to load" in shown[0]
    assert "The AgnView window stopped" in caplog.text


def test_a_normal_quit_exits_with_zero(monkeypatch):
    shown = []
    monkeypatch.setattr(desktop, "message_box", shown.append)

    class Quits:
        quitting = True

        def run(self):
            pass

    assert desktop.run_window(Quits()) == 0
    assert shown == []


def test_an_older_copy_that_will_not_close_is_reported_not_ignored(monkeypatch):
    class Kernel:
        signalled = False

        def CreateMutexW(self, *args):
            return 1

        def GetLastError(self):
            return desktop.ERROR_ALREADY_EXISTS

        def OpenEventW(self, *args):
            self.signalled = True
            return 0

    kernel = Kernel()
    shown = []
    monkeypatch.setattr(desktop.ctypes, "windll", types.SimpleNamespace(kernel32=kernel), raising=False)
    monkeypatch.setattr(desktop, "read_instance_record", lambda: {"pid": 1, "version": "0.1.8"})
    monkeypatch.setattr(desktop, "current_version", lambda: "0.1.12")
    monkeypatch.setattr(desktop, "end_process", lambda pid: False)
    monkeypatch.setattr(desktop, "message_box", shown.append)

    assert desktop.claim_single_instance() is False
    assert "could not be closed" in shown[0]


# --- The download mark ----------------------------------------------------------------
#
# A zip downloaded in a browser leaves a Zone.Identifier stream on every
# extracted file, and the .NET runtime then refuses to load pythonnet. The
# app deletes the streams from its own folder before it opens the window.

needs_ntfs = pytest.mark.skipif(sys.platform != "win32", reason="NTFS alternate data streams")
STREAM = ":Zone.Identifier"


def _mark(path):
    with open(str(path) + STREAM, "w", encoding="ascii") as handle:
        handle.write("[ZoneTransfer]\r\nZoneId=3\r\n")


def _is_marked(path):
    return os.path.exists(str(path) + STREAM)


def _make_install(root):
    """A small stand-in for the AgnView folder, every file marked."""
    files = [
        root / "AgnView.exe",
        root / "_internal" / "base_library.zip",
        root / "_internal" / "pythonnet" / "runtime" / "Python.Runtime.dll",
        root / "_internal" / "webview" / "lib" / "Microsoft.Web.WebView2.Core.dll",
        root / "_internal" / "deep" / "er" / "module.pyd",
    ]
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"content of " + path.name.encode())
        _mark(path)
    return files


@needs_ntfs
def test_the_mark_is_removed_from_every_file_and_the_files_are_unchanged(tmp_path):
    files = _make_install(tmp_path)
    assert all(_is_marked(path) for path in files)

    assert desktop.clear_download_mark(tmp_path) == len(files)

    for path in files:
        assert not _is_marked(path)
        assert path.read_bytes() == b"content of " + path.name.encode()


@needs_ntfs
def test_files_without_the_mark_are_fine(tmp_path):
    files = _make_install(tmp_path)
    plain = tmp_path / "plain.txt"
    plain.write_text("no mark here")

    assert desktop.clear_download_mark(tmp_path) == len(files)
    assert desktop.clear_download_mark(tmp_path) == 0
    assert plain.read_text() == "no mark here"


@needs_ntfs
def test_the_walk_stays_inside_the_folder(tmp_path):
    install = tmp_path / "AgnView"
    _make_install(install)
    neighbour = tmp_path / "Other" / "notes.txt"
    neighbour.parent.mkdir()
    neighbour.write_text("keep my mark")
    _mark(neighbour)

    desktop.clear_download_mark(install)

    assert _is_marked(neighbour)


@needs_ntfs
def test_a_symlink_is_not_followed(tmp_path):
    install = tmp_path / "AgnView"
    _make_install(install)
    outside = tmp_path / "Outside"
    outside.mkdir()
    target = outside / "file.txt"
    target.write_text("outside")
    _mark(target)
    try:
        os.symlink(outside, install / "link", target_is_directory=True)
        os.symlink(target, install / "filelink.txt")
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symlinks")

    desktop.clear_download_mark(install)

    assert _is_marked(target)


@needs_ntfs
def test_a_junction_is_not_followed(tmp_path):
    import subprocess

    install = tmp_path / "AgnView"
    _make_install(install)
    outside = tmp_path / "Outside"
    outside.mkdir()
    target = outside / "file.txt"
    target.write_text("outside")
    _mark(target)
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(install / "junction"), str(outside)],
        capture_output=True,
        check=False,
    )
    if made.returncode != 0:
        pytest.skip("could not create a junction")

    desktop.clear_download_mark(install)

    assert _is_marked(target)


def test_a_refused_removal_is_ignored(tmp_path, monkeypatch):
    real_remove = os.remove
    refused = []

    def remove(path, *args, **kwargs):
        if str(path).endswith(STREAM):
            refused.append(path)
            raise PermissionError("Access is denied.")
        return real_remove(path, *args, **kwargs)

    (tmp_path / "sub").mkdir()
    (tmp_path / "a.dll").write_bytes(b"a")
    (tmp_path / "sub" / "b.dll").write_bytes(b"b")
    monkeypatch.setattr(desktop.os, "remove", remove)

    assert desktop.clear_download_mark(tmp_path) == 0
    assert len(refused) == 2


def test_a_missing_folder_is_ignored(tmp_path):
    assert desktop.clear_download_mark(tmp_path / "gone") == 0


def test_logs_name_the_install_folder_only(tmp_path, monkeypatch, caplog):
    real_remove = os.remove

    def remove(path, *args, **kwargs):
        if str(path).endswith(STREAM):
            raise PermissionError("Access is denied.")
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(desktop.os, "remove", remove)
    folder = tmp_path / "AgnView"
    (folder / "inner").mkdir(parents=True)
    (folder / "inner" / "secret-name.dll").write_bytes(b"x")

    with caplog.at_level(logging.DEBUG, logger="agnview.desktop"):
        desktop.clear_download_mark(folder)

    assert str(tmp_path) not in caplog.text
    assert "secret-name" not in caplog.text


class _Install:
    """Records what the start-up clean-up did, without touching a disk."""

    def __init__(self, monkeypatch, tmp_path, marked=()):
        self.walks = []
        self.marked = set(marked)
        self.folder = tmp_path / "AgnView"
        library = self.folder / "_internal" / "webview" / "lib"
        library.mkdir(parents=True)
        (library / "Microsoft.Web.WebView2.Core.dll").write_bytes(b"x")
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(self.folder / "AgnView.exe"))
        monkeypatch.setattr(desktop, "MOTW_MARKER_PATH", tmp_path / "note.json")
        monkeypatch.setattr(desktop, "current_version", lambda: "1.2.3")
        monkeypatch.setattr(desktop, "_has_download_mark", lambda path: Path(path).name in self.marked)
        monkeypatch.setattr(desktop, "clear_download_mark", lambda folder: self.walks.append(folder) or 3)


def test_the_first_start_walks_the_folder_once(monkeypatch, tmp_path):
    install = _Install(monkeypatch, tmp_path)

    assert desktop.clear_own_download_mark() == 3
    assert desktop.clear_own_download_mark() == 0
    assert install.walks == [install.folder]


def test_a_new_version_walks_again(monkeypatch, tmp_path):
    install = _Install(monkeypatch, tmp_path)
    desktop.clear_own_download_mark()
    monkeypatch.setattr(desktop, "current_version", lambda: "1.2.4")

    desktop.clear_own_download_mark()

    assert len(install.walks) == 2


def test_a_moved_folder_walks_again(monkeypatch, tmp_path):
    install = _Install(monkeypatch, tmp_path)
    desktop.clear_own_download_mark()
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Moved" / "AgnView.exe"))

    desktop.clear_own_download_mark()

    assert len(install.walks) == 2


@pytest.mark.parametrize("name", ["Python.Runtime.dll", "Microsoft.Web.WebView2.Core.dll"])
def test_a_marked_decisive_file_walks_again(monkeypatch, tmp_path, name):
    # Extracting a fresh zip over the folder brings the mark back.
    install = _Install(monkeypatch, tmp_path)
    desktop.clear_own_download_mark()
    install.marked.add(name)

    desktop.clear_own_download_mark()

    assert len(install.walks) == 2


def test_a_marked_exe_alone_does_not_trigger_a_walk(monkeypatch, tmp_path):
    install = _Install(monkeypatch, tmp_path)
    desktop.clear_own_download_mark()
    install.marked.add("AgnView.exe")

    desktop.clear_own_download_mark()

    assert len(install.walks) == 1


def test_a_forced_clean_ignores_the_saved_note(monkeypatch, tmp_path):
    install = _Install(monkeypatch, tmp_path)
    desktop.clear_own_download_mark()

    desktop.clear_own_download_mark(force=True)

    assert len(install.walks) == 2


def test_a_source_run_and_other_systems_are_left_alone(monkeypatch, tmp_path):
    install = _Install(monkeypatch, tmp_path)
    monkeypatch.delattr(sys, "frozen")
    assert desktop.clear_own_download_mark() == 0
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    assert desktop.clear_own_download_mark() == 0
    assert install.walks == []


def test_a_failing_clean_up_never_stops_the_start(monkeypatch, tmp_path):
    _Install(monkeypatch, tmp_path)

    def boom(folder):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(desktop, "clear_download_mark", boom)

    assert desktop.clear_own_download_mark() == 0


@needs_ntfs
def test_the_real_clean_up_on_a_marked_install(monkeypatch, tmp_path):
    install = tmp_path / "AgnView"
    files = _make_install(install)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(install / "AgnView.exe"))
    monkeypatch.setattr(desktop, "MOTW_MARKER_PATH", tmp_path / "note.json")
    monkeypatch.setattr(desktop, "current_version", lambda: "1.2.3")

    assert desktop.clear_own_download_mark() == len(files)
    assert not any(_is_marked(path) for path in files)
    assert desktop.MOTW_MARKER_PATH.exists()


def test_the_clean_up_runs_before_a_newer_copy_takes_over(monkeypatch):
    order = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(desktop, "configure_logging", lambda: order.append("logging"))
    monkeypatch.setattr(desktop, "clear_own_download_mark", lambda force=False: order.append("clean") or 0)
    monkeypatch.setattr(desktop, "claim_single_instance", lambda: order.append("claim") or False)

    assert desktop.main([]) == 0
    assert order == ["logging", "clean", "claim"]


# --- The window survives one loader failure -------------------------------------------

LOADER_ERROR = "Failed to resolve Python.Runtime.Loader.Initialize from the install folder"


def _fake_webview(monkeypatch, failures):
    """A stand-in webview module whose start fails for the first failures calls."""
    calls = {"created": 0, "started": 0}

    class Closing:
        def __iadd__(self, handler):
            return self

    def create_window(*args, **kwargs):
        calls["created"] += 1
        return types.SimpleNamespace(events=types.SimpleNamespace(closing=Closing()))

    def start(**kwargs):
        calls["started"] += 1
        if calls["started"] <= failures:
            raise RuntimeError(LOADER_ERROR)

    module = types.SimpleNamespace(windows=[object()], create_window=create_window, start=start)
    monkeypatch.setitem(sys.modules, "webview", module)
    return calls, module


def _desktop_app(monkeypatch):
    monkeypatch.setattr(desktop, "watch_for_show_requests", lambda callback: None)
    app = desktop.DesktopApp(hub=types.SimpleNamespace(url="http://127.0.0.1:1"), settings={}, start_hidden=True)
    app.start_tray = lambda: None
    return app


def test_one_loader_failure_is_cleaned_and_retried(monkeypatch):
    calls, module = _fake_webview(monkeypatch, failures=1)
    forced = []
    monkeypatch.setattr(desktop, "clear_own_download_mark", lambda force=False: forced.append(force) or 0)

    _desktop_app(monkeypatch).run()

    assert forced == [True]
    assert calls == {"created": 2, "started": 2}
    assert module.windows == []


def test_a_second_loader_failure_reaches_the_person(monkeypatch):
    calls, _module = _fake_webview(monkeypatch, failures=2)
    monkeypatch.setattr(desktop, "clear_own_download_mark", lambda force=False: 0)

    with pytest.raises(RuntimeError, match="Python.Runtime"):
        _desktop_app(monkeypatch).run()

    assert calls["started"] == 2


def test_another_window_error_is_not_retried(monkeypatch):
    calls, module = _fake_webview(monkeypatch, failures=0)

    def start(**kwargs):
        calls["started"] += 1
        raise RuntimeError("WebView2 is not installed")

    module.start = start
    forced = []
    monkeypatch.setattr(desktop, "clear_own_download_mark", lambda force=False: forced.append(force) or 0)

    with pytest.raises(RuntimeError, match="WebView2"):
        _desktop_app(monkeypatch).run()

    assert calls["started"] == 1
    assert forced == []


def test_the_loader_error_dialog_gives_the_unblock_command(monkeypatch):
    shown = []
    monkeypatch.setattr(desktop, "message_box", shown.append)
    monkeypatch.setattr(desktop, "install_folder", lambda: Path("C:/Apps/O'Neil Apps/AgnView"))

    class Broken:
        quitting = False

        def run(self):
            raise RuntimeError(LOADER_ERROR)

    assert desktop.run_window(Broken()) == 1
    text = shown[0]
    assert LOADER_ERROR in text
    assert "Get-ChildItem -LiteralPath 'C:" in text
    assert "O''Neil Apps" in text
    assert text.count("Unblock-File") == 1
    assert f"Log: {desktop.LOG_PATH}" in text


def test_a_blocked_webview_library_counts_as_a_loader_error():
    blocked = RuntimeError("System.NotSupportedException: An attempt was made to load an assembly from a network location")
    assert desktop.is_loader_error(blocked)
    assert not desktop.is_loader_error(RuntimeError("WebView2 is not installed"))


def test_the_unblock_command_quotes_the_path():
    command = desktop.unblock_command(Path("C:/Apps/It's here/AgnView"))
    assert command.startswith("Get-ChildItem -LiteralPath '")
    assert "It''s here" in command
    assert command.endswith("' -Recurse | Unblock-File")
