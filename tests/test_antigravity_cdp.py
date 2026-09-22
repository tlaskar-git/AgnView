"""The automatic AntiGravity read: its debug port, its panel, and its refusals.

Nothing here needs AntiGravity. The connection half runs against a real
websocket server built by a different library, so a mistake in the hand-written
client shows up as a failed handshake rather than as two matching bugs. The
parsing half runs against the panel wording the operator's own model picker
prints, the same fixtures the pasted extractor is tested with, and the two
implementations are compared against each other so they cannot drift apart.
"""

import inspect
import json
import socket
import threading

import pytest

from agent_relay.core import antigravity
from agent_relay.core.models import UsageAccount
from agent_relay.core.usage import fetch_observation, render_observation
from agent_relay.core.usage.adapters import google_code_assist as google_adapter

from test_antigravity_extractor import INLINE_PANEL, STACKED_PANEL, _run_extractor

websockets_server = pytest.importorskip(
    "websockets.sync.server",
    reason="websockets ships with uvicorn[standard]; without it there is no mock debug port",
)
from websockets.datastructures import Headers  # noqa: E402
from websockets.http11 import Response  # noqa: E402


# ---------------------------------------------------------------------------
# The port file and the running check
# ---------------------------------------------------------------------------


def test_the_port_is_the_first_line_of_the_file(tmp_path):
    """Chromium writes the port on line one and the browser path on line two."""
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("57460\n/devtools/browser/b3431bbf\n", encoding="utf-8")

    assert antigravity.read_devtools_port(port_file) == 57460


@pytest.mark.parametrize("contents", ["", "not-a-port\n", "0\n", "70000\n", "  \n"])
def test_an_unusable_port_file_reads_as_no_port(tmp_path, contents):
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text(contents, encoding="utf-8")

    assert antigravity.read_devtools_port(port_file) is None


def test_a_missing_port_file_reads_as_no_port(tmp_path):
    assert antigravity.read_devtools_port(tmp_path / "nothing-here") is None


def test_a_stale_port_file_does_not_count_as_running(tmp_path):
    """A closed app leaves its port file behind. Only a live listener counts.

    This is the exact state of the machine this was written on: the file named
    a port, and nothing was listening on it.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text(f"{port}\n", encoding="utf-8")

    assert antigravity.is_running(port_file) is True

    listener.close()
    assert antigravity.is_running(port_file) is False


def test_the_well_known_location_is_the_only_one_used():
    """A future user's install, not one machine's paths."""
    assert antigravity.devtools_port_file().name == "DevToolsActivePort"
    assert antigravity.devtools_port_file().parent.name == "Antigravity"


def test_nothing_in_the_module_can_start_an_application():
    """A routine usage refresh must never launch AntiGravity."""
    source = inspect.getsource(antigravity)
    for forbidden in ("subprocess", "startfile", "os.system", "Popen"):
        assert forbidden not in source


# ---------------------------------------------------------------------------
# A stand-in debug port, spoken to exactly as Chromium's is
# ---------------------------------------------------------------------------


class _FakeDebugPort:
    """An HTTP /json/list endpoint and a CDP websocket on one port.

    The websocket side is the ``websockets`` library, not this project's client,
    so the handshake, masking and framing in ``antigravity._WebSocket`` are
    checked against an independent implementation of RFC 6455.
    """

    def __init__(self, panel_text, targets=None, push_event_first=False):
        self.panel_text = panel_text
        self.targets = targets
        self.push_event_first = push_event_first
        self.requests = []
        self._server = websockets_server.serve(
            self._handle,
            "127.0.0.1",
            0,
            compression=None,
            process_request=self._process_request,
        )
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _json_list_body(self):
        if self.targets is not None:
            return self.targets
        return [
            {"type": "service_worker", "url": "chrome://x", "webSocketDebuggerUrl": "ws://x/sw"},
            {
                "type": "page",
                "url": "file:///antigravity/workbench.html",
                "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/PAGE1",
            },
        ]

    def _process_request(self, connection, request):
        if request.path.startswith("/json"):
            body = json.dumps(self._json_list_body()).encode("utf-8")
            return Response(
                200,
                "OK",
                Headers(
                    [("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
                ),
                body,
            )
        return None

    def _handle(self, connection):
        if self.push_event_first:
            # A real target pushes events of its own. The client must skip past
            # them and still find the reply it asked for.
            connection.send(json.dumps({"method": "Runtime.executionContextCreated"}))
        for raw in connection:
            message = json.loads(raw)
            self.requests.append(message)
            if message.get("method") == "Runtime.evaluate":
                connection.send(
                    json.dumps(
                        {
                            "id": message["id"],
                            "result": {"result": {"type": "string", "value": self.panel_text}},
                        }
                    )
                )

    def port_file(self, tmp_path):
        path = tmp_path / "DevToolsActivePort"
        path.write_text(f"{self.port}\n/devtools/browser/fake\n", encoding="utf-8")
        return path

    def close(self):
        self._server.shutdown()
        self._thread.join(timeout=5)


@pytest.fixture
def debug_port(request, tmp_path):
    def _make(panel_text, **kwargs):
        server = _FakeDebugPort(panel_text, **kwargs)
        request.addfinalizer(server.close)
        return server, server.port_file(tmp_path)

    return _make


def test_the_whole_read_works_over_a_real_websocket(debug_port):
    """Port file, /json/list, Runtime.evaluate, parse, payload."""
    server, port_file = debug_port(INLINE_PANEL)

    read = antigravity.read_usage(port_file)

    assert read.error is None
    assert read.payload["session_percent_used"] == 15
    assert [row["group"] for row in read.payload["session_breakdown"]] == [
        "Gemini Models",
        "Claude and GPT models",
    ]
    # The call really was a Runtime.evaluate that only reads what is on screen.
    evaluate = [m for m in server.requests if m.get("method") == "Runtime.evaluate"]
    assert len(evaluate) == 1
    assert evaluate[0]["params"]["expression"] == "(document.body && document.body.innerText) || ''"
    assert evaluate[0]["params"]["returnByValue"] is True


def test_an_event_arriving_before_the_reply_is_stepped_over(debug_port):
    _server, port_file = debug_port(STACKED_PANEL, push_event_first=True)

    read = antigravity.read_usage(port_file)

    assert read.error is None
    assert read.payload["session_percent_used"] == 15


def test_a_window_without_the_panel_says_so_rather_than_showing_a_zero(debug_port):
    _server, port_file = debug_port("Antigravity\nOpen a workspace to begin")

    read = antigravity.read_usage(port_file)

    assert read.payload is None
    assert read.error == antigravity.PANEL_NOT_ON_SCREEN_MESSAGE


def test_a_debug_port_with_no_page_target_is_reported_not_guessed(debug_port):
    _server, port_file = debug_port(
        INLINE_PANEL,
        targets=[{"type": "page", "url": "devtools://devtools/bundled", "webSocketDebuggerUrl": "ws://x/1"}],
    )

    read = antigravity.read_usage(port_file)

    assert read.payload is None
    assert "listed no window" in read.error


def test_a_port_that_answers_but_not_with_json_falls_back_cleanly(tmp_path):
    """Something else holding the port must not raise out of a usage refresh."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text(f"{port}\n", encoding="utf-8")

    def _hang_up():
        try:
            conn, _ = listener.accept()
            conn.close()
        except OSError:
            pass

    threading.Thread(target=_hang_up, daemon=True).start()
    try:
        read = antigravity.read_usage(port_file)
    finally:
        listener.close()

    assert read.payload is None
    assert read.error


def test_nothing_running_is_named_as_the_reason(tmp_path):
    read = antigravity.read_usage(tmp_path / "no-port-file")

    assert read.payload is None
    assert read.error == antigravity.NOT_RUNNING_MESSAGE
    assert "needs to be running" in read.error


# ---------------------------------------------------------------------------
# The parsing rules, and the guard against a second copy drifting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("panel", [INLINE_PANEL, STACKED_PANEL], ids=["inline", "stacked"])
def test_the_parser_reads_both_groups_and_both_windows(panel):
    payload = antigravity.parse_usage_panel(panel)

    assert [row["group"] for row in payload["weekly_breakdown"]] == [
        "Gemini Models",
        "Claude and GPT models",
    ]
    # "Remaining" means what is left, so the used share is the complement.
    assert payload["session_breakdown"][0]["session_percent_used"] == 9
    assert payload["session_breakdown"][0]["session_percent_left"] == 91
    assert payload["session_breakdown"][1]["session_percent_used"] == 15
    assert payload["weekly_breakdown"][0]["weekly_percent_used"] == 0
    assert payload["session_breakdown"][0]["session_title"] == "Five Hour Limit Remaining"
    # The headline figure is the group closest to its limit.
    assert payload["session_percent_used"] == 15
    assert payload["weekly_percent_used"] == 0


def test_the_parser_reports_nothing_when_the_panel_is_not_on_screen():
    assert antigravity.parse_usage_panel("Antigravity\nOpen a workspace to begin") == {}
    assert antigravity.parse_usage_panel("") == {}


def test_the_parser_skips_a_window_whose_wording_says_neither_used_nor_left():
    payload = antigravity.parse_usage_panel(
        "\n".join(["Gemini Models", "Weekly Limit 40%", "Five Hour Limit Remaining 91%"])
    )

    assert "weekly_breakdown" not in payload
    assert payload["session_breakdown"][0]["session_percent_used"] == 9


def test_the_parser_reads_a_panel_that_reports_what_is_used():
    payload = antigravity.parse_usage_panel(
        "\n".join(["Gemini Models", "Weekly Limit Used 12.5%", "Five Hour Limit Used 40%"])
    )

    assert payload["weekly_breakdown"][0]["weekly_percent_used"] == 12.5
    assert payload["weekly_breakdown"][0]["weekly_percent_left"] == 87.5
    assert payload["session_percent_used"] == 40


@pytest.mark.parametrize("panel", [INLINE_PANEL, STACKED_PANEL], ids=["inline", "stacked"])
def test_the_pasted_script_and_the_python_parser_agree(tmp_path, panel):
    """The two copies of the rules must produce the same payload, always.

    The automatic read parses in Python and the Usage tab still hands out a
    JavaScript extractor to paste. This runs the real pasted script in Node over
    the same panel text and compares it with the Python payload field by field,
    so neither copy can be changed on its own without this failing.
    """
    posted = _run_extractor(tmp_path, "gemini", "gemini-test", panel)

    assert posted[0]["body"] == antigravity.parse_usage_panel(panel)


# ---------------------------------------------------------------------------
# What the usage card ends up showing
# ---------------------------------------------------------------------------


def _gemini_account():
    return UsageAccount(id="acc-antigravity", provider="gemini", name="AntiGravity")


@pytest.fixture
def no_google_signin(tmp_path, monkeypatch):
    """Leave Code Assist with nothing to read, so the panel rung is what is tested.

    Without this the lower rungs would reach the developer's own Google
    credential and their reason would join the one under test.
    """
    monkeypatch.setattr(google_adapter, "CREDS_PATH", tmp_path / "no-creds.json")
    monkeypatch.setattr(google_adapter, "ACCOUNTS_PATH", tmp_path / "no-accounts.json")


def test_a_live_read_fills_the_windows_a_manual_sync_would(monkeypatch, no_google_signin):
    """The panel is the top rung for Gemini, so a live read wins outright."""
    monkeypatch.setattr(
        antigravity,
        "read_usage",
        lambda *_a, **_k: antigravity.AntigravityRead(
            payload=antigravity.parse_usage_panel(INLINE_PANEL)
        ),
    )

    observation = fetch_observation(_gemini_account())
    view = render_observation(observation)

    assert view["confidence"] == "measured"
    assert view["source"] == "agy_panel"
    assert view["source_label"] == "AntiGravity usage panel"
    assert view["error"] is None

    session = observation.window("session")
    weekly = observation.window("week")
    assert session.used == 15
    assert session.label == "Five Hour Limit Remaining"
    assert weekly.used == 0
    # Per-model-group rows land on the window they describe, so the two clocks
    # stay apart without a second flat field.
    assert len(session.breakdown) == 2
    assert len(weekly.breakdown) == 2

    # A share of a window is not a token count, so nothing is back-derived.
    assert all(w.unit == "percent" for w in observation.windows)
    assert all(w.limit == 100.0 for w in observation.windows)

    # The panel publishes no reset time, so no countdown is invented for it.
    assert session.window_end is None
    assert view["windows"][0]["countdown_text"] is None


def test_a_closed_antigravity_says_what_to_do_about_it(monkeypatch, no_google_signin):
    monkeypatch.setattr(
        antigravity,
        "read_usage",
        lambda *_a, **_k: antigravity.AntigravityRead(error=antigravity.NOT_RUNNING_MESSAGE),
    )

    observation = fetch_observation(_gemini_account())

    assert observation.confidence == "unavailable"
    assert "needs to be running" in observation.error
    # No windows at all, so no figure can be shown by accident.
    assert observation.windows == []


def test_a_failed_read_says_it_failed_rather_than_going_generic(monkeypatch, no_google_signin):
    reason = "AntiGravity is running, but reading its window failed (timed out)."
    monkeypatch.setattr(
        antigravity,
        "read_usage",
        lambda *_a, **_k: antigravity.AntigravityRead(error=reason),
    )

    observation = fetch_observation(_gemini_account())

    assert observation.confidence == "unavailable"
    assert reason in observation.error


def test_the_panel_reason_leads_even_when_a_lower_rung_also_failed(tmp_path, monkeypatch):
    """The highest rung's reason comes first, because it is the actionable one.

    Reporting the last rung's reason instead buried the useful instruction: a
    Gemini card said only that Google refuses Code Assist, and dropped
    "open the AntiGravity app", which is the thing that fixes it.
    """
    creds = tmp_path / "oauth_creds.json"
    creds.write_text(json.dumps({"access_token": "t", "expiry_date": 1}), encoding="utf-8")
    accounts = tmp_path / "google_accounts.json"
    accounts.write_text(json.dumps({"active": "someone@example.com"}), encoding="utf-8")
    monkeypatch.setattr(google_adapter, "CREDS_PATH", creds)
    monkeypatch.setattr(google_adapter, "ACCOUNTS_PATH", accounts)
    monkeypatch.setattr(
        antigravity,
        "read_usage",
        lambda *_a, **_k: antigravity.AntigravityRead(error=antigravity.NOT_RUNNING_MESSAGE),
    )

    observation = fetch_observation(_gemini_account())

    assert observation.source == "agy_panel", "the panel rung must own the message"
    assert observation.error.startswith(antigravity.NOT_RUNNING_MESSAGE[:30])
    # The lower rung's reason is kept after it, not instead of it.
    assert "someone@example.com" in observation.error


def test_the_panel_also_serves_the_antigravity_card(monkeypatch):
    """One reader, both cards. AntiGravity prints its own limits and Gemini's."""
    monkeypatch.setattr(
        antigravity,
        "read_usage",
        lambda *_a, **_k: antigravity.AntigravityRead(
            payload=antigravity.parse_usage_panel(INLINE_PANEL)
        ),
    )

    observation = fetch_observation(
        UsageAccount(id="acc-agy", provider="antigravity", name="AntiGravity")
    )

    assert observation.source == "agy_panel"
    assert observation.confidence == "measured"
    assert observation.window("session").used == 15
