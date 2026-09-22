"""Usage measurement, built on observations rather than stored presentations.

See docs/adr/ADR-USAGE-OBSERVATIONS.md for why this package exists and what it
replaced.
"""

from .models import (
    CONFIDENCE_DERIVED,
    CONFIDENCE_MEASURED,
    CONFIDENCE_UNAVAILABLE,
    UNIT_CURRENCY,
    UNIT_PERCENT,
    UNIT_REQUESTS,
    UNIT_TOKENS,
    PlanInfo,
    UsageObservation,
    UsageWindow,
    WINDOW_BALANCE,
    WINDOW_RATE_LIMIT,
    WINDOW_SESSION,
    WINDOW_WEEK,
    parse_instant,
    utc_now,
)
from .registry import (
    PROVIDERS,
    UnknownProvider,
    get_adapter,
    is_known,
    provider_options,
)
from .render import (
    format_age,
    format_countdown,
    legacy_status,
    project_legacy_fields,
    render_observation,
)
from .service import fetch_observation, observation_is_stale
from .snippets import USAGE_PAGES, build_sync_snippet

__all__ = [
    "CONFIDENCE_DERIVED",
    "CONFIDENCE_MEASURED",
    "CONFIDENCE_UNAVAILABLE",
    "UNIT_CURRENCY",
    "UNIT_PERCENT",
    "UNIT_REQUESTS",
    "UNIT_TOKENS",
    "PROVIDERS",
    "PlanInfo",
    "UnknownProvider",
    "UsageObservation",
    "UsageWindow",
    "WINDOW_BALANCE",
    "WINDOW_RATE_LIMIT",
    "WINDOW_SESSION",
    "WINDOW_WEEK",
    "USAGE_PAGES",
    "build_sync_snippet",
    "fetch_observation",
    "format_age",
    "format_countdown",
    "get_adapter",
    "is_known",
    "legacy_status",
    "observation_is_stale",
    "parse_instant",
    "project_legacy_fields",
    "provider_options",
    "render_observation",
    "utc_now",
]
