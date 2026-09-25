"""PATH repair for a macOS app started from Finder. No real shell, no GUI."""

import os
import subprocess
import types

from agent_relay.core import cli_path
from agent_relay.core.runner import missing_agent_message


def _fake_run(stdout="", exc=None):
    def run(cmd, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["timeout"] <= cli_path.SHELL_TIMEOUT_SECONDS
        assert cmd[1] in ("-l", "-i") and cmd[-2] == "-c"
        if exc:
            raise exc
        return types.SimpleNamespace(stdout=stdout)

    return run


def _wrap(path):
    return f"rc noise\n{cli_path.SENTINEL_START}{path}{cli_path.SENTINEL_END}\nmore noise"


def test_merge_keeps_existing_first_and_drops_duplicates():
    sep = os.pathsep
    merged = cli_path.merge_paths(sep.join(["/a", "/b"]), ["/b", "/c"], ["/a", "/d"])
    assert merged.split(sep) == ["/a", "/b", "/c", "/d"]


def test_parse_ignores_noise_and_missing_markers():
    sep = os.pathsep
    assert cli_path.parse_shell_path(_wrap(sep.join(["/x", "/y"]))) == ["/x", "/y"]
    assert cli_path.parse_shell_path("no markers") == []
    assert cli_path.parse_shell_path(cli_path.SENTINEL_START + "/x") == []
    assert cli_path.parse_shell_path("") == []


def test_login_shell_path_reads_the_shell_output():
    sep = os.pathsep
    got = cli_path.login_shell_path(["/bin/zsh"], _fake_run(_wrap(sep.join(["/one", "/two"]))))
    assert got == ["/one", "/two"]


def test_login_shell_path_falls_back_and_never_raises():
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd[0])
        if cmd[0] == "/bin/zsh":
            raise subprocess.TimeoutExpired(cmd, 5)
        return types.SimpleNamespace(stdout=_wrap("/from/bash"))

    assert cli_path.login_shell_path(["/bin/zsh", "/bin/bash"], run) == ["/from/bash"]
    assert calls == ["/bin/zsh", "/bin/zsh", "/bin/bash", "/bin/bash"]
    assert cli_path.login_shell_path(["/bin/zsh"], _fake_run(exc=OSError("no shell"))) == []


def test_known_dirs_only_lists_existing_and_picks_newest_nvm(tmp_path):
    for rel in (".local/bin", ".bun/bin", ".nvm/versions/node/v18.2.0/bin", ".nvm/versions/node/v20.11.1/bin", ".nvm/versions/node/v9.0.0/bin"):
        (tmp_path / rel).mkdir(parents=True)
    dirs = cli_path.known_dirs(tmp_path)
    assert str(tmp_path / ".local/bin") in dirs
    assert str(tmp_path / ".bun/bin") in dirs
    assert str(tmp_path / ".cargo/bin") not in dirs
    nvm = [d for d in dirs if ".nvm" in d]
    assert nvm[0] == str(tmp_path / ".nvm/versions/node/v20.11.1/bin")
    assert len(nvm) == 3


def test_repaired_path_lets_which_find_a_cli_outside_the_minimal_path(tmp_path):
    import shutil

    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / ("agnview-fake-cli.cmd" if os.name == "nt" else "agnview-fake-cli")
    exe.write_text("echo hi\n")
    exe.chmod(0o755)
    minimal = os.pathsep.join(["/usr/bin", "/bin"])
    assert shutil.which("agnview-fake-cli", path=minimal) is None
    fixed = cli_path.repaired_path(minimal, tmp_path, ["/bin/zsh"], _fake_run(""))
    assert fixed.startswith(minimal)
    assert shutil.which("agnview-fake-cli", path=fixed) is not None


def test_repair_environment_updates_os_environ(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    (tmp_path / ".bun" / "bin").mkdir(parents=True)
    result = cli_path.repair_environment(tmp_path, ["/bin/zsh"], _fake_run(_wrap("/shell/bin")))
    assert os.environ["PATH"] == result
    parts = result.split(os.pathsep)
    assert parts[0] == "/usr/bin"
    assert "/shell/bin" in parts
    assert str(tmp_path / ".bun" / "bin") in parts


def test_searched_dirs_show_home_as_tilde(tmp_path):
    path = os.pathsep.join([str(tmp_path / ".local" / "bin"), "/usr/bin"])
    assert cli_path.searched_dirs(tmp_path, path) == ["~/.local/bin", "/usr/bin"]


def test_message_lists_directories_on_macos_only(tmp_path):
    path = os.pathsep.join([str(tmp_path / ".bun" / "bin"), "/usr/bin"])
    mac = cli_path.searched_note("darwin", tmp_path, path)
    assert "~/.bun/bin" in mac and "/usr/bin" in mac
    assert "Terminal" in mac and "workaround" in mac
    assert str(tmp_path) not in mac
    assert cli_path.searched_note("win32", tmp_path, path) == ""
    assert cli_path.searched_note("linux", tmp_path, path) == ""


def test_missing_message_keeps_the_original_text_and_no_home_path(monkeypatch):
    message = missing_agent_message("claude_code")
    assert "not installed" in message and "restart AgnView" in message
    assert str(os.path.expanduser("~")) not in message or os.path.expanduser("~") == "/"
