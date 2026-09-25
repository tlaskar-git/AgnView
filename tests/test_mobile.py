"""Tests for AgnView Mobile Gateway, zero-config network detection, QR pairing, and endpoints."""

import os
import pytest
from fastapi.testclient import TestClient
from agent_relay.api.app import create_app
from agent_relay.core.network import (
    get_local_ip,
    get_network_endpoints,
)
from agent_relay.core.pairing import (
    get_or_create_pairing_token,
    build_pairing_payload,
    generate_qr_svg,
    get_terminal_qr_ascii,
)


@pytest.fixture
def client(tmp_path):
    db_file = str(tmp_path / "mobile_test.db")
    app = create_app(db_path=db_file, port=8765)
    return TestClient(app)


def test_network_detection():
    """Verify local IP detection and endpoints generation."""
    local_ip = get_local_ip()
    assert isinstance(local_ip, str)
    assert len(local_ip) > 0

    endpoints = get_network_endpoints(port=8765)
    assert "local" in endpoints
    assert "localhost" in endpoints
    assert endpoints["local"] == "http://localhost:8765"
    if local_ip != "127.0.0.1":
        assert endpoints["lan"] == f"http://{local_ip}:8765"


def test_pairing_token_lifecycle():
    """Verify pairing token generation, format, and stability."""
    token1 = get_or_create_pairing_token()
    assert isinstance(token1, str)
    assert len(token1) >= 32

    token2 = get_or_create_pairing_token()
    assert token1 == token2


def test_qr_generation():
    """Verify SVG and ASCII QR code generation."""
    data = "agnview://pair?v=1&name=Workstation&lan=192.168.1.50:8765&fp=abc&id=xyz&k=key123"
    svg = generate_qr_svg(data)
    assert "<svg" in svg
    assert "</svg>" in svg

    ascii_art = get_terminal_qr_ascii(data)
    assert isinstance(ascii_art, str)
    assert len(ascii_art) > 50


def test_build_pairing_payload():
    """Verify deep link and endpoints packaging."""
    endpoints = {
        "local": "http://localhost:8765",
        "lan": "http://192.168.1.50:8765",
    }
    payload = build_pairing_payload(
        primary_url="http://192.168.1.50:8765",
        endpoints=endpoints,
        token="test_secret_token_32_bytes_long_here",
    )
    assert payload["token"] == "test_secret_token_32_bytes_long_here"
    assert payload["primary_url"] == "http://192.168.1.50:8765"
    assert payload["deeplink"].startswith("agnview://pair?v=1")
    assert "k=test_secret_token_32_bytes_long_here" in payload["deeplink"]


def test_api_mobile_endpoints(client):
    """Verify mobile API routes: pairing, status, and regeneration."""
    # 1. Mobile Status probe
    res_status = client.get("/api/mobile/status")
    assert res_status.status_code == 200
    status_data = res_status.json()
    assert status_data["status"] == "healthy"
    assert "endpoints" in status_data

    # 2. Mobile Pairing endpoint (QR Code & Deep Link)
    res_pair = client.get("/api/mobile/pairing")
    assert res_pair.status_code == 200
    pair_data = res_pair.json()
    assert pair_data["success"] is True
    assert len(pair_data["pairing_token"]) >= 32
    assert "<svg" in pair_data["qr_svg"]
    assert "agnview://pair?v=1" in pair_data["deep_link"]
    assert "endpoints" in pair_data

    # 3. Mobile Pairing regeneration
    res_regen = client.post("/api/mobile/pairing/regenerate")
    assert res_regen.status_code == 200
    regen_data = res_regen.json()
    assert regen_data["success"] is True
    assert len(regen_data["pairing_token"]) >= 32
    assert "pair_id" in regen_data
    # Clean up environment token so subsequent test suites with unauthenticated client do not fail with 401
    os.environ.pop("AGENT_RELAY_TOKEN", None)


def test_a_hostname_lookup_that_never_returns_does_not_block_the_endpoints(monkeypatch):
    import threading
    import time

    from agent_relay.core import network

    release = threading.Event()
    monkeypatch.setattr(network, "HOSTNAME_LOOKUP_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(network.socket, "gethostname", lambda: "example-host")
    monkeypatch.setattr(network.socket, "gethostbyname", lambda name: release.wait(30))
    monkeypatch.setattr(network.socket, "getaddrinfo", lambda *a, **k: release.wait(30))
    started = time.monotonic()
    try:
        endpoints = get_network_endpoints(port=8765)
        assert time.monotonic() - started < 3.0
        assert "hostname" not in endpoints
        assert endpoints["localhost"] == "http://127.0.0.1:8765"
    finally:
        release.set()


def test_a_lookup_result_is_returned_and_an_error_gives_the_default():
    from agent_relay.core.network import resolve_with_timeout

    assert resolve_with_timeout(lambda name: "192.0.2.7", "example-host") == "192.0.2.7"
    assert resolve_with_timeout(lambda name: 1 / 0, "example-host", default="none") == "none"

