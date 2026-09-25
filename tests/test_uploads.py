"""Phone uploads: storage, the chunk protocol, limits, cleanup and the LAN routes.

Every path lives under a temporary directory and every key is generated per
run. The free-disk figure is replaced with a fake, so no test depends on the
machine's disk. Nothing here opens a port or starts an agent.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets

import pytest
from fastapi.testclient import TestClient

from agent_relay.api import upload_routes
from agent_relay.api.app import create_app
from agent_relay.core import dispatch_guard, iroh_api, uploads
from agent_relay.core.pairing import reset_auth_rate_limit
from agent_relay.core.uploads import UploadError, UploadLimits, UploadManager, sanitise_filename

POSIX = os.name == "posix"
GIB = 1024**3
PEER = "iroh:peer-a"


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def free(monkeypatch):
    """A fake free-disk figure. Set free.value to change what the hub sees."""
    class Free:
        value = 500 * GIB

    monkeypatch.setattr(uploads, "free_bytes", lambda path: Free.value)
    return Free


@pytest.fixture
def clock():
    return Clock()


def _manager(tmp_path, clock, **limits):
    limits.setdefault("min_free_bytes", 1)
    return UploadManager(tmp_path / "uploads", UploadLimits(**limits), clock=clock)


def _upload(manager, data, name="photo.jpg", owner=PEER, chunk=None):
    """Upload data in order and finish it. Returns the finish reply."""
    created = manager.create(name, len(data), None, owner)
    step = chunk or uploads.CHUNK_SIZE
    for offset in range(0, len(data), step):
        manager.write_chunk(created["upload_id"], offset, data[offset:offset + step])
    return manager.finish(created["upload_id"])


def _code(call, *args, **kwargs):
    with pytest.raises(UploadError) as raised:
        call(*args, **kwargs)
    return raised.value


# ----------------- File names -----------------

@pytest.mark.parametrize("raw,expected", [
    ("photo.jpg", "photo.jpg"),
    ("..\\..\\x.txt", "x.txt"),
    ("../../etc/passwd", "passwd"),
    ("/etc/passwd", "passwd"),
    ("C:\\Windows\\System32\\a.txt", "a.txt"),
    ("C:a.txt", "a.txt"),
    ("\\\\host\\share\\doc.pdf", "doc.pdf"),
    ("a/b/", "b"),
    ("x\x00y.txt", "xy.txt"),
    ("line\nbreak\r.txt", "linebreak.txt"),
    (".hidden", "hidden"),
    ("...dots.txt", "dots.txt"),
    ("trailing. . ", "trailing"),
    ("a:b*c?.txt", "b_c_.txt"),
    ("ab:c*d?.txt", "ab_c_d_.txt"),
    ("report[1].pdf", "report_1_.pdf"),
    ("\u202egnp.exe", "gnp.exe"),
    ("zero\u200bwidth.txt", "zerowidth.txt"),
    ("full\uff0fwidth.txt", "full_width.txt"),
    ("Caf\u0065\u0301.txt", "Caf\u00e9.txt"),
])
def test_names_are_reduced_to_a_safe_base_name(raw, expected):
    assert sanitise_filename(raw) == expected


@pytest.mark.parametrize("raw", [
    "CON", "con.txt", "NUL", "nul.tar.gz", "PRN.log", "AUX", "COM1", "com9.txt", "LPT1.txt",
    "CONIN$", "COM\u00b9.txt", "  Con  .txt",
    "", "   ", "..", "...", ".", "\\", "/", "///", "\x00", "..\\..\\", None, 5, [], "x" * 2000,
])
def test_names_that_cannot_be_kept_are_refused(raw):
    assert _code(sanitise_filename, raw).code == "invalid_name"


def test_a_long_name_is_capped_in_bytes_and_keeps_its_extension():
    for name in ("a" * 300 + ".jpg", "\u00e9" * 200 + ".png", "b" * 250):
        out = sanitise_filename(name)
        assert len(out.encode("utf-8")) <= uploads.MAX_NAME_BYTES
        assert not out.startswith(".")
    assert sanitise_filename("a" * 300 + ".jpg").endswith(".jpg")
    # An absurd extension is cut with the rest rather than kept whole.
    assert len(sanitise_filename("a." + "b" * 300).encode("utf-8")) <= uploads.MAX_NAME_BYTES


def test_a_sanitised_name_never_contains_a_separator_or_control_character():
    nasty = "..\\a/b\x00c\x1f:d\u2215e\u2044f\u29f5g*.txt"
    out = sanitise_filename(nasty)
    assert not re.search(r"[/\\:*\x00-\x1f]", out)
    assert os.path.basename(out) == out


# ----------------- Storage and permissions -----------------

def test_an_upload_lands_in_its_own_random_folder_under_the_root(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    reply = _upload(manager, b"hello world")
    stored = reply["path"]
    root = os.path.realpath(tmp_path / "uploads")
    assert re.fullmatch(r"[0-9a-f]{32}", reply["upload_id"])
    assert os.path.dirname(os.path.dirname(stored)) == root
    assert os.path.basename(os.path.dirname(stored)) == reply["upload_id"]
    assert open(stored, "rb").read() == b"hello world"
    assert reply["sha256"] == hashlib.sha256(b"hello world").hexdigest()
    assert reply["name"] == "photo.jpg" and reply["size"] == 11


def test_ids_are_random(tmp_path, clock, free):
    manager = _manager(tmp_path, clock, max_per_peer=50, max_concurrent=50)
    ids = {manager.create("a.txt", 1, None, PEER)["upload_id"] for _ in range(10)}
    assert len(ids) == 10


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
def test_folders_are_private_and_files_are_never_executable(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    reply = _upload(manager, b"#!/bin/sh\necho hi\n", name="run.sh")
    folder = os.path.dirname(reply["path"])
    assert os.stat(tmp_path / "uploads").st_mode & 0o077 == 0
    assert os.stat(folder).st_mode & 0o077 == 0
    assert os.stat(reply["path"]).st_mode & 0o111 == 0
    assert os.stat(reply["path"]).st_mode & 0o077 == 0


def test_a_hostile_name_stays_inside_the_upload_folder(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    reply = _upload(manager, b"x", name="..\\..\\..\\outside.txt")
    assert os.path.dirname(os.path.dirname(reply["path"])) == os.path.realpath(tmp_path / "uploads")
    assert not (tmp_path / "outside.txt").exists()
    assert not (tmp_path.parent / "outside.txt").exists()


def test_a_finished_upload_is_accepted_by_the_dispatch_file_rule(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    reply = _upload(manager, b"data")
    assert dispatch_guard.check_files([reply["path"]], None, [], str(manager.root)) == [reply["path"]]


def test_the_bookkeeping_and_partial_files_are_not_attachable(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    created = manager.create("a.txt", 4, None, PEER)
    folder = manager.root / created["upload_id"]
    manager.write_chunk(created["upload_id"], 0, b"ab")
    for hidden in (uploads.PART_NAME, uploads.META_NAME):
        with pytest.raises(dispatch_guard.DispatchRefused):
            dispatch_guard.check_files([str(folder / hidden)], None, [], str(manager.root))


# ----------------- The chunk protocol -----------------

def test_chunks_in_order_then_finish(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    data = secrets.token_bytes(3 * uploads.CHUNK_SIZE + 17)
    reply = _upload(manager, data)
    assert open(reply["path"], "rb").read() == data
    assert manager.status(reply["upload_id"])["state"] == "finished"


def test_status_lets_a_client_resume(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    data = secrets.token_bytes(1000)
    created = manager.create("a.bin", len(data), None, PEER)
    upload_id = created["upload_id"]
    manager.write_chunk(upload_id, 0, data[:400])
    status = manager.status(upload_id)
    assert (status["received"], status["size"], status["state"]) == (400, 1000, "receiving")
    manager.write_chunk(upload_id, status["received"], data[400:])
    assert open(manager.finish(upload_id)["path"], "rb").read() == data


def test_a_resume_works_after_a_hub_restart(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    data = secrets.token_bytes(900)
    upload_id = manager.create("a.bin", len(data), None, PEER)["upload_id"]
    manager.write_chunk(upload_id, 0, data[:500])

    again = _manager(tmp_path, clock)
    assert again.status(upload_id)["received"] == 500
    again.write_chunk(upload_id, 500, data[500:])
    reply = again.finish(upload_id, sha256=hashlib.sha256(data).hexdigest())
    assert open(reply["path"], "rb").read() == data


def test_a_finished_upload_survives_a_restart(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    reply = _upload(manager, b"keep me")
    again = _manager(tmp_path, clock)
    assert again.status(reply["upload_id"])["state"] == "finished"
    assert again.finish(reply["upload_id"])["path"] == reply["path"]


def test_a_resend_of_the_last_chunk_is_stored_once(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    created = manager.create("a.bin", 10, None, PEER)
    upload_id = created["upload_id"]
    first = manager.write_chunk(upload_id, 0, b"01234")
    again = manager.write_chunk(upload_id, 0, b"01234")
    assert first["received"] == again["received"] == 5
    assert (manager.root / upload_id / uploads.PART_NAME).stat().st_size == 5


def test_a_resend_at_the_same_offset_with_other_bytes_is_refused(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    upload_id = manager.create("a.bin", 10, None, PEER)["upload_id"]
    manager.write_chunk(upload_id, 0, b"01234")
    error = _code(manager.write_chunk, upload_id, 0, b"XXXXX")
    assert (error.code, error.status, error.extra["received"]) == ("offset_mismatch", 409, 5)
    assert (manager.root / upload_id / uploads.PART_NAME).read_bytes() == b"01234"


def test_a_gap_is_refused_and_reports_where_the_upload_stands(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    upload_id = manager.create("a.bin", 10, None, PEER)["upload_id"]
    manager.write_chunk(upload_id, 0, b"012")
    error = _code(manager.write_chunk, upload_id, 5, b"56")
    assert (error.code, error.extra["received"]) == ("offset_mismatch", 3)


def test_an_overlap_is_refused(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    upload_id = manager.create("a.bin", 10, None, PEER)["upload_id"]
    manager.write_chunk(upload_id, 0, b"0123")
    manager.write_chunk(upload_id, 4, b"45")
    # Offset 2 overlaps bytes already held, and it is not the last chunk again.
    assert _code(manager.write_chunk, upload_id, 2, b"2345").code == "offset_mismatch"
    assert (manager.root / upload_id / uploads.PART_NAME).read_bytes() == b"012345"


def test_bytes_beyond_the_declared_size_are_refused(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    upload_id = manager.create("a.bin", 6, None, PEER)["upload_id"]
    manager.write_chunk(upload_id, 0, b"0123")
    error = _code(manager.write_chunk, upload_id, 4, b"456")
    assert (error.code, error.status) == ("beyond_declared_size", 413)
    assert manager.status(upload_id)["received"] == 4


def test_a_chunk_over_the_chunk_size_and_an_empty_one_are_refused(tmp_path, clock, free):
    manager = _manager(tmp_path, clock, max_file_bytes=4 * uploads.CHUNK_SIZE)
    upload_id = manager.create("a.bin", 3 * uploads.CHUNK_SIZE, None, PEER)["upload_id"]
    assert _code(manager.write_chunk, upload_id, 0, b"x" * (uploads.CHUNK_SIZE + 1)).status == 413
    assert _code(manager.write_chunk, upload_id, 0, b"").status == 400
    assert manager.status(upload_id)["received"] == 0


@pytest.mark.parametrize("offset", [-1, True, "0", 1.5, None])
def test_a_bad_offset_is_refused(tmp_path, clock, free, offset):
    manager = _manager(tmp_path, clock)
    upload_id = manager.create("a.bin", 4, None, PEER)["upload_id"]
    assert _code(manager.write_chunk, upload_id, offset, b"x").status == 400


def test_a_file_over_the_maximum_is_refused_at_the_start(tmp_path, clock, free):
    manager = _manager(tmp_path, clock, max_file_bytes=100)
    error = _code(manager.create, "a.bin", 101, None, PEER)
    assert (error.code, error.status, error.extra["max_size"]) == ("too_large", 413, 100)
    assert manager.create("a.bin", 100, None, PEER)["max_size"] == 100
    assert list(manager.root.iterdir()) and manager.active_count() == 1


@pytest.mark.parametrize("size", [0, -5, True, "10", 1.5, None])
def test_a_bad_size_is_refused(tmp_path, clock, free, size):
    assert _code(_manager(tmp_path, clock).create, "a.bin", size, None, PEER).status == 400


@pytest.mark.parametrize("mime", ["not a mime", 5, "a/b\nc", "x" * 300 + "/y"])
def test_a_bad_mime_type_is_refused(tmp_path, clock, free, mime):
    assert _code(_manager(tmp_path, clock).create, "a.bin", 1, mime, PEER).status == 400
    assert _manager(tmp_path, clock).create("a.bin", 1, "image/jpeg", PEER)


def test_finish_before_all_bytes_have_arrived_is_refused(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    upload_id = manager.create("a.bin", 10, None, PEER)["upload_id"]
    manager.write_chunk(upload_id, 0, b"0123")
    error = _code(manager.finish, upload_id)
    assert (error.code, error.status, error.extra["received"]) == ("incomplete", 409, 4)
    assert manager.status(upload_id)["state"] == "receiving"


def test_a_wrong_sha256_does_not_finish_the_upload(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    upload_id = manager.create("a.bin", 4, None, PEER)["upload_id"]
    manager.write_chunk(upload_id, 0, b"abcd")
    error = _code(manager.finish, upload_id, sha256="0" * 64)
    assert (error.code, error.status) == ("checksum_mismatch", 422)
    assert manager.status(upload_id)["state"] == "receiving"
    assert not (manager.root / upload_id / "a.bin").exists()
    # The bytes are discarded and the upload restarts at offset 0.
    assert manager.status(upload_id)["received"] == 0
    manager.write_chunk(upload_id, 0, b"abcd")
    good = hashlib.sha256(b"abcd").hexdigest().upper()
    assert manager.finish(upload_id, sha256=good)["sha256"] == good.lower()


@pytest.mark.parametrize("digest", ["abc", "z" * 64, 5, "0" * 65])
def test_a_malformed_sha256_is_refused(tmp_path, clock, free, digest):
    manager = _manager(tmp_path, clock)
    upload_id = manager.create("a.bin", 1, None, PEER)["upload_id"]
    manager.write_chunk(upload_id, 0, b"a")
    assert _code(manager.finish, upload_id, sha256=digest).status == 400


def test_finish_twice_is_answered_the_same_and_writes_are_then_refused(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    reply = _upload(manager, b"abcd")
    assert manager.finish(reply["upload_id"]) == reply
    assert _code(manager.write_chunk, reply["upload_id"], 0, b"abcd").code == "not_receiving"


def test_cancel_deletes_everything_stored(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    upload_id = manager.create("a.bin", 4, None, PEER)["upload_id"]
    manager.write_chunk(upload_id, 0, b"ab")
    assert manager.cancel(upload_id)["status"] == "cancelled"
    assert not (manager.root / upload_id).exists()
    assert _code(manager.status, upload_id).code == "not_found"
    assert _code(manager.cancel, upload_id).status == 404
    assert manager.active_count() == 0


def test_a_finished_upload_can_be_deleted_too(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    reply = _upload(manager, b"abcd")
    manager.cancel(reply["upload_id"])
    assert not os.path.exists(reply["path"])


@pytest.mark.parametrize("bad_id", ["", "../x", "..", "a" * 31, "A" * 32, "g" * 32, "a" * 33, "a" * 32 + "\n",
                                    "/etc/passwd", None, 5])
def test_an_id_that_is_not_one_the_hub_minted_is_not_found(tmp_path, clock, free, bad_id):
    manager = _manager(tmp_path, clock)
    for call in (manager.status, manager.finish, manager.cancel):
        assert _code(call, bad_id).status == 404
    assert _code(manager.write_chunk, bad_id, 0, b"x").status == 404


def test_a_failed_write_leaves_the_upload_where_it_was(tmp_path, clock, free, monkeypatch):
    manager = _manager(tmp_path, clock)
    upload_id = manager.create("a.bin", 10, None, PEER)["upload_id"]
    manager.write_chunk(upload_id, 0, b"0123")
    real_write = os.write

    def half_then_fail(fd, view):
        real_write(fd, bytes(view[: len(view) // 2]))
        raise OSError("disk went away")

    monkeypatch.setattr(uploads.os, "write", half_then_fail)
    assert _code(manager.write_chunk, upload_id, 4, b"456789").code == "storage_error"
    monkeypatch.undo()
    assert (manager.root / upload_id / uploads.PART_NAME).read_bytes() == b"0123"
    assert manager.status(upload_id)["received"] == 4
    manager.write_chunk(upload_id, 4, b"456789")
    assert open(manager.finish(upload_id)["path"], "rb").read() == b"0123456789"


def test_a_part_file_changed_from_outside_is_reported_not_extended(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    upload_id = manager.create("a.bin", 10, None, PEER)["upload_id"]
    manager.write_chunk(upload_id, 0, b"0123")
    (manager.root / upload_id / uploads.PART_NAME).write_bytes(b"01")
    error = _code(manager.write_chunk, upload_id, 4, b"4567")
    assert (error.code, error.extra["received"]) == ("offset_mismatch", 2)
    assert manager.status(upload_id)["received"] == 2


@pytest.mark.skipif(not POSIX, reason="needs symlinks and O_NOFOLLOW")
def test_a_part_file_swapped_for_a_symlink_is_never_written_through(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    upload_id = manager.create("a.bin", 10, None, PEER)["upload_id"]
    target = tmp_path / "victim.txt"
    target.write_bytes(b"safe")
    part = manager.root / upload_id / uploads.PART_NAME
    part.unlink()
    part.symlink_to(target)
    assert _code(manager.write_chunk, upload_id, 0, b"boom").code == "storage_error"
    assert target.read_bytes() == b"safe"


# ----------------- Limits -----------------

def test_the_total_storage_quota_counts_finished_and_declared_bytes(tmp_path, clock, free):
    manager = _manager(tmp_path, clock, max_total_bytes=10_000, max_per_peer=10, max_concurrent=10)
    _upload(manager, b"x" * 5000)
    error = _code(manager.create, "b.bin", 6000, None, PEER)
    assert (error.code, error.status) == ("quota_exceeded", 507)
    # An unfinished upload holds its whole declared size.
    manager.create("c.bin", 4100, None, PEER)
    assert _code(manager.create, "d.bin", 1, None, PEER).code == "quota_exceeded"
    assert manager.storage_used() == 9100


def test_deleting_an_upload_frees_its_quota(tmp_path, clock, free):
    manager = _manager(tmp_path, clock, max_total_bytes=10_000)
    reply = _upload(manager, b"x" * 10_000)
    assert _code(manager.create, "b.bin", 1, None, PEER).code == "quota_exceeded"
    manager.cancel(reply["upload_id"])
    assert manager.create("b.bin", 1, None, PEER)


def test_the_free_disk_floor_refuses_a_start_that_would_cross_it(tmp_path, clock, free):
    manager = _manager(tmp_path, clock, min_free_bytes=2 * GIB)
    free.value = 2 * GIB + 999
    error = _code(manager.create, "a.bin", 1000, None, PEER)
    assert (error.code, error.status) == ("insufficient_storage", 507)
    assert manager.create("a.bin", 999, None, PEER)


def test_the_floor_counts_what_other_uploads_still_have_to_write(tmp_path, clock, free):
    manager = _manager(tmp_path, clock, min_free_bytes=1000)
    free.value = 1000 + 500
    manager.create("a.bin", 300, None, PEER)
    assert _code(manager.create, "b.bin", 300, None, "iroh:other").code == "insufficient_storage"


def test_the_floor_is_checked_again_on_every_chunk(tmp_path, clock, free):
    manager = _manager(tmp_path, clock, min_free_bytes=1000)
    free.value = 5000
    upload_id = manager.create("a.bin", 100, None, PEER)["upload_id"]
    free.value = 1050
    assert _code(manager.write_chunk, upload_id, 0, b"x" * 100).code == "insufficient_storage"
    assert manager.status(upload_id)["received"] == 0
    free.value = 5000
    manager.write_chunk(upload_id, 0, b"x" * 100)


def test_an_unreadable_disk_counts_as_full(tmp_path, clock, monkeypatch):
    def broken(path):
        raise OSError("no such volume")

    monkeypatch.setattr(uploads, "free_bytes", broken)
    manager = _manager(tmp_path, clock)
    assert _code(manager.create, "a.bin", 1, None, PEER).code == "insufficient_storage"


def test_each_peer_may_run_two_uploads_and_the_hub_four(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    a1 = manager.create("a.bin", 1, None, "iroh:a")["upload_id"]
    manager.create("a.bin", 1, None, "iroh:a")
    error = _code(manager.create, "a.bin", 1, None, "iroh:a")
    assert (error.code, error.status) == ("too_many_uploads", 429)
    manager.create("a.bin", 1, None, "iroh:b")
    manager.create("a.bin", 1, None, "iroh:b")
    # Four are running: a fifth peer is turned away although it holds none.
    assert _code(manager.create, "a.bin", 1, None, "iroh:c").code == "too_many_uploads"
    manager.cancel(a1)
    assert manager.create("a.bin", 1, None, "iroh:c")


def test_a_finished_upload_frees_its_slot(tmp_path, clock, free):
    manager = _manager(tmp_path, clock, max_per_peer=1)
    _upload(manager, b"x")
    assert manager.create("a.bin", 1, None, PEER)


def test_creating_uploads_is_rate_limited_per_peer_and_for_the_hub(tmp_path, clock, free):
    manager = _manager(tmp_path, clock, max_per_peer=1000, max_concurrent=1000)
    for _ in range(uploads.CREATE_RATE_PER_PEER):
        manager.create("a.bin", 1, None, "iroh:a")
    error = _code(manager.create, "a.bin", 1, None, "iroh:a")
    assert (error.code, error.status) == ("rate_limited", 429)
    # Another peer is not held back by it.
    assert manager.create("a.bin", 1, None, "iroh:b")
    clock.now += uploads.CREATE_RATE_WINDOW_SECONDS + 1
    assert manager.create("a.bin", 1, None, "iroh:a")


def test_the_hub_wide_creation_rate_holds_against_many_peers(tmp_path, clock, free):
    manager = _manager(tmp_path, clock, max_per_peer=1000, max_concurrent=1000)
    for n in range(uploads.CREATE_RATE_TOTAL):
        manager.create("a.bin", 1, None, f"iroh:peer-{n}")
    assert _code(manager.create, "a.bin", 1, None, "iroh:one-more").code == "rate_limited"


# ----------------- Expiry and cleanup -----------------

def test_an_idle_unfinished_upload_expires_after_an_hour(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    upload_id = manager.create("a.bin", 4, None, PEER)["upload_id"]
    manager.write_chunk(upload_id, 0, b"ab")
    clock.now += 3599
    assert manager.cleanup() == 0
    # A chunk is activity: the hour starts again from it.
    manager.write_chunk(upload_id, 2, b"cd")
    clock.now += 3599
    assert manager.cleanup() == 0
    clock.now += 2
    assert manager.cleanup() == 1
    assert not (manager.root / upload_id).exists()
    assert _code(manager.status, upload_id).code == "not_found"
    assert manager.active_count() == 0


def test_a_finished_upload_is_kept_for_the_retention_period(tmp_path, clock, free):
    manager = _manager(tmp_path, clock, retention_days=14)
    reply = _upload(manager, b"abcd")
    clock.now += 14 * 86400 - 1
    assert manager.cleanup() == 0 and os.path.exists(reply["path"])
    clock.now += 2
    assert manager.cleanup() == 1
    assert not os.path.exists(reply["path"])


def test_the_defaults_are_the_documented_ones():
    limits = UploadLimits()
    assert limits.max_file_bytes == 2 * GIB
    assert limits.max_total_bytes == 10 * GIB
    assert limits.min_free_bytes == 2 * GIB
    assert (limits.max_per_peer, limits.max_concurrent) == (2, 4)
    assert limits.idle_expiry_seconds == 3600
    assert limits.retention_days == 14
    assert uploads.CHUNK_SIZE == 1024 * 1024


def test_cleanup_only_ever_deletes_upload_folders(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    (manager.root / "keep-me").mkdir()
    (manager.root / "keep-me" / "notes.txt").write_text("x")
    (manager.root / "stray.txt").write_text("x")
    stray = manager.root / ("f" * 32)
    stray.mkdir()
    (stray / "leftover").write_text("x")
    clock.now = os.stat(stray).st_mtime + 2 * 3600
    assert manager.cleanup() == 1
    assert not stray.exists()
    assert (manager.root / "keep-me" / "notes.txt").exists()
    assert (manager.root / "stray.txt").exists()


@pytest.mark.skipif(not POSIX, reason="needs symlinks")
def test_cleanup_never_follows_a_symlink_named_like_an_upload(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("x")
    (manager.root / ("e" * 32)).symlink_to(outside, target_is_directory=True)
    clock.now += 10 * 86400
    manager.cleanup()
    assert (outside / "precious.txt").exists()


def test_a_folder_with_bad_bookkeeping_is_ignored_on_load(tmp_path, clock, free):
    manager = _manager(tmp_path, clock)
    upload_id = manager.create("a.bin", 4, None, PEER)["upload_id"]
    (manager.root / upload_id / uploads.META_NAME).write_text(json.dumps({"id": upload_id, "size": "big"}))
    assert _code(_manager(tmp_path, clock).status, upload_id).code == "not_found"


# ----------------- Logging -----------------

def test_the_log_carries_ids_and_sizes_but_never_names_paths_or_bytes(tmp_path, clock, free, caplog):
    manager = _manager(tmp_path, clock)
    secret_name = "Quarterly-Numbers-" + secrets.token_hex(4) + ".xlsx"
    secret_bytes = secrets.token_hex(16).encode()
    with caplog.at_level(logging.DEBUG):
        reply = _upload(manager, secret_bytes, name=secret_name)
        manager.cancel(reply["upload_id"])
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert reply["upload_id"] in text
    assert secret_name not in text
    assert secret_name.split(".")[0] not in text
    assert secret_bytes.decode() not in text
    assert str(tmp_path) not in text


# ----------------- The LAN routes -----------------

def _hub(tmp_path, token=None, monkeypatch=None):
    app = create_app(db_path=str(tmp_path / "hub.db"), auth_token=token)
    return app


def _client(app, host="203.0.113.7"):
    return TestClient(app, client=(host, 41000))


def _headers(token):
    return {"X-AgnView-Token": token}


@pytest.fixture(autouse=True)
def _clean_limiter():
    keys = ("203.0.113.7", "203.0.113.8", "127.0.0.1", "testclient")
    for key in keys:
        reset_auth_rate_limit(key)
    yield
    for key in keys:
        reset_auth_rate_limit(key)


def test_the_whole_upload_flow_over_http(tmp_path, free):
    token = secrets.token_urlsafe(32)
    app = _hub(tmp_path, token)
    client = _client(app)
    headers = _headers(token)
    data = secrets.token_bytes(2 * uploads.CHUNK_SIZE + 5)

    created = client.post("/api/uploads", json={"name": "..\\..\\pic.jpg", "size": len(data), "mime": "image/jpeg"},
                          headers=headers)
    assert created.status_code == 201
    info = created.json()
    assert (info["chunk_size"], info["name"]) == (uploads.CHUNK_SIZE, "pic.jpg")
    assert info["max_size"] == 2 * GIB
    upload_id = info["upload_id"]

    first = client.put(f"/api/uploads/{upload_id}", params={"offset": 0}, content=data[:uploads.CHUNK_SIZE],
                       headers=headers)
    assert first.status_code == 200 and first.json()["received"] == uploads.CHUNK_SIZE
    status = client.get(f"/api/uploads/{upload_id}", headers=headers).json()
    assert (status["received"], status["state"]) == (uploads.CHUNK_SIZE, "receiving")
    for offset in (uploads.CHUNK_SIZE, 2 * uploads.CHUNK_SIZE):
        res = client.put(f"/api/uploads/{upload_id}", params={"offset": offset},
                         content=data[offset:offset + uploads.CHUNK_SIZE], headers=headers)
        assert res.status_code == 200

    done = client.post(f"/api/uploads/{upload_id}/finish", json={"sha256": hashlib.sha256(data).hexdigest()},
                       headers=headers)
    assert done.status_code == 200
    body = done.json()
    assert open(body["path"], "rb").read() == data
    assert dispatch_guard.check_files([body["path"]], None, [], app.state.uploads_dir) == [body["path"]]

    assert client.delete(f"/api/uploads/{upload_id}", headers=headers).json()["status"] == "cancelled"
    assert client.get(f"/api/uploads/{upload_id}", headers=headers).status_code == 404


def test_finish_takes_no_body(tmp_path, free):
    app = _hub(tmp_path)
    client = _client(app)
    upload_id = client.post("/api/uploads", json={"name": "a.txt", "size": 2}).json()["upload_id"]
    client.put(f"/api/uploads/{upload_id}", params={"offset": 0}, content=b"ab")
    assert client.post(f"/api/uploads/{upload_id}/finish").status_code == 200


def test_http_errors_carry_the_code_and_where_the_upload_stands(tmp_path, free):
    client = _client(_hub(tmp_path))
    upload_id = client.post("/api/uploads", json={"name": "a.txt", "size": 4}).json()["upload_id"]
    client.put(f"/api/uploads/{upload_id}", params={"offset": 0}, content=b"ab")
    gap = client.put(f"/api/uploads/{upload_id}", params={"offset": 3}, content=b"d")
    assert (gap.status_code, gap.json()) == (409, {"detail": "offset_mismatch", "received": 2})
    big = client.put(f"/api/uploads/{upload_id}", params={"offset": 2}, content=b"cde")
    assert (big.status_code, big.json()["detail"]) == (413, "beyond_declared_size")
    early = client.post(f"/api/uploads/{upload_id}/finish")
    assert (early.status_code, early.json()["detail"]) == (409, "incomplete")
    assert client.put("/api/uploads/" + "0" * 32, params={"offset": 0}, content=b"x").status_code == 404
    assert client.put(f"/api/uploads/{upload_id}", content=b"x").status_code == 422
    assert client.put(f"/api/uploads/{upload_id}", params={"offset": -1}, content=b"x").status_code == 422


def test_a_chunk_over_one_mebibyte_is_refused_before_it_is_read(tmp_path, free):
    client = _client(_hub(tmp_path))
    upload_id = client.post("/api/uploads", json={"name": "a.bin", "size": 3 * uploads.CHUNK_SIZE}).json()["upload_id"]
    res = client.put(f"/api/uploads/{upload_id}", params={"offset": 0}, content=b"x" * (uploads.CHUNK_SIZE + 1))
    assert (res.status_code, res.json()["detail"]) == (413, "too_large")
    assert client.get(f"/api/uploads/{upload_id}").json()["received"] == 0


@pytest.mark.parametrize("body", [
    {"size": 5},
    {"name": "a", "size": "5"},
    {"name": "a", "size": 5.5},
    {"name": "a", "size": True},
    {"name": 5, "size": 5},
    ["not", "an", "object"],
])
def test_a_malformed_create_body_is_refused(tmp_path, free, body):
    res = _client(_hub(tmp_path)).post("/api/uploads", json=body)
    assert res.status_code == 422


def test_a_reserved_name_is_refused_over_http(tmp_path, free):
    res = _client(_hub(tmp_path)).post("/api/uploads", json={"name": "NUL.txt", "size": 1})
    assert (res.status_code, res.json()["detail"]) == (422, "invalid_name")


def test_too_large_and_quota_errors_over_http(tmp_path, free):
    client = _client(_hub(tmp_path))
    res = client.post("/api/uploads", json={"name": "a.bin", "size": 2 * GIB + 1})
    assert (res.status_code, res.json()["detail"]) == (413, "too_large")


@pytest.mark.parametrize("method,path,kwargs", [
    ("post", "/api/uploads", {"json": {"name": "a", "size": 1}}),
    ("get", "/api/uploads/" + "a" * 32, {}),
    ("put", "/api/uploads/" + "a" * 32 + "?offset=0", {"content": b"x"}),
    ("post", "/api/uploads/" + "a" * 32 + "/finish", {}),
    ("delete", "/api/uploads/" + "a" * 32, {}),
])
def test_every_upload_route_needs_the_pairing_key(tmp_path, free, method, path, kwargs):
    token = secrets.token_urlsafe(32)
    client = _client(_hub(tmp_path, token))
    assert getattr(client, method)(path, **kwargs).status_code == 401
    wrong = getattr(client, method)(path, headers=_headers(secrets.token_urlsafe(32)), **kwargs)
    assert wrong.status_code == 401
    # With the key the request reaches the route: 404 for an id nobody minted.
    ok = getattr(client, method)(path, headers=_headers(token), **kwargs)
    assert ok.status_code != 401


def test_a_wrong_key_writes_nothing(tmp_path, free):
    token = secrets.token_urlsafe(32)
    app = _hub(tmp_path, token)
    client = _client(app)
    client.post("/api/uploads", json={"name": "a.bin", "size": 1})
    assert list(app.state.uploads.root.iterdir()) == []


def test_a_valid_key_is_not_blocked_by_another_clients_failures(tmp_path, free):
    token = secrets.token_urlsafe(32)
    app = _hub(tmp_path, token)
    attacker = _client(app, host="203.0.113.7")
    for _ in range(30):
        attacker.get("/api/uploads/" + "a" * 32, headers=_headers(secrets.token_urlsafe(32)))
    assert attacker.get("/api/uploads/" + "a" * 32, headers=_headers(secrets.token_urlsafe(32))).status_code == 429
    owner = _client(app, host="203.0.113.8")
    res = owner.post("/api/uploads", json={"name": "a.bin", "size": 1}, headers=_headers(token))
    assert res.status_code == 201


def test_the_switch_turns_every_route_off(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("uploads_enabled: false\n", encoding="utf-8")
    monkeypatch.setenv("AGNVIEW_CONFIG", str(config))
    app = _hub(tmp_path)
    assert app.state.uploads is None and app.state.uploads_dir is None
    assert app.state.iroh.capabilities == ["console", "api"]
    client = _client(app)
    res = client.post("/api/uploads", json={"name": "a.bin", "size": 1})
    assert (res.status_code, res.json()["detail"]) == (403, "uploads_disabled")
    assert client.get("/api/uploads/" + "a" * 32).status_code == 403
    assert not (tmp_path / "uploads").exists()


def test_the_environment_switch_turns_uploads_off(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNVIEW_UPLOADS", "0")
    app = _hub(tmp_path)
    assert app.state.uploads is None
    assert _client(app).post("/api/uploads", json={"name": "a.bin", "size": 1}).status_code == 403


def test_a_config_the_hub_cannot_read_turns_uploads_off(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("uploads_max_file_bytes: -5\n", encoding="utf-8")
    monkeypatch.setenv("AGNVIEW_CONFIG", str(config))
    assert _hub(tmp_path).state.uploads is None


def test_the_limits_come_from_the_configuration(tmp_path, monkeypatch, free):
    config = tmp_path / "config.yaml"
    config.write_text("uploads_max_file_bytes: 1000\nuploads_max_per_peer: 1\nuploads_min_free_bytes: 5\n",
                      encoding="utf-8")
    monkeypatch.setenv("AGNVIEW_CONFIG", str(config))
    client = _client(_hub(tmp_path))
    assert client.post("/api/uploads", json={"name": "a", "size": 1001}).status_code == 413
    assert client.post("/api/uploads", json={"name": "a", "size": 10}).json()["max_size"] == 1000
    assert client.post("/api/uploads", json={"name": "b", "size": 10}).status_code == 429


def test_the_uploads_folder_can_be_moved_by_configuration(tmp_path, monkeypatch, free):
    target = tmp_path / "elsewhere"
    config = tmp_path / "config.yaml"
    config.write_text(f"uploads_dir: '{target.as_posix()}'\n", encoding="utf-8")
    monkeypatch.setenv("AGNVIEW_CONFIG", str(config))
    app = _hub(tmp_path)
    assert app.state.uploads_dir == str(target)
    assert target.is_dir()


def test_the_uploads_folder_is_beside_the_database_by_default(tmp_path):
    app = _hub(tmp_path)
    assert app.state.uploads_dir == str(tmp_path / "uploads")


def test_the_dashboard_and_other_routes_are_untouched(tmp_path):
    client = _client(_hub(tmp_path), host="127.0.0.1")
    assert client.get("/").status_code == 200
    assert client.get("/api/jobs").status_code == 200
    assert client.get("/api/mobile/status").status_code == 200


def test_a_chunk_that_never_arrives_times_out_and_stores_nothing(tmp_path, free, monkeypatch):
    monkeypatch.setattr(upload_routes, "CHUNK_BODY_TIMEOUT_SECONDS", 0.05)
    app = _hub(tmp_path)
    upload_id = _client(app).post("/api/uploads", json={"name": "a.bin", "size": 10}).json()["upload_id"]
    sent = []

    async def receive():
        await asyncio.sleep(3600)

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "PUT",
        "scheme": "http", "path": f"/api/uploads/{upload_id}", "raw_path": b"", "root_path": "",
        "query_string": b"offset=0", "headers": [(b"host", b"testserver")], "client": ("203.0.113.7", 1),
        "server": ("testserver", 80),
    }
    asyncio.run(app(scope, receive, send))
    assert sent[0]["status"] == 408
    assert app.state.uploads.status(upload_id)["received"] == 0


def test_the_owner_of_an_upload_is_the_lan_address_or_the_iroh_peer(tmp_path, free):
    app = _hub(tmp_path)
    lan = _client(app).post("/api/uploads", json={"name": "a.bin", "size": 1}).json()["upload_id"]
    assert app.state.uploads._uploads[lan].owner == "lan:203.0.113.7"

    request = iroh_api.ApiRequest(
        method="POST", path="/api/uploads", query="", template="/api/uploads",
        body=json.dumps({"name": "a.bin", "size": 1}).encode(),
    )
    app.state.iroh_uploads_enabled = True
    frame = asyncio.run(iroh_api.forward_to_app(app, request, token=None, peer="iroh:peer-a"))
    assert frame["status"] == 201
    assert app.state.uploads._uploads[frame["body"]["upload_id"]].owner == "iroh:peer-a"


def test_a_client_cannot_name_its_own_peer(tmp_path, free):
    app = _hub(tmp_path)
    client = _client(app)
    res = client.post("/api/uploads", json={"name": "a.bin", "size": 1},
                      headers={"X-AgnView-Peer": "iroh:spoof", "agnview_peer": "iroh:spoof"})
    owner = app.state.uploads._uploads[res.json()["upload_id"]].owner
    assert owner == "lan:203.0.113.7"


def test_uploads_over_iroh_are_refused_when_that_switch_is_off(tmp_path, free):
    app = _hub(tmp_path)
    app.state.iroh_uploads_enabled = False
    request = iroh_api.ApiRequest(
        method="POST", path="/api/uploads", query="", template="/api/uploads",
        body=json.dumps({"name": "a.bin", "size": 1}).encode(),
    )
    frame = asyncio.run(iroh_api.forward_to_app(app, request, token=None, peer="iroh:peer-a"))
    assert (frame["status"], frame["body"]["detail"]) == (403, "uploads_disabled")
    assert list(app.state.uploads.root.iterdir()) == []
    # The LAN is not affected.
    assert _client(app).post("/api/uploads", json={"name": "a.bin", "size": 1}).status_code == 201


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
def test_a_folder_the_operator_chose_keeps_its_permissions(tmp_path, clock, free):
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    os.chmod(chosen, 0o755)
    UploadManager(chosen, UploadLimits(min_free_bytes=1), clock=clock)
    assert os.stat(chosen).st_mode & 0o777 == 0o755
