"""Console buffer retrieval.

The ALL badge counts what the client holds, so a trimmed hydration must
return the newest entries, and after_id must return only what is newer.
"""

import os
import tempfile

from agent_relay.core.db import Database


def _db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return Database(path)


def _fill(db, n):
    return [
        db.add_console_log(agent="codex", source="agent_stdout", content=f"line {i}")
        for i in range(n)
    ]


def test_a_trimmed_read_returns_the_newest_entries_in_order():
    db = _db()
    _fill(db, 20)
    rows = db.get_console_logs(limit=5)
    assert [r["content"] for r in rows] == [f"line {i}" for i in range(15, 20)]


def test_after_id_returns_only_newer_entries():
    db = _db()
    ids = _fill(db, 10)
    rows = db.get_console_logs(after_id=ids[6])
    assert [r["content"] for r in rows] == ["line 7", "line 8", "line 9"]


def test_after_id_on_the_newest_entry_returns_nothing():
    db = _db()
    ids = _fill(db, 4)
    assert db.get_console_logs(after_id=ids[-1]) == []


def test_hydration_plus_stream_covers_the_whole_buffer_without_overlap():
    # What the dashboard does: hydrate, then follow the stream from that point.
    db = _db()
    _fill(db, 12)
    hydrated = db.get_console_logs(limit=12)
    newest = max(r["id"] for r in hydrated)
    _fill(db, 3)
    streamed = db.get_console_logs(after_id=newest)

    combined = [r["id"] for r in hydrated] + [r["id"] for r in streamed]
    assert len(combined) == len(set(combined)) == 15
