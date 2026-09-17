"""Tests for the connection order and the transport the hub reports."""

from fastapi.testclient import TestClient

from agent_relay.api.app import create_app
from agent_relay.core.network import (
    CONNECTION_ORDER,
    LAN_CONNECT_TIMEOUT_SECONDS,
    RESOLVED_TRANSPORTS,
    available_transports,
    resolve_hub_transport,
)


def test_the_connection_order_is_lan_then_direct_then_relay():
    assert CONNECTION_ORDER == ("lan", "iroh-direct", "iroh-relay")
    assert LAN_CONNECT_TIMEOUT_SECONDS == 0.8


def test_a_loopback_hub_without_iroh_is_offline():
    assert resolve_hub_transport("loopback", {"state": "failed"}) == "offline"
    assert available_transports("loopback", {"state": "failed"}) == []


def test_a_lan_hub_resolves_to_lan():
    assert resolve_hub_transport("lan", {"state": "failed"}) == "lan"
    assert resolve_hub_transport("tailscale", {"state": "failed"}) == "lan"


def test_a_ready_endpoint_without_a_client_is_offline_but_offers_the_relay_rung():
    # The relay rung is a real capability (available_transports), but nothing
    # is actually connected yet, so the hub's own resolved state stays
    # offline rather than guessing relay just because it is possible.
    status = {"state": "ready", "last_resolved_transport": None}
    assert available_transports("loopback", status) == ["iroh-relay"]
    assert resolve_hub_transport("loopback", status) == "offline"
    # A LAN bind is a standing fact, not a guess, so it still wins.
    assert resolve_hub_transport("lan", status) == "lan"


def test_a_measured_client_rung_beats_the_guess():
    status = {"state": "ready", "last_resolved_transport": "iroh-direct"}
    assert resolve_hub_transport("lan", status) == "iroh-direct"


def test_the_transport_route_describes_the_order(tmp_path):
    app = create_app(db_path=str(tmp_path / "transport.db"))
    with TestClient(app) as client:
        data = client.get("/api/transport").json()

    assert data["connection_order"] == list(CONNECTION_ORDER)
    assert data["lan_timeout_ms"] == 800
    assert data["resolved_transport"] in RESOLVED_TRANSPORTS
    assert data["resolved_transport_label"]
    assert data["iroh"]["state"] == "disabled"
    assert data["iroh"]["ticket"] is None


def test_the_transport_route_surfaces_the_iroh_ticket_and_rung(tmp_path):
    app = create_app(db_path=str(tmp_path / "transport_iroh.db"))
    with TestClient(app) as client:
        app.state.iroh._state = "ready"
        app.state.iroh._ticket = "endpointabc"
        app.state.iroh._last_resolved = "iroh-direct"
        data = client.get("/api/transport").json()

    assert data["resolved_transport"] == "iroh-direct"
    assert data["iroh"]["ticket"] == "endpointabc"
    assert data["iroh"]["state"] == "ready"


def test_the_mobile_status_route_carries_the_resolved_transport(tmp_path):
    app = create_app(db_path=str(tmp_path / "mobile_transport.db"))
    with TestClient(app) as client:
        data = client.get("/api/mobile/status").json()
    assert data["resolved_transport"] in RESOLVED_TRANSPORTS
    assert data["iroh"]["state"] == "disabled"


# The connected frame of GET /api/events carries the same resolved_transport
# value. It is checked against a running hub rather than here: TestClient holds
# a streaming response open until the generator finishes, so an SSE assertion
# in pytest hangs the suite.
