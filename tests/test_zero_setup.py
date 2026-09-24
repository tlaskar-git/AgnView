"""A new install finds its accounts by itself, keeps finding tools signed in
later, and never brings back a card the operator deleted. ChatGPT reads with
Codex's own sign-in before any token pasted into the account."""

import json
import time

from agent_relay.core.usage import discover
from agent_relay.core.usage.adapters import chatgpt


def _no_tools(monkeypatch, tmp_path):
    monkeypatch.setattr(discover._claude_adapter, "read_claude_oauth", lambda: None)
    monkeypatch.setattr(discover._chatgpt_adapter, "CODEX_AUTH_PATH", tmp_path / "no-codex.json")
    monkeypatch.setattr(discover._google, "CREDS_PATH", tmp_path / "no-gemini.json")
    monkeypatch.setattr(discover._agy, "CLI_LOG", tmp_path / "no-cli.log")
    monkeypatch.setattr(discover._agy, "DESKTOP_LOG", tmp_path / "no-desktop.log")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(discover.Path, "home", lambda: tmp_path / "home")


def test_an_antigravity_install_is_found_without_its_log(monkeypatch, tmp_path):
    _no_tools(monkeypatch, tmp_path)
    (tmp_path / "local" / "Programs" / "antigravity").mkdir(parents=True)

    providers = [found["provider"] for found in discover.discover_local_accounts()]

    # Gemini's figures come from AntiGravity's sign-in, so both cards appear.
    assert providers == ["gemini", "antigravity"]


def test_nothing_installed_finds_nothing(monkeypatch, tmp_path):
    _no_tools(monkeypatch, tmp_path)
    assert discover.discover_local_accounts() == []


def test_a_dismissed_provider_is_skipped_until_added_again(monkeypatch, tmp_path):
    monkeypatch.setattr(discover, "DISMISSED_PATH", tmp_path / "dismissed.json")
    monkeypatch.setattr(
        discover,
        "discover_local_accounts",
        lambda: [{"provider": "claude", "name": "Claude"}, {"provider": "chatgpt", "name": "ChatGPT"}],
    )

    discover.dismiss_provider("ChatGPT")
    assert discover.dismissed_providers() == ["chatgpt"]
    found = discover.missing_providers([], skip=discover.dismissed_providers())
    assert [f["provider"] for f in found] == ["claude"]

    discover.undismiss_provider("chatgpt")
    found = discover.missing_providers([], skip=discover.dismissed_providers())
    assert [f["provider"] for f in found] == ["claude", "chatgpt"]


def test_deleting_the_last_card_of_a_provider_dismisses_it(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from agent_relay.api import routes
    from agent_relay.api.app import create_app

    monkeypatch.setattr(discover, "DISMISSED_PATH", tmp_path / "dismissed.json")
    monkeypatch.setattr(routes, "_last_discovery", time.monotonic())
    client = TestClient(create_app(db_path=str(tmp_path / "hub.db")))
    created = client.post(
        "/api/usage/accounts", json={"provider": "deepseek", "name": "DeepSeek", "credential": "sk-test"}
    )
    assert created.status_code == 200, created.text

    client.delete(f"/api/usage/accounts/{created.json()['id']}")

    assert discover.dismissed_providers() == ["deepseek"]


def _codex_auth(tmp_path, token):
    path = tmp_path / "auth.json"
    path.write_text(json.dumps({"tokens": {"access_token": token, "account_id": "acct-test"}}))
    return path


class _Account:
    id = "chatgpt-test"
    provider = "chatgpt"
    name = "ChatGPT"
    base_url = None
    plan_name = "Plus"

    def __init__(self, credential=""):
        self.credential = credential


class _Response:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}
        self.headers = {}

    def json(self):
        return self._body


def _client(monkeypatch, answers):
    seen = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, headers=None):
            token = headers["Authorization"].removeprefix("Bearer ")
            seen.append(token)
            return answers[token]

    monkeypatch.setattr(chatgpt.httpx, "Client", Client)
    return seen


def test_codex_sign_in_is_used_before_a_pasted_token(monkeypatch, tmp_path):
    fresh = "eyJ" + "f" * 60
    pasted = "eyJ" + "p" * 60
    monkeypatch.setattr(chatgpt, "CODEX_AUTH_PATH", _codex_auth(tmp_path, fresh))
    seen = _client(monkeypatch, {fresh: _Response(200, {}), pasted: _Response(401)})

    chatgpt.fetch_codex_auth(_Account(credential=pasted))

    assert seen == [fresh]


def test_a_refused_codex_token_falls_back_to_the_pasted_one(monkeypatch, tmp_path):
    stale = "eyJ" + "s" * 60
    pasted = "eyJ" + "p" * 60
    monkeypatch.setattr(chatgpt, "CODEX_AUTH_PATH", _codex_auth(tmp_path, stale))
    seen = _client(monkeypatch, {stale: _Response(401), pasted: _Response(200, {})})

    chatgpt.fetch_codex_auth(_Account(credential=pasted))

    assert seen == [stale, pasted]


def test_no_contradicting_message_when_codex_is_signed_in(monkeypatch, tmp_path):
    monkeypatch.setattr(chatgpt, "CODEX_AUTH_PATH", _codex_auth(tmp_path, "eyJ" + "x" * 60))
    assert chatgpt.fetch_nothing(_Account()) is None
