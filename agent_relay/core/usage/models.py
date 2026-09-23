"""The usage observation model.

An observation is what a source said, when it said it, and which window it
described. Nothing here is formatted for a human, and nothing here is a
countdown. A window carries ``window_end`` as an absolute instant, so the time
left is a subtraction performed at render time and is correct whenever it is
performed.

That is the whole point of this module. The previous design stored strings like
"Resets in 6d 22h", which were true at the moment of the write and wrong one
second later, and it stored no instant from which a correct value could be
recovered. See docs/adr/ADR-USAGE-OBSERVATIONS.md.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Source identity
# ---------------------------------------------------------------------------

# Every source an adapter can read, with the label the card shows. The label
# names the source, not the provider, so an operator can see at a glance
# whether a figure came from the provider's own accounting or was derived here.
SOURCE_LABELS: Dict[str, str] = {
    # Claude
    "claude_oauth_api": "Anthropic account usage",
    "claude_api_key": "Anthropic API rate limit",
    "claude_transcripts": "Counted from local transcripts",
    # ChatGPT and Codex
    "codex_auth": "ChatGPT account usage",
    "openai_api_key": "OpenAI API rate limit",
    # Gemini
    "gemini_code_assist": "Google Code Assist tier",
    "gemini_api_key": "Gemini API key check",
    # AntiGravity. The panel is the only source on this machine that yields a
    # real percentage for either AntiGravity or Gemini.
    "agy_cloud": "AntiGravity account quota",
    "agy_panel": "AntiGravity usage panel",
    "agy_log": "AntiGravity log",
    # Others
    "deepseek_api": "DeepSeek account balance",
    "custom_probe": "Endpoint probe",
    # Provider-independent
    "browser_sync": "Synced from the provider's usage page",
    "none": "No source",
}

# A measurement the provider itself performed and reported.
CONFIDENCE_MEASURED = "measured"
# A figure this machine worked out, such as a token sum or a count against a
# published limit. Real, but not the provider's own accounting.
CONFIDENCE_DERIVED = "derived"
# Nothing could be read. The reason is in ``error``.
CONFIDENCE_UNAVAILABLE = "unavailable"

# Window units. A window carries exactly one.
UNIT_PERCENT = "percent"
UNIT_TOKENS = "tokens"
UNIT_REQUESTS = "requests"
UNIT_CURRENCY = "currency"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_instant(raw: Optional[str]) -> Optional[datetime]:
    """Read an ISO stamp, treating a naive one as UTC. None when unreadable.

    Providers are inconsistent about the trailing Z, so it is normalised here
    rather than at each call site.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


class UsageWindow(BaseModel):
    """One limit window, as a source reported it.

    ``used`` and ``limit`` share the window's single ``unit``. A source that
    reports a share of a window sets ``unit="percent"`` and ``limit=100``. A
    source that can count but publishes no denominator leaves ``limit`` None,
    and the renderer then draws no bar, because there is nothing to be a share
    of.
    """

    key: str
    # How the source names this window. Kept verbatim so the card does not
    # rename a provider's own vocabulary.
    label: str
    # An optional short line explaining how the figure was reached, such as
    # "902 turns across 24 projects, subagents included". It renders in its own
    # full-width truncating row, never squeezed beside the value: a long
    # sub-line in a shrink-0 span used to overflow and draw over the token
    # count next to it.
    sub_label: Optional[str] = None
    unit: str
    used: Optional[float] = None
    limit: Optional[float] = None
    # Absolute instants. Never a countdown, never a formatted string.
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    # As the provider graded it, when it grades at all: "normal", "warning",
    # "exhausted". None means the provider said nothing and the renderer must
    # not invent a grading.
    severity: Optional[str] = None
    # True when the provider says this is the window currently binding.
    is_active: bool = False
    # Currency code, set only when unit is UNIT_CURRENCY.
    currency: Optional[str] = None
    # Same shape, one level deep. Per-model or per-surface rows.
    breakdown: List["UsageWindow"] = Field(default_factory=list)

    @property
    def percent_used(self) -> Optional[float]:
        """The share of this window spent, or None when there is no share.

        A window with no limit has no percentage. This property is the only
        place a percentage is worked out, so a card cannot invent one by
        dividing by a denominator the source never gave.
        """
        if self.used is None:
            return None
        if self.unit == UNIT_PERCENT:
            return round(float(self.used), 1)
        if not self.limit:
            return None
        return round(float(self.used) / float(self.limit) * 100.0, 1)

    @property
    def has_bar(self) -> bool:
        """True when this window can honestly be drawn as a progress bar."""
        return self.percent_used is not None


UsageWindow.model_rebuild()


class PlanInfo(BaseModel):
    """The plan a source named. Nothing is inferred when a source is silent."""

    name: Optional[str] = None
    label: Optional[str] = None


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


class UsageObservation(BaseModel):
    """What one source said at one moment.

    An observation is never edited after the fact and never partially refreshed.
    A new read produces a new observation. That is what stops a stale figure
    from being re-asserted over a fresh one, which is how a three-day-old
    percentage came to be shown as current.
    """

    source: str = "none"
    measured_at: datetime = Field(default_factory=utc_now)
    confidence: str = CONFIDENCE_UNAVAILABLE
    windows: List[UsageWindow] = Field(default_factory=list)
    plan: Optional[PlanInfo] = None
    error: Optional[str] = None
    # How long this source's figures stay current, which the renderer uses to
    # decide when to mark a card stale. Set by the adapter, because only the
    # adapter knows how often its source moves.
    expected_refresh_seconds: int = 900
    # Anything the adapter wants kept for diagnosis. Never rendered as a figure.
    notes: Dict[str, Any] = Field(default_factory=dict)

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)

    @property
    def is_measured(self) -> bool:
        return self.confidence == CONFIDENCE_MEASURED

    def window(self, key: str) -> Optional[UsageWindow]:
        for w in self.windows:
            if w.key == key:
                return w
        return None

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        return max(0.0, ((now or utc_now()) - self.measured_at).total_seconds())

    def is_stale(self, now: Optional[datetime] = None) -> bool:
        """True once this observation is older than twice its refresh interval.

        Twice, not once, so a card does not flicker into "stale" during the
        ordinary gap between a scheduled refresh and the read that follows it.
        """
        return self.age_seconds(now) >= self.expected_refresh_seconds * 2

    @classmethod
    def unavailable(
        cls,
        source: str,
        reason: str,
        plan: Optional[PlanInfo] = None,
        expected_refresh_seconds: int = 900,
    ) -> "UsageObservation":
        """Say plainly that nothing could be measured.

        An unavailable observation carries no windows at all, so there is no
        figure for a card to show even by accident. The previous design blanked
        a dozen fields by hand and missed ``weekly_breakdown``, which left
        invented per-model rows under an "unavailable" summary.
        """
        return cls(
            source=source,
            confidence=CONFIDENCE_UNAVAILABLE,
            windows=[],
            plan=plan,
            error=reason,
            expected_refresh_seconds=expected_refresh_seconds,
        )


# Standard window keys, so the renderer and the tests agree on names.
WINDOW_SESSION = "session"
WINDOW_WEEK = "week"
WINDOW_RATE_LIMIT = "rate_limit"
WINDOW_BALANCE = "balance"
WINDOW_CREDITS = "credits"
