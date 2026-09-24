"""The console never pretends an agent answered, and it reads replies from
older and newer Codex CLIs alike."""

import asyncio
import json

from agent_relay.core import runner as runner_module
from agent_relay.core.live_sessions import parse_codex_stream_line
from agent_relay.core.runner import missing_agent_message, parse_codex_output


class _Recorder:
    """Stands in for the runner's emit methods."""

    def __init__(self):
        self.chunks = []
        self.finished = []

    async def emit_chunk(self, agent, source, content, session_id=None):
        self.chunks.append((agent, source, content))

    async def emit_finished(self, agent, exit_code, summary, session_id=None):
        self.finished.append((agent, exit_code))


def _runner_outside_tests(monkeypatch):
    cls = next(
        value for value in vars(runner_module).values()
        if isinstance(value, type) and hasattr(value, "_simulate_agent_execution")
    )
    monkeypatch.setattr(cls, "_is_testing", property(lambda self: False))
    instance = cls.__new__(cls)
    recorder = _Recorder()
    instance._emit_chunk = recorder.emit_chunk
    instance._emit_finished = recorder.emit_finished
    return instance, recorder


def test_a_missing_cli_is_reported_not_answered(monkeypatch):
    instance, recorder = _runner_outside_tests(monkeypatch)

    asyncio.run(instance._simulate_agent_execution("antigravity", "ping", None))

    [(agent, source, content)] = recorder.chunks
    assert source == "agent_stderr"
    assert "agy" in content
    assert "I have processed your request" not in content
    assert recorder.finished == [("antigravity", 127)]


def test_every_known_agent_names_its_install_step():
    for agent in ("claude_code", "codex", "antigravity"):
        message = missing_agent_message(agent)
        assert "install" in message.lower()
        assert "restart AgnView" in message


def test_an_unknown_agent_still_gets_a_plain_message():
    assert "not installed" in missing_agent_message("someagent")


def _older_codex_lines():
    # The shape Codex CLI 0.69 prints with exec --json.
    return [
        json.dumps({"type": "thread.started", "thread_id": "thread-test"}),
        json.dumps({"type": "item.completed", "item": {"id": "item_0", "item_type": "reasoning", "text": "thinking"}}),
        json.dumps({"type": "item.completed", "item": {"id": "item_1", "item_type": "assistant_message", "text": "Pong."}}),
        json.dumps({"type": "turn.completed"}),
    ]


def test_an_older_codex_reply_is_read_in_full():
    text, session = parse_codex_output("\n".join(_older_codex_lines()))
    assert text == "Pong."
    assert session == "thread-test"


def test_an_older_codex_reply_is_read_while_streaming():
    updates = [parse_codex_stream_line(line) for line in _older_codex_lines()]
    texts = [u.text for u in updates if u.kind == "delta"]
    assert texts == ["Pong."]


def test_a_newer_codex_reply_is_still_read():
    line = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Pong."}})
    assert parse_codex_stream_line(line).text == "Pong."
    assert parse_codex_output(line)[0] == "Pong."
