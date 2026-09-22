"""DeepSeek usage.

DeepSeek reports money, not a share of a window. The balance is carried as a
currency window with no limit, so the renderer draws no bar and no percentage.
Back-deriving a percentage from a balance would mean inventing a starting
figure nobody reported.
"""

from typing import List, Optional, Tuple

import httpx

from ..base import AccountLike, SourceFn, UsageAdapter
from ..models import (
    CONFIDENCE_MEASURED,
    UNIT_CURRENCY,
    PlanInfo,
    UsageObservation,
    UsageWindow,
    WINDOW_BALANCE,
)

DEFAULT_BASE_URL = "https://api.deepseek.com"
REFRESH_SECONDS = 900


def _as_float(value) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_balance(account: AccountLike) -> Optional[UsageObservation]:
    cred = (account.credential or "").strip()
    base_url = (account.base_url or DEFAULT_BASE_URL).rstrip("/")
    plan = PlanInfo(name="DeepSeek", label="Pay as you go")

    if not cred:
        return UsageObservation.unavailable(
            "deepseek_api",
            "This DeepSeek account carries no API key.",
            plan=plan,
            expected_refresh_seconds=REFRESH_SECONDS,
        )

    headers = {"Authorization": f"Bearer {cred}", "Accept": "application/json"}
    try:
        with httpx.Client(timeout=12.0) as client:
            models = client.get(f"{base_url}/models", headers=headers)
            if models.status_code in (401, 403):
                return UsageObservation.unavailable(
                    "deepseek_api",
                    "Invalid DeepSeek API key.",
                    plan=plan,
                    expected_refresh_seconds=REFRESH_SECONDS,
                )
            if models.status_code != 200:
                return UsageObservation.unavailable(
                    "deepseek_api",
                    f"DeepSeek returned status {models.status_code}.",
                    plan=plan,
                    expected_refresh_seconds=REFRESH_SECONDS,
                )

            balance = None
            if "api.deepseek.com" in base_url:
                try:
                    res = client.get(f"{base_url}/user/balance", headers=headers)
                    if res.status_code == 200:
                        infos = (res.json() or {}).get("balance_infos") or []
                        if infos:
                            balance = infos[0]
                except Exception:
                    balance = None
    except Exception as exc:
        return UsageObservation.unavailable(
            "deepseek_api",
            f"Could not reach DeepSeek: {exc}",
            plan=plan,
            expected_refresh_seconds=REFRESH_SECONDS,
        )

    if balance is None:
        return UsageObservation.unavailable(
            "deepseek_api",
            "The DeepSeek key is valid but the balance endpoint returned nothing, "
            "so there is no usage figure to show.",
            plan=plan,
            expected_refresh_seconds=REFRESH_SECONDS,
        )

    total = _as_float(balance.get("total_balance"))
    if total is None:
        return UsageObservation.unavailable(
            "deepseek_api",
            "DeepSeek reported a balance record with no total in it.",
            plan=plan,
            expected_refresh_seconds=REFRESH_SECONDS,
        )

    return UsageObservation(
        source="deepseek_api",
        confidence=CONFIDENCE_MEASURED,
        windows=[
            UsageWindow(
                key=WINDOW_BALANCE,
                label="Balance remaining",
                unit=UNIT_CURRENCY,
                used=total,
                limit=None,
                currency=balance.get("currency") or "USD",
            )
        ],
        plan=plan,
        expected_refresh_seconds=REFRESH_SECONDS,
    )


class DeepSeekAdapter(UsageAdapter):
    provider = "deepseek"
    display_name = "DeepSeek"
    hint = "DeepSeek API key (V3 / R1)"

    def sources(self) -> List[Tuple[str, SourceFn]]:
        return [("deepseek_api", fetch_balance)]
