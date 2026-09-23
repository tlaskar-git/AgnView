"""Per-adapter behaviour: what each source reports, and what it refuses to.

Replaces tests/test_usage_telemetry.py, which tested the flat-field fetchers
and, in two cases, tested the replay defect itself as if it were a feature.
See docs/adr/ADR-USAGE-OBSERVATIONS.md.
"""

import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from agent_relay.api.app import create_app
from agent_relay.core.db import Database
from agent_relay.core.models import UsageAccount
from agent_relay.core.usage import (
    CONFIDENCE_DERIVED,
    CONFIDENCE_MEASURED,
    CONFIDENCE_UNAVAILABLE,
    UNIT_PERCENT,
    UNIT_TOKENS,
    UsageObservation,
    UsageWindow,
    observation_is_stale,
    utc_now,
)
from agent_relay.core.usage.adapters import antigravity as agy_adapter
from agent_relay.core.usage.adapters import chatgpt as chatgpt_adapter
from agent_relay.core.usage.adapters import claude as claude_adapter
from agent_relay.core.usage.adapters import google_code_assist as google_adapter
from agent_relay.core.claude_code_usage import ClaudeCodeUsage


def _account(provider: str, **kwargs) -> UsageAccount:
    return UsageAccount(id=f"test-{provider}", provider=provider, name=provider, **kwargs)


class _Response:
    def __init__(self, status_code=200, json_body=None, headers=None):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._json


class _Client:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, *args, **kwargs):
        return self._response

    def post(self, *args, **kwargs):
        return self._response


def _patch_http(monkeypatch, module, response):
    monkeypatch.setattr(module.httpx, "Client", lambda *a, **k: _Client(response))


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------


def _write_claude_token(tmp_path, monkeypatch, expires_in_hours=5):
    path = tmp_path / ".credentials.json"
    expires_at = int((utc_now() + timedelta(hours=expires_in_hours)).timestamp() * 1000)
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "token-value",
                    "expiresAt": expires_at,
                    "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_20x",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(claude_adapter, "CREDENTIALS_PATH", path)
    return path


# The shape confirmed against the live endpoint on 2026-09-22, abridged.
_CLAUDE_USAGE_BODY = {
    "five_hour": {"utilization": 35.0, "resets_at": "2026-09-22T20:30:00+00:00"},
    "seven_day": {"utilization": 38.0, "resets_at": "2026-09-26T18:00:00+00:00"},
    "limits": [
        {
            "kind": "session",
            "group": "session",
            "percent": 35,
            "severity": "normal",
            "resets_at": "2026-09-22T20:30:00+00:00",
            "scope": None,
            "is_active": False,
        },
        {
            "kind": "weekly_all",
            "group": "weekly",
            "percent": 38,
            "severity": "normal",
            "resets_at": "2026-09-26T18:00:00+00:00",
            "scope": None,
            "is_active": False,
        },
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 42,
            "severity": "warning",
            "resets_at": "2026-09-26T18:00:00+00:00",
            "scope": {"model": {"display_name": "Fable"}},
            "is_active": True,
        },
    ],
    "seven_day_breakdown": {
        "as_of": "2026-09-22T17:37:20+00:00",
        "window_started_at": "2026-09-19T18:00:00+00:00",
        "rows": [
            {"key": "claude_code", "display_name": "Claude Code", "percent": 71},
            {"key": "chat", "display_name": "Chats", "percent": 26},
        ],
    },
    # Codenamed keys for unreleased limits. Every one must be ignored.
    "nimbus_quill": {"utilization": 0.0, "resets_at": None},
    "tangelo": None,
    "iguana_necktie": None,
}


def test_claude_reports_the_real_plan_windows(tmp_path, monkeypatch):
    """Session and weekly come from Anthropic's own account usage endpoint."""
    _write_claude_token(tmp_path, monkeypatch)
    _patch_http(monkeypatch, claude_adapter, _Response(200, _CLAUDE_USAGE_BODY))

    observation = claude_adapter.fetch_oauth_api(_account("claude"))

    assert observation.confidence == CONFIDENCE_MEASURED
    assert observation.source == "claude_oauth_api"
    assert observation.plan.name == "Claude Max"
    assert observation.plan.label == "Max 20x"

    labels = [w.label for w in observation.windows]
    assert labels == [
        "Session, last 5 hours",
        "Weekly, all models, 7 days",
        "Weekly, Fable",
    ]

    session = observation.windows[0]
    assert session.unit == UNIT_PERCENT
    assert session.used == 35.0
    # An absolute instant, so the countdown is correct whenever it is drawn.
    assert session.window_end == datetime(2026, 9, 22, 20, 30, tzinfo=timezone.utc)

    scoped = observation.windows[2]
    assert scoped.used == 42.0
    assert scoped.is_active is True
    assert scoped.severity == "warning"


def test_claude_attaches_the_per_surface_split_to_the_weekly_window(tmp_path, monkeypatch):
    """The breakdown belongs to the unscoped weekly window it describes.

    It must not land on the session window or on the scoped per-model one,
    because Anthropic reports these rows as shares of the weekly total.
    """
    _write_claude_token(tmp_path, monkeypatch)
    _patch_http(monkeypatch, claude_adapter, _Response(200, _CLAUDE_USAGE_BODY))

    observation = claude_adapter.fetch_oauth_api(_account("claude"))
    session, weekly_all, weekly_scoped = observation.windows

    assert session.breakdown == []
    assert weekly_scoped.breakdown == []
    assert [child.label for child in weekly_all.breakdown] == ["Claude Code", "Chats"]
    assert [child.used for child in weekly_all.breakdown] == [71.0, 26.0]
    # The window start comes from the breakdown block, which is the only place
    # the endpoint reports it.
    assert weekly_all.window_start == datetime(2026, 9, 19, 18, 0, tzinfo=timezone.utc)


def test_claude_ignores_the_codenamed_limit_keys(tmp_path, monkeypatch):
    """Only `limits[]` is read. The codenamed blocks are not usage windows."""
    _write_claude_token(tmp_path, monkeypatch)
    _patch_http(monkeypatch, claude_adapter, _Response(200, _CLAUDE_USAGE_BODY))

    observation = claude_adapter.fetch_oauth_api(_account("claude"))
    labels = " ".join(w.label for w in observation.windows).lower()
    for codename in ("nimbus", "quill", "tangelo", "iguana"):
        assert codename not in labels


def test_claude_reports_an_expired_token_it_could_not_renew(tmp_path, monkeypatch):
    """AgnView never rewrites the file Claude Code owns. With the renewal
    through Claude Code switched off, the card says what to run."""
    _write_claude_token(tmp_path, monkeypatch, expires_in_hours=-1)

    observation = claude_adapter.fetch_oauth_api(_account("claude"))

    assert observation.confidence == CONFIDENCE_UNAVAILABLE
    assert "expired" in observation.error
    assert "Run any claude command" in observation.error
    # The plan is still known from the credential, so the card can name it.
    assert observation.plan.label == "Max 20x"


def test_claude_refused_sign_in_is_unavailable(tmp_path, monkeypatch):
    _write_claude_token(tmp_path, monkeypatch)
    _patch_http(monkeypatch, claude_adapter, _Response(401))

    observation = claude_adapter.fetch_oauth_api(_account("claude"))

    assert observation.confidence == CONFIDENCE_UNAVAILABLE
    assert observation.windows == []


def test_claude_falls_back_to_counted_tokens_with_no_percentage(monkeypatch):
    """The transcript sum is a count, marked derived, with no bar.

    Anthropic writes no rate-limit data into the transcripts, so no share of a
    plan limit can be derived from them. Reporting one would be an invention.
    """
    monkeypatch.setattr(
        claude_adapter,
        "read_claude_code_usage",
        lambda: ClaudeCodeUsage(
            session_tokens=1500,
            session_turns=3,
            week_tokens=9000,
            week_turns=20,
            transcripts_read=4,
            latest_activity=None,
            projects_counted=2,
        ),
    )
    monkeypatch.setattr(claude_adapter, "PROFILE_PATH", claude_adapter.PROFILE_PATH.parent / "none")

    observation = claude_adapter.fetch_transcripts(_account("claude"))

    assert observation.confidence == CONFIDENCE_DERIVED
    for window in observation.windows:
        assert window.unit == UNIT_TOKENS
        assert window.limit is None
        assert window.percent_used is None
        assert window.has_bar is False
    assert observation.windows[0].used == 1500
    assert observation.windows[1].used == 9000


def test_an_api_key_account_skips_the_subscription_rung(tmp_path, monkeypatch):
    """An sk-ant- key must not report a different account's plan windows."""
    _write_claude_token(tmp_path, monkeypatch)
    assert claude_adapter.fetch_oauth_api(_account("claude", credential="sk-ant-abc")) is None


# ---------------------------------------------------------------------------
# ChatGPT
# ---------------------------------------------------------------------------


def test_chatgpt_anchors_a_reset_offset_to_an_absolute_instant(tmp_path, monkeypatch):
    """"reset_after_seconds" is only true at the moment it is read."""
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps({"tokens": {"access_token": "token-value", "account_id": "acct"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(chatgpt_adapter, "CODEX_AUTH_PATH", auth)
    _patch_http(
        monkeypatch,
        chatgpt_adapter,
        _Response(
            200,
            {
                "plan_type": "plus",
                "rate_limit": {
                    "primary_window": {"used_percent": 41.5, "reset_after_seconds": 7380},
                    "secondary_window": {"used_percent": 12.0, "reset_after_seconds": 180000},
                },
            },
        ),
    )

    before = utc_now()
    observation = chatgpt_adapter.fetch_codex_auth(_account("chatgpt"))
    after = utc_now()

    assert observation.confidence == CONFIDENCE_MEASURED
    session, weekly = observation.windows
    assert session.used == 41.5
    assert weekly.used == 12.0
    # Anchored to the read, not stored as a countdown.
    assert before + timedelta(seconds=7380) <= session.window_end <= after + timedelta(seconds=7380)
    assert before + timedelta(seconds=180000) <= weekly.window_end <= after + timedelta(seconds=180000)
    # ChatGPT reports no token count, so none is derived from the percentage.
    assert all(w.unit == UNIT_PERCENT for w in observation.windows)


def test_chatgpt_refused_sign_in_is_unavailable(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "stale"}}), encoding="utf-8")
    monkeypatch.setattr(chatgpt_adapter, "CODEX_AUTH_PATH", auth)
    _patch_http(monkeypatch, chatgpt_adapter, _Response(401))

    observation = chatgpt_adapter.fetch_codex_auth(_account("chatgpt"))

    assert observation.confidence == CONFIDENCE_UNAVAILABLE
    assert observation.windows == []


# ---------------------------------------------------------------------------
# Gemini and AntiGravity
# ---------------------------------------------------------------------------


def test_gemini_without_a_sign_in_is_unavailable_rather_than_a_constant(tmp_path, monkeypatch):
    monkeypatch.setattr(google_adapter, "CREDS_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(google_adapter, "ACCOUNTS_PATH", tmp_path / "missing-accounts.json")

    observation = google_adapter.fetch_code_assist("gemini_code_assist", "Gemini", "GEMINI")

    assert observation.confidence == CONFIDENCE_UNAVAILABLE
    assert observation.windows == []
    assert observation.error


def test_gemini_names_the_signed_in_account_in_an_expiry_reason(tmp_path, monkeypatch):
    creds = tmp_path / "oauth_creds.json"
    creds.write_text(
        json.dumps({"access_token": "t", "expiry_date": 1_000_000_000_000}), encoding="utf-8"
    )
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text(json.dumps({"active": "someone@example.com"}), encoding="utf-8")
    monkeypatch.setattr(google_adapter, "CREDS_PATH", creds)
    monkeypatch.setattr(google_adapter, "ACCOUNTS_PATH", accounts)

    observation = google_adapter.fetch_code_assist("gemini_code_assist", "Gemini", "GEMINI")

    assert observation.confidence == CONFIDENCE_UNAVAILABLE
    assert "someone@example.com" in observation.error
    assert "expired" in observation.error


def test_a_tier_without_a_quota_reports_no_figure(tmp_path, monkeypatch):
    """Google names a tier but attaches no usage count. That is not a figure."""
    creds = tmp_path / "oauth_creds.json"
    creds.write_text(
        json.dumps(
            {"access_token": "t", "expiry_date": int((utc_now().timestamp() + 3600) * 1000)}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(google_adapter, "CREDS_PATH", creds)
    monkeypatch.setattr(google_adapter, "ACCOUNTS_PATH", tmp_path / "none.json")
    _patch_http(
        monkeypatch, google_adapter, _Response(200, {"currentTier": {"name": "free-tier"}})
    )

    observation = google_adapter.fetch_code_assist("gemini_code_assist", "Gemini", "GEMINI")

    assert observation.confidence == CONFIDENCE_UNAVAILABLE
    assert observation.plan.label == "free-tier"
    assert observation.windows == []
    assert "no quota figure" in observation.error


def test_a_quota_needs_both_a_count_and_a_limit_to_be_shown(tmp_path, monkeypatch):
    """A limit on its own is a denominator with no numerator."""
    creds = tmp_path / "oauth_creds.json"
    creds.write_text(
        json.dumps(
            {"access_token": "t", "expiry_date": int((utc_now().timestamp() + 3600) * 1000)}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(google_adapter, "CREDS_PATH", creds)
    monkeypatch.setattr(google_adapter, "ACCOUNTS_PATH", tmp_path / "none.json")
    _patch_http(
        monkeypatch,
        google_adapter,
        _Response(200, {"currentTier": {"name": "standard"}, "quota": {"limit": 1000}}),
    )

    observation = google_adapter.fetch_code_assist("gemini_code_assist", "Gemini", "GEMINI")
    assert observation.windows == [], "a limit with no used count is not a measurement"


def test_antigravity_reports_the_signed_out_reason_from_its_own_log(tmp_path, monkeypatch):
    """A blank card is replaced by the actual, actionable cause."""
    log = tmp_path / "cli.log"
    log.write_text(
        "I0922 18:07:00.655620 188 server.go:3605] GetG1Credits: starting fetch\n"
        "E0922 18:07:00.658800 188 credits_manager.go:42] failed to refresh G1 credits: "
        "failed to get load code assist response: error getting token source: "
        "You are not logged into Antigravity.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(agy_adapter, "CLI_LOG", log)
    monkeypatch.setattr(agy_adapter, "DESKTOP_LOG", tmp_path / "missing.log")

    observation = agy_adapter.fetch_log(_account("antigravity"))

    assert observation.confidence == CONFIDENCE_UNAVAILABLE
    assert "not signed in" in observation.error
    assert "Run agy and sign in" in observation.error
    assert observation.windows == []


def test_antigravity_prefers_an_outcome_over_an_announcement(tmp_path, monkeypatch):
    """"starting fetch" is not a result, even when it is the newest line."""
    log = tmp_path / "cli.log"
    log.write_text(
        "E0922 18:07:00.100000 188 credits_manager.go:42] failed to refresh G1 credits: "
        "quota service unreachable\n"
        "I0922 18:07:00.900000 191 quota_manager.go:45] doRefreshQuota: starting reload\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(agy_adapter, "CLI_LOG", log)
    monkeypatch.setattr(agy_adapter, "DESKTOP_LOG", tmp_path / "missing.log")

    observation = agy_adapter.fetch_log(_account("antigravity"))
    assert "quota service unreachable" in observation.error
    assert "starting reload" not in observation.error


def test_antigravity_ignores_an_auth_line_that_merely_says_quotaproject(tmp_path, monkeypatch):
    """A loose match on "quota" pulled in an OAuth line that says nothing."""
    log = tmp_path / "cli.log"
    log.write_text(
        "I0922 18:07:00.773313 1 server_oauth.go:196] applyAuthResult: "
        "email=someone@example.com, authMethod=consumer, quotaProject=\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(agy_adapter, "CLI_LOG", log)
    monkeypatch.setattr(agy_adapter, "DESKTOP_LOG", tmp_path / "missing.log")

    observation = agy_adapter.fetch_log(_account("antigravity"))
    assert "applyAuthResult" not in (observation.error or "")


def test_antigravity_with_no_log_says_it_has_never_run(tmp_path, monkeypatch):
    monkeypatch.setattr(agy_adapter, "CLI_LOG", tmp_path / "missing.log")
    monkeypatch.setattr(agy_adapter, "DESKTOP_LOG", tmp_path / "missing2.log")

    observation = agy_adapter.fetch_log(_account("antigravity"))
    assert "has not run on this machine" in observation.error


# ---------------------------------------------------------------------------
# Refresh intervals and persistence
# ---------------------------------------------------------------------------


def test_refresh_intervals_reflect_what_a_read_costs():
    """A local file walk is cheap. A rate-limited endpoint is not."""
    local = UsageObservation(source="claude_transcripts", expected_refresh_seconds=60)
    remote = UsageObservation(source="claude_oauth_api", expected_refresh_seconds=300)

    now = utc_now()
    local.measured_at = now - timedelta(seconds=90)
    remote.measured_at = now - timedelta(seconds=90)

    assert observation_is_stale(local, now) is True
    assert observation_is_stale(remote, now) is False
    assert observation_is_stale(None, now) is True


def test_an_observation_survives_a_round_trip_through_sqlite(tmp_path):
    """Absolute instants must come back as instants, not as text."""
    db = Database(str(tmp_path / "round-trip.db"))
    window_end = datetime(2026, 9, 26, 18, 0, tzinfo=timezone.utc)

    account = UsageAccount(id="acc-round-trip", provider="claude", name="Personal Claude")
    account.observation = UsageObservation(
        source="claude_oauth_api",
        confidence=CONFIDENCE_MEASURED,
        windows=[
            UsageWindow(
                key="week",
                label="Weekly, all models, 7 days",
                unit=UNIT_PERCENT,
                used=38.0,
                limit=100.0,
                window_end=window_end,
            )
        ],
    )
    db.save_usage_account(account.model_dump())

    loaded = UsageAccount(**db.get_usage_account("acc-round-trip"))
    assert loaded.observation.windows[0].window_end == window_end
    assert loaded.observation.windows[0].used == 38.0


def test_a_telemetry_sync_is_stored_as_its_own_observation(tmp_path):
    """A browser sync is one source among several, not an override."""
    app = create_app(db_path=str(tmp_path / "sync.db"))
    client = TestClient(app)

    created = client.post("/api/usage/accounts", json={
        "id": "acc-sync-test",
        "provider": "gemini",
        "name": "Sync Test",
        "auth_type": "api_key",
        "auth_credential": "placeholder-token",
    })
    assert created.status_code == 200

    res = client.post("/api/usage/accounts/acc-sync-test/telemetry", json={
        "plan_label": "PRO",
        "windows": [
            {
                "key": "session",
                "label": "Current usage",
                "percent_used": 4.0,
                "window_end": "2026-09-22T23:16:00+00:00",
            },
            {
                "key": "week",
                "label": "Weekly limit",
                "percent_used": 3.0,
                "window_end": "2026-09-26T13:16:00+00:00",
            },
        ],
    })
    assert res.status_code == 200
    synced = res.json()

    assert synced["usage"]["source"] == "browser_sync"
    assert synced["usage"]["source_label"] == "Synced from the provider's usage page"
    assert synced["session_title"] == "Current usage"
    assert synced["session_percent_used"] == 4.0
    assert synced["weekly_percent_used"] == 3.0
    assert synced["plan_label"] == "PRO"
    # The countdown is computed from the instant, never stored.
    assert synced["usage"]["windows"][0]["window_end"] == "2026-09-22T23:16:00+00:00"


def test_a_sync_with_no_window_is_refused(tmp_path):
    """A failed scrape must report nothing rather than post a zero."""
    app = create_app(db_path=str(tmp_path / "sync-empty.db"))
    client = TestClient(app)
    client.post("/api/usage/accounts", json={
        "id": "acc-empty",
        "provider": "gemini",
        "name": "Empty",
        "auth_type": "api_key",
        "auth_credential": "placeholder-token",
    })

    res = client.post("/api/usage/accounts/acc-empty/telemetry", json={"plan_label": "PRO"})
    assert res.status_code == 400
    assert "no usage window" in res.json()["detail"]


# ---------------------------------------------------------------------------
# Shapes confirmed live on 2026-09-22 after signing both tools in
# ---------------------------------------------------------------------------

# What loadCodeAssist actually returns for a personal Gemini CLI sign-in. There
# is no currentTier at all, and Google refuses the client outright.
_CODE_ASSIST_INELIGIBLE = {
    "allowedTiers": [
        {
            "id": "standard-tier",
            "name": "Gemini Code Assist",
            "isDefault": True,
            "userDefinedCloudaicompanionProject": True,
        }
    ],
    "ineligibleTiers": [
        {
            "reasonCode": "UNSUPPORTED_CLIENT",
            "reasonMessage": (
                "This client is no longer supported for Gemini Code Assist for "
                "individuals. To continue using Gemini, please migrate to the "
                "Antigravity suite of products: https://antigravity.google"
            ),
            "tierId": "free-tier",
            "tierName": "Gemini Code Assist for individuals",
        }
    ],
}


def _fresh_google_token(tmp_path, monkeypatch):
    creds = tmp_path / "oauth_creds.json"
    creds.write_text(
        json.dumps(
            {"access_token": "t", "expiry_date": int((utc_now().timestamp() + 3600) * 1000)}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(google_adapter, "CREDS_PATH", creds)
    monkeypatch.setattr(google_adapter, "ACCOUNTS_PATH", tmp_path / "none.json")


def test_google_ineligibility_is_reported_verbatim(tmp_path, monkeypatch):
    """Google's own refusal names the cause and the fix, so it is surfaced.

    Flattening this into "no quota figure" threw away the one actionable thing
    in the response.
    """
    _fresh_google_token(tmp_path, monkeypatch)
    _patch_http(monkeypatch, google_adapter, _Response(200, _CODE_ASSIST_INELIGIBLE))

    observation = google_adapter.fetch_code_assist("gemini_code_assist", "Gemini", "GEMINI")

    assert observation.confidence == CONFIDENCE_UNAVAILABLE
    assert observation.windows == []
    assert "no longer supported" in observation.error
    assert "antigravity.google" in observation.error
    assert "Gemini Code Assist for individuals" in observation.error


def test_a_tier_is_named_even_without_a_current_tier(tmp_path, monkeypatch):
    """An account that never picked a tier still gets a plan label."""
    _fresh_google_token(tmp_path, monkeypatch)
    _patch_http(monkeypatch, google_adapter, _Response(200, _CODE_ASSIST_INELIGIBLE))

    observation = google_adapter.fetch_code_assist("gemini_code_assist", "Gemini", "GEMINI")
    assert observation.plan.label == "Gemini Code Assist (available)"


def test_a_current_tier_wins_over_an_allowed_one(tmp_path, monkeypatch):
    _fresh_google_token(tmp_path, monkeypatch)
    payload = dict(_CODE_ASSIST_INELIGIBLE, currentTier={"name": "Enterprise"})
    _patch_http(monkeypatch, google_adapter, _Response(200, payload))

    observation = google_adapter.fetch_code_assist("gemini_code_assist", "Gemini", "GEMINI")
    assert observation.plan.label == "Enterprise"


def test_antigravity_signed_in_says_it_publishes_no_figure(tmp_path, monkeypatch):
    """Signed in is a different answer from signed out, and must read as one.

    AntiGravity refreshes its credits and logs no numbers with them, keeps its
    credential in the running process, and binds a fresh random port each run,
    so there is genuinely nothing to read.
    """
    log = tmp_path / "cli.log"
    log.write_text(
        "E0922 21:17:22.350825 215 credits_manager.go:42] failed to refresh G1 credits: "
        "error getting token source: You are not logged into Antigravity.\n"
        "I0922 21:17:25.505153 219 experiment_manager.go:70] Experiments refreshed after login\n"
        "I0922 21:17:25.692170 215 server.go:3605] GetG1Credits: starting fetch\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(agy_adapter, "CLI_LOG", log)
    monkeypatch.setattr(agy_adapter, "DESKTOP_LOG", tmp_path / "missing.log")

    observation = agy_adapter.fetch_log(_account("antigravity"))

    assert observation.confidence == CONFIDENCE_UNAVAILABLE
    assert "is signed in" in observation.error
    assert "not signed in" not in observation.error
    assert observation.notes.get("signed_in") is True


def test_a_login_supersedes_an_earlier_signed_out_failure(tmp_path, monkeypatch):
    """A failure only stands while nothing has signed in since.

    The adapter reported "not signed in" for a machine that signed in three
    seconds later: the failure was the newest outcome and every line after it
    was an announcement, which the announcement rule correctly skips.
    """
    log = tmp_path / "cli.log"
    log.write_text(
        "E0922 21:17:22.000000 215 credits_manager.go:42] failed to refresh G1 credits: "
        "You are not logged into Antigravity.\n"
        "I0922 21:17:25.000000 219 server.go:3008] Auth succeeded, refreshing managers\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(agy_adapter, "CLI_LOG", log)
    monkeypatch.setattr(agy_adapter, "DESKTOP_LOG", tmp_path / "missing.log")

    assert "is signed in" in agy_adapter.fetch_log(_account("antigravity")).error


def test_a_failure_after_a_login_still_reports_signed_out(tmp_path, monkeypatch):
    """The other direction: a sign-out after a sign-in wins."""
    log = tmp_path / "cli.log"
    log.write_text(
        "I0922 21:17:22.000000 219 server.go:3008] Auth succeeded, refreshing managers\n"
        "E0922 21:17:30.000000 215 credits_manager.go:42] failed to refresh G1 credits: "
        "You are not logged into Antigravity.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(agy_adapter, "CLI_LOG", log)
    monkeypatch.setattr(agy_adapter, "DESKTOP_LOG", tmp_path / "missing.log")

    error = agy_adapter.fetch_log(_account("antigravity")).error
    assert "is not signed in" in error
    assert "Run agy and sign in" in error
