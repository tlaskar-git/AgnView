"""Multi-account subscription usage: the adapter contract and the REST API."""

import json

import pytest
from fastapi.testclient import TestClient

from agent_relay.api.app import create_app
from agent_relay.core.models import UsageAccount
from agent_relay.core.usage import (
    CONFIDENCE_UNAVAILABLE,
    fetch_observation,
    render_observation,
)
from agent_relay.core.usage.adapters import antigravity as agy_adapter
from agent_relay.core.usage.adapters import chatgpt as chatgpt_adapter
from agent_relay.core.usage.adapters import claude as claude_adapter
from agent_relay.core.usage.adapters import google_code_assist as google_adapter


@pytest.fixture
def client(tmp_path):
    db_file = str(tmp_path / "usage_test.db")
    app = create_app(db_path=db_file)
    return TestClient(app)


@pytest.fixture
def no_local_signins(tmp_path, monkeypatch):
    """Point every adapter at an empty home, so nothing real is read.

    The adapters resolve their paths as module constants, so a test redirects
    them by name. Without this the suite would read the developer's own Claude
    Code and Codex sign-ins and assert against live figures.
    """
    empty = tmp_path / "empty-home"
    empty.mkdir()
    monkeypatch.setattr(claude_adapter, "CREDENTIALS_PATH", empty / ".credentials.json")
    monkeypatch.setattr(claude_adapter, "PROFILE_PATH", empty / ".claude.json")
    monkeypatch.setattr(claude_adapter, "read_claude_code_usage", lambda: None)
    monkeypatch.setattr(chatgpt_adapter, "CODEX_AUTH_PATH", empty / "auth.json")
    monkeypatch.setattr(google_adapter, "CREDS_PATH", empty / "oauth_creds.json")
    monkeypatch.setattr(google_adapter, "ACCOUNTS_PATH", empty / "google_accounts.json")
    monkeypatch.setattr(agy_adapter, "CLI_LOG", empty / "cli.log")
    monkeypatch.setattr(agy_adapter, "DESKTOP_LOG", empty / "desktop.log")
    return empty


def test_an_account_with_no_readable_source_reports_unavailable(no_local_signins):
    """No source to read means no numbers, and a reason the operator can act on.

    Every one of these providers used to answer with an invented percentage.
    """
    for provider in ("claude", "chatgpt", "gemini", "antigravity"):
        account = UsageAccount(
            provider=provider,
            name=f"{provider} account",
            auth_type="session_token",
            auth_credential="placeholder-not-a-real-key",
        )
        view = render_observation(fetch_observation(account))

        assert view["confidence"] == CONFIDENCE_UNAVAILABLE, provider
        assert view["error"], f"{provider} gave no reason"
        # No windows at all, so there is no figure a card could render.
        assert view["windows"] == [], provider
        assert account.masked_credential == "plac...-key", provider


def test_unavailable_carries_no_leftover_breakdown(no_local_signins):
    """A per-model breakdown must not survive into an unavailable result.

    The old code nulled the summary fields by hand and missed
    weekly_breakdown, so the dashboard rendered an invented per-model table
    underneath a summary that said "unavailable". An unavailable observation
    now holds no windows, so there is nothing left to leak.
    """
    account = UsageAccount(
        provider="gemini",
        name="gemini account",
        auth_type="api_key",
        auth_credential="placeholder-not-a-real-key",
    )
    account.observation = fetch_observation(account)
    view = account.masked()

    assert view["status"] == "unavailable"
    assert view["weekly_breakdown"] is None
    assert view["usage"]["windows"] == []


def test_the_provider_endpoint_lists_every_adapter(client):
    res = client.get("/api/usage/providers")
    assert res.status_code == 200
    values = [option["value"] for option in res.json()]
    assert "antigravity" in values, "AntiGravity must be offerable in the UI"
    assert values == ["claude", "chatgpt", "gemini", "antigravity", "deepseek", "custom"]


def test_an_unknown_provider_is_refused_rather_than_probed(client):
    """An unregistered provider used to become an Ollama probe silently."""
    res = client.post(
        "/api/usage/accounts",
        json={
            "provider": "mistral",
            "name": "nope",
            "auth_type": "api_key",
            "auth_credential": "x",
        },
    )
    assert res.status_code == 400
    assert "not a usage provider" in res.json()["detail"]


def test_multi_account_api_lifecycle(client, no_local_signins):
    """Full multi-account CRUD and refresh through the REST API."""
    res = client.get("/api/usage/accounts")
    assert res.status_code == 200
    assert res.json() == []

    res_c1 = client.post("/api/usage/accounts", json={
        "provider": "claude",
        "name": "Personal Claude Code",
        "plan_name": "Claude Pro",
        "auth_type": "api_key",
        "auth_credential": "sk-test-personal-token-placeholder",
    })
    assert res_c1.status_code == 200
    c1 = res_c1.json()
    assert c1["provider"] == "claude"
    assert c1["name"] == "Personal Claude Code"
    assert "personal-token" not in c1["masked_credential"]
    assert "auth_credential" not in c1, "the raw secret must never be returned"
    # Every account carries its provenance, even before anything is readable.
    assert "usage" in c1 and c1["usage"]["source_label"]

    client.post("/api/usage/accounts", json={
        "provider": "claude",
        "name": "Work Enterprise Claude",
        "plan_name": "Claude Team",
        "auth_type": "api_key",
        "auth_credential": "sk-test-work-token-placeholder",
    })
    client.post("/api/usage/accounts", json={
        "provider": "chatgpt",
        "name": "Codex / ChatGPT Plus",
        "plan_name": "ChatGPT Plus",
        "auth_type": "api_key",
        "auth_credential": "sk-test-chatgpt-codex-key-placeholder",
    })
    res_agy = client.post("/api/usage/accounts", json={
        "provider": "antigravity",
        "name": "AntiGravity",
        "plan_name": "AntiGravity",
        "auth_type": "session_token",
        "auth_credential": "none",
    })
    assert res_agy.status_code == 200, "AntiGravity must be addable"

    accounts = client.get("/api/usage/accounts").json()
    assert len(accounts) == 4

    assert len(client.get("/api/usage/accounts?provider=claude").json()) == 2
    assert len(client.get("/api/usage/accounts?provider=antigravity").json()) == 1

    c1_id = c1["id"]
    refreshed = client.post(f"/api/usage/accounts/{c1_id}/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["last_synced_at"] is not None

    all_refreshed = client.post("/api/usage/refresh-all")
    assert all_refreshed.status_code == 200
    assert len(all_refreshed.json()) == 4

    res_del = client.delete(f"/api/usage/accounts/{c1_id}")
    assert res_del.status_code == 200
    assert res_del.json()["deleted"] is True

    remaining = client.get("/api/usage/accounts").json()
    assert len(remaining) == 3
    assert c1_id not in [a["id"] for a in remaining]


def test_the_sync_snippet_is_built_for_the_account_that_asked(client, no_local_signins):
    """The snippet must reach the account it belongs to, on this hub, with the token."""
    created = client.post("/api/usage/accounts", json={
        "provider": "claude",
        "name": "Snippet target",
        "auth_type": "session_token",
        "auth_credential": "none",
    }).json()

    res = client.get(f"/api/usage/accounts/{created['id']}/sync-snippet")
    assert res.status_code == 200
    body = res.json()
    assert body["account_id"] == created["id"]
    assert created["id"] in body["snippet"]
    assert body["endpoint"].endswith(f"/api/usage/accounts/{created['id']}/telemetry")
    assert "localhost:8765" not in body["snippet"]


def test_a_provider_with_nothing_to_read_gets_no_snippet(client, no_local_signins):
    """DeepSeek's adapter reads its API directly, so there is no script to paste."""
    created = client.post("/api/usage/accounts", json={
        "provider": "deepseek",
        "name": "DeepSeek",
        "auth_type": "api_key",
        "auth_credential": "sk-placeholder-deepseek-key",
    }).json()

    body = client.get(f"/api/usage/accounts/{created['id']}/sync-snippet").json()
    assert body["snippet"] is None


def test_antigravity_gets_a_snippet_for_its_own_panel(client, no_local_signins):
    """AntiGravity's model picker is readable, so it gets a script too."""
    created = client.post("/api/usage/accounts", json={
        "provider": "antigravity",
        "name": "AntiGravity",
        "auth_type": "session_token",
        "auth_credential": "none",
    }).json()

    body = client.get(f"/api/usage/accounts/{created['id']}/sync-snippet").json()
    assert body["snippet"]
    assert created["id"] in body["snippet"]
    assert "model picker" in body["usage_page"]


# ---------------------------------------------------------------------------
# Discovery: a fresh install must show correct usage with no setup
# ---------------------------------------------------------------------------


def test_a_fresh_install_seeds_an_account_for_every_signed_in_tool(tmp_path, monkeypatch):
    """Nothing to configure. Every adapter reads a tool's own sign-in.

    An empty Usage tab on a machine with Claude Code and Codex signed in meant
    only that nobody had clicked Add Account yet.
    """
    from agent_relay.core.usage.discover import discover_local_accounts

    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    claude_creds = home / ".credentials.json"
    claude_creds.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "t", "subscriptionType": "max"}}),
        encoding="utf-8",
    )
    codex_auth = home / ".codex" / "auth.json"
    codex_auth.write_text(json.dumps({"tokens": {"access_token": "t"}}), encoding="utf-8")
    gemini_creds = home / "oauth_creds.json"
    gemini_creds.write_text(json.dumps({"access_token": "t"}), encoding="utf-8")
    agy_log = home / "cli.log"
    agy_log.write_text("I0922 21:00:00.0 1 quota_manager.go:45] doRefreshQuota: x\n", encoding="utf-8")

    monkeypatch.setattr(claude_adapter, "CREDENTIALS_PATH", claude_creds)
    monkeypatch.setattr(chatgpt_adapter, "CODEX_AUTH_PATH", codex_auth)
    monkeypatch.setattr(google_adapter, "CREDS_PATH", gemini_creds)
    monkeypatch.setattr(google_adapter, "ACCOUNTS_PATH", home / "google_accounts.json")
    monkeypatch.setattr(agy_adapter, "CLI_LOG", agy_log)
    monkeypatch.setattr(agy_adapter, "DESKTOP_LOG", home / "missing.log")

    found = discover_local_accounts()
    providers = [entry["provider"] for entry in found]

    # Registry order, so a seeded dashboard lays out like a hand-built one.
    assert providers == ["claude", "chatgpt", "gemini", "antigravity"]
    # The plan comes from the credential the tool itself wrote.
    assert found[0]["name"] == "Claude Max"
    # Every entry says where it was found, for the log line and for diagnosis.
    assert all(entry["detected_from"] for entry in found)


def test_nothing_is_discovered_on_a_machine_with_no_agent_tools(tmp_path, monkeypatch, no_local_signins):
    from agent_relay.core.usage.discover import discover_local_accounts

    assert discover_local_accounts() == []


def test_discovery_never_duplicates_an_account_that_exists(tmp_path, monkeypatch):
    from agent_relay.core.usage.discover import missing_providers

    claude_creds = tmp_path / ".credentials.json"
    claude_creds.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "t", "subscriptionType": "max"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(claude_adapter, "CREDENTIALS_PATH", claude_creds)
    monkeypatch.setattr(chatgpt_adapter, "CODEX_AUTH_PATH", tmp_path / "no-codex.json")
    monkeypatch.setattr(google_adapter, "CREDS_PATH", tmp_path / "no-gemini.json")
    monkeypatch.setattr(google_adapter, "ACCOUNTS_PATH", tmp_path / "no-accounts.json")
    monkeypatch.setattr(agy_adapter, "CLI_LOG", tmp_path / "no-agy.log")
    monkeypatch.setattr(agy_adapter, "DESKTOP_LOG", tmp_path / "no-agy2.log")

    assert [entry["provider"] for entry in missing_providers([])] == ["claude"]
    assert missing_providers(["claude"]) == []
    # Case and padding must not create a duplicate.
    assert missing_providers([" Claude "]) == []


def test_the_discover_route_is_safe_to_run_twice(client, no_local_signins):
    """Running it again adds nothing, so a dashboard button cannot duplicate."""
    first = client.post("/api/usage/accounts/discover")
    assert first.status_code == 200
    second = client.post("/api/usage/accounts/discover")
    assert second.status_code == 200
    assert second.json()["added"] == []
