"""API key storage for Phantom.

Third-party services (Hunter.io for the --email feature, future
integrations) need credentials. We keep them at
`~/.config/phantom/apis.json` (XDG_CONFIG_HOME aware), 0600 mode, one
flat dict of {service: key}. Service names are lower-cased on write so
`--api add Hunter ...` and `--api add hunter ...` collapse to one
entry.

Public surface: get(service), add(service, key), list_services(). The
file format is intentionally tiny — `cat ~/.config/phantom/apis.json`
should be useful to a human auditing what's stored.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Optional


def config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "phantom" / "apis.json"


def _load() -> dict[str, str]:
    p = config_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k.lower(): v for k, v in data.items()
        if isinstance(k, str) and isinstance(v, str) and v.strip()
    }


def _save(data: dict[str, str]) -> Path:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return p


def get(service: str) -> Optional[str]:
    """Return the stored key for `service`, or None if not configured."""
    return _load().get(service.lower().strip())


def add(service: str, key: str) -> Path:
    """Save `key` under `service`. Returns the file path written to."""
    s = service.lower().strip()
    k = key.strip()
    if not s:
        raise ValueError("service name must be non-empty")
    if not k:
        raise ValueError("API key must be non-empty")
    data = _load()
    data[s] = k
    return _save(data)


def list_services() -> list[str]:
    """Return the sorted list of service names that have a key stored."""
    return sorted(_load().keys())
