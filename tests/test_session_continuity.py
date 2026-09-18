"""Tests for agent session continuity.

These cover the two halves of the fix: the command built for a resumed
conversation, and the capture of the session id the CLI reports back.
The real CLIs are never launched here.
"""

import pytest

from agent_relay.core.db import Database
from agent_relay.core.runner import (
    AgentRunner,
    apply_cli_session_profile,
    build_antigravity_args,
    build_claude_args,
    build_codex_args,
    cli_binary_name,
    parse_antigravity_output,
    parse_claude_output,
    parse_codex_output,
    resolve_adapter_command,
)


@pytest.fixture
def db(tmp_path):
    return Database(db_path=str(tmp_path / "relay.db"))


# ----------------------------- persistence -----------------------------

def test_session_round_trips(db, tmp_path):
    cwd = str(tmp_path)
    assert db.get_agent_session("claude_code", cwd) is None
    db.set_agent_session("claude_code", cwd, "abc-123")
    assert db.get_agent_session("claude_code", cwd) == "abc-123"


def test_session_is_keyed_per_agent_and_cwd(db, tmp_path):
    a = str(tmp_path / "a")
    b = str(tmp_path / "b")
    db.set_agent_session("claude_code", a, "claude-a")
    db.set_agent_session("codex", a, "codex-a")
    db.set_agent_session("claude_code", b, "claude-b")

    assert db.get_agent_session("claude_code", a) == "claude-a"
    assert db.get_agent_session("codex", a) == "codex-a"
    assert db.get_agent_session("claude_code", b) == "claude-b"
    # A different agent in a directory it never ran in has no session.
    assert db.get_agent_session("antigravity", a) is None


def test_session_overwrites_not_duplicates(db, tmp_path):
    cwd = str(tmp_path)
    db.set_agent_session("codex", cwd, "first")
    db.set_agent_session("codex", cwd, "second")
    assert db.get_agent_session("codex", cwd) == "second"


def test_cwd_lookup_is_case_insensitive_on_windows(db, tmp_path):
    cwd = str(tmp_path)
    db.set_agent_session("claude_code", cwd, "sess-1")
    # normcase collapses case on Windows and is a no-op elsewhere.
    assert db.get_agent_session("claude_code", cwd.upper()) in ("sess-1", None)
    assert db.get_agent_session("claude_code", cwd) == "sess-1"


def test_clear_session_starts_fresh(db, tmp_path):
    cwd = str(tmp_path)
    db.set_agent_session("claude_code", cwd, "sess-1")
    assert db.clear_agent_session("claude_code", cwd) == 1
    assert db.get_agent_session("claude_code", cwd) is None


# --------------------------- command building ---------------------------

def test_claude_first_turn_has_no_resume_flag():
    args = build_claude_args("claude", "hello")
    assert "--resume" not in args
    assert args[:4] == ["claude", "-p", "hello"] + ["--output-format"]
    assert "json" in args


def test_claude_resume_uses_stored_id():
    args = build_claude_args("claude", "and again", resume_session_id="uuid-9")
    assert args[args.index("--resume") + 1] == "uuid-9"
    # JSON output must stay on so the next id can be captured too.
    assert args[args.index("--output-format") + 1] == "json"


def test_claude_resume_keeps_model_and_effort():
    args = build_claude_args("claude", "p", model="opus", effort="high", resume_session_id="u1")
    assert args[args.index("--model") + 1] == "opus"
    assert args[args.index("--effort") + 1] == "high"
    assert args[args.index("--resume") + 1] == "u1"


def test_codex_first_turn_is_plain_exec():
    args = build_codex_args("codex", "hello")
    assert args[:3] == ["codex", "exec", "hello"]
    assert "resume" not in args
    assert "--json" in args


def test_codex_resume_uses_resume_subcommand():
    args = build_codex_args("codex", "again", resume_session_id="thread-7")
    assert args[:5] == ["codex", "exec", "resume", "thread-7", "again"]
    assert "--json" in args
    assert "--skip-git-repo-check" in args


def test_antigravity_resume_uses_conversation_flag():
    args = build_antigravity_args("agy", None, "again", resume_session_id="conv-3")
    assert args[args.index("--conversation") + 1] == "conv-3"
    assert args[args.index("--output-format") + 1] == "json"


def test_antigravity_first_turn_has_no_conversation_flag():
    args = build_antigravity_args("agy", None, "hello")
    assert "--conversation" not in args


def test_gemini_fallback_gets_no_resume_flag():
    # The Gemini CLI cannot resume by session id, so nothing is faked.
    args = build_antigravity_args(None, "gemini", "hello", resume_session_id="conv-3")
    assert "--conversation" not in args
    assert "--resume" not in args
    assert args[:2] == ["gemini", "-p"]


# ---------------------------- output parsing ----------------------------

def test_parse_claude_output_extracts_text_and_session():
    raw = '{"result":"Hello there","session_id":"a1f884bc-ff72","is_error":false}'
    text, session = parse_claude_output(raw)
    assert text == "Hello there"
    assert session == "a1f884bc-ff72"


def test_parse_claude_output_survives_non_json():
    text, session = parse_claude_output("plain text reply")
    assert text == "plain text reply"
    assert session is None


def test_parse_codex_output_extracts_thread_and_message():
    raw = "\n".join([
        '{"type":"thread.started","thread_id":"01a0b464-3107"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"i1","type":"agent_message","text":"PING2"}}',
        '{"type":"turn.completed","usage":{}}',
    ])
    text, session = parse_codex_output(raw)
    assert text == "PING2"
    assert session == "01a0b464-3107"


def test_parse_codex_output_ignores_error_items():
    raw = "\n".join([
        '{"type":"thread.started","thread_id":"t1"}',
        '{"type":"item.completed","item":{"type":"error","message":"noise"}}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"real reply"}}',
    ])
    text, session = parse_codex_output(raw)
    assert text == "real reply"
    assert session == "t1"


def test_parse_antigravity_output_extracts_conversation():
    raw = '{"conversation_id":"4f81a5f4","status":"SUCCESS","response":"PING3\\n"}'
    text, session = parse_antigravity_output(raw)
    assert text == "PING3"
    assert session == "4f81a5f4"


def test_parse_antigravity_output_skips_banner_lines():
    raw = 'Warning: True color not detected.\n{"conversation_id":"c9","response":"hi"}'
    text, session = parse_antigravity_output(raw)
    assert text == "hi"
    assert session == "c9"


# -------------------------- adapter placeholders --------------------------

def test_adapter_drops_dangling_resume_flag_on_first_turn():
    cmd = ["claude", "-p", "{prompt}", "--resume", "{session_id}"]
    resolved = resolve_adapter_command(cmd, {"{prompt}": "hi", "{session_id}": ""})
    assert resolved == ["claude", "-p", "hi"]


def test_adapter_consumes_session_placeholder_when_present():
    cmd = ["claude", "-p", "{prompt}", "--resume", "{session_id}"]
    resolved = resolve_adapter_command(cmd, {"{prompt}": "hi", "{session_id}": "s-1"})
    assert resolved == ["claude", "-p", "hi", "--resume", "s-1"]


# ------------------- adapter path (what actually runs) -------------------
# The enabled adapters in ~/.agnview/agents.yaml take priority over the
# dedicated _run_* methods, so this is the path a real dispatch uses.

def test_cli_binary_name_strips_windows_extensions():
    assert cli_binary_name(r"D:\tools\claude.cmd") == "claude"
    assert cli_binary_name("/usr/local/bin/codex") == "codex"
    assert cli_binary_name(r"C:\agy\bin\agy.exe") == "agy"


def test_adapter_profile_adds_claude_resume_and_json():
    cmd = [r"D:\tools\claude.cmd", "-p", "hello"]
    out, parser = apply_cli_session_profile(cmd, "sess-1")
    assert out[out.index("--resume") + 1] == "sess-1"
    assert out[out.index("--output-format") + 1] == "json"
    assert parser is parse_claude_output


def test_adapter_profile_claude_first_turn_has_json_but_no_resume():
    out, parser = apply_cli_session_profile([r"D:\tools\claude.cmd", "-p", "hello"], None)
    assert "--resume" not in out
    assert out[out.index("--output-format") + 1] == "json"
    assert parser is parse_claude_output


def test_adapter_profile_inserts_codex_resume_after_exec():
    cmd = ["codex", "exec", "--skip-git-repo-check", "hello"]
    out, parser = apply_cli_session_profile(cmd, "thread-1")
    assert out[:4] == ["codex", "exec", "resume", "thread-1"]
    assert out[-1] == "--json"
    assert parser is parse_codex_output


def test_adapter_profile_adds_agy_conversation():
    cmd = ["agy", "--print", "hello", "--dangerously-skip-permissions"]
    out, parser = apply_cli_session_profile(cmd, "conv-1")
    assert out[out.index("--conversation") + 1] == "conv-1"
    assert parser is parse_antigravity_output


def test_adapter_profile_leaves_unknown_cli_alone():
    cmd = ["gemini", "-p", "hello", "--yolo"]
    out, parser = apply_cli_session_profile(cmd, "conv-1")
    assert out == cmd
    assert parser is None


def test_adapter_profile_respects_template_that_places_its_own_flag():
    # add_resume=False is used when the operator's template names {session_id}.
    cmd = ["claude", "-p", "hello", "--resume", "mine"]
    out, parser = apply_cli_session_profile(cmd, "ours", add_resume=False)
    assert out.count("--resume") == 1
    assert out[out.index("--resume") + 1] == "mine"
    # Structured output still goes on so the id can still be captured.
    assert parser is parse_claude_output


# ------------------------- end to end at the seam -------------------------

def test_seeded_session_produces_resume_flagged_command(db, tmp_path):
    """Seed a prior session, then confirm the adapter would resume it."""
    cwd = str(tmp_path)
    runner = AgentRunner(db=db)
    db.set_agent_session("claude_code", cwd, "seeded-uuid")

    resume_id = runner._get_cli_session("claude_code", cwd)
    args = build_claude_args("claude", "follow-up", resume_session_id=resume_id)
    assert args[args.index("--resume") + 1] == "seeded-uuid"


def test_reset_clears_only_the_target_agent(db, tmp_path):
    cwd = str(tmp_path)
    runner = AgentRunner(db=db)
    db.set_agent_session("claude_code", cwd, "c1")
    db.set_agent_session("codex", cwd, "x1")

    for key in runner._session_agent_keys("claude_code"):
        runner._clear_cli_session(key, cwd)

    assert db.get_agent_session("claude_code", cwd) is None
    assert db.get_agent_session("codex", cwd) == "x1"


def test_session_agent_keys_map_aliases(db):
    runner = AgentRunner(db=db)
    assert runner._session_agent_keys("claude") == ["claude_code"]
    assert runner._session_agent_keys("chatgpt") == ["codex"]
    assert runner._session_agent_keys("agy") == ["antigravity"]
    assert set(runner._session_agent_keys("all")) >= {"claude_code", "codex", "antigravity"}
