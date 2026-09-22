"""Custom and local harness usage.

A local harness enforces no quota, so there is nothing to measure beyond whether
it answers. The observation carries no windows at all, which is the honest
result: reachable, and no limit to report. The card shows the endpoint and its
reachability, and no bar, because there is no share of anything to draw.

This adapter is now reachable only when an account asks for it by name. It used
to be the silent fallthrough for every unrecognised provider, which is how an
AntiGravity account ended up being probed as an Ollama server.
"""

from typing import List, Optional, Tuple

import httpx

from ..base import AccountLike, SourceFn, UsageAdapter
from ..models import (
    CONFIDENCE_MEASURED,
    PlanInfo,
    UsageObservation,
)

DEFAULT_BASE_URL = "http://localhost:11434"
PROBE_PATHS = ("/v1/models", "/models", "/api/tags", "/health")
REFRESH_SECONDS = 300


def probe(account: AccountLike) -> Optional[UsageObservation]:
    cred = (account.credential or "").strip()
    base_url = (account.base_url or DEFAULT_BASE_URL).rstrip("/")
    plan = PlanInfo(name="Local harness", label="No quota")

    headers = {"Accept": "application/json"}
    if cred:
        headers["Authorization"] = f"Bearer {cred}"

    try:
        with httpx.Client(timeout=8.0) as client:
            for path in PROBE_PATHS:
                try:
                    res = client.get(f"{base_url}{path}", headers=headers)
                except Exception:
                    continue
                if res.status_code in (200, 204):
                    return UsageObservation(
                        source="custom_probe",
                        confidence=CONFIDENCE_MEASURED,
                        windows=[],
                        plan=plan,
                        error=None,
                        expected_refresh_seconds=REFRESH_SECONDS,
                        notes={"endpoint": base_url, "answered_at": path},
                    )
                if res.status_code == 401:
                    return UsageObservation.unavailable(
                        "custom_probe",
                        "The harness rejected the credential on this account.",
                        plan=plan,
                        expected_refresh_seconds=REFRESH_SECONDS,
                    )
    except Exception as exc:
        return UsageObservation.unavailable(
            "custom_probe",
            f"Could not reach the harness at {base_url}: {exc}",
            plan=plan,
            expected_refresh_seconds=REFRESH_SECONDS,
        )

    return UsageObservation.unavailable(
        "custom_probe",
        f"Could not reach the harness at {base_url}.",
        plan=plan,
        expected_refresh_seconds=REFRESH_SECONDS,
    )


class CustomAdapter(UsageAdapter):
    provider = "custom"
    display_name = "Custom or local LLM harness"
    hint = "Ollama, vLLM, LM Studio or any OpenAI-compatible endpoint"

    def sources(self) -> List[Tuple[str, SourceFn]]:
        return [("custom_probe", probe)]
