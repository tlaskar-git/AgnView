"""Shared pytest fixtures for the AgnView suite.

Every module under ``agent_relay`` resolves its on-disk state from
``Path.home()`` at import time: the pairing token and pair id, the TLS
certificate, the iroh secret key, the autostart opt-out marker, the agent and
notification configuration. A test run that uses the real home writes over all
of it. ``tests/test_mobile.py`` exercises
``POST /api/mobile/pairing/regenerate``, which rewrote the developer's own
``~/.agnview/pairing_token`` and ``~/.agnview/pair_id``, so every paired phone
was silently dropped and the next ``agnview serve`` printed a different pairing
token with nobody having asked for a new one.

Redirect the home directory to a throwaway directory here, at import time,
before any ``agent_relay`` module is imported and therefore before any of those
constants are computed. Everything the suite writes then lands in the
throwaway copy. ``tests/test_pairing_stability.py`` holds this in place.

``regenerate_pairing_token()`` also used to export the new token into
``os.environ["AGENT_RELAY_TOKEN"]`` so a running server picked it up without a
restart. Inside a single pytest process that write is global: once one test
regenerates, every subsequent ``create_app()`` sees a token and installs the
auth middleware, so unrelated tests start getting 401s purely because of
collection order. Snapshot and restore the variable around every test so each
one builds its app in the environment it expects. The auth check itself is
untouched.
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest


# The home directory the developer actually uses, captured before the redirect
# below. Tests assert against it to prove the redirect is in force.
REAL_HOME = Path.home()

_FAKE_HOME = Path(tempfile.mkdtemp(prefix="agnview-test-home-"))

# USERPROFILE is what Path.home() reads on Windows, HOME on POSIX. Set both so
# the redirect holds on every platform the suite runs on.
os.environ["USERPROFILE"] = str(_FAKE_HOME)
os.environ["HOME"] = str(_FAKE_HOME)

FAKE_HOME = Path.home()

# The same reasoning for the application-data directory. AntiGravity's debug
# port file lives under APPDATA on Windows and XDG_CONFIG_HOME on Linux, and the
# usage fetcher reads it on every Gemini refresh. Left pointing at the real
# machine, a test run would read whatever the developer's own AntiGravity was
# doing at that moment, so the same test would pass or fail depending on whether
# an app happened to be open. Point both at the throwaway home instead.
os.environ["APPDATA"] = str(_FAKE_HOME / "AppData" / "Roaming")
os.environ["XDG_CONFIG_HOME"] = str(_FAKE_HOME / ".config")

# A Claude card with an expired sign-in asks Claude Code to refresh it by
# running a real prompt. A test must never do that, so it is off for the
# whole suite. Tests of the refresh itself switch it back on and replace the
# command.
os.environ["AGNVIEW_CLAUDE_REFRESH"] = "0"


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_FAKE_HOME, ignore_errors=True)


@pytest.fixture(autouse=True)
def disable_iroh_transport_by_default():
    """Keep the suite off the network.

    Every ``create_app()`` schedules an iroh bind on startup. That is right for
    a real hub and wrong for a test run, which would open a public endpoint per
    test. Tests that exercise the transport turn it back on themselves.
    """
    previous = os.environ.get("AGNVIEW_IROH")
    os.environ["AGNVIEW_IROH"] = "0"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("AGNVIEW_IROH", None)
        else:
            os.environ["AGNVIEW_IROH"] = previous


@pytest.fixture(autouse=True)
def isolate_relay_token_env():
    sentinel = object()
    previous = os.environ.get("AGENT_RELAY_TOKEN", sentinel)
    try:
        yield
    finally:
        if previous is sentinel:
            os.environ.pop("AGENT_RELAY_TOKEN", None)
        else:
            os.environ["AGENT_RELAY_TOKEN"] = previous
