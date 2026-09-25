"""Hub configuration at ~/.agnview/config.yaml.

One file, few keys, no required edits. The file is written with its defaults on
first run so the settings are discoverable, and a hub that never touches it
behaves exactly as if it did not exist.

A bad value here is never papered over. The loader records the reason, the log
carries it, and the iroh transport refuses to start rather than quietly falling
back to something the user did not ask for. The rest of the hub starts as
normal, so a typo in a relay URL cannot take the dashboard down.
"""

import logging
import os
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger("agnview.config")

CONFIG_DIR = Path.home() / ".agnview"
DEFAULT_CONFIG_PATH = CONFIG_DIR / "config.yaml"

# Point the hub at a different configuration file, mainly for tests.
CONFIG_PATH_ENV = "AGNVIEW_CONFIG"

ALLOWED_RELAY_SCHEMES = ("http", "https")

DEFAULT_CONFIG_TEMPLATE = """# AgnView hub configuration.

# relay_url: leave empty to use the relays bundled with iroh. They need no
# account, no key and no setup. Set this only to point the hub at a relay you
# run yourself, for example https://relay.example.com. A value that is not a
# valid http or https URL stops the iroh transport and says so in the log. The
# rest of the hub still starts.
relay_url: ""

# iroh_enabled: set to false to keep the hub on the LAN only.
iroh_enabled: true

# iroh_api_enabled: with iroh on, a paired phone can use Usage, Pipelines,
# Sessions and prompt dispatch over iroh too, gated by the same pairing key as
# on the LAN. Set to false to serve only the live console over iroh.
iroh_api_enabled: true

# iroh_dispatch_roots: folders a phone may run an agent in over iroh. Empty
# means any existing folder. Set a list, for example ["/home/me/work"], to keep
# phone dispatches inside those folders. The LAN is not affected.
iroh_dispatch_roots: []
"""


class ConfigError(ValueError):
    """Raised for a configuration value the hub refuses to act on."""


@dataclass
class HubConfig:
    """Loaded hub settings plus anything that was wrong with them."""

    relay_url: str = ""
    iroh_enabled: bool = True
    iroh_api_enabled: bool = True
    iroh_dispatch_roots: List[str] = field(default_factory=list)
    path: Optional[Path] = None
    errors: List[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": str(self.path) if self.path else None,
            "relay_url": self.relay_url,
            "iroh_enabled": self.iroh_enabled,
            "iroh_api_enabled": self.iroh_api_enabled,
            "iroh_dispatch_roots": list(self.iroh_dispatch_roots),
            "errors": list(self.errors),
        }


def validate_relay_url(value: Any) -> str:
    """Return a clean relay URL, or raise ConfigError explaining what is wrong.

    An empty value is valid and means the bundled relays.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"relay_url must be a string, got {type(value).__name__}")

    url = value.strip()
    if not url:
        return ""

    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme:
        raise ConfigError(
            f"relay_url '{url}' has no scheme, it must start with https:// or http://"
        )
    if parsed.scheme.lower() not in ALLOWED_RELAY_SCHEMES:
        raise ConfigError(
            f"relay_url '{url}' uses scheme '{parsed.scheme}', "
            f"only {' and '.join(ALLOWED_RELAY_SCHEMES)} are accepted"
        )
    if not parsed.netloc:
        raise ConfigError(f"relay_url '{url}' has no host")
    if parsed.query or parsed.fragment:
        raise ConfigError(f"relay_url '{url}' must not carry a query string or fragment")
    return url


def get_config_path(path: Optional[Path] = None) -> Path:
    """Resolve which configuration file to read."""
    if path is not None:
        return Path(path)
    override = os.environ.get(CONFIG_PATH_ENV)
    if override:
        return Path(override)
    return DEFAULT_CONFIG_PATH


def ensure_default_config(path: Optional[Path] = None) -> Path:
    """Write the default configuration file when it does not exist yet."""
    config_path = get_config_path(path)
    if config_path.exists():
        return config_path
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    except Exception as exc:
        logger.warning("could not write the default configuration to %s: %s", config_path, exc)
    return config_path


def load_config(path: Optional[Path] = None) -> HubConfig:
    """Read ~/.agnview/config.yaml, recording every problem instead of raising."""
    config_path = ensure_default_config(path)
    config = HubConfig(path=config_path)

    if not config_path.exists():
        return config

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        message = f"{config_path} is not readable YAML: {exc}"
        logger.error("AgnView configuration error: %s", message)
        config.errors.append(message)
        return config

    if raw is None:
        return config
    if not isinstance(raw, dict):
        message = f"{config_path} must contain a mapping of settings, got {type(raw).__name__}"
        logger.error("AgnView configuration error: %s", message)
        config.errors.append(message)
        return config

    try:
        config.relay_url = validate_relay_url(raw.get("relay_url"))
    except ConfigError as exc:
        message = f"{config_path}: {exc}"
        logger.error("AgnView configuration error: %s", message)
        logger.error("the iroh transport will not start. Fix relay_url or clear it to use the bundled relays.")
        config.errors.append(message)

    enabled = raw.get("iroh_enabled", True)
    if isinstance(enabled, bool):
        config.iroh_enabled = enabled
    else:
        message = f"{config_path}: iroh_enabled must be true or false, got {enabled!r}"
        logger.error("AgnView configuration error: %s", message)
        config.errors.append(message)

    api_enabled = raw.get("iroh_api_enabled", True)
    if isinstance(api_enabled, bool):
        config.iroh_api_enabled = api_enabled
    else:
        message = f"{config_path}: iroh_api_enabled must be true or false, got {api_enabled!r}"
        logger.error("AgnView configuration error: %s", message)
        config.errors.append(message)

    roots = raw.get("iroh_dispatch_roots", [])
    if roots is None:
        roots = []
    if isinstance(roots, list) and all(isinstance(r, str) and r.strip() and "\x00" not in r for r in roots):
        config.iroh_dispatch_roots = [r.strip() for r in roots]
    else:
        message = f"{config_path}: iroh_dispatch_roots must be a list of folder paths, got {roots!r}"
        logger.error("AgnView configuration error: %s", message)
        config.errors.append(message)

    return config
