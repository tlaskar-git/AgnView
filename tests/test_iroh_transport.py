"""Tests for the bundled iroh transport.

The load-bearing property here is that iroh never gets on the startup path. A
hub on an unreachable network must still start and must still serve the
dashboard, so these tests stand in an endpoint bind that hangs or raises and
assert the hub comes up regardless.
"""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from agent_relay.api.app import create_app
from agent_relay.core.iroh_transport import IrohTransport, get_or_create_iroh_secret
from agent_relay.core.network import Transport, resolve_iroh_path_transport


class _Path:
    def __init__(self, is_selected=False, is_ip=False, is_relay=False):
        self.is_selected = is_selected
        self.is_ip = is_ip
        self.is_relay = is_relay


@pytest.fixture
def iroh_enabled(monkeypatch):
    monkeypatch.setenv("AGNVIEW_IROH", "1")


def _hang_forever(monkeypatch):
    async def _never_returns(self):
        await asyncio.sleep(3600)

    monkeypatch.setattr(IrohTransport, "_bind", _never_returns)


def _raise_immediately(monkeypatch):
    async def _boom(self):
        raise OSError("no route to host")

    monkeypatch.setattr(IrohTransport, "_bind", _boom)


def test_iroh_transport_implements_the_transport_interface():
    assert issubclass(IrohTransport, Transport)
    transport = IrohTransport(enabled=False, disabled_reason="off for this test")
    status = transport.status()
    assert status["name"] == "iroh"
    assert status["state"] == "disabled"
    assert status["ticket"] is None


def test_disabled_transport_never_touches_iroh():
    transport = IrohTransport(enabled=False, disabled_reason="off for this test")
    transport.start()
    assert transport.state == "disabled"
    assert transport.ticket is None


def test_hub_starts_and_serves_the_dashboard_when_the_bind_hangs(tmp_path, monkeypatch, iroh_enabled):
    _hang_forever(monkeypatch)
    app = create_app(db_path=str(tmp_path / "hang.db"))

    started = time.monotonic()
    with TestClient(app) as client:
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, f"startup waited {elapsed:.1f}s on an unreachable network"
        assert app.state.iroh.state == "starting"
        assert client.get("/").status_code == 200
        assert client.get("/api/mobile/status").status_code == 200


def test_hub_starts_and_serves_the_dashboard_when_the_bind_fails(tmp_path, monkeypatch, iroh_enabled):
    _raise_immediately(monkeypatch)
    app = create_app(db_path=str(tmp_path / "fail.db"))

    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        # The bind task runs on the serving loop, so give it a turn.
        for _ in range(50):
            if app.state.iroh.state == "failed":
                break
            time.sleep(0.02)
            client.get("/api/mobile/status")
        assert app.state.iroh.state == "failed"
        assert "no route to host" in (app.state.iroh.status()["error"] or "")
        assert client.get("/").status_code == 200


def test_a_hanging_bind_is_bounded_by_the_timeout(monkeypatch):
    monkeypatch.setattr("agent_relay.core.iroh_transport.IROH_BIND_TIMEOUT_SECONDS", 0.1)
    _hang_forever(monkeypatch)

    async def _run():
        transport = IrohTransport()
        transport.start()
        assert transport.state == "starting"
        await asyncio.sleep(0.5)
        return transport

    transport = asyncio.run(_run())
    assert transport.state == "failed"
    assert "timed out" in (transport.status()["error"] or "")


def test_the_iroh_secret_is_created_once_and_reused(tmp_path):
    key_file = tmp_path / "iroh_secret"
    first = get_or_create_iroh_secret(key_file)
    assert len(first) == 32
    assert key_file.exists()
    assert get_or_create_iroh_secret(key_file) == first


def test_a_corrupt_iroh_secret_is_replaced(tmp_path):
    key_file = tmp_path / "iroh_secret"
    key_file.write_text("not a key", encoding="utf-8")
    assert len(get_or_create_iroh_secret(key_file)) == 32


def test_a_selected_ip_path_resolves_to_a_direct_connection():
    paths = [
        _Path(is_selected=False, is_relay=True),
        _Path(is_selected=True, is_ip=True),
    ]
    assert resolve_iroh_path_transport(paths) == "iroh-direct"


def test_a_relay_only_connection_resolves_to_the_relay():
    assert resolve_iroh_path_transport([_Path(is_selected=True, is_relay=True)]) == "iroh-relay"
    assert resolve_iroh_path_transport([]) == "iroh-relay"
    assert resolve_iroh_path_transport(None) == "iroh-relay"


def test_an_unauthorised_request_is_rejected():
    transport = IrohTransport(token_provider=lambda: "expected-token")
    assert transport._authorised("expected-token") is True
    assert transport._authorised("wrong-token") is False
    assert transport._authorised(None) is False


def test_a_hub_without_a_token_accepts_any_request():
    transport = IrohTransport(token_provider=lambda: None)
    assert transport._authorised(None) is True
