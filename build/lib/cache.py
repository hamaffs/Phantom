"""On-disk response cache, transient-retry policy, and CheckResult⇄cache
serializers. Keeps Phantom scans deterministic and fast on re-runs.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from models import CheckResult, Site


# ---------------------------------------------------------------------------
# Response cache
# ---------------------------------------------------------------------------

_DEFAULT_CACHE_PATH = Path(
    os.environ.get("PHANTOM_CACHE_PATH")
    or Path.home() / ".cache" / "phantom" / "cache.json"
)
_CACHE_TTL = 3600                # 1 hour — default for one-off scans
_RESUME_TTL = 7 * 24 * 3600      # 7 days — when --resume is set
_CACHE_MAX_ENTRIES = 5000        # LRU-trim threshold
_FLUSH_INTERVAL_SECONDS = 5      # how often the scanner asks us to save


class ResponseCache:
    """Tiny TTL cache keyed by (method, url, body_payload).

    Used to skip re-fetching the same URL within `ttl_seconds`. Big win for
    iterative use ("phantom alice; phantom alice --export html; phantom
    alice --found-only") and for resuming an interrupted scan.

    On-disk format is plain JSON. We deliberately don't persist response
    bodies for non-FOUND results to keep the cache small — if a site
    answered 404 once it'll answer 404 again, and the only thing the
    evaluator needs from it is the (status, exists) tuple.
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        enabled: bool = True,
        ttl_seconds: int = _CACHE_TTL,
    ):
        self.path = Path(path) if path else _DEFAULT_CACHE_PATH
        self.enabled = enabled
        self.ttl_seconds = ttl_seconds
        self._mem: dict[str, dict] = {}
        self._dirty = False
        self._last_flush = time.time()
        # Stats for the --resume summary line.
        self.disk_hits = 0
        self.fresh_writes = 0
        if self.enabled:
            self._load()

    @staticmethod
    def _key(method: str, url: str, body: Optional[str]) -> str:
        return f"{method.upper()} {url}\n{body or ''}"

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        now = time.time()
        for k, v in raw.items():
            if not isinstance(v, dict):
                continue
            if now - v.get("ts", 0) > self.ttl_seconds:
                continue
            # Skip pre-existing UNKNOWNs from older runs that didn't yet
            # filter them out — caching uncertainty is the wrong policy.
            if v.get("exists") is None:
                self._dirty = True
                continue
            self._mem[k] = v

    def get(self, method: str, url: str, body: Optional[str]) -> Optional[dict]:
        if not self.enabled:
            return None
        entry = self._mem.get(self._key(method, url, body))
        if not entry:
            return None
        if time.time() - entry.get("ts", 0) > self.ttl_seconds:
            return None
        self.disk_hits += 1
        return entry

    def set(self, method: str, url: str, body: Optional[str], entry: dict) -> None:
        if not self.enabled:
            return
        entry = {**entry, "ts": time.time()}
        self._mem[self._key(method, url, body)] = entry
        self._dirty = True
        self.fresh_writes += 1

    def save(self) -> None:
        if not self.enabled or not self._dirty:
            return
        if len(self._mem) > _CACHE_MAX_ENTRIES:
            # Keep newest by timestamp.
            keep = sorted(
                self._mem.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True
            )[:_CACHE_MAX_ENTRIES]
            self._mem = dict(keep)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._mem, separators=(",", ":")),
                encoding="utf-8",
            )
            self._dirty = False
            self._last_flush = time.time()
        except Exception:
            pass  # cache is best-effort, never fatal

    def maybe_flush(self) -> None:
        """Periodic save during a scan so a Ctrl-C doesn't lose progress.

        Cheaper than calling save() on every set(): we only flush when
        the on-disk image is at least _FLUSH_INTERVAL_SECONDS stale.
        """
        if not self.enabled or not self._dirty:
            return
        if time.time() - self._last_flush < _FLUSH_INTERVAL_SECONDS:
            return
        self.save()
# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

# These are the answers we treat as "transient" — worth a single retry
# before recording. The point is not to pretend flakes don't exist, just
# to give the network one more chance before we lock in a verdict.
_TRANSIENT_REASONS = {"timeout"}
_TRANSIENT_ERROR_PREFIXES = (
    "ServerDisconnected",
    "ClientConnector",
    "ClientOSError",
    "ClientPayload",
    "ConnectionResetError",
    "CurlError",
    "RemoteProtocolError",
    "TimeoutError",
)


def _is_transient(result: "CheckResult") -> bool:
    """True if we should give the same request one more try.

    Conservative on purpose: a real 4xx or a real "absence" match is
    locked in immediately. Only timeouts, transport errors, and 5xx that
    landed as `unexpected-NNN` are retried. Bot-walled responses are NOT
    retried — the wall isn't going to disappear in 200ms.
    """
    if result.exists is True or result.exists is False:
        return False
    if result.reason in _TRANSIENT_REASONS:
        return True
    if result.error and any(
        result.error.startswith(p) for p in _TRANSIENT_ERROR_PREFIXES
    ):
        return True
    if result.status is not None and 500 <= result.status < 600:
        return True
    if result.reason and result.reason.startswith("unexpected-5"):
        return True
    return False

def _result_to_cache(r: CheckResult) -> dict:
    """Pack the bits of a CheckResult we want to persist."""
    return {
        "exists": r.exists,
        "status": r.status,
        "reason": r.reason,
        "final_url": r.final_url,
        "backend": r.backend,
        "profile": r.profile or {},
    }


def _result_from_cache(
    site: Site, username: str, entry: dict, backend: str
) -> CheckResult:
    """Rebuild a CheckResult from a cache entry. `cached` reason marks it."""
    base_reason = entry.get("reason") or ""
    return CheckResult(
        site=site.name,
        category=site.category,
        url=site.display_url_for(username),
        exists=entry.get("exists"),
        reliability=site.reliability,
        status=entry.get("status"),
        elapsed_ms=0,
        reason=(base_reason + "+cached") if base_reason else "cached",
        final_url=entry.get("final_url"),
        backend=entry.get("backend") or backend,
        profile=entry.get("profile") or {},
        variant=username,
    )
