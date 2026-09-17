"""Tests for the QR pairing payload, v1 and v2.

The contract in docs/PAIRING.md is strict: a device paired under v1 must keep
working unchanged after iroh lands. These tests hold the hub to that.
"""

import pytest
from fastapi.testclient import TestClient

from agent_relay.api.app import create_app
from agent_relay.core.pairing import (
    PAIRING_PAYLOAD_VERSION,
    build_pairing_payload,
    build_pairing_qr_uri,
    parse_pairing_qr_uri,
)


# A payload captured from the hub before iroh existed. Nothing here may change.
V1_PAYLOAD = (
    "agnview://pair?v=1"
    "&name=Workstation"
    "&lan=192.168.1.50:8765"
    "&fp=9f2c4a1b"
    "&id=Zm9vYmFyYmF6cXV4MTIzNA"
    "&k=c2VjcmV0LWtleS0zMi1ieXRlcy1sb25nLWhlcmU"
)


def test_the_original_v1_payload_still_parses():
    parsed = parse_pairing_qr_uri(V1_PAYLOAD)
    assert parsed["version"] == 1
    assert parsed["name"] == "Workstation"
    assert parsed["lan"] == "192.168.1.50:8765"
    assert parsed["fingerprint"] == "9f2c4a1b"
    assert parsed["pair_id"] == "Zm9vYmFyYmF6cXV4MTIzNA"
    assert parsed["token"] == "c2VjcmV0LWtleS0zMi1ieXRlcy1sb25nLWhlcmU"
    assert parsed["iroh_ticket"] is None


def test_a_hub_without_an_iroh_ticket_still_emits_v1():
    uri = build_pairing_qr_uri(port=8765, token="test-token")
    assert uri.startswith("agnview://pair?v=1")
    assert "iroh=" not in uri
    assert parse_pairing_qr_uri(uri)["version"] == 1


def test_a_hub_with_an_iroh_ticket_emits_v2_and_keeps_every_v1_field():
    ticket = "endpointacwgqblgarrzdzfrjteo2hx4goxnri4j6nm26eyb63moftlpz54beaq"
    v1 = build_pairing_qr_uri(port=8765, token="test-token")
    v2 = build_pairing_qr_uri(port=8765, token="test-token", iroh_ticket=ticket)

    assert v2.startswith(f"agnview://pair?v={PAIRING_PAYLOAD_VERSION}")
    # v2 is v1 with the version bumped and one field appended, nothing else.
    assert v2 == v1.replace("?v=1", f"?v={PAIRING_PAYLOAD_VERSION}", 1) + f"&iroh={ticket}"

    parsed = parse_pairing_qr_uri(v2)
    assert parsed["version"] == 2
    assert parsed["iroh_ticket"] == ticket
    assert parsed["token"] == "test-token"


def test_an_unknown_field_in_a_v2_payload_is_ignored():
    parsed = parse_pairing_qr_uri(V1_PAYLOAD.replace("?v=1", "?v=2") + "&future=whatever")
    assert parsed["version"] == 2
    assert parsed["iroh_ticket"] is None
    assert parsed["token"] == "c2VjcmV0LWtleS0zMi1ieXRlcy1sb25nLWhlcmU"


def test_an_unsupported_version_is_refused():
    with pytest.raises(ValueError, match="unsupported pairing payload version"):
        parse_pairing_qr_uri(V1_PAYLOAD.replace("?v=1", "?v=99"))


def test_a_malformed_uri_is_refused():
    with pytest.raises(ValueError, match="not an AgnView pairing URI"):
        parse_pairing_qr_uri("https://example.invalid/pair?v=1")
    with pytest.raises(ValueError, match="missing 'k'"):
        parse_pairing_qr_uri("agnview://pair?v=1&name=Box&lan=10.0.0.2:8765&id=abc&k=")


def test_the_payload_dict_reports_its_version_and_ticket():
    without = build_pairing_payload("http://10.0.0.2:8765", {}, token="t", port=8765)
    assert without["payload_version"] == 1
    assert without["iroh_ticket"] is None

    with_ticket = build_pairing_payload(
        "http://10.0.0.2:8765", {}, token="t", port=8765, iroh_ticket="endpointabc"
    )
    assert with_ticket["payload_version"] == 2
    assert with_ticket["iroh_ticket"] == "endpointabc"


def test_the_pairing_route_emits_v1_when_iroh_is_not_up(tmp_path):
    app = create_app(db_path=str(tmp_path / "pairing.db"))
    with TestClient(app) as client:
        data = client.get("/api/mobile/pairing").json()
    assert data["deep_link"].startswith("agnview://pair?v=1")
    assert parse_pairing_qr_uri(data["deep_link"])["iroh_ticket"] is None


def test_the_pairing_route_emits_v2_once_a_ticket_exists(tmp_path):
    ticket = "endpointacwgqblgarrzdzfrjteo2hx4goxnri4j6nm26eyb63moftlpz54beaq"
    app = create_app(db_path=str(tmp_path / "pairing_iroh.db"))
    with TestClient(app) as client:
        app.state.iroh._ticket = ticket
        data = client.get("/api/mobile/pairing").json()

    parsed = parse_pairing_qr_uri(data["deep_link"])
    assert parsed["version"] == 2
    assert parsed["iroh_ticket"] == ticket
    # The LAN rung is untouched, so a v1 client loses nothing.
    assert parsed["lan"] == parse_pairing_qr_uri(
        build_pairing_qr_uri(port=8765, token=parsed["token"])
    )["lan"]
