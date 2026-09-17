"""Shared pytest fixtures for the AgnView suite.

``regenerate_pairing_token()`` deliberately exports the new token into
``os.environ["AGENT_RELAY_TOKEN"]`` so a running server picks it up without a
restart.  Inside a single pytest process that write is global: once
``tests/test_mobile.py`` exercises ``POST /api/mobile/pairing/regenerate`` every
subsequent ``create_app()`` sees a token and installs the auth middleware, so
unrelated tests start getting 401s purely because of collection order.

Snapshot and restore the variable around every test so each one builds its app
in the environment it expects.  The auth check itself is untouched.
"""

import os

import pytest


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
