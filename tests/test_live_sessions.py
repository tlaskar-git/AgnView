"""Tests for the live streaming session layer.

Every event body below was copied from the real CLIs running on a developer
machine, so these lock in the shapes AgnView actually parses.

What is covered here is the pure logic: NDJSON parsing, the turn-input
envelopes, the idle sweep rule, and the rewrite that turns a one-shot adapter
command into a long-lived streaming one. Spawning a real CLI, holding it open
across turns and killing it on shutdown are live checks, not unit tests.
"""

import asyncio
import json
import os
import time

from agent_relay.core.live_sessions import (
    LiveSession,
    StreamUpdate,
    encode_antigravity_turn,
    encode_claude_turn,
    find_idle_keys,
    live_session_key,
    parse_antigravity_stream_line,
    parse_claude_stream_line,
    parse_codex_stream_line,
)
from agent_relay.core.runner import (
    build_antigravity_live_args,
    build_claude_live_args,
    build_live_args_from_adapter,
)


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------

def test_claude_init_event_carries_the_session_id():
    line = json.dumps({"type": "system", "subtype": "init", "session_id": "118b6b19-a3db"})
    update = parse_claude_stream_line(line)
    assert update.kind == "session"
    assert update.session_id == "118b6b19-a3db"


def test_claude_partial_message_gives_a_text_delta():
    line = json.dumps({
        "type": "stream_event",
        "session_id": "s1",
        "event": {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "text_delta", "text": "Spring"},
        },
    })
    update = parse_claude_stream_line(line)
    assert update.kind == "delta"
    assert update.text == "Spring"


def test_claude_thinking_delta_is_not_reply_text():
    line = json.dumps({
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}},
    })
    assert parse_claude_stream_line(line).kind == "session"


def test_claude_assistant_message_is_a_snapshot_not_a_delta():
    line = json.dumps({
        "type": "assistant",
        "session_id": "s1",
        "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "..."},
            {"type": "text", "text": "alpha\nbeta"},
        ]},
    })
    update = parse_claude_stream_line(line)
    assert update.kind == "snapshot"
    assert update.text == "alpha\nbeta"


def test_claude_result_is_the_authoritative_final_text():
    line = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "session_id": "118b6b19-a3db", "result": "alpha\nbeta\ngamma",
    })
    update = parse_claude_stream_line(line)
    assert update.kind == "complete"
    assert update.text == "alpha\nbeta\ngamma"
    assert update.session_id == "118b6b19-a3db"
    assert update.is_error is False


def test_claude_error_result_is_flagged():
    line = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": ""})
    assert parse_claude_stream_line(line).is_error is True


def test_claude_turn_envelope_matches_the_transcript_format():
    payload = json.loads(encode_claude_turn("hello"))
    assert payload == {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    }


# --------------------------------------------------------------------------
# AntiGravity
# --------------------------------------------------------------------------

def test_antigravity_init_event_carries_the_conversation_id():
    line = json.dumps({"event": "init", "conversation_id": "e6647dc0", "init": {"cwd": "C:/tmp"}})
    update = parse_antigravity_stream_line(line)
    assert update.kind == "session"
    assert update.session_id == "e6647dc0"


def test_antigravity_step_update_gives_a_text_delta():
    line = json.dumps({"event": "step_update", "step_update": {
        "conversation_id": "e6647dc0", "step_index": 1, "state": "ACTIVE",
        "step_type": "agent_response", "text_delta": "OK",
    }})
    update = parse_antigravity_stream_line(line)
    assert update.kind == "delta"
    assert update.text == "OK"
    assert update.session_id == "e6647dc0"


def test_antigravity_user_input_step_is_not_reply_text():
    line = json.dumps({"event": "step_update", "step_update": {
        "conversation_id": "e6647dc0", "step_index": 0, "state": "DONE", "step_type": "user_input",
    }})
    assert parse_antigravity_stream_line(line).kind == "session"


def test_antigravity_result_is_the_authoritative_final_text():
    line = json.dumps({"event": "result", "result": {
        "conversation_id": "e6647dc0", "status": "SUCCESS", "response": "OK\n", "num_turns": 1,
    }})
    update = parse_antigravity_stream_line(line)
    assert update.kind == "complete"
    assert update.text == "OK\n"
    assert update.session_id == "e6647dc0"
    assert update.is_error is False


def test_antigravity_failed_result_is_flagged():
    line = json.dumps({"event": "result", "result": {"status": "ERROR", "response": ""}})
    assert parse_antigravity_stream_line(line).is_error is True


def test_antigravity_turn_envelope_uses_the_event_key():
    payload = json.loads(encode_antigravity_turn("hello"))
    assert payload == {
        "event": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    }


# --------------------------------------------------------------------------
# Codex
# --------------------------------------------------------------------------

def test_codex_thread_started_carries_the_thread_id():
    line = json.dumps({"type": "thread.started", "thread_id": "01a0b571"})
    update = parse_codex_stream_line(line)
    assert update.kind == "session"
    assert update.session_id == "01a0b571"


def test_codex_agent_message_is_a_delta():
    line = json.dumps({"type": "item.completed", "item": {
        "id": "item_1", "type": "agent_message", "text": "alpha\nbeta",
    }})
    update = parse_codex_stream_line(line)
    assert update.kind == "delta"
    assert update.text == "alpha\nbeta"


def test_codex_error_item_is_not_reply_text():
    line = json.dumps({"type": "item.completed", "item": {"type": "error", "message": "budget trimmed"}})
    assert parse_codex_stream_line(line).kind == "ignore"


def test_codex_turn_completed_keeps_the_accumulated_text():
    update = parse_codex_stream_line(json.dumps({"type": "turn.completed", "usage": {}}))
    assert update.kind == "complete"
    assert update.text == ""


# --------------------------------------------------------------------------
# Malformed input
# --------------------------------------------------------------------------

def test_non_json_lines_are_ignored_by_every_parser():
    for parse in (parse_claude_stream_line, parse_antigravity_stream_line, parse_codex_stream_line):
        assert parse("").kind == "ignore"
        assert parse("Welcome to the CLI").kind == "ignore"
        assert parse("{not json").kind == "ignore"
        assert parse("[1, 2, 3]").kind == "ignore"


# --------------------------------------------------------------------------
# Registry keys and the idle sweep
# --------------------------------------------------------------------------

def test_live_session_key_is_stable_for_the_same_directory(tmp_path):
    # Relative and absolute spellings of one directory share a live process.
    nested = tmp_path / "work"
    nested.mkdir()
    a = live_session_key("claude_code", str(nested))
    b = live_session_key("claude_code", str(tmp_path / "." / "work"))
    assert a == b


def test_live_session_key_separates_agents_and_directories(tmp_path):
    assert live_session_key("claude_code", str(tmp_path)) != live_session_key("antigravity", str(tmp_path))
    assert live_session_key("claude_code", str(tmp_path)) != live_session_key("claude_code", str(tmp_path / "sub"))


def test_live_session_key_follows_the_platform_on_case(tmp_path):
    # normcase lowercases on Windows, where paths are case insensitive, and
    # leaves the path alone elsewhere.
    same = live_session_key("claude_code", str(tmp_path)) == live_session_key("claude_code", str(tmp_path).upper())
    assert same is (os.name == "nt")


class _FakeSession:
    def __init__(self, last_activity, busy=False):
        self.last_activity = last_activity
        self.busy = busy


def test_idle_sweep_picks_only_sessions_past_the_timeout():
    now = time.monotonic()
    sessions = {
        ("claude_code", "a"): _FakeSession(now - 100),
        ("claude_code", "b"): _FakeSession(now - 5),
    }
    assert find_idle_keys(sessions, now, 60) == [("claude_code", "a")]


def test_idle_sweep_never_closes_a_session_mid_turn():
    now = time.monotonic()
    sessions = {("claude_code", "a"): _FakeSession(now - 10_000, busy=True)}
    assert find_idle_keys(sessions, now, 60) == []


# --------------------------------------------------------------------------
# Command building
# --------------------------------------------------------------------------

def test_claude_live_command_streams_both_ways_and_carries_no_prompt():
    args = build_claude_live_args("claude", resume_session_id="abc")
    assert "--output-format" in args and args[args.index("--output-format") + 1] == "stream-json"
    assert "--input-format" in args and args[args.index("--input-format") + 1] == "stream-json"
    assert "--include-partial-messages" in args
    # The CLI refuses stream-json output under -p without it.
    assert "--verbose" in args
    assert args[args.index("--resume") + 1] == "abc"


def test_claude_live_command_omits_resume_on_a_first_turn():
    assert "--resume" not in build_claude_live_args("claude")


def test_antigravity_live_command_attaches_an_empty_print_value():
    # A bare --print swallows the next argument, so agy refuses the command.
    args = build_antigravity_live_args("agy", resume_session_id="conv-1")
    assert args[1] == "--print="
    assert args[args.index("--conversation") + 1] == "conv-1"


def test_adapter_command_is_rewritten_for_a_live_claude_process():
    cmd = ["C:/bin/claude.cmd", "-p", "do the thing"]
    args, dialect = build_live_args_from_adapter(cmd, "do the thing", "sess-9")
    assert dialect == "claude"
    assert "do the thing" not in args
    assert args[:2] == ["C:/bin/claude.cmd", "-p"]
    assert args[args.index("--resume") + 1] == "sess-9"


def test_adapter_command_is_rewritten_for_a_live_antigravity_process():
    cmd = ["C:/bin/agy.exe", "--print", "do the thing", "--dangerously-skip-permissions"]
    args, dialect = build_live_args_from_adapter(cmd, "do the thing", None)
    assert dialect == "antigravity"
    assert "do the thing" not in args
    assert args[1] == "--print="
    assert "--dangerously-skip-permissions" in args
    assert "--conversation" not in args


def test_adapter_rewrite_keeps_one_copy_of_each_format_flag():
    cmd = ["claude", "-p", "hi", "--output-format", "json", "--verbose"]
    args, _ = build_live_args_from_adapter(cmd, "hi", None)
    assert args.count("--output-format") == 1
    assert args.count("--verbose") == 1
    assert "json" not in args


def test_adapter_rewrite_refuses_a_cli_with_no_stream_input():
    assert build_live_args_from_adapter(["codex", "exec", "hi"], "hi", None) is None
    assert build_live_args_from_adapter(["gemini", "-p", "hi"], "hi", None) is None


def test_adapter_rewrite_refuses_an_ambiguous_prompt():
    # The prompt has to be one whole argument, or it cannot be removed safely.
    assert build_live_args_from_adapter(["claude", "-p", "hi", "hi"], "hi", None) is None
    assert build_live_args_from_adapter(["claude", "--print=hi"], "hi", None) is None


# --------------------------------------------------------------------------
# Turn queueing
# --------------------------------------------------------------------------

class _FakeStdin:
    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)

    async def drain(self):
        return None


class _FakeProcess:
    def __init__(self):
        self.stdin = _FakeStdin()
        self.stdout = None
        self.returncode = None


def _fake_live_session():
    async def noop(*args):
        return None

    return LiveSession(
        agent="claude_code", cwd="C:/tmp", dialect="claude", process=_FakeProcess(),
        on_delta=noop, on_complete=noop, on_process_lost=noop,
    )


async def _test_a_second_turn_waits_instead_of_racing_the_first():
    session = _fake_live_session()
    first = await session.submit("one")
    second = await session.submit("two")

    assert session.current is first
    assert session.queue == [second]
    # Only the first turn reached the CLI. The second is held back so both
    # land in the same conversation rather than overlapping.
    assert len(session.process.stdin.written) == 1
    assert json.loads(session.process.stdin.written[0].decode())["message"]["content"][0]["text"] == "one"


async def _test_the_queued_turn_is_sent_once_the_first_one_completes():
    session = _fake_live_session()
    first = await session.submit("one")
    await session.submit("two")

    await session._handle_line(json.dumps({"type": "result", "subtype": "success",
                                           "session_id": "s1", "result": "done"}))

    assert first.done.is_set()
    assert first.text == "done"
    assert session.queue == []
    assert len(session.process.stdin.written) == 2
    assert json.loads(session.process.stdin.written[1].decode())["message"]["content"][0]["text"] == "two"


async def _test_a_message_snapshot_is_dropped_once_token_deltas_have_arrived():
    session = _fake_live_session()
    turn = await session.submit("one")

    await session._handle_line(json.dumps({"type": "stream_event", "event": {
        "type": "content_block_delta", "delta": {"type": "text_delta", "text": "alpha"}}}))
    await session._handle_line(json.dumps({"type": "assistant", "message": {
        "content": [{"type": "text", "text": "alpha"}]}}))

    assert turn.text == "alpha"


async def _test_a_lost_process_fails_every_waiting_turn():
    lost = []

    async def noop(*args):
        return None

    async def on_lost(session, reason):
        lost.append(reason)

    session = LiveSession(
        agent="claude_code", cwd="C:/tmp", dialect="claude", process=_FakeProcess(),
        on_delta=noop, on_complete=noop, on_process_lost=on_lost,
    )
    first = await session.submit("one")
    second = await session.submit("two")
    session.process.returncode = 1

    await session._handle_eof()

    assert first.done.is_set() and first.is_error
    assert second.done.is_set() and second.is_error
    assert lost and "ended unexpectedly" in lost[0]


def test_a_second_turn_waits_instead_of_racing_the_first():
    asyncio.run(_test_a_second_turn_waits_instead_of_racing_the_first())

def test_the_queued_turn_is_sent_once_the_first_one_completes():
    asyncio.run(_test_the_queued_turn_is_sent_once_the_first_one_completes())

def test_a_message_snapshot_is_dropped_once_token_deltas_have_arrived():
    asyncio.run(_test_a_message_snapshot_is_dropped_once_token_deltas_have_arrived())

def test_a_lost_process_fails_every_waiting_turn():
    asyncio.run(_test_a_lost_process_fails_every_waiting_turn())


def test_stream_update_defaults_to_ignoring_the_line():
    assert StreamUpdate().kind == "ignore"
