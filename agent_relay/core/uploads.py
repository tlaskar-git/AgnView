"""Phone uploads: files a paired phone sends to the hub to attach to a prompt or task.

This is a write path onto the hub's disk that a phone reaches with the pairing
key, on the LAN and over iroh. It is built to be boring and small:

* Every upload lives in its own folder, <uploads dir>/<128-bit random id>/, made
  private to the hub's user. Nothing is ever written into a project folder.
* The file name comes from the phone and is reduced to a safe base name. The
  stored file is a plain private data file. The hub never runs it, never marks
  it executable and never reads it back except to hash it.
* Bytes arrive in ordered chunks at an exact offset. A chunk is held in memory
  whole, checked, and only then appended, so a peer that drops mid-chunk leaves
  the upload exactly as it was.
* Size, total storage, free disk, concurrent uploads and creation rate are all
  capped, and unfinished uploads expire.

The routes (agent_relay/api/upload_routes.py) and the iroh upload_chunk op both
call UploadManager, so both transports apply the same checks.
"""

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Optional

logger = logging.getLogger("agnview.uploads")

_O_BINARY = getattr(os, "O_BINARY", 0)

# The largest chunk the hub accepts in one write, on either transport.
CHUNK_SIZE = 1024 * 1024

# Defaults for every limit. The configuration file can change all of them.
DEFAULT_MAX_FILE_BYTES = 2 * 1024**3
DEFAULT_MAX_TOTAL_BYTES = 10 * 1024**3
DEFAULT_MIN_FREE_BYTES = 2 * 1024**3
DEFAULT_MAX_PER_PEER = 2
DEFAULT_MAX_CONCURRENT = 4
DEFAULT_IDLE_EXPIRY_SECONDS = 3600
DEFAULT_RETENTION_DAYS = 14

# How many uploads one peer, and all peers together, can start in a minute.
CREATE_RATE_PER_PEER = 20
CREATE_RATE_TOTAL = 60
CREATE_RATE_WINDOW_SECONDS = 60.0

MAX_NAME_INPUT_CHARS = 1024
MAX_NAME_BYTES = 180
MAX_EXTENSION_CHARS = 16
FALLBACK_NAME = "upload"

STATE_RECEIVING = "receiving"
STATE_FINISHED = "finished"
STATE_REMOVED = "removed"

# Error codes. The route turns a code into an HTTP status and the iroh op puts
# the same status in its response frame.
ERROR_DISABLED = "uploads_disabled"
ERROR_NOT_FOUND = "not_found"
ERROR_BAD_REQUEST = "bad_request"
ERROR_INVALID_NAME = "invalid_name"
ERROR_TOO_LARGE = "too_large"
ERROR_QUOTA = "quota_exceeded"
ERROR_DISK_LOW = "insufficient_storage"
ERROR_TOO_MANY = "too_many_uploads"
ERROR_RATE_LIMITED = "rate_limited"
ERROR_OFFSET = "offset_mismatch"
ERROR_BEYOND_SIZE = "beyond_declared_size"
ERROR_NOT_RECEIVING = "not_receiving"
ERROR_INCOMPLETE = "incomplete"
ERROR_CHECKSUM = "checksum_mismatch"
ERROR_STORAGE = "storage_error"

_STATUS = {
    ERROR_DISABLED: 403,
    ERROR_NOT_FOUND: 404,
    ERROR_BAD_REQUEST: 400,
    ERROR_INVALID_NAME: 422,
    ERROR_TOO_LARGE: 413,
    ERROR_BEYOND_SIZE: 413,
    ERROR_QUOTA: 507,
    ERROR_DISK_LOW: 507,
    ERROR_TOO_MANY: 429,
    ERROR_RATE_LIMITED: 429,
    ERROR_OFFSET: 409,
    ERROR_NOT_RECEIVING: 409,
    ERROR_INCOMPLETE: 409,
    ERROR_CHECKSUM: 422,
    ERROR_STORAGE: 500,
}

UPLOAD_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
_SHA256_PATTERN = re.compile(r"[0-9a-fA-F]{64}")
_MIME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}")

META_NAME = ".meta.json"
PART_NAME = ".upload.part"
_MAX_META_BYTES = 8192

_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)
# Characters Windows refuses in a name, the slash look-alikes that some tools
# treat as separators, and square brackets, which would end the "[Context
# Files: ...]" line an agent prompt carries. Each becomes an underscore.
_UNSAFE_NAME_CHARS = frozenset('<>:"/\\|?*[]' + "⁄∕⧵⧸／＼")


class UploadError(Exception):
    """A refused upload request. `code` is stable text, `status` the HTTP status."""

    def __init__(self, code: str, **extra: Any):
        super().__init__(code)
        self.code = code
        self.status = _STATUS.get(code, 400)
        self.extra = extra

    def body(self) -> Dict[str, Any]:
        return {"detail": self.code, **self.extra}


def sanitise_filename(raw: Any) -> str:
    """Reduce a name from a phone to a safe base name, or raise invalid_name.

    Keeps the last path segment only (either separator), drops a drive letter,
    control and format characters, and characters Windows refuses. Strips
    leading dots and spaces and trailing dots and spaces. Refuses a Windows
    device name. Caps the length in bytes and keeps the extension.
    """
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_NAME_INPUT_CHARS:
        raise UploadError(ERROR_INVALID_NAME)
    name = unicodedata.normalize("NFC", raw)
    # Control, format (bidi overrides, zero width), surrogate, private use and
    # unassigned characters, and line and paragraph separators, all go.
    name = "".join(
        ch for ch in name
        if unicodedata.category(ch)[0] != "C" and unicodedata.category(ch) not in ("Zl", "Zp")
    )

    segments = [s for s in re.split(r"[/\\]", name) if s.strip()]
    if not segments:
        raise UploadError(ERROR_INVALID_NAME)
    name = segments[-1]
    if re.match(r"^[A-Za-z]:", name):
        name = name[2:]
    name = "".join("_" if ch in _UNSAFE_NAME_CHARS else ch for ch in name)
    name = name.lstrip(". ").rstrip(". ")
    if not name:
        raise UploadError(ERROR_INVALID_NAME)

    stem = unicodedata.normalize("NFKC", name.split(".", 1)[0]).rstrip(" ").upper()
    if stem in _RESERVED_NAMES:
        raise UploadError(ERROR_INVALID_NAME)

    name = _cap_length(name)
    if not name or name.startswith("."):
        raise UploadError(ERROR_INVALID_NAME)
    return name


def _cap_length(name: str) -> str:
    if len(name.encode("utf-8")) <= MAX_NAME_BYTES:
        return name
    stem, dot, extension = name.rpartition(".")
    if not dot or not stem or len(extension) > MAX_EXTENSION_CHARS:
        stem, extension = name, ""
    else:
        extension = "." + extension
    budget = MAX_NAME_BYTES - len(extension.encode("utf-8"))
    while stem and len(stem.encode("utf-8")) > budget:
        stem = stem[:-1]
    return (stem.rstrip(". ") + extension) if stem else FALLBACK_NAME + extension


def free_bytes(path: str) -> int:
    """Free space on the volume holding path. Tests replace this."""
    return shutil.disk_usage(path).free


@dataclass
class UploadLimits:
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES
    max_per_peer: int = DEFAULT_MAX_PER_PEER
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    idle_expiry_seconds: int = DEFAULT_IDLE_EXPIRY_SECONDS
    retention_days: int = DEFAULT_RETENTION_DAYS

    @classmethod
    def from_config(cls, config: Any) -> "UploadLimits":
        return cls(
            max_file_bytes=config.uploads_max_file_bytes,
            max_total_bytes=config.uploads_max_total_bytes,
            min_free_bytes=config.uploads_min_free_bytes,
            max_per_peer=config.uploads_max_per_peer,
            max_concurrent=config.uploads_max_concurrent,
            idle_expiry_seconds=config.uploads_idle_expiry_seconds,
            retention_days=config.uploads_retention_days,
        )


@dataclass
class _Upload:
    id: str
    name: str
    size: int
    mime: Optional[str]
    owner: str
    created_at: float
    updated_at: float
    state: str = STATE_RECEIVING
    received: int = 0
    finished_at: Optional[float] = None
    # Hash of the bytes received so far. None after a restart, when finish
    # reads the file instead.
    hasher: Any = None
    sha256: Optional[str] = None
    # Offset, length and digest of the last chunk written, so a re-send of it
    # (a lost reply) is answered as done and never written twice.
    last_chunk: Optional[tuple] = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class _RateWindow:
    """Counts events in a sliding window."""

    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self._events: Deque[float] = deque()

    def allow(self, now: float) -> bool:
        while self._events and now - self._events[0] >= self.window:
            self._events.popleft()
        if len(self._events) >= self.limit:
            return False
        self._events.append(now)
        return True


class UploadManager:
    """Stores uploads under one folder and enforces every limit."""

    def __init__(
        self,
        root: Any,
        limits: Optional[UploadLimits] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.root = Path(root)
        self.limits = limits or UploadLimits()
        self._clock = clock
        self._lock = threading.Lock()
        self._uploads: Dict[str, _Upload] = {}
        self._create_total = _RateWindow(CREATE_RATE_TOTAL, CREATE_RATE_WINDOW_SECONDS)
        self._create_by_owner: Dict[str, _RateWindow] = {}
        self._ensure_root()
        self._load()

    # ----------------- Public calls -----------------

    def create(self, name: Any, size: Any, mime: Any, owner: str) -> Dict[str, Any]:
        stored_name = sanitise_filename(name)
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise UploadError(ERROR_BAD_REQUEST)
        if size > self.limits.max_file_bytes:
            raise UploadError(ERROR_TOO_LARGE, max_size=self.limits.max_file_bytes)
        if mime is not None and (not isinstance(mime, str) or not _MIME_PATTERN.fullmatch(mime)):
            raise UploadError(ERROR_BAD_REQUEST)

        now = self._clock()
        with self._lock:
            if not self._rate_ok(owner, now):
                raise UploadError(ERROR_RATE_LIMITED)
            active = [u for u in self._uploads.values() if u.state == STATE_RECEIVING]
            if len(active) >= self.limits.max_concurrent:
                raise UploadError(ERROR_TOO_MANY)
            if sum(1 for u in active if u.owner == owner) >= self.limits.max_per_peer:
                raise UploadError(ERROR_TOO_MANY)
            # Finished files count at their size and unfinished ones at the size
            # they declared, so a set of uploads can never overshoot together.
            committed = sum(u.size for u in self._uploads.values())
            if committed + size > self.limits.max_total_bytes:
                raise UploadError(ERROR_QUOTA)
            still_to_come = sum(u.size - u.received for u in active)
            if self._free() - still_to_come - size < self.limits.min_free_bytes:
                raise UploadError(ERROR_DISK_LOW)

            upload_id = secrets.token_hex(16)
            upload = _Upload(
                id=upload_id, name=stored_name, size=size, mime=mime, owner=owner,
                created_at=now, updated_at=now, hasher=hashlib.sha256(),
            )
            self._make_folder(upload)
            self._uploads[upload_id] = upload
        logger.info("upload %s started, %d bytes", upload_id, size)
        return {
            "upload_id": upload_id,
            "chunk_size": CHUNK_SIZE,
            "max_size": self.limits.max_file_bytes,
            "name": stored_name,
            "size": size,
        }

    def status(self, upload_id: Any) -> Dict[str, Any]:
        upload = self._get(upload_id)
        return self._describe(upload)

    def write_chunk(self, upload_id: Any, offset: Any, data: bytes) -> Dict[str, Any]:
        upload = self._get(upload_id)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise UploadError(ERROR_BAD_REQUEST)
        if not data or len(data) > CHUNK_SIZE:
            raise UploadError(ERROR_BAD_REQUEST if not data else ERROR_TOO_LARGE, max_chunk=CHUNK_SIZE)

        with upload.lock:
            if upload.state != STATE_RECEIVING:
                raise UploadError(ERROR_NOT_RECEIVING)
            digest = hashlib.sha256(data).hexdigest()
            if upload.last_chunk == (offset, len(data), digest):
                # A re-send of the chunk just written: already done.
                upload.updated_at = self._clock()
                return self._describe(upload)
            if offset != upload.received:
                # A gap, or an overlap that is not the last chunk again.
                raise UploadError(ERROR_OFFSET, received=upload.received)
            if offset + len(data) > upload.size:
                raise UploadError(ERROR_BEYOND_SIZE, received=upload.received)
            if self._free() - len(data) < self.limits.min_free_bytes:
                raise UploadError(ERROR_DISK_LOW)

            path = self._folder(upload.id) / PART_NAME
            self._append(path, upload, data)
            if upload.hasher is not None:
                upload.hasher.update(data)
            upload.received += len(data)
            upload.last_chunk = (offset, len(data), digest)
            upload.updated_at = self._clock()
            return self._describe(upload)

    def finish(self, upload_id: Any, sha256: Any = None) -> Dict[str, Any]:
        upload = self._get(upload_id)
        expected = None
        if sha256 is not None:
            if not isinstance(sha256, str) or not _SHA256_PATTERN.fullmatch(sha256):
                raise UploadError(ERROR_BAD_REQUEST)
            expected = sha256.lower()

        with upload.lock:
            if upload.state == STATE_FINISHED:
                if expected is not None and upload.sha256 != expected:
                    raise UploadError(ERROR_CHECKSUM)
                return self._finished_body(upload)
            if upload.received != upload.size:
                raise UploadError(ERROR_INCOMPLETE, received=upload.received, size=upload.size)

            part = self._folder(upload.id) / PART_NAME
            digest = upload.hasher.hexdigest() if upload.hasher is not None else self._hash_file(part)
            if expected is not None and digest != expected:
                raise UploadError(ERROR_CHECKSUM)

            final = self._folder(upload.id) / upload.name
            try:
                if os.path.lexists(final):
                    raise UploadError(ERROR_STORAGE)
                os.replace(part, final)
                self._private_file(final)
            except UploadError:
                raise
            except OSError:
                raise UploadError(ERROR_STORAGE)
            upload.sha256 = digest
            upload.state = STATE_FINISHED
            upload.finished_at = self._clock()
            upload.updated_at = upload.finished_at
            upload.hasher = None
            self._write_meta(upload)
            logger.info("upload %s finished, %d bytes", upload.id, upload.size)
            return self._finished_body(upload)

    def cancel(self, upload_id: Any) -> Dict[str, Any]:
        upload = self._get(upload_id)
        with upload.lock:
            self._remove(upload)
        logger.info("upload %s removed", upload.id)
        return {"status": "cancelled", "upload_id": upload.id}

    def cleanup(self) -> int:
        """Remove unfinished uploads that went idle and finished ones past
        retention, and stray folders. Returns how many folders went."""
        now = self._clock()
        idle = self.limits.idle_expiry_seconds
        keep = self.limits.retention_days * 86400
        removed = 0
        with self._lock:
            snapshot = list(self._uploads.values())
        for upload in snapshot:
            if upload.state == STATE_RECEIVING:
                expired = now - upload.updated_at >= idle
            else:
                expired = now - (upload.finished_at or upload.updated_at) >= keep
            if expired:
                with upload.lock:
                    self._remove(upload)
                removed += 1
        removed += self._remove_strays(now, idle)
        if removed:
            logger.info("upload cleanup removed %d folders", removed)
        return removed

    def storage_used(self) -> int:
        with self._lock:
            return sum(u.size for u in self._uploads.values())

    def active_count(self, owner: Optional[str] = None) -> int:
        with self._lock:
            return sum(
                1 for u in self._uploads.values()
                if u.state == STATE_RECEIVING and (owner is None or u.owner == owner)
            )

    # ----------------- Internals -----------------

    def _get(self, upload_id: Any) -> _Upload:
        if not isinstance(upload_id, str) or not UPLOAD_ID_PATTERN.fullmatch(upload_id):
            raise UploadError(ERROR_NOT_FOUND)
        upload = self._uploads.get(upload_id)
        if upload is None:
            raise UploadError(ERROR_NOT_FOUND)
        return upload

    def _describe(self, upload: _Upload) -> Dict[str, Any]:
        return {
            "upload_id": upload.id,
            "received": upload.received,
            "size": upload.size,
            "state": upload.state,
            "name": upload.name,
        }

    def _finished_body(self, upload: _Upload) -> Dict[str, Any]:
        path = os.path.realpath(self._folder(upload.id) / upload.name)
        return {
            "upload_id": upload.id,
            "path": path,
            "name": upload.name,
            "size": upload.size,
            "sha256": upload.sha256,
        }

    def _free(self) -> int:
        try:
            return free_bytes(str(self.root))
        except OSError:
            # An unreadable volume is treated as full.
            return 0

    def _rate_ok(self, owner: str, now: float) -> bool:
        window = self._create_by_owner.setdefault(
            owner, _RateWindow(CREATE_RATE_PER_PEER, CREATE_RATE_WINDOW_SECONDS)
        )
        if len(self._create_by_owner) > 1024:
            self._create_by_owner = {owner: window}
        return window.allow(now) and self._create_total.allow(now)

    def _folder(self, upload_id: str) -> Path:
        return self.root / upload_id

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    def _make_folder(self, upload: _Upload) -> None:
        folder = self._folder(upload.id)
        try:
            os.mkdir(folder, 0o700)
            os.chmod(folder, 0o700)
            fd = os.open(folder / PART_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY, 0o600)
            os.close(fd)
            self._write_meta(upload)
        except OSError:
            shutil.rmtree(folder, ignore_errors=True)
            raise UploadError(ERROR_STORAGE)

    def _append(self, path: Path, upload: _Upload, data: bytes) -> None:
        flags = os.O_WRONLY | os.O_APPEND | _O_BINARY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError:
            raise UploadError(ERROR_STORAGE)
        try:
            size_now = os.fstat(fd).st_size
            if size_now != upload.received:
                # Something else changed the file. Tell the client where it stands.
                upload.received = size_now
                upload.hasher = None
                raise UploadError(ERROR_OFFSET, received=size_now)
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
            except OSError:
                # Put the file back to the last good length, then report.
                try:
                    os.ftruncate(fd, upload.received)
                except OSError:
                    pass
                raise UploadError(ERROR_STORAGE)
        finally:
            os.close(fd)

    @staticmethod
    def _private_file(path: Path) -> None:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(CHUNK_SIZE), b""):
                    digest.update(block)
        except OSError:
            raise UploadError(ERROR_STORAGE)
        return digest.hexdigest()

    def _write_meta(self, upload: _Upload) -> None:
        meta = {
            "id": upload.id, "name": upload.name, "size": upload.size, "mime": upload.mime,
            "owner": upload.owner, "state": upload.state, "created_at": upload.created_at,
            "finished_at": upload.finished_at, "sha256": upload.sha256,
        }
        target = self._folder(upload.id) / META_NAME
        temp = self._folder(upload.id) / (META_NAME + ".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_BINARY, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(meta).encode("utf-8"))
        os.replace(temp, target)

    def _remove(self, upload: _Upload) -> None:
        upload.state = STATE_REMOVED
        with self._lock:
            self._uploads.pop(upload.id, None)
        self._delete_folder(self._folder(upload.id))

    def _delete_folder(self, folder: Path) -> None:
        """Delete one upload folder. Refuses anything that is not a real folder
        named like an upload id directly under the uploads root."""
        try:
            if (
                folder.parent != self.root
                or not UPLOAD_ID_PATTERN.fullmatch(folder.name)
                or folder.is_symlink()
                or not folder.is_dir()
            ):
                return
            shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass

    def _remove_strays(self, now: float, idle: float) -> int:
        removed = 0
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return 0
        for entry in entries:
            if entry.name in self._uploads or not UPLOAD_ID_PATTERN.fullmatch(entry.name):
                continue
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if age >= idle:
                self._delete_folder(entry)
                removed += 1
        return removed

    def _load(self) -> None:
        """Pick up uploads left by an earlier run, so a phone can resume."""
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return
        for entry in entries:
            if not UPLOAD_ID_PATTERN.fullmatch(entry.name) or entry.is_symlink() or not entry.is_dir():
                continue
            upload = self._read_upload(entry)
            if upload is not None:
                self._uploads[upload.id] = upload

    def _read_upload(self, folder: Path) -> Optional[_Upload]:
        try:
            meta_path = folder / META_NAME
            if meta_path.stat().st_size > _MAX_META_BYTES:
                return None
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            upload_id = str(meta["id"])
            size = meta["size"]
            state = meta["state"]
            if upload_id != folder.name or isinstance(size, bool) or not isinstance(size, int) or size < 1:
                return None
            if state not in (STATE_RECEIVING, STATE_FINISHED):
                return None
            name = sanitise_filename(meta["name"])
            created = float(meta.get("created_at") or self._clock())
            finished = meta.get("finished_at")
            upload = _Upload(
                id=upload_id, name=name, size=size, mime=None, owner=str(meta.get("owner") or ""),
                created_at=created, updated_at=folder.stat().st_mtime, state=state,
            )
            if state == STATE_RECEIVING:
                part = (folder / PART_NAME).stat()
                upload.received = part.st_size
                upload.updated_at = max(upload.updated_at, part.st_mtime)
                if upload.received > size:
                    return None
            else:
                if (folder / name).stat().st_size != size:
                    return None
                upload.received = size
                upload.finished_at = float(finished) if finished else upload.updated_at
                upload.sha256 = meta.get("sha256") if isinstance(meta.get("sha256"), str) else None
            return upload
        except (OSError, ValueError, KeyError, TypeError, UploadError):
            return None


def resolve_uploads_dir(configured: str, db_path: str) -> str:
    """The folder uploads are stored in: the configured one, else an "uploads"
    folder beside the hub database."""
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "uploads")
