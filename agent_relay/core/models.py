"""Data models for AgentRelay."""

import uuid
from enum import Enum
from typing import List, Dict, Optional, Any
from datetime import datetime, timezone
from pydantic import BaseModel, Field


def _get_utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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

class UsageAccount(BaseModel):
    id: str = Field(default_factory=lambda: f"acc-{uuid.uuid4().hex[:8]}")  # e.g., "claude-pro-personal", "chatgpt-team-work"
    provider: str                            # "claude", "chatgpt", "gemini"
    name: str                                # User-friendly label
    auth_type: str = "api_key"               # "api_key" or "session_token"
    credential: str = ""                     # Stored securely, masked on client output
    org_id: Optional[str] = None
    plan_name: str = "Pro"                   # "Claude Pro", "ChatGPT Plus", "Gemini Advanced", etc.
    requests_used: int = 0
    requests_limit: int = 1000
    requests_remaining: int = 1000
    tokens_used: int = 0
    tokens_limit: int = 1000000
    tokens_remaining: int = 1000000
    cost_used_usd: Optional[float] = None
    cost_limit_usd: Optional[float] = None
    reset_time: Optional[str] = None         # Reset timestamp or countdown
    percent_used: float = 0.0
    status: str = "active"                   # "active", "warning", "exhausted", "error"
    last_checked: str = Field(default_factory=_get_utc_now_iso)
    error_message: Optional[str] = None
    base_url: Optional[str] = None           # Optional custom endpoint (e.g. DeepSeek or Ollama)

    # Detailed Session & Weekly Quota Telemetry (matching Claude, ChatGPT, Gemini settings pages)
    plan_label: Optional[str] = None         # e.g. "Max (20x)", "Plus (Codex & Agents)", "PRO"
    session_title: Optional[str] = None      # e.g. "Current session", "5-hour limit", "Current usage"
    session_reset_time: Optional[str] = None # e.g. "Resets in 1 hr 34 min", "Resets in 5h 0m", "Resets at 23:16"
    session_percent_used: Optional[float] = None # e.g. 4.0, 0.0
    session_percent_left: Optional[float] = None # e.g. 100.0, 96.0
    weekly_title: Optional[str] = None       # e.g. "Weekly limits", "Weekly limit"
    weekly_reset_time: Optional[str] = None  # e.g. "Resets Sat 7:00 PM", "Resets in 7d 0h", "Resets on 16 Sept at 13:16"
    weekly_percent_used: Optional[float] = None # e.g. 0.0, 3.0
    weekly_percent_left: Optional[float] = None # e.g. 100.0, 97.0
    weekly_breakdown: Optional[List[Dict[str, Any]]] = None # e.g. [{"label": "All models", "percent_used": 0, "reset_time": "Resets Sat 7:00 PM"}, {"label": "Fable", ...}]

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

    def masked(self) -> Dict[str, Any]:
        """Return dict with sensitive credentials masked."""
        d = self.model_dump()
        masked_c = self.masked_credential
        d["credential"] = masked_c
        d["masked_credential"] = masked_c
        d["last_synced_at"] = self.last_checked
        return d


class UsageTelemetryPayload(BaseModel):
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
    tokens_used: Optional[int] = None
    tokens_limit: Optional[int] = None
    percent_used: Optional[float] = None


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


