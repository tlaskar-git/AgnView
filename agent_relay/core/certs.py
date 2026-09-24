"""Self-signed TLS certificate generation and fingerprint extraction for AgnView."""

import os
import sys
import shutil
import hashlib
import subprocess
from pathlib import Path
from typing import Tuple, Optional

from .proc import no_window


CERT_DIR = Path.home() / ".agnview" / "certs"
CERT_FILE = CERT_DIR / "cert.pem"
KEY_FILE = CERT_DIR / "key.pem"


def _find_openssl_binary() -> Optional[str]:
    """Find available openssl executable across system PATH and standard locations."""
    which_path = shutil.which("openssl")
    if which_path:
        return which_path

    # Common Windows locations (e.g. Git for Windows, OpenSSL)
    if sys.platform == "win32":
        prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        prog_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        system_drive = os.environ.get("SystemDrive", "C:")
        candidates = [
            os.path.join(prog_files, "Git", "usr", "bin", "openssl.exe"),
            os.path.join(prog_files_x86, "Git", "usr", "bin", "openssl.exe"),
            os.path.join(system_drive, "\\OpenSSL-Win64", "bin", "openssl.exe"),
            os.path.join(system_drive, "\\OpenSSL-Win32", "bin", "openssl.exe"),
        ]
        for candidate in candidates:
            if os.path.isfile(candidate):
                return candidate

    return None


def ensure_self_signed_cert() -> Tuple[Path, Path]:
    """Ensure a self-signed certificate and private key exist under ~/.agnview/certs/."""
    CERT_DIR.mkdir(parents=True, exist_ok=True)

    if CERT_FILE.exists() and KEY_FILE.exists():
        return CERT_FILE, KEY_FILE

    openssl_bin = _find_openssl_binary()
    if not openssl_bin:
        raise RuntimeError(
            "openssl binary is required to generate self-signed TLS certificate but was not found."
        )

    # Generate self-signed RSA certificate valid for 10 years (3650 days)
    cmd = [
        openssl_bin, "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", str(KEY_FILE),
        "-out", str(CERT_FILE),
        "-days", "3650",
        "-nodes",
        "-subj", "/CN=AgnView Hub/O=AgnView/C=UK"
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, **no_window())
    if result.returncode != 0:
        raise RuntimeError(f"Failed to generate self-signed certificate: {result.stderr}")

    # Set mode 0600 on private key if supported on the OS
    try:
        os.chmod(KEY_FILE, 0o600)
    except Exception:
        pass

    return CERT_FILE, KEY_FILE


def get_cert_fingerprint() -> str:
    """Calculate and return the SHA-256 fingerprint (hex) of the hub self-signed certificate."""
    cert_path, _ = ensure_self_signed_cert()
    cert_data = cert_path.read_bytes()

    # If the certificate is PEM-encoded, parse DER bytes or hash the DER certificate
    lines = cert_data.decode("ascii", errors="ignore").splitlines()
    b64_body = "".join(
        line.strip() for line in lines
        if line.strip() and not line.startswith("-----")
    )
    import base64
    der_bytes = base64.b64decode(b64_body)
    return hashlib.sha256(der_bytes).hexdigest()
