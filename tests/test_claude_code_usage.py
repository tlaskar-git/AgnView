"""Tests for counting Claude Code usage from its own transcripts.

The two bugs pinned here both under-reported real usage: subagent transcripts
sit two directories deeper than the session file and were skipped entirely, and
a request written twice was counted at whichever figure happened to be read
first rather than at its finished total.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from agent_relay.core.claude_code_usage import read_usage


NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def _write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _turn(when, tokens, request_id=None, **extra):
    usage = {"input_tokens": tokens, "output_tokens": 0}
    usage.update(extra)
    record = {
        "type": "assistant",
        "timestamp": when.isoformat().replace("+00:00", "Z"),
        "message": {"usage": usage},
    }
    if request_id:
        record["requestId"] = request_id
    return record


@pytest.fixture
def projects(tmp_path):
    return tmp_path / "projects"


def test_no_transcript_directory_reports_nothing(tmp_path):
    assert read_usage(projects_dir=tmp_path / "absent", now=NOW) is None


def test_an_empty_directory_reports_zero(projects):
    projects.mkdir(parents=True)
    usage = read_usage(projects_dir=projects, now=NOW)
    assert usage is not None
    assert usage.session_tokens == 0
    assert usage.week_tokens == 0


def test_subagent_transcripts_are_counted(projects):
    """Work handed to a subagent is charged to the same account."""
    _write(
        projects / "a-project" / "session.jsonl",
        [_turn(NOW - timedelta(minutes=10), 100, "req-main")],
    )
    _write(
        projects / "a-project" / "session" / "subagents" / "agent-1.jsonl",
        [_turn(NOW - timedelta(minutes=5), 400, "req-sub")],
    )

    usage = read_usage(projects_dir=projects, now=NOW)

    assert usage.session_tokens == 500
    assert usage.session_turns == 2


def test_a_request_written_twice_counts_its_finished_total_once(projects):
    """An intermediate record and the final one share a request id."""
    _write(
        projects / "a-project" / "session.jsonl",
        [
            _turn(NOW - timedelta(minutes=9), 300, "req-1"),
            _turn(NOW - timedelta(minutes=8), 950, "req-1"),
        ],
    )

    usage = read_usage(projects_dir=projects, now=NOW)

    assert usage.session_turns == 1
    assert usage.session_tokens == 950


def test_the_largest_total_wins_whichever_file_is_read_first(projects):
    """The same request in two files must not depend on traversal order."""
    _write(
        projects / "zzz-late" / "session.jsonl",
        [_turn(NOW - timedelta(minutes=7), 120, "req-shared")],
    )
    _write(
        projects / "aaa-early" / "session.jsonl",
        [_turn(NOW - timedelta(minutes=7), 800, "req-shared")],
    )

    usage = read_usage(projects_dir=projects, now=NOW)

    assert usage.session_tokens == 800
    assert usage.session_turns == 1


def test_cache_tokens_count_towards_the_total(projects):
    _write(
        projects / "a-project" / "session.jsonl",
        [
            _turn(
                NOW - timedelta(minutes=1),
                10,
                "req-cache",
                cache_creation_input_tokens=1000,
                cache_read_input_tokens=5000,
            )
        ],
    )

    usage = read_usage(projects_dir=projects, now=NOW)

    assert usage.session_tokens == 6010


def test_the_two_windows_are_measured_separately(projects):
    _write(
        projects / "a-project" / "session.jsonl",
        [
            _turn(NOW - timedelta(hours=1), 50, "req-recent"),
            _turn(NOW - timedelta(hours=30), 70, "req-older"),
            _turn(NOW - timedelta(days=9), 900, "req-ancient"),
        ],
    )

    usage = read_usage(projects_dir=projects, now=NOW)

    assert usage.session_tokens == 50
    assert usage.session_turns == 1
    assert usage.week_tokens == 120
    assert usage.week_turns == 2


def test_unreadable_lines_are_skipped_rather_than_failing(projects):
    path = projects / "a-project" / "session.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        "not json at all\n"
        + json.dumps({"type": "user", "message": {"content": "hello"}})
        + "\n"
        + json.dumps(_turn(NOW - timedelta(minutes=2), 42, "req-ok"))
        + "\n",
        encoding="utf-8",
    )

    usage = read_usage(projects_dir=projects, now=NOW)

    assert usage.session_tokens == 42


def test_projects_are_counted_so_a_big_turn_count_is_explainable(projects):
    """The window covers the whole machine, so the card can say how widely.

    1432 turns in five hours reads as absurd for one person until the card
    also says how many projects produced them. Subagent transcripts belong to
    the project above them and must not inflate that count.
    """
    _write(
        projects / "project-one" / "session.jsonl",
        [_turn(NOW - timedelta(minutes=5), 10, "req-one")],
    )
    _write(
        projects / "project-one" / "session" / "subagents" / "helper.jsonl",
        [_turn(NOW - timedelta(minutes=4), 20, "req-sub")],
    )
    _write(
        projects / "project-two" / "session.jsonl",
        [_turn(NOW - timedelta(minutes=3), 30, "req-two")],
    )

    usage = read_usage(projects_dir=projects, now=NOW)

    assert usage.projects_counted == 2
    assert usage.session_tokens == 60
    assert usage.session_turns == 3


def test_a_project_with_no_turn_in_the_window_is_not_counted(projects):
    """Only projects that actually contributed a counted turn are named."""
    _write(
        projects / "recent" / "session.jsonl",
        [_turn(NOW - timedelta(hours=2), 10, "req-recent")],
    )
    _write(
        projects / "stale" / "session.jsonl",
        [_turn(NOW - timedelta(days=9), 500, "req-stale")],
    )

    usage = read_usage(projects_dir=projects, now=NOW)

    assert usage.projects_counted == 1
    assert usage.week_tokens == 10
