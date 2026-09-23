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
    def __init__(self, models_response):
        self.models_response = models_response
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None):
        self.calls.append(url)
        if url.endswith(":loadCodeAssist"):
            return _Response(200, {"cloudaicompanionProject": "test-project", "paidTier": {"name": "Google AI Pro"}})
        return self.models_response


def _enable(monkeypatch, sign_in, models_response):
    monkeypatch.setenv(agy_cloud.ENABLE_ENV, "1")
    monkeypatch.setattr(agy_cloud, "read_sign_in", lambda: sign_in)
    monkeypatch.setattr(agy_cloud, "_project_cache", {})
    client = _Client(models_response)
    monkeypatch.setattr(agy_cloud.httpx, "Client", lambda **kwargs: client)
    return client


def test_a_valid_sign_in_gives_a_measured_reading(monkeypatch):
    _enable(monkeypatch, _sign_in(30), _Response(200, _models()))
    observation = agy_cloud.fetch_cloud_quota("AntiGravity")
    assert observation.confidence == CONFIDENCE_MEASURED
    assert observation.source == "agy_cloud"
    assert observation.plan.name == "Google AI Pro"
    assert observation.plan.label == "user@example.com"


def test_an_expired_sign_in_says_to_open_antigravity(monkeypatch):
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
