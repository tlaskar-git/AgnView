"""Functional tests for AgentRelay CLI."""

import os
import sys
import subprocess


def test_cli_help():
    result = subprocess.run(
        [sys.executable, "-m", "agent_relay.cli.main", "--help"],
        capture_output=True,
        text=True
    )
    assert result.returncode == 0
    assert ("agnview" in result.stdout or "agent-relay" in result.stdout)
    assert "create-job" in result.stdout
    assert "wait" in result.stdout
    assert "reject" in result.stdout


def test_cli_lifecycle(tmp_path, monkeypatch):
    db_file = str(tmp_path / "cli_test.db")
    monkeypatch.setenv("AGENT_RELAY_DB_PATH", db_file)

    # 1. Create Job from YAML
    yaml_file = tmp_path / "job.yaml"
    yaml_file.write_text("""
id: job-cli-test
title: CLI Test
tasks:
  - id: t1
    title: T1
    assigned_agent: codex
    dependencies: []
  - id: t2
    title: T2
    assigned_agent: claude_code
    dependencies: [t1]
""", encoding="utf-8")

    env = {**os.environ, "AGENT_RELAY_DB_PATH": db_file}

    r_create = subprocess.run(
        [sys.executable, "-m", "agent_relay.cli.main", "create-job", str(yaml_file)],
        capture_output=True,
        text=True,
        env=env
    )
    assert r_create.returncode == 0
    assert "Job Created Successfully" in r_create.stdout

    # 2. Status
    r_status = subprocess.run(
        [sys.executable, "-m", "agent_relay.cli.main", "status", "job-cli-test"],
        capture_output=True,
        text=True,
        env=env
    )
    assert r_status.returncode == 0
    assert "t1" in r_status.stdout
    assert "t2" in r_status.stdout

    # 3. Claim and complete t1
    r_claim = subprocess.run(
        [sys.executable, "-m", "agent_relay.cli.main", "claim", "t1", "--agent", "codex"],
        capture_output=True,
        text=True,
        env=env
    )
    assert r_claim.returncode == 0

    r_comp = subprocess.run(
        [sys.executable, "-m", "agent_relay.cli.main", "complete", "t1", "--summary", "Finished T1"],
        capture_output=True,
        text=True,
        env=env
    )
    assert r_comp.returncode == 0

    # 4. Wait on t2 (should immediately return 0 since t1 is complete)
    r_wait = subprocess.run(
        [sys.executable, "-m", "agent_relay.cli.main", "wait", "t2", "--timeout", "5"],
        capture_output=True,
        text=True,
        env=env
    )
    assert r_wait.returncode == 0
    assert "READY" in r_wait.stdout

    # 5. Reject t1 (request revision)
    r_rej = subprocess.run(
        [sys.executable, "-m", "agent_relay.cli.main", "reject", "t1", "--feedback", "Please fix bug", "--by", "claude_code"],
        capture_output=True,
        text=True,
        env=env
    )
    assert r_rej.returncode == 0
    assert "Revision Requested" in r_rej.stdout


def test_cmd_serve_passes_actual_port_to_app(tmp_path, monkeypatch):
    """Regression test: cmd_serve must build the FastAPI app with the port it was
    actually started on. It previously always fell back to create_app's default of
    8765, because the standard (non dual-stack) path handed uvicorn.run an unbound
    factory string, which cannot receive arguments, instead of constructing the app
    directly the way the dual-stack 0.0.0.0 branch already did."""
    monkeypatch.setattr("agent_relay.core.db.DEFAULT_DB_PATH", str(tmp_path / "serve_port_test.db"))

    captured = {}

    def fake_run(app, **kwargs):
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr("uvicorn.run", fake_run)

    from argparse import Namespace
    from agent_relay.cli.main import cmd_serve

    args = Namespace(host=None, port=8845, listen_lan=False, listen_tailscale=False, token="test-token")
    cmd_serve(args)

    assert captured["kwargs"].get("port") == 8845
    assert captured["app"].state.engine.port == 8845
