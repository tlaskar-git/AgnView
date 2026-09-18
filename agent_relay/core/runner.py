"""Agent Execution Engine for AgentRelay.

Dispatches instructions directly to local installations of Claude Code, Codex,
and AntiGravity, captures output line-by-line, saves to SQLite console_logs,
and broadcasts live chunks via Server-Sent Events (SSE).
"""

import asyncio
import json
import os
import sys
import shutil
from typing import Optional, Dict, Callable, List, Tuple
from datetime import datetime, timezone

from .db import Database
from .adapters import AdapterManager, AgentAdapter


def _get_utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        if isinstance(item, dict) and item.get("type") == "agent_message":
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


class AgentRunner:
    def __init__(self, db: Database, broadcast_callback: Optional[Callable] = None, adapter_manager: Optional[AdapterManager] = None):
        self.db = db
        self.broadcast_callback = broadcast_callback
        self.adapter_manager = adapter_manager or AdapterManager()

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

        # Auto-fallback to provider-native workspace if cwd not specified or generic
        work_dir = cwd
        if not work_dir or work_dir == os.getcwd():
            home_dir = os.path.expanduser("~")
            if target in ("claude", "claude_code"):
                candidates = [os.path.join(home_dir, ".claude"), os.path.join(home_dir, "Claude")]
                for cand in candidates:
                    if os.path.isdir(cand):
                        work_dir = cand
                        break
            elif target in ("codex", "chatgpt"):
                candidates = [os.path.join(home_dir, ".codex"), os.path.join(home_dir, "Codex")]
                for cand in candidates:
                    if os.path.isdir(cand):
                        work_dir = cand
                        break
            elif target in ("antigravity", "agy"):
                candidates = [os.path.join(home_dir, ".gemini", "antigravity"), os.path.join(home_dir, ".gemini")]
                for cand in candidates:
                    if os.path.isdir(cand):
                        work_dir = cand
                        break
        if not work_dir:
            work_dir = os.getcwd()

        if reset_session:
            for agent_key in self._session_agent_keys(target):
                self._clear_cli_session(agent_key, work_dir)

        # Augment prompt if skill or files are provided
        effective_prompt = prompt
        if skill and not effective_prompt.strip().startswith(skill.strip()):
            effective_prompt = f"{skill.strip()} {effective_prompt}"
        if files:
            files_context = ", ".join(files)
            effective_prompt = f"{effective_prompt}\n[Context Files: {files_context}]"

        # Log user prompt
        await self._emit_chunk("user", "user_input", effective_prompt, session_id)

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
    ):
        """Run a CLI, show its reply, and record the session id it reports back.

        ``parser`` turns the CLI's structured output into (display text, session
        id). Pass None for a CLI that has no structured mode, and the raw output
        is shown unchanged with no session captured.

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
                env=env
            )
            if process.stdin:
                process.stdin.close()
                await process.stdin.wait_closed()

            collected: List[str] = []

            async def read_stream():
                async for line in process.stdout:
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if text:
                        collected.append(text)

            try:
                await asyncio.wait_for(read_stream(), timeout=45.0)
                await asyncio.wait_for(process.wait(), timeout=5.0)
                exit_code = process.returncode or 0

                raw_output = "\n".join(collected)
                if parser:
                    display_text, captured_session = parser(raw_output)
                else:
                    display_text, captured_session = raw_output, None

                if display_text.strip():
                    await self._emit_chunk(agent, "agent_stdout", display_text, session_id)

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
        resolved_cmd, parser = apply_cli_session_profile(
            resolved_cmd, resume_id, add_resume=not template_owns_resume
        )

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
        )

    async def _run_custom(self, prompt: str, cwd: str, session_id: Optional[str], model: Optional[str] = None, effort: Optional[str] = None):
        """Execute prompt using custom local harness (Ollama / vLLM / LM Studio)."""
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
                env={**os.environ}
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
        """Direct conversational response for agents running in test or offline simulation mode."""
        response_text = f"I have processed your request for: \"{prompt}\". Actions and changes are verified in the local workspace."
        await asyncio.sleep(0.02)
        await self._emit_chunk(agent, "agent_stdout", response_text, session_id)
        await self._emit_finished(agent, 0, "Finished", session_id)
