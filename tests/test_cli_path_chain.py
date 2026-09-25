"""The whole PATH chain for a Finder-launched macOS app, with no real shell.

Chain under test: repaired os.environ, then the detection function, then the
environment handed to a spawned agent. Every home directory is a temporary
placeholder.
"""

import os
import subprocess
import time
import types

import pytest

from agent_relay.core import cli_path

SEP = os.pathsep
MINIMAL = SEP.join(["/usr/bin", "/bin", "/usr/sbin", "/sbin"])


def _exe(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name + (".cmd" if os.name == "nt" else ""))
    path.write_text("echo hi\n")
    path.chmod(0o755)
    return path


def _out(path, bins=()):
    return (
        f"{cli_path.SENTINEL_START}{path}{cli_path.SENTINEL_END}"
        f"{cli_path.BINS_START}\n" + "\n".join(bins) + f"\n{cli_path.BINS_END}\n"
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(cli_path, "_last_repair_at", None)
    monkeypatch.setattr(cli_path, "LAST_REPORT", cli_path.RepairReport())
    monkeypatch.setattr(cli_path, "system_path_dirs", lambda etc=None: [])


def test_shell_candidates_without_shell_variable_use_the_account_shell():
    got = cli_path.shell_candidates({}, account_shell="/bin/zsh")
    assert got == ["/bin/zsh", "/bin/bash"]
    got = cli_path.shell_candidates({"SHELL": "/usr/local/bin/fish"}, account_shell="/bin/zsh")
    assert got == ["/usr/local/bin/fish", "/bin/zsh", "/bin/bash"]
    # Nothing known at all still leaves the two system shells.
    assert cli_path.shell_candidates({}, account_shell="") == ["/bin/zsh", "/bin/bash"]


def test_noise_after_the_path_and_a_duplicate_entry_are_tolerated():
    printed = (
        "banner\n"
        + _out(SEP.join(["/h/.local/bin", "/usr/local/bin", "/usr/bin", "/h/.local/bin"]))
        + "Saving session...\n...copying shared history...\n...saving history...truncating history files...\n...completed.\n"
    )
    assert cli_path.parse_shell_path(printed) == ["/h/.local/bin", "/usr/local/bin", "/usr/bin", "/h/.local/bin"]
    merged = cli_path.merge_paths(MINIMAL, cli_path.parse_shell_path(printed))
    assert merged.split(SEP).count("/h/.local/bin") == 1


def test_bins_block_yields_directories_and_ignores_aliases_and_noise():
    printed = _out("/x", ["/opt/tool/bin/claude", "claude: aliased to something", "/opt/tool/bin/codex", "", "/z/agy"])
    assert cli_path.parse_shell_bins(printed) == ["/opt/tool/bin", "/z"]
    assert cli_path.parse_shell_bins("no markers") == []


def test_login_only_and_interactive_results_are_merged():
    def run(cmd, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL
        path = "/from/profile" if "-i" not in cmd else "/from/zshrc"
        return types.SimpleNamespace(stdout=_out(path))

    probe = cli_path.probe_login_shell(["/bin/zsh"], run)
    assert probe.ok and probe.shell == "/bin/zsh"
    assert set(probe.path) == {"/from/profile", "/from/zshrc"}
    assert probe.attempts == 2


def test_a_shell_that_never_returns_cannot_hang_the_start(monkeypatch):
    monkeypatch.setattr(cli_path, "SHELL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(cli_path, "HANG_GRACE_SECONDS", 0.05)

    def run(cmd, **kwargs):
        time.sleep(1.0)
        return types.SimpleNamespace(stdout="")

    started = time.monotonic()
    probe = cli_path.probe_login_shell(["/bin/zsh"], run, budget=1.0)
    assert time.monotonic() - started < 0.9
    assert not probe.ok
    assert probe.failures and "TimeoutExpired" in probe.failures[0]


def test_failures_are_recorded_not_raised():
    def run(cmd, **kwargs):
        raise OSError("no shell")

    probe = cli_path.probe_login_shell(["/bin/zsh", "/bin/bash"], run)
    assert not probe.ok and probe.path == []
    assert len(probe.failures) == 4


def test_shell_reported_cli_folder_is_added_even_when_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", MINIMAL)
    run = lambda cmd, **kw: types.SimpleNamespace(stdout=_out("/usr/bin", ["/opt/odd/place/agnview-odd-cli"]))  # noqa: E731
    cli_path.repair_environment(tmp_path, ["/bin/zsh"], run)
    assert "/opt/odd/place" in os.environ["PATH"].split(SEP)


def test_chain_repair_then_detection_then_spawn_env(tmp_path, monkeypatch):
    """The failure seen on a real Mac: a CLI in ~/.local/bin, a minimal PATH, a login shell that fails."""
    _exe(tmp_path / ".local" / "bin", "agnview-fake-agent")
    monkeypatch.setenv("PATH", MINIMAL)
    assert cli_path.which_any("agnview-fake-agent", platform="linux") is None

    def dead_shell(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 10)

    cli_path.repair_environment(tmp_path, ["/bin/zsh"], dead_shell)
    assert cli_path.LAST_REPORT.ran and not cli_path.LAST_REPORT.shell_ok
    assert str(tmp_path / ".local" / "bin") in os.environ["PATH"].split(SEP)

    found = cli_path.which_any("agnview-fake-agent", home=tmp_path, platform="darwin", repair=lambda: None)
    assert found and found.startswith(str(tmp_path))

    env = cli_path.child_env({"PATH": MINIMAL}, platform="darwin", home=tmp_path)
    assert str(tmp_path / ".local" / "bin") in env["PATH"].split(SEP)


def test_lookup_recovers_when_the_start_repair_never_ran(tmp_path, monkeypatch):
    _exe(tmp_path / ".local" / "bin", "agnview-fake-agent")
    monkeypatch.setenv("PATH", MINIMAL)
    calls = []
    found = cli_path.which_any(
        "agnview-fake-agent", home=tmp_path, platform="darwin", repair=lambda: calls.append(1)
    )
    # The repair hook did nothing, so only the direct folder search can find it.
    assert calls == [1]
    assert found and found.startswith(str(tmp_path))


def test_a_repeated_miss_does_not_repair_again_within_the_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", MINIMAL)
    calls = []
    for _ in range(3):
        assert cli_path.which_any("agnview-absent", home=tmp_path, platform="darwin", repair=lambda: calls.append(1)) is None
        monkeypatch.setattr(cli_path, "_last_repair_at", time.monotonic())
    assert len(calls) == 1 or calls == []


def test_other_platforms_behave_like_shutil_which(tmp_path, monkeypatch):
    _exe(tmp_path / ".local" / "bin", "agnview-fake-agent")
    monkeypatch.setenv("PATH", MINIMAL)
    calls = []
    assert cli_path.which_any("agnview-fake-agent", home=tmp_path, platform="linux", repair=lambda: calls.append(1)) is None
    assert cli_path.which_any("agnview-fake-agent", home=tmp_path, platform="win32", repair=lambda: calls.append(1)) is None
    assert calls == []
    env = cli_path.child_env({"PATH": MINIMAL}, platform="linux", home=tmp_path)
    assert env["PATH"] == MINIMAL


def test_summary_line_hides_the_home_directory(tmp_path):
    report = cli_path.RepairReport(
        ran=True, shell=str(tmp_path / "bin" / "zsh"), shell_ok=True, attempts=2, added=3,
        path=SEP.join([str(tmp_path / ".local" / "bin"), "/usr/bin"]),
    )
    line = report.summary(tmp_path)
    assert str(tmp_path) not in line
    assert "shell_ok=True" in line and "added=3" in line and "~" in line
    assert "did not run" in cli_path.RepairReport().summary(tmp_path)


def test_repair_logs_one_line_with_tilde(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("PATH", MINIMAL)
    (tmp_path / ".bun" / "bin").mkdir(parents=True)
    run = lambda cmd, **kw: types.SimpleNamespace(stdout=_out("/usr/bin"))  # noqa: E731
    with caplog.at_level("INFO", logger="agnview.cli_path"):
        cli_path.repair_environment(tmp_path, ["/bin/zsh"], run)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("PATH repair:")]
    assert len(lines) == 1
    line = lines[0].replace("\\", "/")
    assert str(tmp_path).replace("\\", "/") not in line and "~/.bun/bin" in line


def test_missing_note_lists_directories_and_the_shell_outcome(tmp_path, caplog):
    path = SEP.join([str(tmp_path / ".bun" / "bin"), "/usr/bin"])
    with caplog.at_level("WARNING", logger="agnview.cli_path"):
        note = cli_path.searched_note("darwin", tmp_path, path)
    assert "PATH repair did not run" in note
    assert str(tmp_path) not in note
    assert any("CLI not found" in r.getMessage() for r in caplog.records)


def test_diagnostics_shape_and_no_home_path(tmp_path, monkeypatch):
    _exe(tmp_path / ".local" / "bin", "claude")
    monkeypatch.setenv("PATH", SEP.join([str(tmp_path / ".local" / "bin"), "/usr/bin"]))
    data = cli_path.diagnostics(tmp_path, platform="linux")
    assert set(data["lookup"]) == set(cli_path.CLI_NAMES)
    assert data["lookup"]["claude"]["found"] is True
    assert data["lookup"]["claude"]["path"].startswith("~")
    assert data["lookup"]["codex"] == {"found": False, "path": None}
    assert data["path"][0] == "~/.local/bin"
    assert str(tmp_path) not in repr(data)


def test_diagnostics_endpoint_is_served(tmp_path):
    from fastapi.testclient import TestClient

    from agent_relay.api.app import create_app

    client = TestClient(create_app(db_path=str(tmp_path / "diag.db")))
    res = client.get("/api/diagnostics/path")
    assert res.status_code == 200
    body = res.json()
    assert {"path", "repair", "lookup", "version"} <= set(body)
    assert set(body["lookup"]) == set(cli_path.CLI_NAMES)
    assert os.path.expanduser("~") not in repr(body) or os.path.expanduser("~") in ("/", "\\")


def _run_missing(tmp_path, program, folder):
    import asyncio

    from agent_relay.core.db import Database
    from agent_relay.core.runner import AgentRunner

    db = Database(str(tmp_path / "chain.db"))
    runner = AgentRunner(db)
    asyncio.run(
        runner._run_cli_with_session(
            agent="claude_code",
            proc_args=[str(program)],
            cwd=str(folder),
            env=dict(os.environ),
            session_id="chain-sess",
            parser=None,
            fallback_prompt="hello",
        )
    )
    return " ".join(str(log["content"]) for log in db.get_console_logs(session_id="chain-sess"))


@pytest.mark.skipif(os.name == "nt", reason="Windows raises a different error for a missing folder")
def test_a_missing_working_folder_is_not_reported_as_not_installed(tmp_path):
    program = _exe(tmp_path / "bin", "agnview-real-program")
    text = _run_missing(tmp_path, program, tmp_path / "no-such-folder")
    assert "working folder does not exist" in text
    assert "not installed" not in text
