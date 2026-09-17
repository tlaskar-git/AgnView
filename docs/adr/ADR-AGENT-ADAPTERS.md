# Architecture Decision Record: Dynamic Agent Adapters (ADR-AGENT-ADAPTERS)

## Context & Problem Statement

In AgnView v1, supported agents (`codex`, `antigravity`, `claude_code`, and `gemini`) are hardcoded into:
- The dispatch runner execution logic (`agent_relay/core/runner.py`)
- CLI dispatch defaults and working directory resolvers (`agent_relay/cli/main.py`)
- Web frontend filter pills, target selects, command palette items, and badge styling (`agent_relay/web/templates/index.html`)
- Server-side task assignment validation (`agent_relay/api/routes.py`)
- A fifth identifier, `deepseek`, appears in the console filter tabs and usage telemetry copy but has no backing agent process or native CLI.

Users who do not use this exact local toolchain cannot use AgnView without editing python source files.

## Decision

Decouple agent definitions from the codebase into a user-configurable adapter system:
- Stored at `~/.agnview/agents.yaml`, overridable with `--agents-file`.
- Schema defines `display`, `command`, `cwd`, `icon`, `colour`, and `enabled`.
- Supported placeholders: `{prompt}`, `{workspace}`, `{session_id}`, `{model}`, `{effort}`, `{skill}`.
- Unknown keys fail load with file and line information.
- The 4 default agents are written to `~/.agnview/agents.yaml` on first run if the file is absent.
- `deepseek` is removed from hardcoded console pills and usage copy.
- All UI selects, filter pills, and server validation read dynamically from the loaded adapters.
- `GET /api/agents` returns adapter metadata (with the `command` array stripped to prevent local path exposure).
- A `POST /api/agents/reload` endpoint and UI action re-reads the adapters configuration without restarting the server.

---

## Codebase Audit & Mapping

### 1. Hardcoded Agent Identifier Occurrences (`codex`, `antigravity`, `claude_code`, `gemini`)

#### `agent_relay/api/routes.py`
- Line 52: Validation set `valid_roles = {"codex", "antigravity", "claude_code", "gemini", "user", "system"}` in `create_job`
- Lines 524-526: Installed CLIs check for `claude_code`, `codex`, and `antigravity` in `/api/system/capabilities`
- Lines 542-550: System capabilities default working directory mapping for `claude_code`, `codex`, and `antigravity`

#### `agent_relay/cli/main.py`
- Lines 407, 442, 452: Role choices and default roles for `agent-relay worker` (`claude_code`, `codex`, `antigravity`)
- Lines 700-725: CLI dispatch targets (`claude_code`, `codex`, `antigravity`, `gemini`)

#### `agent_relay/core/runner.py`
- Lines 85, 91, 97: Default workspace candidate directory searches for `claude_code`, `codex`, `antigravity`
- Lines 117-124: Dispatch routing branches (`_run_claude_code`, `_run_codex`, `_run_antigravity`, `_run_deepseek`, `_run_custom`)
- Lines 147-153: Provider environment credential injection for `claude`, `chatgpt`, `gemini`, `deepseek`
- Lines 163-226: `_run_claude_code` method
- Lines 227-287: `_run_codex` method
- Lines 288-366: `_run_antigravity` method
- Lines 367-380: `_run_deepseek` method

#### `agent_relay/web/templates/index.html`
- Lines 473-491: Console filter pills (`pill-claude`, `pill-codex`, `pill-antigravity`, `pill-deepseek`)
- Lines 539-546: Dispatch target select dropdown (`<option value="claude_code">`, `codex`, `antigravity`, `deepseek`)
- Lines 1717, 1826-1830: New pipeline task assigned agent select options
- Lines 1957-2046: `PROVIDER_DATA` dictionary holding models and settings for `claude_code`, `codex`, `antigravity`, `deepseek`
- Lines 2890-2894: `updateConsoleCounters()` hardcoded counts for `claude`, `codex`, `antigravity`, `deepseek`
- Lines 2925-2930: `filterConsoleLogs()` hardcoded filtering checks
- Lines 3058-3083: `renderConsoleStream()` hardcoded agent styles and badges
- Lines 3788-3796: `getAgentBadge()` hardcoded agent badge CSS classes
- Lines 5294-5300: Command palette dispatch actions (`dispatch-claude`, `dispatch-codex`, `dispatch-antigravity`)

---

### 2. Exact Dispatch Call Path

When a user dispatches an instruction:
1. **HTTP Endpoint**: The client POSTs to `/api/console/dispatch` with payload `{ target_agent, prompt, working_dir, session_id, model, effort, files, skill }`.
2. **FastAPI Route**: `routes.py:dispatch_to_agent()` receives the request and calls `engine.runner.dispatch(...)`.
3. **Runner Core**: `runner.py:dispatch()` normalises `target = agent.lower().strip()`.
4. **Workspace Resolution**: If `cwd` is omitted, it scans predefined candidate directories under the user's home folder (e.g. `~/.claude` or `~/Claude`, `~/.codex` or `~/Codex`, `~/.gemini/antigravity` or `~/.gemini`).
5. **Prompt Augmentation**: Prepends active `/skill` and appends context files.
6. **Execution Subprocess**: Matches target in `if/elif` chain:
   - `claude_code` -> `runner._run_claude_code()` -> spawns `claude -p "{prompt}"`
   - `codex` -> `runner._run_codex()` -> spawns `codex exec --skip-git-repo-check "{prompt}"`
   - `antigravity` -> `runner._run_antigravity()` -> spawns `agy --print "{prompt}" --dangerously-skip-permissions` or `gemini -p "{prompt}"`
7. **Line Capture & Output Stream**: Standard output lines are read asynchronously, saved to SQLite via `db.add_console_log()`, and broadcast to clients via SSE (`agent_output_chunk`).

---

### 3. Sources of Agent Lists in the Frontend

- **Console Filter Pills**: Hardcoded HTML buttons in `index.html` (lines 473-491) with IDs `pill-all`, `pill-claude`, `pill-codex`, `pill-antigravity`, `pill-deepseek`.
- **Target Select**: Hardcoded `<select id="dispatch-target-agent">` options in `index.html` (lines 539-546).
- **Pipeline Agent List**: Hardcoded `<select>` inside the New Job modal in `index.html` (lines 1826-1830).
- **Server Validation**: Hardcoded set `valid_roles` in `routes.py:create_job()` (line 52).

Under Section 2, all four of these locations will populate dynamically from the adapter schema loaded via `~/.agnview/agents.yaml`.

---

### 4. Analysis of the `deepseek` Identifier

The identifier `deepseek` appears in:
1. `index.html`:
   - Line 487: A hidden button `#pill-deepseek` in console filter pills.
   - Line 544: A hidden `<option value="deepseek">` in the dispatch target select.
   - Line 1830: An `<option value="deepseek">` in the new task assigned agent select.
   - Line 2035: In `PROVIDER_DATA` with placeholder models.
   - Line 2893: In `updateConsoleCounters()`.
   - Lines 4089, 4201, 4688, 4719, 4996: Usage telemetry references for DeepSeek API tokens.
2. `agent_relay/core/runner.py`:
   - Line 123, 367-380: `_run_deepseek()` which only prints a simulated notice.
3. `agent_relay/core/usage_fetcher.py`:
   - Line 386: `_fetch_deepseek_live()` for pulling usage telemetry from platform.deepseek.com.

Findings:
There is no local DeepSeek CLI agent binary installed or supported natively. Its presence in the console and agent dropdowns displays a permanent zero count or falls back to synthetic simulation. Under the dynamic adapter design, `deepseek` is removed from hardcoded agent dropdowns and console filters, allowing users to configure it via `~/.agnview/agents.yaml` if they use an agent CLI for it.
