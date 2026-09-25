"""Who may reach the pairing routes.

The pairing routes hand out the key that runs agents on this computer, so they
are for the dashboard on this computer and nobody else. A request must come
from a loopback address, carry a Host header that names this hub on a loopback
name and its own port (which stops DNS rebinding), and, when a browser sent it,
come from the hub's own page.
"""

import ipaddress
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse

PAIRING_PREFIX = "/api/mobile/pairing"
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "[::1]")


def _is_loopback_client(request: Request) -> bool:
    host = request.client.host if request.client else None
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if getattr(address, "ipv4_mapped", None) is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def pairing_request_allowed(request: Request, port: int) -> bool:
    if not _is_loopback_client(request):
        return False
    host_header: Optional[str] = (request.headers.get("host") or "").strip().lower()
    if host_header not in {f"{name}:{port}" for name in LOOPBACK_NAMES}:
        return False
    origin = request.headers.get("origin")
    if origin is not None and origin.strip().lower() != f"http://{host_header}":
        return False
    site = request.headers.get("sec-fetch-site")
    if site is not None and site.strip().lower() not in ("same-origin", "none"):
        return False
    return True


def refuse_pairing_request() -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"detail": "The pairing routes are only open to the dashboard on this computer."},
    )
