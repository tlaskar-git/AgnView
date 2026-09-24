"""Persistent, interactive CLI sessions for AgnView.

Claude Code and AntiGravity both read a stream of NDJSON turns on stdin and
answer with a stream of NDJSON events on stdout. AgnView holds one CLI process
open per (agent, working directory), shows each reply as it is written, and
feeds a follow-up message into the same live conversation instead of starting a
second competing process.

The real event shapes, confirmed against the installed CLIs:

Claude Code, ``claude -p --output-format stream-json --input-format stream-json
--verbose``

* in:  ``{"type":"user","message":{"role":"user","content":[{"type":"text",
  "text":"..."}]}}``
* out: ``{"type":"system","subtype":"init","session_id":"..."}``, then
  ``{"type":"assistant","message":{"content":[{"type":"thinking"|"text",...}]},
  "session_id":"..."}`` per content block, then
  ``{"type":"result","subtype":"success","session_id":"...","result":"<final
  text>","is_error":false}``.

AntiGravity, ``agy --print= --output-format stream-json --input-format
stream-json``

* in:  ``{"event":"user","message":{"role":"user","content":[{"type":"text",
  "text":"..."}]}}``
* out: ``{"event":"init","conversation_id":"..."}``, then
  ``{"event":"step_update","step_update":{"conversation_id":"...","step_type":
  "agent_response","state":"ACTIVE"|"DONE","text_delta":"..."}}``, then
  ``{"event":"result","result":{"conversation_id":"...","status":"SUCCESS",
  "response":"<final text>"}}``.

Codex, ``codex exec --json``, prints JSONL events as they happen but has no
stream input mode, so it cannot take a second turn on a running process. It
keeps one process per dispatch and only gains incremental output here.

* out: ``{"type":"thread.started","thread_id":"..."}``,
  ``{"type":"item.completed","item":{"type":"agent_message","text":"..."}}``,
  ``{"type":"turn.completed","usage":{...}}``.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple


logger = logging.getLogger("agent_relay.live_sessions")

# How much of the CLI's stderr is kept for the failure message. Enough to carry
# a stack trace or a usage error, small enough that a chatty CLI cannot grow the
# session without bound.
LIVE_SESSION_STDERR_KEEP_CHARS = 4000


# A live CLI process with no traffic for this long is closed and dropped from
# the registry, so an abandoned conversation does not hold a child process open
# for ever. Override with AGENT_RELAY_LIVE_SESSION_IDLE_TIMEOUT (seconds).
LIVE_SESSION_IDLE_TIMEOUT_SECONDS = float(
    os.environ.get("AGENT_RELAY_LIVE_SESSION_IDLE_TIMEOUT", "1200")
)

# How often the sweep looks for idle processes. Kept below the idle timeout so
# a short timeout set for a test is still honoured promptly.
LIVE_SESSION_SWEEP_INTERVAL_SECONDS = float(
    os.environ.get("AGENT_RELAY_LIVE_SESSION_SWEEP_INTERVAL", "15")
)

# Escape hatch: set to 0 to go back to one process per dispatch everywhere.
LIVE_SESSIONS_ENABLED = os.environ.get("AGENT_RELAY_LIVE_SESSIONS", "1").strip().lower() not in (
    "0", "off", "false", "no",
)


# ---------------------------------------------------------------------------
# NDJSON event parsing.
#
# One pure function per CLI dialect, so every event shape above can be unit
# tested without launching a process.
# ---------------------------------------------------------------------------

@dataclass
class StreamUpdate:
    """One parsed NDJSON event.

    ``kind`` is one of:

    * ``ignore``   nothing useful in this line
    * ``session``  carries only a session id
    * ``delta``    new reply text to append to the open bubble
    * ``snapshot`` the whole text of one finished assistant message. Used only
      when the CLI sent no token deltas for this turn, because the two describe
      the same text and appending both would double it.
    * ``complete`` the turn ended; ``text`` is the authoritative final reply,
      or empty to mean "keep whatever the deltas built up"
    """

    kind: str = "ignore"
    text: str = ""
    session_id: Optional[str] = None
    is_error: bool = False


def _load_event(line: str) -> Optional[dict]:
    line = (line or "").strip()
    if not line.startswith("{"):
        return None
    try:
        data = json.loads(line)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _as_str(value) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def parse_claude_stream_line(line: str) -> StreamUpdate:
    """Parse one line of Claude Code ``--output-format stream-json`` output."""
    data = _load_event(line)
    if data is None:
        return StreamUpdate()

    session_id = _as_str(data.get("session_id"))
    etype = data.get("type")

    if etype == "stream_event":
        # --include-partial-messages turns on token-level deltas, which is what
        # makes a long reply visibly grow rather than land in one block.
        event = data.get("event")
        if isinstance(event, dict) and event.get("type") == "content_block_delta":
            delta = event.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                piece = delta.get("text")
                if isinstance(piece, str) and piece:
                    return StreamUpdate(kind="delta", text=piece, session_id=session_id)
        return StreamUpdate(kind="session", session_id=session_id)

    if etype == "assistant":
        message = data.get("message")
        text = ""
        if isinstance(message, dict):
            for block in message.get("content") or []:
                # Thinking blocks are deliberately skipped: they are internal
                # reasoning, not the reply the operator asked for.
                if isinstance(block, dict) and block.get("type") == "text":
                    piece = block.get("text")
                    if isinstance(piece, str):
                        text += piece
        if text:
            return StreamUpdate(kind="snapshot", text=text, session_id=session_id)
        return StreamUpdate(kind="session", session_id=session_id)

    if etype == "result":
        text = data.get("result")
        subtype = data.get("subtype")
        return StreamUpdate(
            kind="complete",
            text=text if isinstance(text, str) else "",
            session_id=session_id,
            is_error=bool(data.get("is_error")) or (subtype is not None and subtype != "success"),
        )

    if session_id:
        return StreamUpdate(kind="session", session_id=session_id)
    return StreamUpdate()


def parse_antigravity_stream_line(line: str) -> StreamUpdate:
    """Parse one line of AntiGravity ``--output-format stream-json`` output."""
    data = _load_event(line)
    if data is None:
        return StreamUpdate()

    event = data.get("event")

    if event == "init":
        return StreamUpdate(kind="session", session_id=_as_str(data.get("conversation_id")))

    if event == "step_update":
        step = data.get("step_update")
        if not isinstance(step, dict):
            return StreamUpdate()
        session_id = _as_str(step.get("conversation_id"))
        if step.get("step_type") == "agent_response":
            delta = step.get("text_delta")
            if isinstance(delta, str) and delta:
                return StreamUpdate(kind="delta", text=delta, session_id=session_id)
        return StreamUpdate(kind="session", session_id=session_id)

    if event == "result":
        result = data.get("result")
        if not isinstance(result, dict):
            return StreamUpdate()
        status = result.get("status")
        response = result.get("response")
        return StreamUpdate(
            kind="complete",
            text=response if isinstance(response, str) else "",
            session_id=_as_str(result.get("conversation_id")),
            is_error=isinstance(status, str) and status.upper() not in ("SUCCESS", "OK"),
        )

    return StreamUpdate()


def parse_codex_stream_line(line: str) -> StreamUpdate:
    """Parse one line of Codex ``exec --json`` output."""
    data = _load_event(line)
    if data is None:
        return StreamUpdate()

    etype = data.get("type")

    if etype == "thread.started":
        return StreamUpdate(kind="session", session_id=_as_str(data.get("thread_id")))

    if etype == "item.completed":
        item = data.get("item")
        # Newer Codex CLIs label a reply type "agent_message". Older ones,
        # such as 0.69, label it item_type "assistant_message".
        kind = (item.get("type") or item.get("item_type")) if isinstance(item, dict) else None
        if kind in ("agent_message", "assistant_message"):
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                return StreamUpdate(kind="delta", text=text)
        return StreamUpdate()

    if etype == "turn.completed":
        # Codex has no single final-text field, so the accumulated deltas are
        # the reply. Empty text tells the caller to keep them.
        return StreamUpdate(kind="complete")

    if etype in ("turn.failed", "error"):
        return StreamUpdate(kind="complete", is_error=True)

    return StreamUpdate()


def encode_claude_turn(prompt: str) -> str:
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
    })


def encode_antigravity_turn(prompt: str) -> str:
    return json.dumps({
        "event": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
    })


DIALECTS: Dict[str, Tuple[Callable[[str], StreamUpdate], Callable[[str], str]]] = {
    "claude": (parse_claude_stream_line, encode_claude_turn),
    "antigravity": (parse_antigravity_stream_line, encode_antigravity_turn),
}


def live_session_key(agent: str, cwd: Optional[str]) -> Tuple[str, str]:
    """Key a live process on the agent and the directory the dispatch asked for."""
    return (agent or "", os.path.normcase(os.path.abspath(cwd)) if cwd else "")


def find_idle_keys(
    sessions: Dict[Tuple[str, str], "LiveSession"],
    now: float,
    idle_timeout: float,
) -> List[Tuple[str, str]]:
    """Return the keys of every session idle for longer than the timeout.

    Split out as a pure function so the sweep rule can be unit tested without
    real processes or real waiting.
    """
    stale = []
    for key, session in sessions.items():
        if session.busy:
            continue
        if now - session.last_activity >= idle_timeout:
            stale.append(key)
    return stale


# ---------------------------------------------------------------------------
# The live process itself.
# ---------------------------------------------------------------------------

@dataclass
class LiveTurn:
    """One prompt sent into a live session, and the reply being built for it."""

    prompt: str
    ui_session_id: Optional[str] = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    text: str = ""
    session_id: Optional[str] = None
    is_error: bool = False
    failure: Optional[str] = None
    saw_delta: bool = False
    # The growing console row this reply is written into. Owned by the runner.
    bubble: object = None


class LiveSession:
    """One CLI process held open across turns."""

    def __init__(
        self,
        agent: str,
        cwd: str,
        dialect: str,
        process,
        on_delta: Callable,
        on_complete: Callable,
        on_process_lost: Callable,
    ):
        self.agent = agent
        self.cwd = cwd
        self.dialect = dialect
        self.process = process
        self._parse, self._encode = DIALECTS[dialect]
        self._on_delta = on_delta
        self._on_complete = on_complete
        self._on_process_lost = on_process_lost

        self.last_activity = time.monotonic()
        self.current: Optional[LiveTurn] = None
        self.queue: List[LiveTurn] = []
        self.cli_session_id: Optional[str] = None
        self.closed = False
        self.reader_task: Optional[asyncio.Task] = None
        self.stderr_task: Optional[asyncio.Task] = None

        # Diagnostics. Without these two, a crash inside the read loop and a CLI
        # that explains itself on stderr both surface as the same useless
        # "process ended unexpectedly" line.
        self.read_failure: Optional[str] = None
        self.stderr_tail: str = ""

        # Everything below exists so the Sessions view can identify this
        # process without reaching into the runner. started_at is wall clock
        # for display; the monotonic pair drives the ages, because wall clock
        # can move under us.
        self.started_at = time.time()
        self.started_monotonic = self.last_activity
        self.turns_completed = 0
        # The console thread this process is writing into, so clicking a
        # session can load its transcript and reply into the same thread.
        self.ui_session_id: Optional[str] = None

    @property
    def busy(self) -> bool:
        return self.current is not None or bool(self.queue)

    @property
    def alive(self) -> bool:
        return not self.closed and self.process.returncode is None

    def start_reader(self) -> None:
        self.reader_task = asyncio.ensure_future(self._read_loop())
        if getattr(self.process, "stderr", None) is not None:
            self.stderr_task = asyncio.ensure_future(self._drain_stderr())

    def describe(self, now: Optional[float] = None) -> dict:
        """Describe this process for the Sessions view.

        ``now`` is a monotonic reading, passed in so the ages can be tested
        without waiting for real time to pass.
        """
        moment = time.monotonic() if now is None else now
        return {
            "agent": self.agent,
            "working_directory": self.cwd,
            "dialect": self.dialect,
            "session_id": self.ui_session_id,
            "cli_session_id": self.cli_session_id,
            "busy": self.busy,
            "alive": self.alive,
            "queued_turns": len(self.queue),
            "turns_completed": self.turns_completed,
            "started_at": self.started_at,
            "uptime_seconds": max(0.0, moment - self.started_monotonic),
            "idle_seconds": max(0.0, moment - self.last_activity),
            "pid": getattr(self.process, "pid", None),
        }

    # -- sending -----------------------------------------------------------

    async def submit(self, prompt: str, ui_session_id: Optional[str] = None) -> LiveTurn:
        """Queue a turn, sending it straight away when the process is idle."""
        turn = LiveTurn(prompt=prompt, ui_session_id=ui_session_id)
        if ui_session_id:
            self.ui_session_id = ui_session_id
        self.last_activity = time.monotonic()
        if self.current is None:
            await self._start_turn(turn)
        else:
            self.queue.append(turn)
        return turn

    async def _start_turn(self, turn: LiveTurn) -> None:
        self.current = turn
        payload = (self._encode(turn.prompt) + "\n").encode("utf-8")
        try:
            self.process.stdin.write(payload)
            await self.process.stdin.drain()
        except Exception as exc:
            self.current = None
            turn.failure = f"Could not reach the live {self.agent} process: {exc}"
            turn.is_error = True
            turn.done.set()
            await self._fail_pending(turn.failure)
            await self._on_process_lost(self, turn.failure)

    # -- reading -----------------------------------------------------------

    async def _read_loop(self) -> None:
        try:
            async for raw in self.process.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                await self._handle_line(line)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Anything thrown here used to be dropped, and the EOF handler then
            # blamed the CLI for dying. Keep the traceback and say what really
            # happened.
            self.read_failure = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "Live %s session reader failed in %s: %s",
                self.agent, self.cwd, self.read_failure,
            )
        await self._handle_eof()

    async def _drain_stderr(self) -> None:
        """Read the CLI's stderr so it cannot fill its pipe buffer and stall.

        The tail is kept for the failure message, because a live session that
        dies usually said why on stderr.
        """
        try:
            while True:
                chunk = await self.process.stderr.read(4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                self.stderr_tail = (self.stderr_tail + text)[-LIVE_SESSION_STDERR_KEEP_CHARS:]
                logger.debug("Live %s session stderr in %s: %s", self.agent, self.cwd, text.strip())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Live %s session stderr drain failed in %s: %s", self.agent, self.cwd, exc,
            )

    async def _handle_line(self, line: str) -> None:
        update = self._parse(line)
        if update.session_id:
            self.cli_session_id = update.session_id
        if update.kind == "ignore" or update.kind == "session":
            return

        self.last_activity = time.monotonic()
        turn = self.current
        if turn is None:
            return

        if update.kind == "delta":
            turn.saw_delta = True
            turn.text += update.text
            await self._on_delta(self, turn)
            return

        if update.kind == "snapshot":
            if turn.saw_delta:
                return
            turn.text += update.text
            await self._on_delta(self, turn)
            return

        if update.kind == "complete":
            if update.text:
                turn.text = update.text
            turn.session_id = update.session_id or self.cli_session_id
            turn.is_error = update.is_error
            self.turns_completed += 1
            self.current = None
            await self._on_complete(self, turn)
            turn.done.set()
            await self._drain_queue()

    async def _drain_queue(self) -> None:
        if self.current is not None or not self.queue:
            return
        await self._start_turn(self.queue.pop(0))

    async def _handle_eof(self) -> None:
        """The process ended. Fail the open turn and drop the session."""
        if self.closed:
            return
        try:
            code = self.process.returncode
            if code is None:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
                code = self.process.returncode
        except Exception:
            code = None

        # stdout and stderr end independently, so give the stderr drain a moment
        # to land. Without this the failure message can be written before the
        # CLI's own explanation has been read.
        if self.stderr_task is not None and not self.stderr_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.stderr_task), timeout=2.0)
            except Exception:
                pass

        reason = self._failure_reason(code)
        open_turn = self.current
        self.current = None
        if open_turn is not None:
            open_turn.failure = reason
            open_turn.is_error = True
            open_turn.done.set()
        await self._fail_pending(reason)
        await self._on_process_lost(self, reason)

    def _failure_reason(self, code) -> str:
        """Say why the live session stopped, with the evidence we have."""
        if self.read_failure:
            reason = (
                f"The live {self.agent} session stopped reading output after an error "
                f"({self.read_failure}). The process itself reported exit code {code}."
            )
        else:
            reason = f"The live {self.agent} process ended unexpectedly (exit code {code})."
        tail = self.stderr_tail.strip()
        if tail:
            reason += f" Its stderr said: {tail}"
        return reason

    async def _fail_pending(self, reason: str) -> None:
        pending, self.queue = self.queue, []
        for turn in pending:
            turn.failure = reason
            turn.is_error = True
            turn.done.set()

    # -- teardown ----------------------------------------------------------

    async def close(self, reason: Optional[str] = None) -> None:
        """Close stdin, give the CLI a moment, then terminate and kill."""
        if self.closed:
            return
        self.closed = True
        if reason:
            await self._fail_pending(reason)
            if self.current is not None:
                self.current.failure = reason
                self.current.is_error = True
                self.current.done.set()
                self.current = None

        for task in (self.reader_task, self.stderr_task):
            if task is not None:
                task.cancel()

        try:
            if self.process.stdin is not None and not self.process.stdin.is_closing():
                self.process.stdin.close()
        except Exception:
            pass

        if self.process.returncode is None:
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass
        if self.process.returncode is None:
            for stop in (self.process.terminate, self.process.kill):
                try:
                    stop()
                    await asyncio.wait_for(self.process.wait(), timeout=3.0)
                except Exception:
                    continue
                if self.process.returncode is not None:
                    break
