"""Pairing and QR code generation for mobile clients (iOS / Android)."""

import os
import secrets
import socket
import io
import time
import base64
import urllib.parse
from pathlib import Path
from typing import Dict, Any, Optional, Tuple
import qrcode
import qrcode.image.svg

from .certs import get_cert_fingerprint
from .network import get_bind_mode, get_local_ip, is_rfc1918_address


TOKEN_DIR = Path.home() / ".agnview"
TOKEN_FILE = TOKEN_DIR / "pairing_token"
PAIR_ID_FILE = TOKEN_DIR / "pair_id"

# Highest QR payload version this hub emits. v2 adds one optional field, iroh,
# carrying the hub's iroh node ticket. Everything v1 defines is untouched.
PAIRING_PAYLOAD_VERSION = 2

# Every payload version a client may present. A v1 payload from a device paired
# before iroh existed must keep working exactly as it did.
SUPPORTED_PAIRING_PAYLOAD_VERSIONS = (1, 2)

PAIRING_URI_SCHEME = "agnview"

# Rate limiting for pairing token verification: max 10 failed attempts per IP per 60s
_FAILED_ATTEMPTS: Dict[str, list] = {}
RATE_LIMIT_MAX_ATTEMPTS = 10
RATE_LIMIT_WINDOW_SECONDS = 60.0


def check_auth_rate_limit(client_ip: str) -> bool:
    """Return True if the client IP is within rate limits, False if blocked."""
    now = time.time()
    attempts = _FAILED_ATTEMPTS.get(client_ip, [])
    # Evict attempts older than the rate limit window
    attempts = [t for t in attempts if now - t < RATE_LIMIT_WINDOW_SECONDS]
    _FAILED_ATTEMPTS[client_ip] = attempts
    return len(attempts) < RATE_LIMIT_MAX_ATTEMPTS


def record_failed_auth(client_ip: str):
    """Record a failed authentication attempt for rate limiting."""
    now = time.time()
    if client_ip not in _FAILED_ATTEMPTS:
        _FAILED_ATTEMPTS[client_ip] = []
    _FAILED_ATTEMPTS[client_ip].append(now)


def reset_auth_rate_limit(client_ip: str):
    """Clear failed authentication history on successful auth."""
    _FAILED_ATTEMPTS.pop(client_ip, None)


def _set_posix_permissions_0600(file_path: Path):
    """Set 0600 permissions on POSIX systems."""
    try:
        os.chmod(file_path, 0o600)
    except Exception:
        pass


def get_or_create_pair_id() -> str:
    """Retrieve existing 16-byte base64url pair ID or generate a new one."""
    if PAIR_ID_FILE.exists():
        try:
            stored = PAIR_ID_FILE.read_text(encoding="utf-8").strip()
            if stored:
                return stored
        except Exception:
            pass

    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    pair_id_bytes = secrets.token_bytes(16)
    pair_id = base64.urlsafe_b64encode(pair_id_bytes).decode("ascii").rstrip("=")
    PAIR_ID_FILE.write_text(pair_id, encoding="utf-8")
    _set_posix_permissions_0600(PAIR_ID_FILE)
    return pair_id


def get_or_create_pairing_token() -> str:
    """Retrieve existing 32-byte CSPRNG token or generate and persist one at mode 0600."""
    env_token = os.environ.get("AGENT_RELAY_TOKEN")
    if env_token:
        return env_token.strip()

    if TOKEN_FILE.exists():
        try:
            stored = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if stored:
                return stored
        except Exception:
            pass

    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    # Generate 32 bytes from CSPRNG, encoded as base64url
    raw_key = secrets.token_bytes(32)
    new_token = base64.urlsafe_b64encode(raw_key).decode("ascii").rstrip("=")

    TOKEN_FILE.write_text(new_token, encoding="utf-8")
    _set_posix_permissions_0600(TOKEN_FILE)
    return new_token


def regenerate_pairing_token() -> Tuple[str, str]:
    """Regenerate pairing token and pair ID, invalidating all previously paired devices."""
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)

    pair_id_bytes = secrets.token_bytes(16)
    new_pair_id = base64.urlsafe_b64encode(pair_id_bytes).decode("ascii").rstrip("=")
    PAIR_ID_FILE.write_text(new_pair_id, encoding="utf-8")
    _set_posix_permissions_0600(PAIR_ID_FILE)

    raw_key = secrets.token_bytes(32)
    new_token = base64.urlsafe_b64encode(raw_key).decode("ascii").rstrip("=")
    TOKEN_FILE.write_text(new_token, encoding="utf-8")
    _set_posix_permissions_0600(TOKEN_FILE)

    # Deliberately does not write os.environ. The new token is already
    # persisted above, and the regenerate route sets it on app.state for the
    # running hub. Mutating process-global state here leaked the token into
    # every application constructed later in the same process, which silently
    # switched on token authentication for them.
    return new_pair_id, new_token


def build_pairing_qr_uri(
    port: int = 8765,
    token: Optional[str] = None,
    iroh_ticket: Optional[str] = None
) -> str:
    """Generate the canonical agnview://pair URI according to the PAIRING.md contract.

    Without an iroh ticket the output is a v1 payload, byte for byte what this
    hub emitted before iroh existed. With a ticket it is a v2 payload: the same
    fields in the same order plus one optional field, iroh.
    """
    pair_id = get_or_create_pair_id()
    key = token or get_or_create_pairing_token()
    machine_name = socket.gethostname()
    lan_ip = get_local_ip()
    if not is_rfc1918_address(lan_ip) or get_bind_mode() == "loopback":
        # A hub listening on loopback only cannot be reached at its LAN
        # address. Offering it made a phone wait out the LAN rung's 800 ms
        # before trying iroh. The field is required by the contract, so it
        # carries 127.0.0.1, which a client treats as "skip the LAN rung".
        lan_ip = "127.0.0.1"

    try:
        fingerprint = get_cert_fingerprint()
    except Exception:
        fingerprint = ""

    encoded_name = urllib.parse.quote(machine_name)
    lan_addr = f"{lan_ip}:{port}"
    ticket = (iroh_ticket or "").strip()
    version = PAIRING_PAYLOAD_VERSION if ticket else 1

    uri = (
        f"agnview://pair?v={version}"
        f"&name={encoded_name}"
        f"&lan={lan_addr}"
        f"&fp={fingerprint}"
        f"&id={pair_id}"
        f"&k={key}"
    )
    if ticket:
        uri += f"&iroh={urllib.parse.quote(ticket, safe='')}"
    return uri


def parse_pairing_qr_uri(uri: str) -> Dict[str, Any]:
    """Parse an agnview://pair URI into its fields.

    Accepts v1 and v2. A v1 payload parses to exactly the fields it always
    carried, with iroh set to None. Raises ValueError on anything else.
    """
    if not uri or not uri.startswith(f"{PAIRING_URI_SCHEME}://pair?"):
        raise ValueError("not an AgnView pairing URI")

    query = uri.split("?", 1)[1]
    params = urllib.parse.parse_qs(query, keep_blank_values=True)

    def _one(key: str) -> str:
        values = params.get(key) or [""]
        return values[0]

    raw_version = _one("v")
    try:
        version = int(raw_version)
    except ValueError:
        raise ValueError(f"unreadable pairing payload version '{raw_version}'")
    if version not in SUPPORTED_PAIRING_PAYLOAD_VERSIONS:
        raise ValueError(f"unsupported pairing payload version {version}")

    for required in ("name", "lan", "id", "k"):
        if not _one(required):
            raise ValueError(f"pairing payload is missing '{required}'")

    return {
        "version": version,
        "name": _one("name"),
        "lan": _one("lan"),
        "fingerprint": _one("fp"),
        "pair_id": _one("id"),
        "token": _one("k"),
        "iroh_ticket": _one("iroh") or None,
    }


def build_pairing_payload(
    primary_url: str,
    endpoints: Dict[str, str],
    token: Optional[str] = None,
    port: int = 8765,
    iroh_ticket: Optional[str] = None
) -> Dict[str, Any]:
    """Generate standardized pairing configuration for iOS/Android apps."""
    active_token = token or get_or_create_pairing_token()
    pair_id = get_or_create_pair_id()
    ticket = (iroh_ticket or "").strip() or None
    qr_uri = build_pairing_qr_uri(port=port, token=active_token, iroh_ticket=ticket)

    return {
        "app": "AgnView",
        "version": "0.1.10",
        "payload_version": PAIRING_PAYLOAD_VERSION if ticket else 1,
        "hostname": socket.gethostname(),
        "pair_id": pair_id,
        "token": active_token,
        "primary_url": primary_url,
        "endpoints": endpoints,
        "iroh_ticket": ticket,
        "deeplink": qr_uri,
        "uri": qr_uri
    }


def generate_qr_svg(data_str: str) -> str:
    """Generate standalone vector SVG XML for embedding in HTML dashboard."""
    factory = qrcode.image.svg.SvgPathImage
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
        image_factory=factory
    )
    qr.add_data(data_str)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#080c10", back_color="#00ffff")

    stream = io.BytesIO()
    img.save(stream)
    return stream.getvalue().decode("utf-8")


def get_terminal_qr_ascii(data_str: str) -> str:
    """Generate ASCII representation of QR code for terminal display."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=1,
        border=1
    )
    qr.add_data(data_str)
    qr.make(fit=True)

    out = io.StringIO()
    qr.print_ascii(out=out, invert=True)
    return out.getvalue()

