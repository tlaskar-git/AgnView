"""Tests for Multi-Account Live Subscription Quotas & Usage Dashboard."""

import pytest
from fastapi.testclient import TestClient
from agent_relay.api.app import create_app
from agent_relay.core.models import UsageAccount
from agent_relay.core.usage_fetcher import fetch_account_usage


@pytest.fixture
def client(tmp_path):
    db_file = str(tmp_path / "usage_test.db")
    app = create_app(db_path=db_file)
    return TestClient(app)


def test_an_account_with_no_readable_source_reports_unavailable():
    """No source to read means no numbers, and a reason the operator can act on.

    The suite runs against a throwaway home directory, so none of the three
    providers has a sign-in or a transcript to read here. Every one of them
    used to answer with an invented percentage anyway.
    """
    for provider in ("claude", "chatgpt", "gemini"):
        account = UsageAccount(
            provider=provider,
            name=f"{provider} account",
            auth_type="api_key",
            auth_credential="placeholder-not-a-real-key",
        )
        result = fetch_account_usage(account)

        assert result.status == "unavailable", provider
        assert result.error_message, provider
        assert result.session_percent_used is None, provider
        assert result.weekly_percent_used is None, provider
        assert result.percent_used is None, provider
        assert result.tokens_used is None, provider
        assert result.tokens_limit is None, provider
        assert result.masked_credential == "plac...-key", provider


def test_multi_account_api_lifecycle(client):
    """Test full multi-account CRUD and live refresh via REST API."""
    # 1. Initially empty
    res = client.get("/api/usage/accounts")
    assert res.status_code == 200
    assert res.json() == []

    # 2. Add Claude Account 1 (Personal)
    res_c1 = client.post("/api/usage/accounts", json={
        "provider": "claude",
        "name": "Personal Claude Code",
        "plan_name": "Claude Pro",
        "auth_type": "api_key",
        "auth_credential": "sk-test-personal-token-placeholder",
        "tokens_limit": 1_000_000
    })
    assert res_c1.status_code == 200
    c1 = res_c1.json()
    assert c1["provider"] == "claude"
    assert c1["name"] == "Personal Claude Code"
    assert "personal-token" not in c1["masked_credential"]
    assert "auth_credential" not in c1  # Raw secret never exposed

    # 3. Add Claude Account 2 (Work/Enterprise)
    res_c2 = client.post("/api/usage/accounts", json={
        "provider": "claude",
        "name": "Work Enterprise Claude",
        "plan_name": "Claude Team",
        "auth_type": "api_key",
        "auth_credential": "sk-test-work-token-placeholder",
        "tokens_limit": 5_000_000
    })
    assert res_c2.status_code == 200
    res_c2.json()

    # 4. Add ChatGPT Account (OpenAI)
    res_gpt = client.post("/api/usage/accounts", json={
        "provider": "chatgpt",
        "name": "Codex / ChatGPT Plus",
        "plan_name": "ChatGPT Plus",
        "auth_type": "api_key",
        "auth_credential": "sk-test-chatgpt-codex-key-placeholder",
        "cost_limit_usd": 100.0
    })
    assert res_gpt.status_code == 200
    gpt = res_gpt.json()
    assert gpt["provider"] == "chatgpt"

    # 5. Add Gemini Account (gemini.google.com / API)
    res_gem = client.post("/api/usage/accounts", json={
        "provider": "gemini",
        "name": "Google Gemini Advanced",
        "plan_name": "Gemini Advanced 1.5 Pro",
        "auth_type": "api_key",
        "auth_credential": "AIza-mock-gemini-api-key-test",
        "tokens_limit": 2_000_000
    })
    assert res_gem.status_code == 200
    gem = res_gem.json()
    assert gem["provider"] == "gemini"

    # 6. List all accounts (should have 4 total)
    res_all = client.get("/api/usage/accounts")
    assert res_all.status_code == 200
    accounts = res_all.json()
    assert len(accounts) == 4

    # 7. Filter by provider
    res_filter_claude = client.get("/api/usage/accounts?provider=claude")
    assert res_filter_claude.status_code == 200
    assert len(res_filter_claude.json()) == 2

    res_filter_chatgpt = client.get("/api/usage/accounts?provider=chatgpt")
    assert res_filter_chatgpt.status_code == 200
    assert len(res_filter_chatgpt.json()) == 1
    assert res_filter_chatgpt.json()[0]["name"] == "Codex / ChatGPT Plus"

    res_filter_gemini = client.get("/api/usage/accounts?provider=gemini")
    assert res_filter_gemini.status_code == 200
    assert len(res_filter_gemini.json()) == 1
    assert res_filter_gemini.json()[0]["name"] == "Google Gemini Advanced"

    # 8. Refresh single account
    c1_id = c1["id"]
    res_refresh_single = client.post(f"/api/usage/accounts/{c1_id}/refresh")
    assert res_refresh_single.status_code == 200
    refreshed_c1 = res_refresh_single.json()
    assert refreshed_c1["id"] == c1_id
    assert refreshed_c1["last_synced_at"] is not None

    # 9. Refresh all accounts
    res_refresh_all = client.post("/api/usage/refresh-all")
    assert res_refresh_all.status_code == 200
    all_refreshed = res_refresh_all.json()
    assert len(all_refreshed) == 4

    # 10. Delete an account
    res_del = client.delete(f"/api/usage/accounts/{c1_id}")
    assert res_del.status_code == 200
    assert res_del.json()["deleted"] is True

    # Confirm account count is now 3
    res_after_del = client.get("/api/usage/accounts")
    assert len(res_after_del.json()) == 3
    remaining_ids = [a["id"] for a in res_after_del.json()]
    assert c1_id not in remaining_ids
