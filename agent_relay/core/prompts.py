"""Prompt generation and web context exporter for Claude.ai, ChatGPT.com, and Gemini (gemini.google.com)."""

import json
from typing import List, Optional

from .models import Task, Job


def append_files_context(prompt: str, files: Optional[List[str]]) -> str:
    """Add the "[Context Files: ...]" line every agent prompt carries.

    Console dispatch and the task prompt both call this, so a file is named to
    an agent the same way wherever it comes from.

    Entries are separated by a comma and a space and the line ends at a
    bracket, so an entry that holds a comma, a bracket, a double quote or a
    control character is written as a JSON string. That keeps a file name from
    ending the line early or from passing for two entries. A plain path, a
    Windows path with backslashes included, is written as it is.
    """
    if not files:
        return prompt
    return f"{prompt}\n[Context Files: {', '.join(_file_entry(f) for f in files)}]"


_NEEDS_QUOTING = frozenset(',[]"')


def _file_entry(path: str) -> str:
    if any(ch in _NEEDS_QUOTING or ord(ch) < 32 or ord(ch) == 127 for ch in path):
        return json.dumps(path, ensure_ascii=False)
    return path


def task_options_section(task: Task) -> str:
    """The requested model, effort and files of a task, or an empty string."""
    lines = []
    if task.model:
        lines.append(f"- Requested model: `{task.model}`")
    if task.effort:
        lines.append(f"- Requested effort: `{task.effort}`")
    if not lines and not task.files:
        return ""
    text = "\n### Run Options\n" + "\n".join(lines)
    return append_files_context(text, task.files).rstrip() + "\n"


def format_web_prompt_for_agent(
    job: Job,
    task: Task,
    server_url: str = "http://localhost:8765",
    target_llm: str = "claude.ai"
) -> str:
    """
    Formats a context-rich prompt ready to paste directly into Claude.ai, ChatGPT.com, or Google Gemini (gemini.google.com).
    Includes job objective, task instructions, outputs produced by upstream agents (Codex, AntiGravity, etc.),
    and instructions on how to signal completion or trigger revisions.
    """
    target_display = {
        "claude.ai": "Claude.ai",
        "chatgpt": "ChatGPT.com",
        "gemini": "Google Gemini (gemini.google.com)",
        "gemini.google.com": "Google Gemini (gemini.google.com)"
    }.get(target_llm.lower(), target_llm)
    upstream_sections = []
    for dep_id in task.dependencies:
        dep_task = job.tasks.get(dep_id)
        if dep_task:
            artifacts_text = "\n".join([f"    - {a}" for a in dep_task.artifacts]) if dep_task.artifacts else "    - None specified"
            summary_text = dep_task.output_summary or "Completed with no summary"
            upstream_sections.append(f"""
### Upstream Dependency: [{dep_task.id}] {dep_task.title}
- **Completed by Agent**: `{dep_task.assigned_agent}`
- **Status**: `{dep_task.status.value}`
- **Artifacts / Files Created**:
{artifacts_text}
- **Output Summary**:
```markdown
{summary_text}
```
""")

    upstream_text = "\n".join(upstream_sections) if upstream_sections else "No upstream prerequisites (first task in pipeline)."

    # Check for active revisions
    open_revisions = [r for r in task.revisions if r.status == "open"]
    revision_text = ""
    if open_revisions:
        rev_items = []
        for r in open_revisions:
            rev_items.append(f"""
> [!WARNING]
> **REVISION REQUESTED by `{r.from_agent}`**:
> {r.feedback}
""")
        revision_text = "\n### Active Revision Feedback:\n" + "\n".join(rev_items) + "\n"

    template = f"""# Agent Coordination Context: [{job.id}] {job.title}

Target Platform: **{target_display}**
You are acting as **{task.assigned_agent.upper()}** working on task **`{task.id}`**.
This job is being coordinated across multiple AI agents (Codex, AntiGravity, Claude Code, Gemini, and Web LLMs) using **AgentRelay**.

---

## 1. Overall Job Goal
{job.description or job.title}

---

## 2. Upstream Outputs from Prior Agents
{upstream_text}
{revision_text}
---

## 3. Your Assigned Task: `{task.id}` ({task.title})
{task.description or "Complete the assigned objective using the upstream artifacts provided above."}
{task_options_section(task)}
---

## 4. How to Report Your Work Back to the Team

Once you have finished (or if you encounter an issue with upstream tasks):

### Option A: Complete this task
Run this command in terminal, call the REST API, or click "Mark Complete" in the AgentRelay Web UI:
```bash
curl -X POST "{server_url}/api/tasks/{task.id}/complete" \\
     -H "Content-Type: application/json" \\
     -d '{{"summary": "<Brief summary of what you did>", "artifacts": ["path/to/file1", "path/to/file2"]}}'
```

### Option B: Request a revision if upstream work has defects
If you found something broken in an upstream task (e.g., in `{task.dependencies}`), ask that agent to fix it:
```bash
curl -X POST "{server_url}/api/tasks/<TARGET_TASK_ID>/request-revision" \\
     -H "Content-Type: application/json" \\
     -d '{{"feedback": "<Explain what is broken and what needs fixing>", "from_agent": "{task.assigned_agent}"}}'
```

Web Dashboard URL: {server_url}/#job/{job.id}
"""
    return template
