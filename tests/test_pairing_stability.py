"""The pairing token must survive a restart and change only when asked.

A phone pairs once. Everything it needs to come back after the hub restarts is
in ~/.agnview: the token and the pair id. If either is rewritten behind the
user's back the phone is dropped with no error anywhere, which is what happened
when the test suite ran against the developer's real home directory and
rewrote both files.
"""

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_relay.api.app import create_app
from agent_relay.core import autostart, certs, pairing
from conftest import FAKE_HOME, REAL_HOME


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point the pairing files at a private directory for one test."""
    monkeypatch.delenv("AGENT_RELAY_TOKEN", raising=False)
    monkeypatch.setattr(pairing, "TOKEN_DIR", tmp_path)
    monkeypatch.setattr(pairing, "TOKEN_FILE", tmp_path / "pairing_token")
    monkeypatch.setattr(pairing, "PAIR_ID_FILE", tmp_path / "pair_id")
    return tmp_path


def test_the_token_is_identical_across_restarts(home):
    """Two consecutive hub starts must read back the same token."""
    first = pairing.get_or_create_pairing_token()

    # A restart is a fresh process with nothing in memory. The only thing that
    # carries over is the file, so read it back exactly as a new process would.
    for _ in range(2):
        assert pairing.get_or_create_pairing_token() == first

    assert (home / "pairing_token").read_text(encoding="utf-8").strip() == first


def test_the_pair_id_is_identical_across_restarts(home):
    first = pairing.get_or_create_pair_id()
    assert pairing.get_or_create_pair_id() == first
    assert (home / "pair_id").read_text(encoding="utf-8").strip() == first


def test_only_an_explicit_regenerate_changes_the_token(home):
    original_token = pairing.get_or_create_pairing_token()
    original_pair_id = pairing.get_or_create_pair_id()

    new_pair_id, new_token = pairing.regenerate_pairing_token()

    assert new_token != original_token
    assert new_pair_id != original_pair_id
    # And the new pair survives the next start, rather than rotating again.
    assert pairing.get_or_create_pairing_token() == new_token
    assert pairing.get_or_create_pair_id() == new_pair_id


def test_serve_reuses_the_persisted_token(home, monkeypatch):
    """`agnview serve` resolves the same token on every start.

    This is the exact expression cmd_serve uses, with no token on the command
    line and none in the environment.
    """
    from agent_relay.cli import main as cli

    monkeypatch.setattr(cli, "DEFAULT_TOKEN", None)

    def resolve_as_serve_does():
        return None or cli.DEFAULT_TOKEN or pairing.get_or_create_pairing_token()

    first = resolve_as_serve_does()
    assert resolve_as_serve_does() == first


def test_an_empty_token_file_is_replaced_rather_than_returned(home):
    (home / "pairing_token").write_text("   \n", encoding="utf-8")
    assert pairing.get_or_create_pairing_token().strip() != ""


# --------------------------------------------------------------------------
# The suite itself must never touch the machine it runs on.
# --------------------------------------------------------------------------

def test_the_suite_runs_against_a_throwaway_home():
    assert FAKE_HOME != REAL_HOME
    assert Path.home() == FAKE_HOME


@pytest.mark.parametrize(
    "path",
    [
        pairing.TOKEN_DIR,
        pairing.TOKEN_FILE,
        pairing.PAIR_ID_FILE,
        certs.CERT_DIR,
        autostart.OPT_OUT_MARKER,
    ],
)
def test_hub_state_files_resolve_outside_the_real_home(path):
    """Import-time constants must follow the redirect, not the real home."""
    real_state_dir = REAL_HOME / ".agnview"
    resolved = Path(path)
    assert resolved != real_state_dir
    assert real_state_dir not in resolved.parents
    assert FAKE_HOME in resolved.parents


def test_regenerating_over_the_api_leaves_the_real_home_alone(tmp_path):
    """The route that rotates the token must only ever write to the fake home."""
    real_token_file = REAL_HOME / ".agnview" / "pairing_token"

    def digest():
        # A digest, never the token itself: a failing assert prints what it
        # compared, and the real token must not reach a test log.
        if not real_token_file.exists():
            return None
        return hashlib.sha256(real_token_file.read_bytes()).hexdigest()

    before = digest()

    client = TestClient(
        create_app(db_path=str(tmp_path / "regen.db"), port=8765),
        base_url="http://127.0.0.1:8765",
        client=("127.0.0.1", 50000),
    )
    response = client.post("/api/mobile/pairing/regenerate")
    assert response.status_code == 200

    after = digest()
    assert after == before
    assert pairing.TOKEN_FILE.exists()
