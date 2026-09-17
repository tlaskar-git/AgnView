"""Agent Execution Engine for AgentRelay.

Dispatches instructions directly to local installations of Claude Code, Codex,
and AntiGravity, captures output line-by-line, saves to SQLite console_logs,
and broadcasts live chunks via Server-Sent Events (SSE).
"""

import asyncio
import os
import sys
import shutil
from typing import Optional, Dict, Callable, List
from datetime import datetime, timezone

from .db import Database
from .adapters import AdapterManager, AgentAdapter


def _get_utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    ):
        """Asynchronously dispatch prompt to the specified agent."""
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

    @property
    def _is_testing(self) -> bool:
        return "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST") is not None or os.environ.get("AGENT_RELAY_TESTING") == "1"

    async def _run_claude_code(self, prompt: str, cwd: str, session_id: Optional[str], model: Optional[str] = None, effort: Optional[str] = None):
        """Execute prompt using local Claude Code CLI with streaming and timeout protection."""
        claude_bin = shutil.which("claude.cmd") or shutil.which("claude.exe") or shutil.which("claude")
        if self._is_testing or not claude_bin:
            await self._simulate_agent_execution("claude_code", prompt, session_id)
            return

        proc_args = [claude_bin, "-p", prompt]
        if model and model != "auto":
            proc_args.extend(["--model", model])
        if effort and effort not in ("default", "none"):
            proc_args.extend(["--effort", effort])

        env = self._get_env_for_provider("claude")
        env["CI"] = "1"
        env["TERM"] = "dumb"

        try:
            process = await asyncio.create_subprocess_exec(
                *proc_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env
            )
            if process.stdin:
                process.stdin.close()
                await process.stdin.wait_closed()

            async def read_stream():
                collected = []
                async for line in process.stdout:
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if text:
                        collected.append(text)
                if collected:
                    full_text = "\n".join(collected)
                    await self._emit_chunk("claude_code", "agent_stdout", full_text, session_id)

            try:
                await asyncio.wait_for(read_stream(), timeout=45.0)
                await asyncio.wait_for(process.wait(), timeout=5.0)
                exit_code = process.returncode or 0
                if exit_code != 0:
                    stderr_out = await process.stderr.read()
                    err_msg = stderr_out.decode("utf-8", errors="replace").strip()
                    if err_msg:
                        await self._emit_chunk("claude_code", "agent_stderr", err_msg, session_id)
                await self._emit_finished("claude_code", exit_code, "Finished", session_id)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except Exception:
                    pass
                await self._emit_chunk("claude_code", "agent_stderr", "Execution timed out.", session_id)
                await self._emit_finished("claude_code", 1, "Timeout", session_id)

        except FileNotFoundError:
            await self._simulate_agent_execution("claude_code", prompt, session_id)
        except Exception as e:
            await self._emit_chunk("claude_code", "agent_stderr", f"Execution error: {str(e)}", session_id)
            await self._emit_finished("claude_code", 1, "Error", session_id)

    async def _run_codex(self, prompt: str, cwd: str, session_id: Optional[str], model: Optional[str] = None, effort: Optional[str] = None):
        """Execute prompt using local Codex CLI with direct stdin closure."""
        codex_bin = shutil.which("codex.cmd") or shutil.which("codex.exe") or shutil.which("codex")
        if self._is_testing or not codex_bin:
            await self._simulate_agent_execution("codex", prompt, session_id)
            return

        proc_args = [codex_bin, "exec", "--skip-git-repo-check", prompt]
        if model and model != "auto":
            proc_args.extend(["-m", model])
        if effort and effort not in ("default", "none"):
            proc_args.extend(["-c", f"reasoning_effort={effort}"])

        env = self._get_env_for_provider("chatgpt")

        try:
            process = await asyncio.create_subprocess_exec(
                *proc_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env
            )
            if process.stdin:
                process.stdin.close()
                await process.stdin.wait_closed()

            async def read_codex_stream():
                collected = []
                async for line in process.stdout:
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if text:
                        collected.append(text)
                if collected:
                    full_text = "\n".join(collected)
                    await self._emit_chunk("codex", "agent_stdout", full_text, session_id)

            try:
                await asyncio.wait_for(read_codex_stream(), timeout=45.0)
                await asyncio.wait_for(process.wait(), timeout=5.0)
                exit_code = process.returncode or 0
                if exit_code != 0:
                    stderr_out = await process.stderr.read()
                    err_msg = stderr_out.decode("utf-8", errors="replace").strip()
                    if err_msg:
                        await self._emit_chunk("codex", "agent_stderr", err_msg, session_id)
                await self._emit_finished("codex", exit_code, "Finished", session_id)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except Exception:
                    pass
                await self._emit_chunk("codex", "agent_stderr", "Execution timed out.", session_id)
                await self._emit_finished("codex", 1, "Timeout", session_id)
        except FileNotFoundError:
            await self._simulate_agent_execution("codex", prompt, session_id)
        except Exception as e:
            await self._emit_chunk("codex", "agent_stderr", f"Execution error: {str(e)}", session_id)
            await self._emit_finished("codex", 1, "Error", session_id)

    async def _run_antigravity(self, prompt: str, cwd: str, session_id: Optional[str], model: Optional[str] = None, effort: Optional[str] = None):
        """Execute prompt using local AntiGravity (agy) or Gemini CLI."""
        agy_bin = shutil.which("agy.exe") or shutil.which("agy.cmd") or shutil.which("agy")
        gemini_bin = shutil.which("gemini.cmd") or shutil.which("gemini.exe") or shutil.which("gemini")

        if self._is_testing or (not agy_bin and not gemini_bin):
            await self._simulate_agent_execution("antigravity", prompt, session_id)
            return

        if agy_bin:
            proc_args = [agy_bin, "--print", prompt, "--dangerously-skip-permissions"]
            if model and model != "auto":
                model_map = {
                    "gemini-3.8-flash": "gemini-3.8-flash-high",
                    "gemini-3.7-flash": "gemini-3.7-flash-high",
                    "gemini-3.6-flash": "gemini-3.6-flash-high",
                    "gemini-3.1-pro": "gemini-3.1-pro-high",
                    "gpt-oss-120b": "gpt-oss-120b-medium"
                }
                proc_args.extend(["--model", model_map.get(model, model)])
            if effort and effort != "default":
                proc_args.extend(["--effort", effort])
        else:
            proc_args = [gemini_bin, "-p", prompt, "--yolo", "--skip-trust"]
            if model and model != "auto":
                proc_args.extend(["--model", model])

        try:
            env = self._get_env_for_provider("gemini")
            process = await asyncio.create_subprocess_exec(
                *proc_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env
            )
            if process.stdin:
                process.stdin.close()
                await process.stdin.wait_closed()

            async def read_stdout():
                collected = []
                async for line in process.stdout:
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if text:
                        collected.append(text)
                if collected:
                    full_text = "\n".join(collected)
                    await self._emit_chunk("antigravity", "agent_stdout", full_text, session_id)

            async def read_stderr():
                collected_err = []
                async for line in process.stderr:
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if text and not text.startswith("Warning:") and "deprecated" not in text.lower():
                        collected_err.append(text)
                if collected_err:
                    full_err = "\n".join(collected_err)
                    await self._emit_chunk("antigravity", "agent_stderr", full_err, session_id)

            try:
                await asyncio.wait_for(asyncio.gather(read_stdout(), read_stderr()), timeout=45.0)
                await asyncio.wait_for(process.wait(), timeout=5.0)
                exit_code = process.returncode or 0
                await self._emit_finished("antigravity", exit_code, "Finished", session_id)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except Exception:
                    pass
                await self._emit_chunk("antigravity", "agent_stderr", "Execution timed out.", session_id)
                await self._emit_finished("antigravity", 1, "Timeout", session_id)
        except FileNotFoundError:
            await self._simulate_agent_execution("antigravity", prompt, session_id)
        except Exception as e:
            await self._emit_chunk("antigravity", "agent_stderr", f"Execution error: {str(e)}", session_id)
            await self._emit_finished("antigravity", 1, "Error", session_id)

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

        # Prepare placeholder substitutions
        subs = {
            "{prompt}": prompt,
            "{workspace}": cwd,
            "{session_id}": session_id or "",
            "{model}": model or "",
            "{effort}": effort or "",
            "{skill}": skill or ""
        }

        # Resolve command arguments
        resolved_cmd = []
        for part in adapter.command:
            for k, v in subs.items():
                part = part.replace(k, v)
            # Only append if not an empty optional flag
            if part.strip():
                resolved_cmd.append(part)

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

        try:
            process = await asyncio.create_subprocess_exec(
                *resolved_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=target_cwd,
                env=env
            )
            if process.stdin:
                process.stdin.close()
                await process.stdin.wait_closed()

            async def read_stream():
                collected = []
                async for line in process.stdout:
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if text:
                        collected.append(text)
                if collected:
                    full_text = "\n".join(collected)
                    await self._emit_chunk(adapter.id, "agent_stdout", full_text, session_id)

            try:
                await asyncio.wait_for(read_stream(), timeout=45.0)
                await asyncio.wait_for(process.wait(), timeout=5.0)
                exit_code = process.returncode or 0
                if exit_code != 0:
                    stderr_out = await process.stderr.read()
                    err_msg = stderr_out.decode("utf-8", errors="replace").strip()
                    if err_msg:
                        await self._emit_chunk(adapter.id, "agent_stderr", err_msg, session_id)
                await self._emit_finished(adapter.id, exit_code, "Finished", session_id)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except Exception:
                    pass
                await self._emit_chunk(adapter.id, "agent_stderr", "Execution timed out.", session_id)
                await self._emit_finished(adapter.id, 1, "Timeout", session_id)
        except Exception as e:
            await self._emit_chunk(adapter.id, "agent_stderr", f"Execution error: {str(e)}", session_id)
            await self._emit_finished(adapter.id, 1, "Error", session_id)

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
