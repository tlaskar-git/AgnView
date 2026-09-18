"""Real Claude Code token usage, read from the transcripts Claude Code writes.

Claude Code appends one JSONL record per API response under
``~/.claude/projects/<project>/<session>.jsonl``, and every assistant record
carries the exact token counts the API charged for that turn. That file is the
only usage measurement available on the machine: Anthropic publishes no local
quota file and no local endpoint that reports how much of a subscription window
is gone.

So this module reports what can be counted and nothing else. Tokens are real,
summed from those records. The share of a plan limit is not derivable here, so
nothing in this module returns one.

Scope: every project directory under ``~/.claude/projects`` on this machine,
including the turns subagents took. That is deliberate. Anthropic bills the
signed-in account for all of it, so narrowing the sum to one repository would
report less than the account actually spent. What the sum cannot see is Claude
Code run under a different account or on another machine, so it is reported as
a per-machine figure and labelled as one. ``projects_counted`` is carried
alongside the totals so a large turn count can be explained rather than merely
asserted.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


PROJECTS_DIR = Path.home() / ".claude" / "projects"

SESSION_WINDOW_HOURS = 5
WEEK_WINDOW_DAYS = 7

# Records older than the widest window cannot affect the totals, and neither can
# a file whose last write predates it.
_FILE_MTIME_MARGIN = timedelta(hours=1)


@dataclass
class ClaudeCodeUsage:
    """Token counts measured over the two windows the dashboard shows."""

    session_tokens: int
    session_turns: int
    week_tokens: int
    week_turns: int
    transcripts_read: int
    latest_activity: Optional[datetime]
    # Distinct project directories that contributed a turn inside the week
    # window. Shown on the card so the turn count reads as a machine total
    # rather than as one person's typing.
    projects_counted: int = 0


def _parse_timestamp(raw: object) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _tokens_in(usage: dict) -> int:
    """Total tokens the API counted for one turn.

    Every field here is billed input or output. Cache reads and cache writes
    are counted because they are what the transcript records; leaving them out
    would under-report a Claude Code session by an order of magnitude.
    """
    total = 0
    for field in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        value = usage.get(field)
        if isinstance(value, int):
            total += value
    return total


def read_usage(
    projects_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Optional[ClaudeCodeUsage]:
    """Sum Claude Code token usage over the last 5 hours and the last 7 days.

    Returns None when there is no transcript directory to read, which is the
    honest answer for a machine where Claude Code has never run. An empty
    directory returns zeroed counts, because that is a real measurement.
    """
    root = Path(projects_dir) if projects_dir is not None else PROJECTS_DIR
    if not root.is_dir():
        return None

    moment = now or datetime.now(timezone.utc)
    session_cutoff = moment - timedelta(hours=SESSION_WINDOW_HOURS)
    week_cutoff = moment - timedelta(days=WEEK_WINDOW_DAYS)
    file_cutoff = (week_cutoff - _FILE_MTIME_MARGIN).timestamp()

    transcripts_read = 0
    latest: Optional[datetime] = None
    # One API request can be written more than once: an intermediate record
    # while the turn streams, then the final one. They carry the same request
    # id and different totals, so keep the largest, which is the finished turn.
    # Taking whichever was read first made the total depend on the order the
    # files happened to be walked.
    by_request: dict = {}
    unkeyed: list = []
    projects: set = set()

    # rglob, not glob: Claude Code writes a subagent's turns to
    # <project>/<session>/subagents/<agent>.jsonl, two levels below the session
    # transcript. Those turns are charged to the same account, so a shallow
    # scan under-reports by however much work was handed to subagents.
    for transcript in root.rglob("*.jsonl"):
        try:
            if transcript.stat().st_mtime < file_cutoff:
                continue
        except OSError:
            continue

        transcripts_read += 1
        # The first path part under the root is the project directory Claude
        # Code encoded from the working directory. A subagent transcript sits
        # deeper, under the same project, so it does not count as another one.
        try:
            project = transcript.relative_to(root).parts[0]
        except (ValueError, IndexError):
            project = transcript.parent.name

        try:
            handle = transcript.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue

        with handle:
            for line in handle:
                if '"usage"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                stamp = _parse_timestamp(record.get("timestamp"))
                if stamp is None or stamp < week_cutoff:
                    continue

                tokens = _tokens_in(usage)
                projects.add(project)
                key = record.get("requestId") or message.get("id")
                if key is None:
                    unkeyed.append((stamp, tokens))
                else:
                    previous = by_request.get(key)
                    if previous is None or tokens > previous[1]:
                        by_request[key] = (stamp, tokens)
                if latest is None or stamp > latest:
                    latest = stamp

    session_tokens = session_turns = week_tokens = week_turns = 0
    for stamp, tokens in list(by_request.values()) + unkeyed:
        week_tokens += tokens
        week_turns += 1
        if stamp >= session_cutoff:
            session_tokens += tokens
            session_turns += 1

    return ClaudeCodeUsage(
        session_tokens=session_tokens,
        session_turns=session_turns,
        week_tokens=week_tokens,
        week_turns=week_turns,
        transcripts_read=transcripts_read,
        latest_activity=latest,
        projects_counted=len(projects),
    )
