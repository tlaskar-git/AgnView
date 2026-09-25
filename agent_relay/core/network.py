"""Network utilities for discovering LAN, Tailscale, and local addresses."""

import os
import socket
import subprocess
import re
import threading
from typing import Any, Dict, List, Optional
import ipaddress

from .proc import no_window


HOSTNAME_LOOKUP_TIMEOUT_SECONDS = 2.0


def resolve_with_timeout(func, *args, timeout: Optional[float] = None, default=None):
    """Run a blocking name lookup and give up after the timeout.

    Resolving the machine's own hostname can block for minutes on a host with
    no working resolver, for example a macOS CI runner. The lookup runs in a
    daemon thread, so a lookup that never returns costs one idle thread and
    never holds up the caller. The default is returned on a timeout or error.
    """
    box: Dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = func(*args)
        except Exception:
            pass

    thread = threading.Thread(target=work, name="agnview-name-lookup", daemon=True)
    thread.start()
    thread.join(HOSTNAME_LOOKUP_TIMEOUT_SECONDS if timeout is None else timeout)
    return box.get("value", default)


def is_tailscale_cgnat_address(ip_str: str) -> bool:
    """Check if an IP string is within the Carrier-Grade NAT (CGNAT) range 100.64.0.0/10 used by Tailscale."""
    try:
        ip = ipaddress.ip_address(ip_str)
        cgnat_network = ipaddress.ip_network("100.64.0.0/10")
        return ip in cgnat_network
    except ValueError:
        return False


def is_allowed_address(ip_str: str) -> bool:
    """Check if an IP string is loopback, RFC1918 private address, or Tailscale CGNAT address."""
    try:
        ip = ipaddress.ip_address(ip_str)
        if ip.is_loopback:
            return True
        if is_tailscale_cgnat_address(ip_str):
            return True
        # Allow RFC1918 private ranges
        return ip.is_private and not ip.is_link_local
    except ValueError:
        return False


def is_rfc1918_address(ip_str: str) -> bool:
    """Check if an IP string is an RFC1918 private address (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16)."""
    try:
        ip = ipaddress.ip_address(ip_str)
        return ip.is_private and not ip.is_loopback and not ip.is_link_local
    except ValueError:
        return False


def get_local_ip() -> str:
    """Get the primary local IPv4 address of this machine, restricted to RFC1918 private addresses."""
    # First, check socket connection routing to find preferred outbound address
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 80))
        ip = s.getsockname()[0]
        if is_rfc1918_address(ip):
            return ip
    except Exception:
        pass
    finally:
        s.close()

    # Second, enumerate local interface addresses to find an RFC1918 address
    for item in resolve_with_timeout(socket.getaddrinfo, socket.gethostname(), None, default=[]):
        if item[0] == socket.AF_INET:
            ip = item[4][0]
            if is_rfc1918_address(ip):
                return ip

    return "127.0.0.1"


def get_tailscale_ip() -> Optional[str]:
    """Detect if Tailscale is running and retrieve the 100.x.y.z interface address."""
    # Method 1: Try running `tailscale ip -4`
    try:
        proc = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=2.0,
            **no_window(),
        )
        if proc.returncode == 0:
            ip = proc.stdout.strip()
            if ip.startswith("100."):
                return ip
    except Exception:
        pass

    # Method 2: Inspect network interfaces via socket/ipconfig
    try:
        if os.name == "nt":
            proc = subprocess.run(
                ["ipconfig"],
                capture_output=True,
                text=True,
                timeout=2.0,
                **no_window(),
            )
            if proc.returncode == 0:
                matches = re.findall(r"IPv4 Address[.\s]+:\s*(100\.\d{1,3}\.\d{1,3}\.\d{1,3})", proc.stdout)
                if matches:
                    return matches[0]
    except Exception:
        pass

    return None


def get_network_endpoints(port: int = 8765) -> Dict[str, Optional[str]]:
    """Return dictionary of available network access URLs, restricted to loopback and RFC1918."""
    hostname = socket.gethostname()
    lan_ip = get_local_ip()
    tailscale_ip = get_tailscale_ip()

    endpoints: Dict[str, Optional[str]] = {
        "local": f"http://localhost:{port}",
        "localhost": f"http://127.0.0.1:{port}",
    }

    if is_rfc1918_address(lan_ip):
        endpoints["lan"] = f"http://{lan_ip}:{port}"

    # Only include hostname if it resolves to loopback or RFC1918
    host_ip = resolve_with_timeout(socket.gethostbyname, hostname)
    if host_ip and is_allowed_address(host_ip):
        endpoints["hostname"] = f"http://{hostname.lower()}:{port}"

    if tailscale_ip:
        endpoints["tailscale"] = f"http://{tailscale_ip}:{port}"

    return endpoints


# Environment variable the serve command uses to record the bind mode it resolved.
BIND_MODE_ENV = "AGENT_RELAY_BIND_MODE"

TRANSPORT_LABELS = {
    "lan": "Connected over LAN",
    "tailscale": "Connected over Tailscale",
    "loopback": "Loopback only",
}


def resolve_bind_mode(host: str) -> str:
    """Map a resolved bind host to a bind mode: 'lan', 'tailscale', or 'loopback'."""
    if host in ("0.0.0.0", "::"):
        return "lan"
    if is_tailscale_cgnat_address(host):
        return "tailscale"
    if is_rfc1918_address(host):
        return "lan"
    return "loopback"


def get_bind_mode() -> str:
    """Return the bind mode of the running server ('lan', 'tailscale' or 'loopback')."""
    mode = os.environ.get(BIND_MODE_ENV, "loopback").strip().lower()
    return mode if mode in TRANSPORT_LABELS else "loopback"


def get_transport_label(mode: Optional[str] = None) -> str:
    """Return the human-readable status line for the active transport."""
    return TRANSPORT_LABELS.get(mode or get_bind_mode(), TRANSPORT_LABELS["loopback"])


# ----------------- Transport abstraction -----------------

class Transport:
    """Minimal contract every AgnView transport implements.

    Keep it small on purpose. A transport owns one way of reaching the hub and
    nothing else. It never owns dispatch, pipelines or the HTTP app.

    start() must return without waiting for the network. A transport that needs
    to reach the internet does that work in the background and reports progress
    through status(), so a hub on an unreachable network still starts.
    """

    name = "transport"

    def start(self) -> None:
        """Bring the transport up. Must not block on network reachability."""
        raise NotImplementedError

    async def stop(self) -> None:
        """Tear the transport down. Safe to call when start() failed."""
        raise NotImplementedError

    def status(self) -> Dict[str, Any]:
        """Return a JSON-serialisable description of the current state."""
        raise NotImplementedError


# ----------------- Client connection order -----------------

# The order every AgnView client tries, best first. A client walks this list
# and stops at the first rung that answers.
CONNECTION_ORDER = ("lan", "iroh-direct", "iroh-relay")

# A client gives up on the LAN address after this long and moves to iroh. The
# LAN rung is either fast or absent, so a short budget costs nothing.
LAN_CONNECT_TIMEOUT_SECONDS = 0.8

# Every value the resolved transport can take.
RESOLVED_TRANSPORTS = ("lan", "iroh-direct", "iroh-relay", "offline")

RESOLVED_TRANSPORT_LABELS = {
    "lan": "Connected over LAN",
    "iroh-direct": "Connected peer to peer",
    "iroh-relay": "Connected through a relay",
    "offline": "Not connected",
}


def get_resolved_transport_label(resolved: Optional[str]) -> str:
    """Return the human-readable status line for a resolved client transport."""
    return RESOLVED_TRANSPORT_LABELS.get(resolved or "offline", RESOLVED_TRANSPORT_LABELS["offline"])


def resolve_iroh_path_transport(paths: Optional[List[Any]]) -> str:
    """Classify an iroh connection as 'iroh-direct' or 'iroh-relay' from its paths.

    iroh reports one snapshot per network path it holds and marks the one in
    use. A selected path that is a plain IP path means hole-punching worked, so
    the traffic is peer to peer. Anything else is still going via a relay.
    """
    if not paths:
        return "iroh-relay"

    selected = [p for p in paths if getattr(p, "is_selected", False)]
    candidates = selected or list(paths)
    for path in candidates:
        if getattr(path, "is_ip", False) and not getattr(path, "is_relay", False):
            return "iroh-direct"
    return "iroh-relay"


def available_transports(bind_mode: str, iroh_status: Optional[Dict[str, Any]] = None) -> List[str]:
    """List the rungs a client could use right now, best first."""
    status = iroh_status or {}
    available = []
    if bind_mode in ("lan", "tailscale"):
        available.append("lan")
    if status.get("state") == "ready":
        # A live endpoint always has the relay rung. The direct rung only
        # exists once hole-punching succeeds, which is a per-client outcome.
        available.append("iroh-relay")
    return [rung for rung in CONNECTION_ORDER if rung in available]


def resolve_hub_transport(bind_mode: str, iroh_status: Optional[Dict[str, Any]] = None) -> str:
    """Return the hub's view of the active transport.

    The rung the last client actually used wins, because that is measured
    rather than guessed. With no client yet: a LAN or Tailscale bind is a real,
    standing fact about reachability, so it is reported as such. iroh's relay
    and direct rungs are per-connection outcomes, never a standing fact, so
    they are only ever reported once a client has actually measured one -
    offering the relay rung as a guess made an idle, unreachable-from-outside
    hub read as "Relayed" despite nothing being connected. Idle and no LAN
    bind is 'offline', which is what it is.
    """
    status = iroh_status or {}
    last = status.get("last_resolved_transport")
    if last in RESOLVED_TRANSPORTS and last != "offline":
        return last

    if bind_mode in ("lan", "tailscale"):
        return "lan"

    return "offline"

