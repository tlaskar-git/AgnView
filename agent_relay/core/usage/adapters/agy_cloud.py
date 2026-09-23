"""AntiGravity quota, read from Google with AntiGravity's own sign-in.

AntiGravity asks Google's Code Assist service for its model quota, and that
service answers any caller holding the account's AntiGravity sign-in. This is
the same figure the app's model picker draws, per model, with its reset time.
Unlike the panel read below it in the ladder, it needs neither the app window
nor its model picker to be open.

On Windows, AntiGravity keeps its sign-in in Windows Credential Manager under
``gemini:antigravity``. AgnView reads it and never writes it. The access token
is used as stored. AntiGravity renews it whenever the app runs, and a token
that has expired is reported as such, so the card keeps its last reading
until the app next runs. The token stays in memory, is never logged, and goes
only to Google.

Google reports one limit per model. Models are grouped the way the model
picker groups them: Gemini models, and Claude and GPT models. A group's usage
is its most used model, because that is the limit a person hits first.
"""

import base64
import ctypes
import json
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import httpx

from ..base import AccountLike
from ..models import (
    CONFIDENCE_MEASURED,
    UNIT_PERCENT,
    PlanInfo,
    UsageObservation,
    UsageWindow,
    WINDOW_SESSION,
    parse_instant,
    utc_now,
)

CREDENTIAL_TARGET = "gemini:antigravity"
# Set to 0 to stop AgnView reading AntiGravity's sign-in at all.
ENABLE_ENV = "AGNVIEW_AGY_CLOUD"
BASE_URL = "https://cloudcode-pa.googleapis.com/v1internal"
REFRESH_SECONDS = 180
# A token this close to expiry is treated as expired, so a read never starts
# with a token that dies in flight.
EXPIRY_MARGIN = timedelta(seconds=60)

GROUP_GEMINI = "Gemini models"
GROUP_OTHERS = "Claude and GPT models"

OPEN_APP_HINT = (
    "Open AntiGravity once so it renews its sign-in. AgnView does not renew "
    "it, and keeps the last reading until then."
)


# ---------------------------------------------------------------------------
# The sign-in
# ---------------------------------------------------------------------------


def _read_windows_credential(target: str) -> Optional[bytes]:
    if sys.platform != "win32":
        return None
    from ctypes import wintypes as wt

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wt.DWORD),
            ("Type", wt.DWORD),
            ("TargetName", wt.LPWSTR),
            ("Comment", wt.LPWSTR),
            ("LastWritten", wt.FILETIME),
            ("CredentialBlobSize", wt.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wt.DWORD),
            ("AttributeCount", wt.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wt.LPWSTR),
            ("UserName", wt.LPWSTR),
        ]

    CRED_TYPE_GENERIC = 1
    advapi = ctypes.windll.advapi32
    pointer = ctypes.POINTER(CREDENTIAL)()
    if not advapi.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        return None
    try:
        record = pointer.contents
        return bytes(record.CredentialBlob[: record.CredentialBlobSize])
    finally:
        advapi.CredFree(pointer)


def read_sign_in() -> Optional[dict]:
    """AntiGravity's stored sign-in, or None when there is none on this machine."""
    blob = _read_windows_credential(CREDENTIAL_TARGET)
    if not blob:
        return None
    try:
        data = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _token_and_expiry(sign_in: dict) -> Tuple[Optional[str], Optional[datetime]]:
    token_block = sign_in.get("token") or {}
    token = (token_block.get("access_token") or "").strip() or None
    expiry = None
    raw = token_block.get("expiry")
    if isinstance(raw, str) and raw:
        # AntiGravity writes seven fractional digits, one more than Python
        # parses, for example 2026-09-23T17:10:08.0770689+01:00.
        head, dot, rest = raw.partition(".")
        if dot:
            count = 0
            while count < len(rest) and rest[count].isdigit():
                count += 1
            raw = f"{head}.{rest[:count][:6]}{rest[count:]}"
        expiry = parse_instant(raw)
    return token, expiry


def account_email(sign_in: dict) -> Optional[str]:
    """The signed-in account, from the ID token AntiGravity stored beside it."""
    id_token = sign_in.get("id_token") or ""
    parts = id_token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return None
    email = claims.get("email")
    return email if isinstance(email, str) else None


# ---------------------------------------------------------------------------
# The quota
# ---------------------------------------------------------------------------

_project_cache: Dict[str, Tuple[Optional[str], Optional[str]]] = {}


def _load_project(client: httpx.Client, headers: dict, cache_key: str) -> Tuple[Optional[str], Optional[str]]:
    """The Code Assist project and the plan name. Both change rarely."""
    if cache_key in _project_cache:
        return _project_cache[cache_key]
    res = client.post(
        f"{BASE_URL}:loadCodeAssist",
        headers=headers,
        json={"metadata": {"ideType": "ANTIGRAVITY", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}},
    )
    res.raise_for_status()
    data = res.json()
    project = data.get("cloudaicompanionProject")
    if isinstance(project, dict):
        project = project.get("id")
    tier = data.get("paidTier") or data.get("currentTier") or {}
    plan = tier.get("name") if isinstance(tier, dict) else None
    _project_cache[cache_key] = (project, plan)
    return project, plan


def _group_of(model_id: str, info: dict) -> Optional[str]:
    provider = (info.get("modelProvider") or "").upper()
    if "GOOGLE" in provider or model_id.startswith("gemini"):
        return GROUP_GEMINI
    if "ANTHROPIC" in provider or "OPENAI" in provider or model_id.startswith(("claude", "gpt")):
        return GROUP_OTHERS
    return None


def windows_from_models(payload: dict, now: Optional[datetime] = None) -> List[UsageWindow]:
    """One limit for the account, broken down by model group.

    Only the models the app offers in its model picker are counted. The
    response also lists internal models, such as tab completion, which carry
    no reset and are not something a person picks.
    """
    now = now or utc_now()
    models = payload.get("models") or {}
    offered: Optional[set] = None
    for sort in payload.get("agentModelSorts") or []:
        for group in (sort or {}).get("groups") or []:
            offered = (offered or set()) | set(group.get("modelIds") or [])

    groups: Dict[str, List[Tuple[float, Optional[datetime]]]] = {}
    for model_id, info in models.items():
        if not isinstance(info, dict):
            continue
        if offered is not None and model_id not in offered:
            continue
        quota = info.get("quotaInfo") or {}
        remaining = quota.get("remainingFraction")
        if remaining is None:
            continue
        group = _group_of(model_id, info)
        if group is None:
            continue
        try:
            used = max(0.0, min(100.0, (1.0 - float(remaining)) * 100.0))
        except (TypeError, ValueError):
            continue
        groups.setdefault(group, []).append((used, parse_instant(quota.get("resetTime"))))

    if not groups:
        return []

    children: List[UsageWindow] = []
    for name in (GROUP_GEMINI, GROUP_OTHERS):
        rows = groups.get(name)
        if not rows:
            continue
        ends = [end for _, end in rows if end is not None and end > now]
        children.append(
            UsageWindow(
                key=f"session_group_{name.lower().replace(' ', '_')}",
                label=name,
                unit=UNIT_PERCENT,
                used=round(max(used for used, _ in rows), 1),
                limit=100.0,
                window_end=min(ends) if ends else None,
            )
        )

    ends = [child.window_end for child in children if child.window_end is not None]
    return [
        UsageWindow(
            key=WINDOW_SESSION,
            label="Model quota",
            unit=UNIT_PERCENT,
            used=max(child.used or 0.0 for child in children),
            limit=100.0,
            window_end=min(ends) if ends else None,
            is_active=True,
            breakdown=children,
        )
    ]


def fetch_cloud_quota(product: str) -> Optional[UsageObservation]:
    """Read the quota from Google. None when AntiGravity has no sign-in here."""
    if os.environ.get(ENABLE_ENV, "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    sign_in = read_sign_in()
    if not sign_in:
        return None

    email = account_email(sign_in)
    plan = PlanInfo(name=product, label=email)
    token, expiry = _token_and_expiry(sign_in)
    if not token:
        return UsageObservation.unavailable(
            "agy_cloud", "AntiGravity's stored sign-in carries no access token. " + OPEN_APP_HINT,
            plan=plan, expected_refresh_seconds=REFRESH_SECONDS,
        )
    if expiry is not None and expiry - EXPIRY_MARGIN <= utc_now():
        return UsageObservation.unavailable(
            "agy_cloud", "AntiGravity's sign-in has expired. " + OPEN_APP_HINT,
            plan=plan, expected_refresh_seconds=REFRESH_SECONDS,
        )

    # Google answers this service for the AntiGravity client, which names
    # itself in the user agent.
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "antigravity",
    }
    try:
        with httpx.Client(timeout=20.0) as client:
            project, plan_name = _load_project(client, headers, email or "default")
            res = client.post(
                f"{BASE_URL}:fetchAvailableModels",
                headers=headers,
                json={"project": project} if project else {},
            )
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        reason = (
            "Google refused AntiGravity's sign-in. " + OPEN_APP_HINT
            if status in (401, 403)
            else f"Google's quota service returned status {status}."
        )
        return UsageObservation.unavailable("agy_cloud", reason, plan=plan, expected_refresh_seconds=REFRESH_SECONDS)
    except Exception as exc:
        return UsageObservation.unavailable(
            "agy_cloud", f"Could not reach Google's quota service: {exc}",
            plan=plan, expected_refresh_seconds=REFRESH_SECONDS,
        )

    if res.status_code in (401, 403):
        return UsageObservation.unavailable(
            "agy_cloud", "Google refused AntiGravity's sign-in. " + OPEN_APP_HINT,
            plan=plan, expected_refresh_seconds=REFRESH_SECONDS,
        )
    if res.status_code != 200:
        return UsageObservation.unavailable(
            "agy_cloud", f"Google's quota service returned status {res.status_code}.",
            plan=plan, expected_refresh_seconds=REFRESH_SECONDS,
        )
    try:
        payload = res.json()
    except ValueError:
        payload = {}

    windows = windows_from_models(payload)
    if not windows:
        return UsageObservation.unavailable(
            "agy_cloud", "Google returned no model quota for this AntiGravity account.",
            plan=plan, expected_refresh_seconds=REFRESH_SECONDS,
        )

    return UsageObservation(
        source="agy_cloud",
        confidence=CONFIDENCE_MEASURED,
        windows=windows,
        plan=PlanInfo(name=plan_name or product, label=email),
        expected_refresh_seconds=REFRESH_SECONDS,
    )


def fetch_antigravity(account: AccountLike) -> Optional[UsageObservation]:
    return fetch_cloud_quota("AntiGravity")


def fetch_gemini(account: AccountLike) -> Optional[UsageObservation]:
    cred = (account.credential or "").strip()
    if cred.startswith("AIza"):
        # An AI Studio key names a different account. Leave it to its rung.
        return None
    return fetch_cloud_quota("Gemini")
