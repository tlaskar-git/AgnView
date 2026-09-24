"""Agent Execution Engine for AgentRelay.

Dispatches instructions directly to local installations of Claude Code, Codex,
and AntiGravity, captures output line-by-line, saves to SQLite console_logs,
and broadcasts live chunks via Server-Sent Events (SSE).
"""

import asyncio
import json
import logging
import os
import sys
import shutil
import time
from typing import Optional, Dict, Callable, List, Tuple
from datetime import datetime, timezone

from .db import Database
from .proc import no_window
from .adapters import AdapterManager, AgentAdapter
from .live_sessions import (
    LIVE_SESSION_IDLE_TIMEOUT_SECONDS,
    LIVE_SESSION_SWEEP_INTERVAL_SECONDS,
    LIVE_SESSIONS_ENABLED,
    LiveSession,
    find_idle_keys,
    live_session_key,
    parse_codex_stream_line,
)


logger = logging.getLogger("agent_relay.runner")


def _get_utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# How long a single dispatched CLI turn is allowed to run before AgnView kills
# it and reports a timeout. Coding agents can take several minutes on a real
# task, so this defaults well above a chat-sized reply. Override with
# AGENT_RELAY_DISPATCH_TIMEOUT (seconds) for a shorter or longer ceiling.
DISPATCH_TIMEOUT_SECONDS = float(os.environ.get("AGENT_RELAY_DISPATCH_TIMEOUT", "600"))

# Smallest gap between two live-progress broadcasts for the same reply. Output
# arrives in bursts, and without this the SSE stream carries one event per
# fragment for no visible gain.
STREAM_EMIT_INTERVAL_SECONDS = float(os.environ.get("AGENT_RELAY_STREAM_EMIT_INTERVAL", "0.15"))

# Longest single NDJSON line a CLI can write before asyncio gives up on it.
# The stdlib default is 64 KiB, and one tool result carrying a file easily beats
# that: readline then raises and the whole session dies. Claude Code caps a file
# read at 256 KB, so this leaves room for that plus the JSON around it.
CLI_STREAM_LINE_LIMIT_BYTES = int(os.environ.get("AGENT_RELAY_CLI_LINE_LIMIT", str(16 * 1024 * 1024)))


# ---------------------------------------------------------------------------
# Command construction and output parsing.
#
# These are deliberately pure functions so the resume-flag wiring and the
# session-id capture can be unit tested without launching a real CLI.
#
# Each supported CLI keeps its own conversation store and hands back its own
# session id. AgnView records that id per (agent, working directory) and replays
# it on the next dispatch, so a follow-up message continues the same
# conversation and the same session stays openable from the native CLI.
# ---------------------------------------------------------------------------

def build_claude_args(
    claude_bin: str,
    prompt: str,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    resume_session_id: Optional[str] = None,
) -> List[str]:
    """Build the Claude Code CLI command.

    ``--resume <session-id>`` continues an existing conversation and
    ``--output-format json`` makes the CLI report the session id it used, which
    is the id AgnView stores for the next turn.
    """
    args = [claude_bin, "-p", prompt, "--output-format", "json"]
    if resume_session_id:
        args.extend(["--resume", resume_session_id])
    if model and model != "auto":
        args.extend(["--model", model])
    if effort and effort not in ("default", "none"):
        args.extend(["--effort", effort])
    return args


def build_codex_args(
    codex_bin: str,
    prompt: str,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    resume_session_id: Optional[str] = None,
) -> List[str]:
    """Build the Codex CLI command.

    Codex resumes through the ``codex exec resume <SESSION_ID> <PROMPT>``
    subcommand, and ``--json`` emits a ``thread.started`` event carrying the
    thread id to store.
    """
    if resume_session_id:
        args = [codex_bin, "exec", "resume", resume_session_id, prompt]
    else:
        args = [codex_bin, "exec", prompt]
    args.extend(["--skip-git-repo-check", "--json"])
    if model and model != "auto":
        args.extend(["-m", model])
    if effort and effort not in ("default", "none"):
        args.extend(["-c", f"reasoning_effort={effort}"])
    return args


ANTIGRAVITY_MODEL_MAP = {
    "gemini-3.8-flash": "gemini-3.8-flash-high",
    "gemini-3.7-flash": "gemini-3.7-flash-high",
    "gemini-3.6-flash": "gemini-3.6-flash-high",
    "gemini-3.1-pro": "gemini-3.1-pro-high",
    "gpt-oss-120b": "gpt-oss-120b-medium",
}


def build_antigravity_args(
    agy_bin: Optional[str],
    gemini_bin: Optional[str],
    prompt: str,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    resume_session_id: Optional[str] = None,
) -> List[str]:
    """Build the AntiGravity command, or the Gemini CLI fallback.

    ``agy`` resumes with ``--conversation <ID>`` and reports ``conversation_id``
    under ``--output-format json``.

    The Gemini CLI fallback gets NO resume flag on purpose. Its ``--resume``
    takes "latest" or a positional index rather than a session id, so it cannot
    reliably target one stored conversation. That path stays stateless rather
    than pretending to continue a conversation it cannot address.
    """
    if agy_bin:
        args = [agy_bin, "--print", prompt, "--dangerously-skip-permissions", "--output-format", "json"]
        if resume_session_id:
            args.extend(["--conversation", resume_session_id])
        if model and model != "auto":
            args.extend(["--model", ANTIGRAVITY_MODEL_MAP.get(model, model)])
        if effort and effort != "default":
            args.extend(["--effort", effort])
        return args

    args = [gemini_bin, "-p", prompt, "--yolo", "--skip-trust"]
    if model and model != "auto":
        args.extend(["--model", model])
    return args


def build_claude_live_args(
    claude_bin: str,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    resume_session_id: Optional[str] = None,
) -> List[str]:
    """Build the Claude Code command for a long-lived, interactive process.

    No prompt goes on the command line. ``--input-format stream-json`` makes the
    CLI read one NDJSON turn per line from stdin and keep running, so AgnView
    can send a follow-up into the same process. ``--verbose`` is required by the
    CLI whenever ``-p`` is paired with ``--output-format stream-json``.
    """
    args = [
        claude_bin, "-p",
        "--output-format", "stream-json",
        "--input-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
    ]
    if resume_session_id:
        args.extend(["--resume", resume_session_id])
    if model and model != "auto":
        args.extend(["--model", model])
    if effort and effort not in ("default", "none"):
        args.extend(["--effort", effort])
    return args


def build_antigravity_live_args(
    agy_bin: str,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    resume_session_id: Optional[str] = None,
) -> List[str]:
    """Build the AntiGravity command for a long-lived, interactive process.

    ``--print=`` with an empty value is deliberate. ``agy`` takes an optional
    value on ``--print``, so a bare ``--print`` swallows the next argument and
    the CLI refuses the command.
    """
    args = [
        agy_bin, "--print=",
        "--output-format", "stream-json",
        "--input-format", "stream-json",
        "--dangerously-skip-permissions",
    ]
    if resume_session_id:
        args.extend(["--conversation", resume_session_id])
    if model and model != "auto":
        args.extend(["--model", ANTIGRAVITY_MODEL_MAP.get(model, model)])
    if effort and effort != "default":
        args.extend(["--effort", effort])
    return args


def cli_binary_name(binary_path: str) -> str:
    """Return the bare CLI name for a resolved executable path.

    Both separators are handled explicitly rather than relying on
    os.path.basename, which does not treat a backslash as a separator on
    POSIX and so would return a whole Windows path unchanged.
    """
    base = (binary_path or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
    for ext in (".cmd", ".exe", ".bat", ".ps1"):
        if base.endswith(ext):
            return base[: -len(ext)]
    return base


def apply_cli_session_profile(
    cmd: List[str],
    resume_id: Optional[str],
    add_resume: bool = True,
) -> Tuple[List[str], Optional[Callable[[str], Tuple[str, Optional[str]]]]]:
    """Add structured output, and optionally a resume flag, to a known CLI command.

    Adapter command templates in ~/.agnview/agents.yaml are plain one-shot
    invocations, and existing installs already have that file on disk. Detecting
    the CLI from the command itself means those installs gain session continuity
    without the operator editing any YAML.

    Returns the command plus the parser that reads the session id back out, or
    None for a CLI AgnView cannot resume.
    """
    if not cmd:
        return cmd, None
    name = cli_binary_name(cmd[0])

    if name == "claude":
        out = list(cmd) + ["--output-format", "json"]
        if add_resume and resume_id:
            out += ["--resume", resume_id]
        return out, parse_claude_output

    if name == "codex":
        out = list(cmd)
        # Resume is a subcommand, so it has to sit right after "exec".
        if add_resume and resume_id and len(out) > 1 and out[1] == "exec":
            out = out[:2] + ["resume", resume_id] + out[2:]
        out += ["--json"]
        return out, parse_codex_output

    if name == "agy":
        out = list(cmd) + ["--output-format", "json"]
        if add_resume and resume_id:
            out += ["--conversation", resume_id]
        return out, parse_antigravity_output

    # Anything else, including the Gemini CLI, has no resume AgnView can drive.
    return cmd, None


def build_live_args_from_adapter(
    resolved_cmd: List[str],
    prompt: str,
    resume_id: Optional[str] = None,
) -> Optional[Tuple[List[str], str]]:
    """Rewrite a one-shot adapter command as a long-lived streaming one.

    A live process takes its turns on stdin, so the prompt has to come off the
    command line along with the flag that carried it. Everything else the
    operator put in the template is kept.

    Returns ``(args, dialect)``, or None for a command AgnView cannot hold open,
    in which case the caller runs it one-shot exactly as before.
    """
    if not resolved_cmd or not prompt:
        return None
    name = cli_binary_name(resolved_cmd[0])
    if name not in ("claude", "agy"):
        return None
    if resolved_cmd.count(prompt) != 1:
        return None

    prompt_flags = ("-p", "--print", "--prompt", "--prompt-interactive", "-i")
    paired_flags = ("--output-format", "--input-format", "--resume", "--conversation", "--model")

    trimmed: List[str] = []
    skip_next = False
    for part in resolved_cmd[1:]:
        if skip_next:
            skip_next = False
            continue
        if part == prompt:
            if trimmed and trimmed[-1] in prompt_flags:
                trimmed.pop()
            continue
        if part in paired_flags:
            # Dropped here and re-added below, so the live flags always win and
            # never appear twice.
            skip_next = True
            continue
        if part in ("--verbose", "--include-partial-messages"):
            continue
        if part.startswith("--print=") or part.startswith("--output-format="):
            continue
        trimmed.append(part)

    model_args: List[str] = []
    tail = resolved_cmd[1:]
    for i, part in enumerate(tail):
        if part == "--model" and i + 1 < len(tail):
            model_args = ["--model", tail[i + 1]]
            break

    if name == "claude":
        args = [resolved_cmd[0], "-p"] + trimmed + model_args + [
            "--output-format", "stream-json",
            "--input-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
        ]
        if resume_id:
            args += ["--resume", resume_id]
        return args, "claude"

    args = [resolved_cmd[0], "--print="] + trimmed + model_args + [
        "--output-format", "stream-json",
        "--input-format", "stream-json",
    ]
    if resume_id:
        args += ["--conversation", resume_id]
    return args, "antigravity"


def resolve_adapter_command(command: List[str], subs: Dict[str, str]) -> List[str]:
    """Substitute placeholders in an adapter command template.

    An argument that is exactly one placeholder and resolves to empty is
    dropped, and so is the flag right before it. Without that, a template such
    as ``[..., "--resume", "{session_id}"]`` would leave a dangling ``--resume``
    on the very first turn, when no session exists yet.
    """
    resolved: List[str] = []
    for part in command:
        is_bare_placeholder = part.strip() in subs
        value = part
        for k, v in subs.items():
            value = value.replace(k, v)
        if not value.strip():
            if is_bare_placeholder and resolved and resolved[-1].startswith("-"):
                resolved.pop()
            continue
        resolved.append(value)
    return resolved


def parse_claude_output(raw: str) -> Tuple[str, Optional[str]]:
    """Extract the reply text and session id from Claude Code JSON output."""
    raw = (raw or "").strip()
    if not raw:
        return "", None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return raw, None
    if not isinstance(data, dict):
        return raw, None
    text = data.get("result")
    if not isinstance(text, str):
        text = raw
    session_id = data.get("session_id")
    return text, session_id if isinstance(session_id, str) else None


MISSING_AGENT_HELP = {
    "claude_code": (
        "Claude Code is not installed, or the claude command is not on PATH. Install it "
        "with: npm install -g @anthropic-ai/claude-code, run claude once to sign in, "
        "then restart AgnView."
    ),
    "codex": (
        "The Codex CLI is not installed, or the codex command is not on PATH. The ChatGPT "
        "and Codex desktop apps do not add it. Install it with: npm install -g "
        "@openai/codex, run codex login once, then restart AgnView."
    ),
    "antigravity": (
        "The AntiGravity CLI is not installed, or the agy command is not on PATH. The "
        "AntiGravity desktop app does not add it. Install the AntiGravity CLI, run agy "
        "once to sign in, then restart AgnView."
    ),
    "deepseek": (
        "DeepSeek chat is not connected in AgnView yet. Its card on the Usage tab still "
        "works."
    ),
    "custom": (
        "No local model is connected. Add one as an adapter in ~/.agnview/agents.yaml, "
        "then restart AgnView."
    ),
}


def missing_agent_message(agent: str) -> str:
    """What to tell a person whose message went to an agent that cannot run."""
    return MISSING_AGENT_HELP.get(
        agent,
        f"The command for {agent} is not installed or not on PATH, so nothing ran. "
        "Install it, then restart AgnView.",
    )


def _is_codex_reply(item) -> bool:
    """Newer Codex CLIs label a reply type "agent_message". Older ones, such as
    0.69, label it item_type "assistant_message". Both are read."""
    if not isinstance(item, dict):
        return False
    kind = item.get("type") or item.get("item_type")
    return kind in ("agent_message", "assistant_message")


def parse_codex_output(raw: str) -> Tuple[str, Optional[str]]:
    """Extract the reply text and thread id from Codex JSONL event output."""
    raw = raw or ""
    messages: List[str] = []
    session_id: Optional[str] = None
    saw_events = False
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        saw_events = True
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str):
                session_id = thread_id
        item = event.get("item")
        if _is_codex_reply(item):
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                messages.append(text)
    if not saw_events:
        return raw.strip(), None
    return "\n".join(messages).strip(), session_id


def parse_antigravity_output(raw: str) -> Tuple[str, Optional[str]]:
    """Extract the reply text and conversation id from AntiGravity JSON output."""
    raw = (raw or "").strip()
    if not raw:
        return "", None
    # agy can print banner lines before the JSON object, so scan for it.
    start = raw.find("{")
    if start == -1:
        return raw, None
    try:
        data = json.loads(raw[start:])
    except (ValueError, TypeError):
        return raw, None
    if not isinstance(data, dict):
        return raw, None
    text = data.get("response")
    if not isinstance(text, str):
        return raw, None
    conversation_id = data.get("conversation_id")
    return text.strip(), conversation_id if isinstance(conversation_id, str) else None


class StreamingBubble:
    """One console row that grows while a reply streams in.

    The row is written on the first piece of text and rewritten as more
    arrives, and each rewrite is broadcast under the same row id. The browser
    updates the bubble it already shows instead of adding a new one, so a
    watching operator sees the reply grow. The stored row ends up holding the
    complete final text.
    """

    def __init__(self, runner: "AgentRunner", agent: str, session_id: Optional[str], source: str = "agent_stdout"):
        self.runner = runner
        self.agent = agent
        self.session_id = session_id
        self.source = source
        self.log_id: Optional[int] = None
        self.text = ""
        self._last_emit = 0.0

    async def update(self, text: str, force: bool = False) -> None:
        self.text = text
        if not text.strip():
            return
        now = time.monotonic()
        if not force and (now - self._last_emit) < STREAM_EMIT_INTERVAL_SECONDS:
            return
        self._last_emit = now
        await self._write(streaming=True)

    async def finish(self, text: Optional[str] = None) -> Optional[int]:
        if text is not None:
            self.text = text
        if not self.text.strip():
            return self.log_id
        await self._write(streaming=False)
        return self.log_id

    async def _write(self, streaming: bool) -> None:
        if self.log_id is None:
            self.log_id = self.runner.db.add_console_log(
                agent=self.agent,
                source=self.source,
                content=self.text,
                session_id=self.session_id,
            )
        else:
            try:
                self.runner.db.update_console_log(self.log_id, self.text)
            except Exception:
                pass
        if self.runner.broadcast_callback:
            await self.runner.broadcast_callback("console", "agent_output_chunk", {
                "id": self.log_id,
                "agent": self.agent,
                "source": self.source,
                "content": self.text,
                "timestamp": _get_utc_now_iso(),
                "session_id": self.session_id,
                "streaming": streaming,
            })


class AgentRunner:
    def __init__(self, db: Database, broadcast_callback: Optional[Callable] = None, adapter_manager: Optional[AdapterManager] = None):
        self.db = db
        self.broadcast_callback = broadcast_callback
        self.adapter_manager = adapter_manager or AdapterManager()
        # (agent, normalised cwd) -> the CLI process held open for it.
        self.live_sessions: Dict[Tuple[str, str], LiveSession] = {}
        self._live_lock = asyncio.Lock()
        self._sweeper_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Live session registry
    # ------------------------------------------------------------------

    def _ensure_sweeper(self) -> None:
        """Start the idle sweep once a live process exists.

        The sweep runs on its own timer, so an abandoned process is closed
        without waiting for another dispatch to notice it.
        """
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return
        try:
            self._sweeper_task = asyncio.ensure_future(self._sweep_idle_sessions())
        except RuntimeError:
            self._sweeper_task = None

    async def _sweep_idle_sessions(self) -> None:
        while True:
            try:
                await asyncio.sleep(LIVE_SESSION_SWEEP_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                return
            try:
                await self.close_idle_live_sessions()
            except asyncio.CancelledError:
                return
            except Exception:
                continue

    async def close_idle_live_sessions(self, idle_timeout: Optional[float] = None) -> List[Tuple[str, str]]:
        """Close and drop every live process idle for longer than the timeout."""
        timeout = LIVE_SESSION_IDLE_TIMEOUT_SECONDS if idle_timeout is None else idle_timeout
        async with self._live_lock:
            stale = find_idle_keys(self.live_sessions, time.monotonic(), timeout)
            sessions = [self.live_sessions.pop(key) for key in stale]
        for session in sessions:
            await session.close()
        return stale

    async def close_live_sessions_for(self, agent: str, cwd: Optional[str] = None) -> int:
        """Close the live process for one target, or every one for that agent."""
        async with self._live_lock:
            if cwd is None:
                keys = [k for k in self.live_sessions if k[0] == agent]
            else:
                keys = [k for k in self.live_sessions if k == live_session_key(agent, cwd)]
            sessions = [self.live_sessions.pop(key) for key in keys]
        for session in sessions:
            await session.close("The conversation was reset.")
        return len(sessions)

    async def shutdown_live_sessions(self) -> None:
        """Terminate every live process. Called when the hub shuts down."""
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            self._sweeper_task = None
        async with self._live_lock:
            sessions = list(self.live_sessions.values())
            self.live_sessions.clear()
        for session in sessions:
            await session.close("AgnView is shutting down.")

    async def _emit_chunk(self, agent: str, source: str, content: str, session_id: Optional[str] = None):
        """Save chunk to SQLite and broadcast via SSE."""
        if not content.strip():
            return
        log_id = self.db.add_console_log(
            agent=agent,
            source=source,
            content=content,
            session_id=session_id
        )
        if self.broadcast_callback:
            await self.broadcast_callback("console", "agent_output_chunk", {
                "id": log_id,
                "agent": agent,
                "source": source,
                "content": content,
                "timestamp": _get_utc_now_iso(),
                "session_id": session_id
            })

    async def _emit_finished(self, agent: str, exit_code: int, summary: str, session_id: Optional[str] = None):
        """Broadcast completion event."""
        log_id = self.db.add_console_log(
            agent=agent,
            source="system_notice",
            content=summary,
            session_id=session_id
        )
        if self.broadcast_callback:
            await self.broadcast_callback("console", "agent_finished", {
                "id": log_id,
                "agent": agent,
                "exit_code": exit_code,
                "summary": summary,
                "timestamp": _get_utc_now_iso(),
                "session_id": session_id
            })

    def resolve_work_dir(self, target: str, cwd: Optional[str]) -> str:
        """Pick the directory a dispatch runs in.

        Sessions and live processes are keyed on this, so every caller has to
        resolve it the same way.
        """
        work_dir = cwd
        if not work_dir or work_dir == os.getcwd():
            home_dir = os.path.expanduser("~")
            if target in ("claude", "claude_code"):
                candidates = [os.path.join(home_dir, ".claude"), os.path.join(home_dir, "Claude")]
            elif target in ("codex", "chatgpt"):
                candidates = [os.path.join(home_dir, ".codex"), os.path.join(home_dir, "Codex")]
            elif target in ("antigravity", "agy"):
                candidates = [os.path.join(home_dir, ".gemini", "antigravity"), os.path.join(home_dir, ".gemini")]
            else:
                candidates = []
            for cand in candidates:
                if os.path.isdir(cand):
                    work_dir = cand
                    break
        return work_dir or os.getcwd()

    async def reset_session(self, agent: str, cwd: Optional[str] = None) -> Dict[str, object]:
        """Forget the stored conversation for a target and end its live process."""
        target = (agent or "").lower().strip()
        work_dir = self.resolve_work_dir(target, cwd)
        closed = 0
        for agent_key in self._session_agent_keys(target):
            self._clear_cli_session(agent_key, work_dir)
            closed += await self.close_live_sessions_for(agent_key, work_dir)
        return {"agent": target, "working_directory": work_dir, "live_processes_closed": closed}

    async def dispatch(
        self,
        agent: str,
        prompt: str,
        cwd: Optional[str] = None,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        files: Optional[List[str]] = None,
        skill: Optional[str] = None,
        reset_session: bool = False,
    ):
        """Asynchronously dispatch prompt to the specified agent.

        Set ``reset_session`` to forget the stored conversation for this target
        and working directory, so the CLI starts a brand new conversation.
        """
        target = agent.lower().strip()
        work_dir = self.resolve_work_dir(target, cwd)

        if reset_session:
            # A new chat also ends the process still holding the old
            # conversation, so nothing carries over into the fresh one.
            for agent_key in self._session_agent_keys(target):
                self._clear_cli_session(agent_key, work_dir)
                await self.close_live_sessions_for(agent_key, work_dir)

        # Augment prompt if skill or files are provided
        effective_prompt = prompt
        if skill and not effective_prompt.strip().startswith(skill.strip()):
            effective_prompt = f"{skill.strip()} {effective_prompt}"
        if files:
            files_context = ", ".join(files)
            effective_prompt = f"{effective_prompt}\n[Context Files: {files_context}]"

        # Log the user's turn tagged with the actual target, not a generic
        # "user" label, so a single-agent filter shows both sides of the
        # conversation instead of only that agent's replies. The console
        # still renders it as a "You" bubble regardless of this tag, since
        # that check keys off source == "user_input", not the agent field.
        await self._emit_chunk(target, "user_input", effective_prompt, session_id)

        adapter = self.adapter_manager.get_adapter(target)
        if adapter and adapter.enabled:
            await self._run_adapter(adapter, effective_prompt, work_dir, session_id, model=model, effort=effort, skill=skill)
        elif target in ("claude", "claude_code"):
            await self._run_claude_code(effective_prompt, work_dir, session_id, model=model, effort=effort)
        elif target in ("codex", "chatgpt"):
            await self._run_codex(effective_prompt, work_dir, session_id, model=model, effort=effort)
        elif target in ("antigravity", "agy"):
            await self._run_antigravity(effective_prompt, work_dir, session_id, model=model, effort=effort)
        elif target in ("custom", "ollama", "local"):
            await self._run_custom(effective_prompt, work_dir, session_id, model=model, effort=effort)
        elif target == "deepseek":
            await self._run_deepseek(effective_prompt, work_dir, session_id, model=model, effort=effort)
        elif target == "all":
            coros = []
            for a_id, adp in self.adapter_manager.adapters.items():
                if adp.enabled:
                    coros.append(self._run_adapter(adp, effective_prompt, work_dir, session_id, model=model, effort=effort, skill=skill))
            if not coros:
                coros = [
                    self._run_claude_code(effective_prompt, work_dir, session_id, model=model, effort=effort),
                    self._run_codex(effective_prompt, work_dir, session_id, model=model, effort=effort),
                    self._run_antigravity(effective_prompt, work_dir, session_id, model=model, effort=effort),
                ]
            await asyncio.gather(*coros, return_exceptions=True)
        else:
            await self._run_generic_command(effective_prompt, work_dir, session_id)

    def _get_env_for_provider(self, provider: str) -> Dict[str, str]:
        """Inject stored credentials only if they are valid raw API keys, preserving CLI native login."""
        env = {**os.environ}
        try:
            accounts = self.db.list_usage_accounts(provider=provider)
            if accounts:
                cred = accounts[0].get("credential", "").strip()
                # Only inject if valid raw API key format, avoiding session/token overrides
                if cred and not any(k in cred.lower() for k in ("sample", "mock", "dummy", "test")):
                    if provider == "claude" and cred.startswith("sk-ant-"):
                        env["ANTHROPIC_API_KEY"] = cred
                    elif provider == "chatgpt" and cred.startswith("sk-"):
                        env["OPENAI_API_KEY"] = cred
                    elif provider == "gemini" and cred.startswith("AIza"):
                        env["GEMINI_API_KEY"] = cred
                    elif provider == "deepseek" and (cred.startswith("sk-") or len(cred) > 20):
                        env["DEEPSEEK_API_KEY"] = cred
        except Exception:
            pass
        return env

    def _session_agent_keys(self, target: str) -> List[str]:
        """Map a dispatch target onto the agent keys its sessions are stored under."""
        adapter = self.adapter_manager.get_adapter(target)
        if adapter and adapter.enabled:
            return [adapter.id]
        if target in ("claude", "claude_code"):
            return ["claude_code"]
        if target in ("codex", "chatgpt"):
            return ["codex"]
        if target in ("antigravity", "agy"):
            return ["antigravity"]
        if target == "all":
            keys = [a_id for a_id, adp in self.adapter_manager.adapters.items() if adp.enabled]
            return keys or ["claude_code", "codex", "antigravity"]
        return [target]

    def _get_cli_session(self, agent: str, cwd: Optional[str]) -> Optional[str]:
        """Look up the CLI's own session id stored for this agent and directory."""
        try:
            return self.db.get_agent_session(agent, cwd)
        except Exception:
            return None

    def _set_cli_session(self, agent: str, cwd: Optional[str], cli_session_id: Optional[str]) -> None:
        if not cli_session_id:
            return
        try:
            self.db.set_agent_session(agent, cwd, cli_session_id)
        except Exception:
            pass

    def _clear_cli_session(self, agent: str, cwd: Optional[str]) -> None:
        try:
            self.db.clear_agent_session(agent, cwd)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Live (persistent, interactive) CLI path
    # ------------------------------------------------------------------

    def _bubble_for(self, agent: str, turn) -> StreamingBubble:
        if turn.bubble is None:
            turn.bubble = StreamingBubble(self, agent, turn.ui_session_id)
        return turn.bubble

    async def _live_on_delta(self, session: LiveSession, turn) -> None:
        await self._bubble_for(session.agent, turn).update(turn.text)

    async def _live_on_complete(self, session: LiveSession, turn) -> None:
        await self._bubble_for(session.agent, turn).finish(turn.text)
        if turn.is_error:
            await self._emit_finished(session.agent, 1, "Error", turn.ui_session_id)
            return
        self._set_cli_session(session.agent, session.cwd, turn.session_id)
        await self._emit_finished(session.agent, 0, "Finished", turn.ui_session_id)

    async def _live_on_process_lost(self, session: LiveSession, reason: str) -> None:
        """Drop a dead process so the next dispatch spawns a fresh one."""
        async with self._live_lock:
            key = live_session_key(session.agent, session.cwd)
            if self.live_sessions.get(key) is session:
                self.live_sessions.pop(key, None)
        session.closed = True

    async def _get_live_session(
        self,
        agent: str,
        dialect: str,
        cwd: str,
        env: Dict[str, str],
        build_args: Callable[[Optional[str]], List[str]],
        run_cwd: Optional[str] = None,
    ) -> Optional[LiveSession]:
        """Return the live process for this target, spawning it when needed."""
        key = live_session_key(agent, cwd)
        async with self._live_lock:
            session = self.live_sessions.get(key)
            if session is not None and not session.alive:
                self.live_sessions.pop(key, None)
                session = None
            if session is not None:
                return session

            # A fresh process replays the last session id AgnView stored for
            # this target, which is the same resume mechanism the one-shot path
            # uses. A crash therefore costs a process, never the conversation.
            resume_id = self._get_cli_session(agent, cwd)
            proc_args = build_args(resume_id)
            try:
                process = await asyncio.create_subprocess_exec(
                    *proc_args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=run_cwd or cwd,
                    env=env,
                    limit=CLI_STREAM_LINE_LIMIT_BYTES,
                    **no_window(),
                )
            except Exception:
                logger.exception("Could not start a live %s process in %s", agent, run_cwd or cwd)
                return None

            session = LiveSession(
                agent=agent,
                cwd=cwd,
                dialect=dialect,
                process=process,
                on_delta=self._live_on_delta,
                on_complete=self._live_on_complete,
                on_process_lost=self._live_on_process_lost,
            )
            session.start_reader()
            self.live_sessions[key] = session
            self._ensure_sweeper()
            return session

    async def _run_live_turn(
        self,
        agent: str,
        dialect: str,
        cwd: str,
        env: Dict[str, str],
        session_id: Optional[str],
        prompt: str,
        build_args: Callable[[Optional[str]], List[str]],
        run_cwd: Optional[str] = None,
    ) -> bool:
        """Send one turn into the live process for this target.

        Returns False when no live process could be started, so the caller can
        fall back to the existing one-shot run.
        """
        session = await self._get_live_session(agent, dialect, cwd, env, build_args, run_cwd=run_cwd)
        if session is None:
            return False

        turn = await session.submit(prompt, ui_session_id=session_id)
        try:
            await asyncio.wait_for(turn.done.wait(), timeout=DISPATCH_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            await self.close_live_sessions_for(agent, cwd)
            await self._emit_chunk(agent, "agent_stderr", "Execution timed out.", session_id)
            await self._emit_finished(agent, 1, "Timeout", session_id)
            return True

        if turn.failure:
            await self._emit_chunk(agent, "agent_stderr", turn.failure, session_id)
            await self._emit_finished(agent, 1, "Error", session_id)
        return True

    async def _run_cli_with_session(
        self,
        agent: str,
        proc_args: List[str],
        cwd: str,
        env: Dict[str, str],
        session_id: Optional[str],
        parser: Optional[Callable[[str], Tuple[str, Optional[str]]]],
        resume_id: Optional[str] = None,
        fallback_prompt: str = "",
        run_cwd: Optional[str] = None,
        stream_parser: Optional[Callable[[str], object]] = None,
    ):
        """Run a CLI, show its reply, and record the session id it reports back.

        ``parser`` turns the CLI's structured output into (display text, session
        id). Pass None for a CLI that has no structured mode, and the raw output
        is shown unchanged with no session captured.

        ``stream_parser`` reads one output line at a time, so the reply appears
        in the console while the CLI is still working rather than all at once at
        the end. It takes precedence over ``parser``.

        ``run_cwd`` is where the process runs when an adapter pins its own
        directory. Sessions stay keyed on ``cwd``, the directory the dispatch
        asked for, so lookup and reset always agree.
        """
        try:
            process = await asyncio.create_subprocess_exec(
                *proc_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=run_cwd or cwd,
                env=env,
                limit=CLI_STREAM_LINE_LIMIT_BYTES,
                **no_window(),
            )
            if process.stdin:
                process.stdin.close()
                await process.stdin.wait_closed()

            collected: List[str] = []
            bubble = StreamingBubble(self, agent, session_id) if stream_parser else None
            streamed_text = ""
            streamed_session: Optional[str] = None
            saw_delta = False

            async def read_stream():
                nonlocal streamed_text, streamed_session, saw_delta
                async for line in process.stdout:
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if not text:
                        continue
                    collected.append(text)
                    if stream_parser is None:
                        continue
                    update = stream_parser(text)
                    if update.session_id:
                        streamed_session = update.session_id
                    if update.kind == "delta":
                        saw_delta = True
                        streamed_text += update.text
                        await bubble.update(streamed_text)
                    elif update.kind == "snapshot" and not saw_delta:
                        streamed_text += update.text
                        await bubble.update(streamed_text)
                    elif update.kind == "complete" and update.text:
                        streamed_text = update.text

            try:
                await asyncio.wait_for(read_stream(), timeout=DISPATCH_TIMEOUT_SECONDS)
                await asyncio.wait_for(process.wait(), timeout=5.0)
                exit_code = process.returncode or 0

                raw_output = "\n".join(collected)
                if stream_parser and (streamed_text.strip() or streamed_session):
                    display_text, captured_session = streamed_text, streamed_session
                elif parser:
                    display_text, captured_session = parser(raw_output)
                else:
                    display_text, captured_session = raw_output, None

                if bubble is not None:
                    await bubble.finish(display_text)
                elif display_text.strip():
                    await self._emit_chunk(agent, "agent_stdout", display_text, session_id)

                if exit_code == 0 and not display_text.strip():
                    # The CLI ran and said nothing AgnView could read. It used
                    # to end there, with no reply and no error. Show the tail
                    # of what it printed so the cause is visible.
                    tail = "\n".join(line[:300] for line in collected[-5:])
                    note = f"{agent} finished but returned no reply AgnView could read."
                    if agent == "codex":
                        note += (
                            " An old Codex CLI can do this. Update it with: npm install -g "
                            "@openai/codex@latest"
                        )
                    if tail:
                        note += f"\nLast output:\n{tail}"
                    await self._emit_chunk(agent, "agent_stderr", note, session_id)

                if exit_code != 0:
                    stderr_out = await process.stderr.read()
                    err_msg = stderr_out.decode("utf-8", errors="replace").strip()
                    if err_msg:
                        await self._emit_chunk(agent, "agent_stderr", err_msg, session_id)
                    # A stored session id can go stale if the conversation was
                    # deleted outside AgnView. Drop it so the next dispatch
                    # starts cleanly rather than failing forever.
                    if resume_id and not captured_session:
                        self._clear_cli_session(agent, cwd)
                else:
                    self._set_cli_session(agent, cwd, captured_session)

                await self._emit_finished(agent, exit_code, "Finished", session_id)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except Exception:
                    pass
                await self._emit_chunk(agent, "agent_stderr", "Execution timed out.", session_id)
                await self._emit_finished(agent, 1, "Timeout", session_id)

        except FileNotFoundError:
            await self._simulate_agent_execution(agent, fallback_prompt, session_id)
        except Exception as e:
            await self._emit_chunk(agent, "agent_stderr", f"Execution error: {str(e)}", session_id)
            await self._emit_finished(agent, 1, "Error", session_id)

    @property
    def _is_testing(self) -> bool:
        return "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST") is not None or os.environ.get("AGENT_RELAY_TESTING") == "1"

    async def _run_claude_code(self, prompt: str, cwd: str, session_id: Optional[str], model: Optional[str] = None, effort: Optional[str] = None):
        """Execute prompt using local Claude Code CLI with streaming and timeout protection."""
        claude_bin = shutil.which("claude.cmd") or shutil.which("claude.exe") or shutil.which("claude")
        if self._is_testing or not claude_bin:
            await self._simulate_agent_execution("claude_code", prompt, session_id)
            return

        resume_id = self._get_cli_session("claude_code", cwd)
        proc_args = build_claude_args(claude_bin, prompt, model=model, effort=effort, resume_session_id=resume_id)

        env = self._get_env_for_provider("claude")
        env["CI"] = "1"
        env["TERM"] = "dumb"

        if LIVE_SESSIONS_ENABLED:
            handled = await self._run_live_turn(
                agent="claude_code",
                dialect="claude",
                cwd=cwd,
                env=env,
                session_id=session_id,
                prompt=prompt,
                build_args=lambda rid: build_claude_live_args(
                    claude_bin, model=model, effort=effort, resume_session_id=rid
                ),
            )
            if handled:
                return

        await self._run_cli_with_session(
            agent="claude_code",
            proc_args=proc_args,
            cwd=cwd,
            env=env,
            session_id=session_id,
            parser=parse_claude_output,
            resume_id=resume_id,
            fallback_prompt=prompt,
        )

    async def _run_codex(self, prompt: str, cwd: str, session_id: Optional[str], model: Optional[str] = None, effort: Optional[str] = None):
        """Execute prompt using local Codex CLI with direct stdin closure."""
        codex_bin = shutil.which("codex.cmd") or shutil.which("codex.exe") or shutil.which("codex")
        if self._is_testing or not codex_bin:
            await self._simulate_agent_execution("codex", prompt, session_id)
            return

        resume_id = self._get_cli_session("codex", cwd)
        proc_args = build_codex_args(codex_bin, prompt, model=model, effort=effort, resume_session_id=resume_id)

        env = self._get_env_for_provider("chatgpt")

        await self._run_cli_with_session(
            agent="codex",
            proc_args=proc_args,
            cwd=cwd,
            env=env,
            session_id=session_id,
            parser=parse_codex_output,
            resume_id=resume_id,
            fallback_prompt=prompt,
            # Codex has no stream input mode, so it keeps one process per
            # dispatch. Its --json events do arrive as they happen, so the reply
            # still appears progressively rather than in one lump at the end.
            stream_parser=parse_codex_stream_line,
        )

    async def _run_antigravity(self, prompt: str, cwd: str, session_id: Optional[str], model: Optional[str] = None, effort: Optional[str] = None):
        """Execute prompt using local AntiGravity (agy) or Gemini CLI."""
        agy_bin = shutil.which("agy.exe") or shutil.which("agy.cmd") or shutil.which("agy")
        gemini_bin = shutil.which("gemini.cmd") or shutil.which("gemini.exe") or shutil.which("gemini")

        if self._is_testing or (not agy_bin and not gemini_bin):
            await self._simulate_agent_execution("antigravity", prompt, session_id)
            return

        # Only agy can resume by id. The Gemini fallback stays stateless: see
        # build_antigravity_args for why.
        resume_id = self._get_cli_session("antigravity", cwd) if agy_bin else None
        proc_args = build_antigravity_args(
            agy_bin, gemini_bin, prompt, model=model, effort=effort, resume_session_id=resume_id
        )

        env = self._get_env_for_provider("gemini")

        if agy_bin and LIVE_SESSIONS_ENABLED:
            handled = await self._run_live_turn(
                agent="antigravity",
                dialect="antigravity",
                cwd=cwd,
                env=env,
                session_id=session_id,
                prompt=prompt,
                build_args=lambda rid: build_antigravity_live_args(
                    agy_bin, model=model, effort=effort, resume_session_id=rid
                ),
            )
            if handled:
                return

        await self._run_cli_with_session(
            agent="antigravity",
            proc_args=proc_args,
            cwd=cwd,
            env=env,
            session_id=session_id,
            parser=parse_antigravity_output if agy_bin else None,
            resume_id=resume_id,
            fallback_prompt=prompt,
        )

    async def _run_deepseek(self, prompt: str, cwd: str, session_id: Optional[str], model: Optional[str] = None, effort: Optional[str] = None):
        """Execute prompt using DeepSeek reasoning engine."""
        if self._is_testing:
            model_tag = f" [{model}]" if model and model != "auto" else ""
            effort_tag = f" ({effort})" if effort and effort != "default" else ""
            await self._emit_chunk(
                "deepseek", "agent_stdout",
                f"[DeepSeek V3/R1{model_tag}{effort_tag}] Processing reasoning harness for: \"{prompt[:60]}...\"",
                session_id
            )
        await self._simulate_agent_execution("deepseek", prompt, session_id)

    async def _run_adapter(
        self,
        adapter: AgentAdapter,
        prompt: str,
        cwd: str,
        session_id: Optional[str],
        model: Optional[str] = None,
        effort: Optional[str] = None,
        skill: Optional[str] = None,
    ):
        """Execute agent command dynamically configured by an adapter."""
        # For legacy hardcoded simulation in tests
        if self._is_testing:
            await self._simulate_agent_execution(adapter.id, prompt, session_id)
            return

        # {session_id} resolves to the CLI's OWN stored session id for this
        # adapter and directory, so a template carrying a resume flag continues
        # the same conversation. It is empty on the first turn.
        resume_id = self._get_cli_session(adapter.id, cwd)

        # Prepare placeholder substitutions
        subs = {
            "{prompt}": prompt,
            "{workspace}": cwd,
            "{session_id}": resume_id or "",
            "{model}": model or "",
            "{effort}": effort or "",
            "{skill}": skill or ""
        }

        resolved_cmd = resolve_adapter_command(adapter.command, subs)
        if not resolved_cmd:
            await self._simulate_agent_execution(adapter.id, prompt, session_id)
            return

        # Resolve cwd
        target_cwd = adapter.cwd or cwd
        for k, v in subs.items():
            target_cwd = target_cwd.replace(k, v)
        if not os.path.isdir(target_cwd):
            target_cwd = cwd

        # Locate executable
        bin_name = resolved_cmd[0]
        full_bin = shutil.which(bin_name)
        if os.name == "nt" and not full_bin:
            for ext in (".cmd", ".exe", ".bat", ".ps1"):
                full_bin = shutil.which(bin_name + ext)
                if full_bin:
                    break

        if not full_bin:
            await self._simulate_agent_execution(adapter.id, prompt, session_id)
            return

        resolved_cmd[0] = full_bin
        provider_name = "claude" if "claude" in adapter.id else ("chatgpt" if "codex" in adapter.id else adapter.id)
        env = self._get_env_for_provider(provider_name)
        env["CI"] = "1"
        env["TERM"] = "dumb"

        # A template that names {session_id} means the operator places the
        # resume flag themselves, so only the structured output is added and the
        # captured id still gets stored for the next turn.
        template_owns_resume = any("{session_id}" in part for part in adapter.command)

        # Claude Code and AntiGravity can be held open across turns, so try that
        # first. A template that places its own resume flag keeps the one-shot
        # path, because the operator is driving the session themselves there.
        if LIVE_SESSIONS_ENABLED and not template_owns_resume:
            live = build_live_args_from_adapter(resolved_cmd, prompt)
            if live is not None:
                _, dialect = live

                def _build(rid: Optional[str], _cmd=list(resolved_cmd)) -> List[str]:
                    return build_live_args_from_adapter(_cmd, prompt, rid)[0]

                handled = await self._run_live_turn(
                    agent=adapter.id,
                    dialect=dialect,
                    cwd=cwd,
                    env=env,
                    session_id=session_id,
                    prompt=prompt,
                    build_args=_build,
                    run_cwd=target_cwd,
                )
                if handled:
                    return

        resolved_cmd, parser = apply_cli_session_profile(
            resolved_cmd, resume_id, add_resume=not template_owns_resume
        )
        stream_parser = {
            parse_codex_output: parse_codex_stream_line,
        }.get(parser)

        await self._run_cli_with_session(
            agent=adapter.id,
            proc_args=resolved_cmd,
            cwd=cwd,
            env=env,
            session_id=session_id,
            parser=parser,
            resume_id=resume_id,
            fallback_prompt=prompt,
            run_cwd=target_cwd,
            stream_parser=stream_parser,
        )

    async def _run_custom(self, prompt: str, cwd: str, session_id: Optional[str], model: Optional[str] = None, effort: Optional[str] = None):
        """Execute prompt using custom local harness (Ollama / vLLM / LM Studio)."""
        if self._is_testing:
            model_tag = f" [{model}]" if model and model != "auto" else ""
            effort_tag = f" ({effort})" if effort and effort != "default" else ""
            await self._emit_chunk(
                "custom", "agent_stdout",
                f"[Custom LLM Harness{model_tag}{effort_tag}] Dispatching to local inference worker for: \"{prompt[:60]}...\"",
                session_id
            )
        await self._simulate_agent_execution("custom", prompt, session_id)


    async def _run_generic_command(self, prompt: str, cwd: str, session_id: Optional[str]):
        """Run shell command directly."""
        cmd = prompt.lstrip("$! ")
        await self._emit_chunk("system", "agent_stdout", f"Executing: {cmd}", session_id)
        try:
            proc_args = ["powershell", "-NoProfile", "-Command", cmd] if os.name == "nt" else ["bash", "-c", cmd]
            process = await asyncio.create_subprocess_exec(
                *proc_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env={**os.environ},
                **no_window(),
            )

            async for line in process.stdout:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    await self._emit_chunk("system", "agent_stdout", text, session_id)

            stderr_out = await process.stderr.read()
            if stderr_out:
                err_text = stderr_out.decode("utf-8", errors="replace").strip()
                if err_text:
                    await self._emit_chunk("system", "agent_stderr", err_text, session_id)

            await process.wait()
            await self._emit_finished("system", process.returncode or 0, f"Command finished with code {process.returncode}", session_id)
        except Exception as e:
            await self._emit_chunk("system", "agent_stderr", str(e), session_id)
            await self._emit_finished("system", 1, f"Failed: {str(e)}", session_id)

    async def _simulate_agent_execution(self, agent: str, prompt: str, session_id: Optional[str]):
        """A canned reply for the test suite, and a plain error everywhere else.

        Outside tests this used to answer too, with "I have processed your
        request", whenever the agent's CLI was missing. That read as a real
        reply from an agent that never ran. Now the console says what is
        missing and how to add it.
        """
        if not self._is_testing:
            await self._emit_chunk(agent, "agent_stderr", missing_agent_message(agent), session_id)
            await self._emit_finished(agent, 127, "Not installed", session_id)
            return
        response_text = f"I have processed your request for: \"{prompt}\". Actions and changes are verified in the local workspace."
        await asyncio.sleep(0.02)
        await self._emit_chunk(agent, "agent_stdout", response_text, session_id)
        await self._emit_finished(agent, 0, "Finished", session_id)
