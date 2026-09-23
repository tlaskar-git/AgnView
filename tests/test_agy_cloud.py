"""AntiGravity quota read from Google with the app's own sign-in."""

import base64
import json
from datetime import timedelta

from agent_relay.core.usage.adapters import agy_cloud
from agent_relay.core.usage.models import CONFIDENCE_MEASURED, CONFIDENCE_UNAVAILABLE, utc_now


def _id_token(email):
    claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return f"header.{claims}.signature"


def _sign_in(expires_in_minutes=30):
    expiry = (utc_now() + timedelta(minutes=expires_in_minutes)).astimezone()
    # Seven fractional digits, as AntiGravity writes them.
    stamp = expiry.strftime("%Y-%m-%dT%H:%M:%S.") + "1234567" + expiry.strftime("%z")
    stamp = stamp[:-2] + ":" + stamp[-2:]
    return {
        "token": {"access_token": "test-access", "token_type": "Bearer", "expiry": stamp},
        "auth_method": "consumer",
        "id_token": _id_token("user@example.com"),
    }


def _models(reset_in_hours=4):
    reset = (utc_now() + timedelta(hours=reset_in_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "models": {
            "gemini-3.6-flash-high": {"modelProvider": "MODEL_PROVIDER_GOOGLE",
                                      "quotaInfo": {"remainingFraction": 0.6, "resetTime": reset}},
            "gemini-pro-agent": {"quotaInfo": {"remainingFraction": 0.9, "resetTime": reset}},
            "claude-sonnet-4-6": {"modelProvider": "MODEL_PROVIDER_ANTHROPIC",
                                  "quotaInfo": {"remainingFraction": 0.75, "resetTime": reset}},
            "gpt-oss-120b-medium": {"modelProvider": "MODEL_PROVIDER_OPENAI",
                                    "quotaInfo": {"remainingFraction": 1, "resetTime": reset}},
            "tab_flash_lite_preview": {"quotaInfo": {"remainingFraction": 0.0}},
        },
        "agentModelSorts": [{"groups": [{"modelIds": [
            "gemini-3.6-flash-high", "gemini-pro-agent", "claude-sonnet-4-6", "gpt-oss-120b-medium"]}]}],
    }


def test_models_are_grouped_as_the_picker_groups_them():
    [window] = agy_cloud.windows_from_models(_models())
    groups = {child.label: child.used for child in window.breakdown}
    assert groups == {"Gemini models": 40.0, "Claude and GPT models": 25.0}
    assert window.used == 40.0
    assert window.window_end is not None


def test_models_outside_the_picker_are_ignored():
    payload = _models()
    [window] = agy_cloud.windows_from_models(payload)
    # tab_flash_lite_preview is fully used but is not a model a person picks.
    assert window.used < 100.0


def test_seven_digit_fractions_are_parsed_with_their_zone():
    token, expiry = agy_cloud._token_and_expiry(
        {"token": {"access_token": "test-access", "expiry": "2026-09-23T17:10:08.0770689+01:00"}}
    )
    assert token == "test-access"
    assert expiry.isoformat() == "2026-09-23T16:10:08.077068+00:00"


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


class _Client:
    def __init__(self, models_response, token_responses=None):
        self.models_response = models_response
        self.token_responses = dict(token_responses or {})
        self.calls = []
        self.secrets_tried = []
        self.last_headers = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None, data=None):
        self.calls.append(url)
        if url == agy_cloud.TOKEN_URL:
            self.secrets_tried.append(data["client_secret"])
            return self.token_responses.get(data["client_secret"], _Response(401, {"error": "invalid_client"}))
        self.last_headers = headers
        if url.endswith(":loadCodeAssist"):
            return _Response(200, {"cloudaicompanionProject": "test-project", "paidTier": {"name": "Google AI Pro"}})
        return self.models_response


def _enable(monkeypatch, sign_in, models_response, secrets=(), token_responses=None):
    monkeypatch.setenv(agy_cloud.ENABLE_ENV, "1")
    monkeypatch.setattr(agy_cloud, "read_sign_in", lambda: sign_in)
    monkeypatch.setattr(agy_cloud, "_project_cache", {})
    monkeypatch.setattr(agy_cloud, "_renewed", {})
    monkeypatch.setattr(agy_cloud, "_client_secrets", {})
    # Never read the developer's own AntiGravity install.
    monkeypatch.setattr(agy_cloud, "installed_client_secrets", lambda: list(secrets))
    client = _Client(models_response, token_responses)
    monkeypatch.setattr(agy_cloud.httpx, "Client", lambda **kwargs: client)
    return client


def _renewable_sign_in(expires_in_minutes):
    sign_in = _sign_in(expires_in_minutes)
    claims = base64.urlsafe_b64encode(
        json.dumps({"email": "user@example.com", "aud": "test-client.apps.googleusercontent.com"}).encode()
    ).decode().rstrip("=")
    sign_in["id_token"] = f"header.{claims}.signature"
    sign_in["token"]["refresh_token"] = "test-refresh"
    return sign_in


def test_an_expired_sign_in_is_renewed_in_memory(monkeypatch):
    client = _enable(
        monkeypatch,
        _renewable_sign_in(-5),
        _Response(200, _models()),
        secrets=["test-secret-a", "test-secret-b"],
        token_responses={"test-secret-b": _Response(200, {"access_token": "renewed", "expires_in": 3599})},
    )
    observation = agy_cloud.fetch_cloud_quota("AntiGravity")
    assert observation.confidence == CONFIDENCE_MEASURED
    assert client.secrets_tried == ["test-secret-a", "test-secret-b"]
    assert client.last_headers["Authorization"] == "Bearer renewed"

    # The renewed token and the working secret are reused, not requested again.
    client.secrets_tried.clear()
    agy_cloud.fetch_cloud_quota("AntiGravity")
    assert client.secrets_tried == []


def test_a_revoked_sign_in_says_to_sign_in_again(monkeypatch):
    _enable(
        monkeypatch,
        _renewable_sign_in(-5),
        _Response(200, _models()),
        secrets=["test-secret-a"],
        token_responses={"test-secret-a": _Response(400, {"error": "invalid_grant"})},
    )
    observation = agy_cloud.fetch_cloud_quota("AntiGravity")
    assert observation.confidence == CONFIDENCE_UNAVAILABLE
    assert "Sign in to AntiGravity again" in observation.error


def test_a_valid_sign_in_gives_a_measured_reading(monkeypatch):
    _enable(monkeypatch, _sign_in(30), _Response(200, _models()))
    observation = agy_cloud.fetch_cloud_quota("AntiGravity")
    assert observation.confidence == CONFIDENCE_MEASURED
    assert observation.source == "agy_cloud"
    assert observation.plan.name == "Google AI Pro"
    assert observation.plan.label == "user@example.com"


def test_an_expired_sign_in_without_renewal_says_to_open_antigravity(monkeypatch):
    client = _enable(monkeypatch, _sign_in(-5), _Response(200, _models()))
    observation = agy_cloud.fetch_cloud_quota("AntiGravity")
    assert observation.confidence == CONFIDENCE_UNAVAILABLE
    assert "Open AntiGravity" in observation.error
    assert client.calls == []


def test_a_refused_sign_in_is_unavailable(monkeypatch):
    _enable(monkeypatch, _sign_in(30), _Response(401, {}))
    observation = agy_cloud.fetch_cloud_quota("AntiGravity")
    assert observation.confidence == CONFIDENCE_UNAVAILABLE
    assert "refused" in observation.error


def test_no_sign_in_lets_the_next_rung_try(monkeypatch):
    monkeypatch.setenv(agy_cloud.ENABLE_ENV, "1")
    monkeypatch.setattr(agy_cloud, "read_sign_in", lambda: None)
    assert agy_cloud.fetch_cloud_quota("AntiGravity") is None


def test_the_switch_turns_the_source_off(monkeypatch):
    monkeypatch.setenv(agy_cloud.ENABLE_ENV, "0")
    monkeypatch.setattr(agy_cloud, "read_sign_in", lambda: (_ for _ in ()).throw(AssertionError("read")))
    assert agy_cloud.fetch_cloud_quota("AntiGravity") is None
