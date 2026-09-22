"""Data models for AgentRelay."""

import uuid
from enum import Enum
from typing import List, Dict, Optional, Any
from datetime import datetime, timezone
from pydantic import BaseModel, Field

from .usage.models import UsageObservation
from .usage.snippets import USAGE_PAGES
from .usage.render import legacy_status, project_legacy_fields, render_observation


def _get_utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _provider_has_usage_page(provider: Optional[str]) -> bool:
    """True when this provider has a page a browser sync could read.

    AntiGravity, DeepSeek and a local harness have none: their adapters read a
    real source directly, so there is never a script to paste for them.
    """
    return bool(USAGE_PAGES.get((provider or "").strip().lower()))


def agent_role_of(assigned_agent: str) -> str:
    """Return the role part of an assignment such as 'codex@alice-laptop'."""
    return (assigned_agent or "").split("@", 1)[0].strip().lower()


class TaskStatus(str, Enum):
    PENDING = "pending"                     # Waiting for upstream dependencies to finish
    READY = "ready"                         # All dependencies met, ready to be claimed
    IN_PROGRESS = "in_progress"             # Agent has claimed and is working on it
    COMPLETED = "completed"                 # Successfully finished
    REVISION_REQUESTED = "revision_requested" # Downstream agent requested fix/revision
    FAILED = "failed"                       # Execution failed
    BLOCKED = "blocked"                     # An upstream task failed, so this cannot start


class JobStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    REVISION_IN_PROGRESS = "revision_in_progress"
    FAILED = "failed"


class RevisionFeedback(BaseModel):
    id: str
    task_id: str
    from_agent: str
    target_agent: str
    feedback: str
    status: str = "open"  # "open" or "resolved"
    created_at: str = Field(default_factory=_get_utc_now_iso)
    resolved_at: Optional[str] = None
    resolution_summary: Optional[str] = None


class Task(BaseModel):
    id: str
    job_id: str
    title: str
    description: str = ""
    assigned_agent: str                     # Target role or specific instance (e.g. "codex", "claude_code@alice-laptop")
    executed_by: Optional[str] = None       # The exact instance/machine/account that claimed/completed it
    dependencies: List[str] = Field(default_factory=list) # List of prerequisite task IDs
    status: TaskStatus = TaskStatus.PENDING
    output_summary: Optional[str] = None
    artifacts: List[str] = Field(default_factory=list)
    revisions: List[RevisionFeedback] = Field(default_factory=list)
    created_at: str = Field(default_factory=_get_utc_now_iso)
    updated_at: str = Field(default_factory=_get_utc_now_iso)
    completed_at: Optional[str] = None


class Job(BaseModel):
    id: str
    title: str
    description: str = ""
    status: JobStatus = JobStatus.PENDING
    tasks: Dict[str, Task] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_get_utc_now_iso)
    updated_at: str = Field(default_factory=_get_utc_now_iso)


class AgentInstance(BaseModel):
    instance_id: str                        # e.g., "claude_code@alice-macbook", "codex@gpu-box-1"
    role: str                               # "codex", "claude_code", "antigravity", etc.
    hostname: str = ""
    account: str = ""
    status: str = "online"                  # "online", "busy", "offline"
    current_task_id: Optional[str] = None
    last_heartbeat: str = Field(default_factory=_get_utc_now_iso)
    metadata: Dict[str, Any] = Field(default_factory=dict)


# Request & Response schemas
class AgentHeartbeatRequest(BaseModel):
    instance_id: str
    role: str
    hostname: Optional[str] = None
    account: Optional[str] = None
    status: str = "online"
    current_task_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TaskSpec(BaseModel):
    id: str
    title: str
    description: str = ""
    assigned_agent: str
    dependencies: List[str] = Field(default_factory=list)


class CreateJobRequest(BaseModel):
    id: Optional[str] = None
    title: str
    description: str = ""
    tasks: List[TaskSpec]


class ClaimTaskRequest(BaseModel):
    agent: str
    instance_id: Optional[str] = None


class CompleteTaskRequest(BaseModel):
    summary: str
    artifacts: List[str] = Field(default_factory=list)
    agent: Optional[str] = None
    instance_id: Optional[str] = None


class RequestRevisionRequest(BaseModel):
    feedback: str
    from_agent: str


class FailTaskRequest(BaseModel):
    reason: str
    agent: Optional[str] = None
    instance_id: Optional[str] = None


class TaskWaitResponse(BaseModel):
    task_id: str
    job_id: str
    ready: bool
    status: TaskStatus
    unmet_dependencies: List[str] = Field(default_factory=list)
    upstream_summaries: Dict[str, Any] = Field(default_factory=dict)
    open_revisions: List[RevisionFeedback] = Field(default_factory=list)
    task: Optional[Task] = None


# ----------------- Subscription Usage & Quota Models -----------------

# Providers whose only real quota percentage comes from the browser telemetry
# sync. Matched as substrings, so "anthropic" and "google" land here too.
_TELEMETRY_SYNC_PROVIDERS = ("claude", "anthropic", "gemini", "google", "antigravity")

_PERCENT_KEYS = (
    "percent_used",
    "percent_left",
    "session_percent_used",
    "session_percent_left",
    "weekly_percent_used",
    "weekly_percent_left",
)


def rows_carry_a_percentage(rows: Optional[List[Dict[str, Any]]]) -> bool:
    """True when any breakdown row holds a percentage something really measured."""
    for row in rows or []:
        if any(row.get(key) is not None for key in _PERCENT_KEYS):
            return True
    return False


class UsageAccount(BaseModel):
    id: str = Field(default_factory=lambda: f"acc-{uuid.uuid4().hex[:8]}")  # e.g., "claude-pro-personal", "chatgpt-team-work"
    provider: str                            # "claude", "chatgpt", "gemini"
    name: str                                # User-friendly label
    auth_type: str = "api_key"               # "api_key" or "session_token"
    credential: str = ""                     # Stored securely, masked on client output
    org_id: Optional[str] = None
    plan_name: str = "Pro"                   # "Claude Pro", "ChatGPT Plus", "Gemini Advanced", etc.
    # Every measurement below is None until something real is read. A default
    # number here reads on the dashboard as a measurement that was taken, which
    # is how invented quota figures reached the Usage tab in the first place.
    requests_used: Optional[int] = None
    requests_limit: Optional[int] = None
    requests_remaining: Optional[int] = None
    tokens_used: Optional[int] = None
    tokens_limit: Optional[int] = None
    tokens_remaining: Optional[int] = None
    cost_used_usd: Optional[float] = None
    cost_limit_usd: Optional[float] = None
    reset_time: Optional[str] = None         # Reset timestamp or countdown
    percent_used: Optional[float] = None
    status: str = "unknown"                  # "active", "unavailable", "warning", "exhausted", "error"
    last_checked: str = Field(default_factory=_get_utc_now_iso)
    error_message: Optional[str] = None
    base_url: Optional[str] = None           # Optional custom endpoint (e.g. DeepSeek or Ollama)

    # Detailed Session & Weekly Quota Telemetry (matching Claude, ChatGPT, Gemini settings pages)
    plan_label: Optional[str] = None         # e.g. "Max (20x)", "Plus (Codex & Agents)", "PRO"
    session_title: Optional[str] = None      # e.g. "Current session", "5-hour limit", "Current usage"
    session_reset_time: Optional[str] = None # e.g. "Resets in 1 hr 34 min", "Resets in 5h 0m", "Resets at 23:16"
    session_percent_used: Optional[float] = None # e.g. 4.0, 0.0
    session_percent_left: Optional[float] = None # e.g. 100.0, 96.0
    # Tokens actually counted in each window. A provider that reports a share
    # of a limit but not a token count leaves these None, and the other way
    # round for a provider that can be counted but publishes no limit.
    session_tokens_used: Optional[int] = None
    weekly_tokens_used: Optional[int] = None
    weekly_title: Optional[str] = None       # e.g. "Weekly limits", "Weekly limit"
    weekly_reset_time: Optional[str] = None  # e.g. "Resets Sat 7:00 PM", "Resets in 7d 0h", "Resets on 16 Sept at 13:16"
    weekly_percent_used: Optional[float] = None # e.g. 0.0, 3.0
    weekly_percent_left: Optional[float] = None # e.g. 100.0, 97.0
    weekly_breakdown: Optional[List[Dict[str, Any]]] = None # e.g. [{"label": "All models", "percent_used": 0, "reset_time": "Resets Sat 7:00 PM"}, {"label": "Fable", ...}]
    # AntiGravity publishes a five-hour figure per model group as well as a
    # weekly one. The observation model carries those as breakdown rows on the
    # window they belong to, which is what keeps the two clocks apart without a
    # second flat field. This stays so a row written by an older version loads.
    session_breakdown: Optional[List[Dict[str, Any]]] = None

    # Retained only so an older row still loads. Nothing reads them. The
    # telemetry-replay mechanism they served is gone: it kept a synced
    # percentage for seven days and wrote it back over every fresh read,
    # re-marking the account active, which is how a three-day-old figure came
    # to be shown as current. See docs/adr/ADR-USAGE-OBSERVATIONS.md.
    session_telemetry_synced_at: Optional[str] = None
    weekly_telemetry_synced_at: Optional[str] = None

    # The single source of truth for every figure on the card. Everything a
    # human sees is computed from this at request time and never stored.
    observation: Optional[UsageObservation] = None

    def __init__(self, **data):
        if "auth_credential" in data and not data.get("credential"):
            data["credential"] = data.pop("auth_credential")
        super().__init__(**data)

    @property
    def masked_credential(self) -> str:
        c = self.credential or ""
        if len(c) > 8:
            return f"{c[:4]}...{c[-4:]}"
        return "****"

    @property
    def needs_telemetry_sync(self) -> bool:
        """True when a browser sync is the only thing that would fill this card.

        Asked by the Usage tab so a card can offer the sync. It reads the
        observation, because that is where every figure lives now: asking for a
        sync while an adapter is already measuring the account would send an
        operator to paste a script they do not need.

        The question is therefore not "which provider is this" but "did the
        source ladder produce a window". A provider whose top rung works needs
        nothing; one whose rungs all came back empty is waiting on a sync, if it
        has a usage page at all.
        """
        observation = self.observation
        if observation is None:
            return _provider_has_usage_page(self.provider)
        if observation.windows:
            return False
        return _provider_has_usage_page(self.provider)

    def masked(self) -> Dict[str, Any]:
        """The account as the API returns it, with the credential masked.

        Every figure is derived from ``observation`` here, at this moment, and
        none of it is written back. A countdown produced at read time cannot be
        stale, which is the defect that made the Claude card wrong.

        ``usage`` is the shape the rebuilt front end reads. The flat fields
        beside it are projected for the older markup and go away with it.
        """
        d = self.model_dump()
        masked_c = self.masked_credential
        d["credential"] = masked_c
        d["masked_credential"] = masked_c

        d["usage"] = render_observation(self.observation)
        d.update(project_legacy_fields(self.observation))
        d["status"] = legacy_status(self.observation)
        d["error_message"] = self.observation.error if self.observation else None
        measured_at = d["usage"].get("measured_at")
        d["last_checked"] = measured_at or self.last_checked
        d["last_synced_at"] = d["last_checked"]
        d["needs_telemetry_sync"] = self.needs_telemetry_sync
        return d


class UsageTelemetryWindow(BaseModel):
    """One window read off a provider's own usage page.

    ``window_end`` is an absolute ISO instant, computed in the browser from
    whatever the page showed. The countdown text itself is never sent, because
    it is only true at the moment it is read.
    """

    key: str                                 # "session" or "week"
    label: Optional[str] = None
    percent_used: float
    window_end: Optional[str] = None         # ISO instant, not a countdown
    breakdown: Optional[List[Dict[str, Any]]] = None


def _telemetry_rows(raw, used_key: str, title_key: str) -> Optional[List[Dict[str, Any]]]:
    """Normalise a per-group breakdown onto one shape.

    AntiGravity names its columns session_percent_used / weekly_percent_used and
    its label session_title / weekly_title, while the claude.ai reader sends
    percent_used and label. Both are accepted, because a sync that silently
    dropped its breakdown would leave a headline figure with nothing under it.

    Module level rather than a method: pydantic reserves underscore-prefixed
    names on a model and hides them from instance access.
    """
    if not raw:
        return None
    rows: List[Dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        percent = row.get(used_key, row.get("percent_used"))
        label = (row.get("group") or row.get("label") or row.get(title_key) or "").strip()
        if percent is None or not label:
            continue
        rows.append({
            "label": label,
            "percent_used": percent,
            "window_end": row.get("window_end"),
        })
    return rows or None


class UsageTelemetryPayload(BaseModel):
    """What a browser sync sends.

    ``windows`` is the shape the generated snippets use. The flat fields below
    it are accepted so a snippet from an older build still lands, but a reset
    countdown sent as text is discarded rather than stored: it cannot be turned
    back into an instant, and storing it is the defect this rebuild removes.
    """

    plan_name: Optional[str] = None
    plan_label: Optional[str] = None
    windows: Optional[List[UsageTelemetryWindow]] = None

    # Accepted for compatibility. Percentages are used, reset strings are not.
    session_title: Optional[str] = None
    session_reset_time: Optional[str] = None
    session_percent_used: Optional[float] = None
    session_percent_left: Optional[float] = None
    weekly_title: Optional[str] = None
    weekly_reset_time: Optional[str] = None
    weekly_percent_used: Optional[float] = None
    weekly_percent_left: Optional[float] = None
    weekly_breakdown: Optional[List[Dict[str, Any]]] = None
    session_breakdown: Optional[List[Dict[str, Any]]] = None
    tokens_used: Optional[int] = None
    tokens_limit: Optional[int] = None
    percent_used: Optional[float] = None

    def to_windows(self) -> List[UsageTelemetryWindow]:
        """Normalise whichever shape arrived into a list of windows."""
        if self.windows:
            return list(self.windows)
        built: List[UsageTelemetryWindow] = []
        if self.session_percent_used is not None:
            built.append(
                UsageTelemetryWindow(
                    key="session",
                    label=self.session_title or "Session",
                    percent_used=self.session_percent_used,
                    breakdown=_telemetry_rows(
                        self.session_breakdown, "session_percent_used", "session_title"
                    ),
                )
            )
        if self.weekly_percent_used is not None:
            built.append(
                UsageTelemetryWindow(
                    key="week",
                    label=self.weekly_title or "Weekly",
                    percent_used=self.weekly_percent_used,
                    breakdown=_telemetry_rows(
                        self.weekly_breakdown, "weekly_percent_used", "weekly_title"
                    ),
                )
            )
        return built


class CreateUsageAccountRequest(BaseModel):
    id: Optional[str] = None
    provider: str                            # "claude", "chatgpt", "gemini", "deepseek", "custom"
    name: str                                # e.g. "Work Claude Pro"
    auth_type: str = "api_key"               # "api_key", "session_token", or "none"
    credential: Optional[str] = None
    auth_credential: Optional[str] = None
    org_id: Optional[str] = None
    plan_name: Optional[str] = None
    plan_label: Optional[str] = None
    session_title: Optional[str] = None
    session_reset_time: Optional[str] = None
    session_percent_used: Optional[float] = None
    session_percent_left: Optional[float] = None
    weekly_title: Optional[str] = None
    weekly_reset_time: Optional[str] = None
    weekly_percent_used: Optional[float] = None
    weekly_percent_left: Optional[float] = None
    weekly_breakdown: Optional[List[Dict[str, Any]]] = None
    session_breakdown: Optional[List[Dict[str, Any]]] = None
    tokens_limit: Optional[int] = None
    cost_limit_usd: Optional[float] = None
    base_url: Optional[str] = None           # Custom API base URL or local harness

    def get_credential(self) -> str:
        return (self.credential or self.auth_credential or "").strip()


class UpdateUsageAccountRequest(BaseModel):
    name: Optional[str] = None
    plan_name: Optional[str] = None
    plan_label: Optional[str] = None
    auth_type: Optional[str] = None
    credential: Optional[str] = None
    auth_credential: Optional[str] = None
    base_url: Optional[str] = None
    org_id: Optional[str] = None
    session_title: Optional[str] = None
    session_reset_time: Optional[str] = None
    session_percent_used: Optional[float] = None
    session_percent_left: Optional[float] = None
    weekly_title: Optional[str] = None
    weekly_reset_time: Optional[str] = None
    weekly_percent_used: Optional[float] = None
    weekly_percent_left: Optional[float] = None
    weekly_breakdown: Optional[List[Dict[str, Any]]] = None
    session_breakdown: Optional[List[Dict[str, Any]]] = None

    def get_credential(self) -> Optional[str]:
        val = self.credential or self.auth_credential
        return val.strip() if val else None

class DetectTokenRequest(BaseModel):
    provider: Optional[str] = None


class UsageSummary(BaseModel):
    total_accounts: int
    accounts_by_provider: Dict[str, int]
    accounts: List[Dict[str, Any]]


# ----------------- Interactive Console Models -----------------

class ConsoleDispatchPayload(BaseModel):
    agent: str                               # "claude_code", "codex", "antigravity", "all", "deepseek", "custom"
    prompt: str
    working_directory: Optional[str] = None
    mode: str = "direct"                     # "direct" (execute immediately) or "pipeline_task"
    session_id: Optional[str] = None         # cosmetic UI grouping label for console_logs
    model: Optional[str] = None
    effort: Optional[str] = None             # "low", "medium", "high"
    files: Optional[List[str]] = None
    skill: Optional[str] = None              # e.g. "/goal", "brandkit", etc.
    reset_session: bool = False              # forget the stored CLI conversation and start fresh


class SavedPrompt(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    title: str
    prompt: str
    category: Optional[str] = "custom"
    created_at: str = Field(default_factory=_get_utc_now_iso)


class ConsoleLogEntry(BaseModel):
    id: Optional[int] = None
    agent: str                               # "claude_code", "codex", "antigravity", "user", "system"
    source: str                              # "user_input", "agent_stdout", "agent_stderr", "system_notice"
    content: str
    timestamp: str = Field(default_factory=_get_utc_now_iso)
    session_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


