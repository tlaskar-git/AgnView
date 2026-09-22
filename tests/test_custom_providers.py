"""Tests for DeepSeek, Custom LLM harnesses, Account Editing, and Token Detection."""

from pathlib import Path

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from agent_relay.api.app import create_app
from agent_relay.core.db import Database
from agent_relay.core.models import UsageAccount
from agent_relay.core.usage import (
    CONFIDENCE_MEASURED,
    CONFIDENCE_UNAVAILABLE,
    UNIT_CURRENCY,
    fetch_observation,
    render_observation,
)
from agent_relay.core.runner import AgentRunner


@pytest.fixture
def client(tmp_path):
    db_file = str(tmp_path / "custom_test.db")
    app = create_app(db_path=db_file)
    return TestClient(app)


def test_deepseek_reports_only_the_balance_it_reads():
    # 1. An account with no key has nothing to read. It used to answer with an
    #    invented $45.40 balance and an 18.4% meter.
    acc_demo = UsageAccount(
        provider="deepseek",
        name="DeepSeek Demo",
        plan_name="DeepSeek V3",
        auth_type="api_key",
        auth_credential="",
    )
    obs_demo = fetch_observation(acc_demo)
    assert obs_demo.confidence == CONFIDENCE_UNAVAILABLE
    assert obs_demo.windows == []
    assert obs_demo.error

    # 2. Test live API probe parsing with mocked responses
    acc_live = UsageAccount(
        provider="deepseek",
        name="DeepSeek Production",
        plan_name="DeepSeek R1",
        auth_type="api_key",
        auth_credential="sk-live-prod-key-88888",
    )

    mock_resp_bal = MagicMock()
    mock_resp_bal.status_code = 200
    mock_resp_bal.json.return_value = {
        "is_available": True,
        "balance_infos": [
            {
                "currency": "USD",
                "total_balance": "85.20",
                "granted_balance": "10.00",
                "topped_up_balance": "75.20",
            }
        ],
    }

    mock_resp_models = MagicMock()
    mock_resp_models.status_code = 200
    mock_resp_models.json.return_value = {"data": [{"id": "deepseek-chat"}]}

    def mock_get(url, *args, **kwargs):
        if "balance" in url:
            return mock_resp_bal
        return mock_resp_models

    with patch("httpx.Client.get", side_effect=mock_get):
        obs_live = fetch_observation(acc_live)

    assert obs_live.confidence == CONFIDENCE_MEASURED
    balance = obs_live.windows[0]
    # Money, not a share of a window. No percentage is back-derived from it,
    # because nobody reported a starting figure to divide by.
    assert balance.unit == UNIT_CURRENCY
    assert balance.used == 85.20
    assert balance.currency == "USD"
    assert balance.limit is None
    assert balance.has_bar is False
    assert render_observation(obs_live)["windows"][0]["amount_text"] == "USD 85.20"
    assert acc_live.masked_credential.startswith("sk-")


def test_custom_harness_reports_reachability_and_no_quota():
    """A local harness enforces no quota, so it reports no window at all.

    Reachable with nothing to measure is the honest result. The card shows the
    endpoint and draws no bar, because there is no share of anything.
    """
    acc = UsageAccount(
        provider="custom",
        name="Local Ollama Server",
        plan_name="Qwen 2.5 Coder",
        auth_type="api_key",
        auth_credential="ollama-prod-key",
        base_url="http://localhost:11434",
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"models": [{"name": "qwen2.5-coder:32b"}]}

    with patch("httpx.Client.get", return_value=mock_resp):
        obs = fetch_observation(acc)

    assert obs.confidence == CONFIDENCE_MEASURED
    assert obs.windows == []
    assert obs.error is None
    assert obs.notes["endpoint"] == "http://localhost:11434"


def test_the_custom_adapter_is_no_longer_a_silent_fallthrough():
    """It used to serve every unrecognised provider, AntiGravity included."""
    from agent_relay.core.usage import UnknownProvider, get_adapter

    assert get_adapter("custom").provider == "custom"
    for name in ("antigravity", "mistral", "cohere"):
        if name == "antigravity":
            assert get_adapter(name).provider == "antigravity"
            continue
        with pytest.raises(UnknownProvider):
            get_adapter(name)


def test_account_update_api(client):
    create_res = client.post(
        "/api/usage/accounts",
        json={
            "provider": "deepseek",
            "name": "Initial DeepSeek",
            "plan_name": "DeepSeek V3",
            "auth_type": "api_key",
            "auth_credential": "sk-initial-secret-key-1111",
        },
    )
    assert create_res.status_code == 200
    acc = create_res.json()
    acc_id = acc["id"]

    get_res = client.get(f"/api/usage/accounts/{acc_id}")
    assert get_res.status_code == 200
    assert get_res.json()["name"] == "Initial DeepSeek"

    update_res = client.put(
        f"/api/usage/accounts/{acc_id}",
        json={
            "name": "Updated DeepSeek Pro",
            "plan_name": "DeepSeek R1",
            "auth_type": "api_key",
            "credential": "sk-updated-new-secret-2222",
            "base_url": "https://api.deepseek.com",
        },
    )
    assert update_res.status_code == 200
    updated = update_res.json()
    assert updated["name"] == "Updated DeepSeek Pro"
    assert updated["plan_name"] == "DeepSeek R1"
    assert updated["base_url"] == "https://api.deepseek.com"
    assert "updated-new" not in updated["masked_credential"]

    list_res = client.get("/api/usage/accounts")
    matching = [a for a in list_res.json() if a["id"] == acc_id]
    assert len(matching) == 1
    assert matching[0]["name"] == "Updated DeepSeek Pro"


def test_detect_local_token_api(client, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-mocked-deepseek-env-token-999")
    res_ds = client.post("/api/usage/detect-local-token", json={"provider": "deepseek"})
    assert res_ds.status_code == 200
    data_ds = res_ds.json()
    assert data_ds["found"] is True
    assert data_ds["token"] == "sk-mocked-deepseek-env-token-999"
    assert "$DEEPSEEK_API_KEY" in data_ds["source"]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-mocked-claude-env-token-888")
    res_claude = client.post("/api/usage/detect-local-token", json={"provider": "claude"})
    assert res_claude.status_code == 200
    data_claude = res_claude.json()
    assert data_claude["found"] is True
    assert data_claude["token"] == "sk-ant-mocked-claude-env-token-888"
    # A raw API key from an environment variable is exactly that, not a CLI
    # session, so the Add Account form's authentication method must stay on
    # API Key rather than being switched to Session Token underneath it.
    assert data_claude["auth_type"] == "api_key"


def test_detect_local_token_names_a_cli_session_correctly(client, monkeypatch, tmp_path):
    """A credential read from Claude Code's own OAuth session is a session
    token, not the provider's API key.

    Saving it under the wrong authentication method is exactly how an account
    ended up correctly connected (the credential still worked) but visibly
    wrong: the Add Account form left Authentication Method on its default of
    API Key because nothing told it otherwise.
    """
    import json as _json

    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        _json.dumps({
            "oauthAccount": {
                "accountUuid": "e118f31c-3bea-4ac3-bfc8-ce62d35dad18",
                "emailAddress": "user@example.com",
                "displayName": "Test User",
                "organizationType": "claude_max",
                "organizationRateLimitTier": "default_claude_max_20x",
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)

    res = client.post("/api/usage/detect-local-token", json={"provider": "claude"})
    assert res.status_code == 200
    data = res.json()
    assert data["found"] is True
    assert "CLI" in data["source"]
    assert data["auth_type"] == "session_token"


@pytest.mark.anyio
async def test_runner_deepseek_and_custom_dispatch(tmp_path):
    db_file = str(tmp_path / "runner_ds.db")
    db = Database(db_file)
    runner = AgentRunner(db)

    # Dispatch to deepseek agent
    await runner.dispatch(
        agent="deepseek",
        prompt="Analyze architectural bottlenecks",
        cwd=str(tmp_path),
        session_id="test-ds-sess"
    )

    logs = db.get_console_logs(session_id="test-ds-sess")
    assert len(logs) >= 2
    user_log = next((entry for entry in logs if entry["source"] == "user_input"), None)
    assert user_log is not None
    assert "architectural bottlenecks" in user_log["content"]

    agent_log = next((entry for entry in logs if entry["agent"] == "deepseek"), None)
    assert agent_log is not None

    # Dispatch to custom local harness
    await runner.dispatch(
        agent="custom",
        prompt="Synthesize microservice handler",
        cwd=str(tmp_path),
        session_id="test-custom-sess"
    )

    logs_custom = db.get_console_logs(session_id="test-custom-sess")
    assert len(logs_custom) >= 2
    agent_custom = next((entry for entry in logs_custom if entry["agent"] == "custom"), None)
    assert agent_custom is not None