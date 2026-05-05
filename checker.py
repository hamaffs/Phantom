"""Phantom — async username availability checker across 60 platforms.

Two HTTP backends:
- aiohttp for sites with no bot protection (fast, default)
- curl_cffi with Chrome TLS impersonation for sites flagged with
  `protection: ["tls_fingerprint"]` (defeats Cloudflare/Akamai/AWS WAF)

Detection model (per site):
- `method = "status"` — decide by HTTP status code (`valid_status` / `invalid_status`).
- `method = "message"` — decide by body content. A site is FOUND iff a
  `presence_text` pattern matches AND no `absence_text` pattern matches.

Two-sided matching is the rule: a hit must produce a *positive* signal,
not just the absence of a negative one. This kills the false positives
from generic 200 walls, signup redirects, and catch-all SPAs.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector

from enrich import extract_profile
from identity import build_identities
from variants import generate as generate_variants
from watch import (
    Snapshot, diff as compute_diff, load_history, render_diff_terminal,
    save_snapshot,
)

try:
    from curl_cffi.requests import AsyncSession as CurlSession  # type: ignore
    HAS_CURL_CFFI = True
except ImportError:
    CurlSession = None  # type: ignore
    HAS_CURL_CFFI = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "DNT": "1",
}

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")

# Title fragments that indicate a Cloudflare / WAF / captcha challenge —
# the body cannot be trusted as a real response.
BOT_TITLE_HINTS = (
    "just a moment",
    "please wait",
    "attention required",
    "verify you are human",
    "checking your browser",
    "robot check",
    "access denied",
    "ddos protection",
    "client challenge",
    "ng guard",
    "prove your humanity",
)

_TITLE_RE = re.compile(r"<title[^>]*>([^<]*)</title>", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Site:
    name: str
    category: str
    url: str
    method: str                              # "status" | "message"
    reliability: int
    valid_status: list[int] = field(default_factory=list)
    invalid_status: list[int] = field(default_factory=list)
    presence_text: list[str] = field(default_factory=list)
    absence_text: list[str] = field(default_factory=list)
    protection: list[str] = field(default_factory=list)
    headers: dict = field(default_factory=dict)
    request_method: str = "GET"              # "GET" | "POST"
    request_body: Optional[str] = None       # raw body template (with {username})

    def url_for(self, username: str) -> str:
        return self.url.replace("{username}", username)

    def body_for(self, username: str) -> Optional[str]:
        return self.request_body.replace("{username}", username) if self.request_body else None

    @property
    def needs_impersonation(self) -> bool:
        return "tls_fingerprint" in self.protection


@dataclass
class CheckResult:
    site: str
    category: str
    url: str
    exists: Optional[bool]      # True = found, False = absent, None = unknown
    reliability: int
    status: Optional[int] = None
    error: Optional[str] = None
    elapsed_ms: int = 0
    reason: Optional[str] = None    # human label for why we decided as we did
    final_url: Optional[str] = None # set when redirects landed elsewhere
    backend: str = "aiohttp"        # which HTTP client handled the request
    profile: dict = field(default_factory=dict)  # display name, bio, photo, follower counts… if FOUND
    variant: Optional[str] = None   # which generated variant produced this


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_sites(path: Path) -> list[Site]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    sites = []
    for entry in raw:
        sites.append(Site(
            name=entry["name"],
            category=entry["category"],
            url=entry["url"],
            method=entry["method"],
            reliability=int(entry["reliability"]),
            valid_status=entry.get("valid_status", []),
            invalid_status=entry.get("invalid_status", []),
            presence_text=entry.get("presence_text", []),
            absence_text=entry.get("absence_text", []),
            protection=entry.get("protection", []),
            headers=entry.get("headers", {}),
            request_method=entry.get("request_method", "GET"),
            request_body=entry.get("request_body"),
        ))
    return sites


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def _expand(patterns: list[str], username: str) -> list[str]:
    """Substitute {username} in detection patterns."""
    return [p.replace("{username}", username) for p in patterns]


def _is_bot_wall(body: str) -> bool:
    if not body:
        return False
    m = _TITLE_RE.search(body)
    if not m:
        return False
    title = m.group(1).strip().lower()
    return any(h in title for h in BOT_TITLE_HINTS)


def evaluate(site: Site, status: int, body: str, username: str) -> tuple[Optional[bool], str]:
    """Return (exists, reason).

    `exists` is True/False (decision), or None for inconclusive.
    `reason` is a short tag suitable for output: e.g. "200", "404",
    "presence", "absence", "no-presence", "bot-wall".

    Decision order (hard rules first, soft signals last):

    1. If the status is in `invalid_status` → MISSING. Status codes are the
       single most reliable signal when the site has clean ones.
    2. If the body matches an `absence_text` pattern → MISSING. A site
       saying "user not found" beats a misleading 200.
    3. Bot-wall detection (Cloudflare/captcha title) → UNKNOWN. Body is
       untrustworthy.
    4. Method-specific positive checks (status_code or presence_text).
    5. Anything else → UNKNOWN.
    """
    presence = _expand(site.presence_text, username)
    absence = _expand(site.absence_text, username)

    body_has_presence = any(p in body for p in presence) if presence else False
    body_has_absence = any(p in body for p in absence) if absence else False

    # 1. Hard MISSING signals.
    if status in site.invalid_status:
        return False, f"{status}"
    if body_has_absence:
        return False, "absence"

    # 2. Bot-wall — body unreliable, status was likely 200 from the WAF.
    if _is_bot_wall(body):
        return None, "bot-wall"

    # 3. Method-specific positive decision.
    if site.method == "status":
        if status in site.valid_status:
            if presence and not body_has_presence:
                return None, "no-presence"
            return True, f"{status}"
        return None, f"unexpected-{status}"

    if site.method == "message":
        if presence:
            if body_has_presence:
                return True, "presence"
            return None, "no-presence"
        # No presence patterns defined — last resort: trust the status.
        if status >= 400:
            return None, f"{status}"
        return True, "no-absence"

    return None, "unknown-method"


# ---------------------------------------------------------------------------
# HTTP backends
# ---------------------------------------------------------------------------

async def _drain(stream, max_body: int) -> bytes:
    """Read until EOF or max_body, in chunks."""
    chunks: list[bytes] = []
    total = 0
    while total < max_body:
        piece = await stream.read(min(64 * 1024, max_body - total))
        if not piece:
            break
        chunks.append(piece)
        total += len(piece)
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Response cache
# ---------------------------------------------------------------------------

_DEFAULT_CACHE_PATH = Path(
    os.environ.get("PHANTOM_CACHE_PATH")
    or Path.home() / ".cache" / "phantom" / "cache.json"
)
_CACHE_TTL = 3600          # 1 hour
_CACHE_MAX_ENTRIES = 5000  # LRU-trim threshold


class ResponseCache:
    """Tiny TTL cache keyed by (method, url, body_payload).

    Used to skip re-fetching the same URL within an hour. Big win for
    iterative use ("phantom alice; phantom alice --export html; phantom
    alice --found-only") and for hot-path retries that already have a fresh
    answer in memory.

    On-disk format is plain JSON. We deliberately don't persist response
    bodies for non-FOUND results to keep the cache small — if a site
    answered 404 once it'll answer 404 again, and the only thing the
    evaluator needs from it is the (status, exists) tuple.
    """

    def __init__(self, path: Optional[Path] = None, enabled: bool = True):
        self.path = Path(path) if path else _DEFAULT_CACHE_PATH
        self.enabled = enabled
        self._mem: dict[str, dict] = {}
        self._dirty = False
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
            if now - v.get("ts", 0) > _CACHE_TTL:
                continue
            self._mem[k] = v

    def get(self, method: str, url: str, body: Optional[str]) -> Optional[dict]:
        if not self.enabled:
            return None
        entry = self._mem.get(self._key(method, url, body))
        if not entry:
            return None
        if time.time() - entry.get("ts", 0) > _CACHE_TTL:
            return None
        return entry

    def set(self, method: str, url: str, body: Optional[str], entry: dict) -> None:
        if not self.enabled:
            return
        entry = {**entry, "ts": time.time()}
        self._mem[self._key(method, url, body)] = entry
        self._dirty = True

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
        except Exception:
            pass  # cache is best-effort, never fatal


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


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------

class Phantom:
    def __init__(
        self,
        sites: list[Site],
        *,
        concurrency: int = 25,
        timeout: float = 15.0,
        max_body: int = 2 * 1024 * 1024,
        impersonate: bool = True,
        retry_on_transient: bool = True,
        retry_delay: float = 0.2,
        cache: Optional[ResponseCache] = None,
    ) -> None:
        self.sites = sites
        self.concurrency = concurrency
        self.timeout = timeout
        self.max_body = max_body
        # Caller can disable the curl_cffi path even when installed.
        self.impersonate = impersonate and HAS_CURL_CFFI
        self.retry_on_transient = retry_on_transient
        self.retry_delay = retry_delay
        self.cache = cache or ResponseCache(enabled=False)

    # ------- aiohttp path --------------------------------------------------

    async def _aiohttp_request(
        self,
        session: ClientSession,
        site: Site,
        username: str,
    ) -> CheckResult:
        """Single aiohttp attempt — no retry, no cache. Used by the wrapper."""
        url = site.url_for(username)
        has_custom_ua = any(k.lower() == "user-agent" for k in site.headers)
        headers = dict(site.headers) if has_custom_ua else {**DEFAULT_HEADERS, **site.headers}
        body_payload = site.body_for(username)
        start = time.monotonic()
        try:
            method = session.post if site.request_method.upper() == "POST" else session.get
            kwargs: dict = {
                "headers": headers,
                "allow_redirects": True,
                "timeout": ClientTimeout(total=self.timeout),
            }
            if body_payload is not None:
                kwargs["data"] = body_payload
            async with method(url, **kwargs) as resp:
                raw = await _drain(resp.content, self.max_body)
                body = raw.decode(resp.charset or "utf-8", errors="replace")
                exists, reason = evaluate(site, resp.status, body, username)
                final = str(resp.url)
                profile = (
                    extract_profile(site.name, body, final, username)
                    if exists is True else {}
                )
                return CheckResult(
                    site=site.name, category=site.category, url=url, exists=exists,
                    reliability=site.reliability, status=resp.status,
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                    reason=reason,
                    final_url=final if final != url else None,
                    backend="aiohttp", profile=profile, variant=username,
                )
        except asyncio.TimeoutError:
            return CheckResult(
                site=site.name, category=site.category, url=url, exists=None,
                reliability=site.reliability, error="timeout", reason="timeout",
                elapsed_ms=int((time.monotonic() - start) * 1000),
                backend="aiohttp", variant=username,
            )
        except aiohttp.ClientError as e:
            return CheckResult(
                site=site.name, category=site.category, url=url, exists=None,
                reliability=site.reliability, error=type(e).__name__,
                reason=type(e).__name__,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                backend="aiohttp", variant=username,
            )

    async def _check_aiohttp(
        self,
        session: ClientSession,
        site: Site,
        username: str,
        sem: asyncio.Semaphore,
    ) -> CheckResult:
        """aiohttp backend with cache + retry."""
        url = site.url_for(username)
        body_payload = site.body_for(username)
        method_name = site.request_method.upper()

        cached = self.cache.get(method_name, url, body_payload)
        if cached:
            return _result_from_cache(site, username, cached, "aiohttp")

        async with sem:
            result = await self._aiohttp_request(session, site, username)
            if self.retry_on_transient and _is_transient(result):
                await asyncio.sleep(self.retry_delay)
                retry = await self._aiohttp_request(session, site, username)
                # Prefer the retry only if it actually upgraded the verdict.
                # A second timeout shouldn't overwrite the first.
                if retry.exists is True or retry.exists is False:
                    retry.reason = (retry.reason or "") + "+retry"
                    result = retry

        # Cache anything that wasn't a transient failure. A bot wall today
        # will be a bot wall in five minutes — caching saves the round trip.
        # Only timeouts and transport errors (where _is_transient is True
        # *and* we still don't have a verdict after retry) are excluded.
        if not _is_transient(result):
            self.cache.set(method_name, url, body_payload, _result_to_cache(result))
        return result

    # ------- curl_cffi path ------------------------------------------------

    async def _curl_request(
        self,
        session,  # CurlSession
        site: Site,
        username: str,
    ) -> CheckResult:
        """Single curl_cffi attempt — no retry, no cache."""
        url = site.url_for(username)
        site_headers = {
            k: v for k, v in site.headers.items()
            if k.lower() not in ("user-agent", "connection")
        }
        body_payload = site.body_for(username)
        start = time.monotonic()
        try:
            kwargs: dict = {
                "url": url,
                "headers": site_headers or None,
                "allow_redirects": True,
                "timeout": self.timeout,
            }
            if body_payload is not None:
                kwargs["data"] = body_payload
            if site.request_method.upper() == "POST":
                resp = await session.post(**kwargs)
            else:
                resp = await session.get(**kwargs)
            body = resp.text or ""
            if len(body) > self.max_body:
                body = body[: self.max_body]
            exists, reason = evaluate(site, resp.status_code, body, username)
            final = str(resp.url)
            profile = (
                extract_profile(site.name, body, final, username)
                if exists is True else {}
            )
            return CheckResult(
                site=site.name, category=site.category, url=url, exists=exists,
                reliability=site.reliability, status=resp.status_code,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                reason=reason,
                final_url=final if final != url else None,
                backend="curl_cffi", profile=profile, variant=username,
            )
        except asyncio.TimeoutError:
            return CheckResult(
                site=site.name, category=site.category, url=url, exists=None,
                reliability=site.reliability, error="timeout", reason="timeout",
                elapsed_ms=int((time.monotonic() - start) * 1000),
                backend="curl_cffi", variant=username,
            )
        except Exception as e:
            return CheckResult(
                site=site.name, category=site.category, url=url, exists=None,
                reliability=site.reliability, error=type(e).__name__,
                reason=type(e).__name__,
                elapsed_ms=int((time.monotonic() - start) * 1000),
                backend="curl_cffi", variant=username,
            )

    async def _check_curl(
        self,
        session,
        site: Site,
        username: str,
        sem: asyncio.Semaphore,
    ) -> CheckResult:
        """curl_cffi backend with cache + retry."""
        url = site.url_for(username)
        body_payload = site.body_for(username)
        method_name = site.request_method.upper()

        cached = self.cache.get(method_name, url, body_payload)
        if cached:
            return _result_from_cache(site, username, cached, "curl_cffi")

        async with sem:
            result = await self._curl_request(session, site, username)
            if self.retry_on_transient and _is_transient(result):
                await asyncio.sleep(self.retry_delay)
                retry = await self._curl_request(session, site, username)
                if retry.exists is True or retry.exists is False:
                    retry.reason = (retry.reason or "") + "+retry"
                    result = retry

        # Cache anything that wasn't a transient failure. A bot wall today
        # will be a bot wall in five minutes — caching saves the round trip.
        # Only timeouts and transport errors (where _is_transient is True
        # *and* we still don't have a verdict after retry) are excluded.
        if not _is_transient(result):
            self.cache.set(method_name, url, body_payload, _result_to_cache(result))
        return result

    # ------- driver --------------------------------------------------------

    async def run_many(self, variants: list[str]) -> list[tuple[str, list[CheckResult]]]:
        """Scan a list of variants against all configured sites in one pool.

        Previous behaviour ran variants sequentially: variant 1 finishes
        all 60 sites, then variant 2 starts. With a small number of slow
        sites that meant lots of idle bandwidth — variant 2 was waiting
        for variant 1's stragglers. This version pools every (variant,
        site) pair behind one semaphore, so the queue stays full and any
        one slow site doesn't block the next variant from starting.
        """
        if self.impersonate:
            curl_sites = [s for s in self.sites if s.needs_impersonation]
            aio_sites = [s for s in self.sites if not s.needs_impersonation]
        else:
            curl_sites = []
            aio_sites = list(self.sites)

        # Bigger semaphore for multi-variant runs — most of the work is
        # network-bound and per-host already throttled by TCPConnector,
        # so we can afford to be aggressive with the global cap.
        cap = self.concurrency * 2 if len(variants) > 1 else self.concurrency
        sem = asyncio.Semaphore(cap)

        connector = TCPConnector(
            limit=cap,
            limit_per_host=8,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        timeout = ClientTimeout(total=self.timeout + 5)

        aio_session_cm = ClientSession(connector=connector, timeout=timeout)
        curl_session_cm = CurlSession(impersonate="chrome") if curl_sites else None

        # Bucket so we can rebuild per-variant ordering at the end.
        all_tasks: list[tuple[str, asyncio.Task]] = []

        try:
            aio_session = await aio_session_cm.__aenter__()
            curl_session = (
                await curl_session_cm.__aenter__() if curl_session_cm else None
            )

            for v in variants:
                for s in aio_sites:
                    t = asyncio.create_task(
                        self._check_aiohttp(aio_session, s, v, sem)
                    )
                    all_tasks.append((v, t))
                for s in curl_sites:
                    t = asyncio.create_task(
                        self._check_curl(curl_session, s, v, sem)
                    )
                    all_tasks.append((v, t))

            await asyncio.gather(*(t for _, t in all_tasks))
        finally:
            await aio_session_cm.__aexit__(None, None, None)
            if curl_session_cm:
                await curl_session_cm.__aexit__(None, None, None)

        grouped: dict[str, list[CheckResult]] = {v: [] for v in variants}
        for v, t in all_tasks:
            grouped[v].append(t.result())
        return [(v, grouped[v]) for v in variants]

    async def run(self, username: str) -> list[CheckResult]:
        """Backwards-compatible single-variant scan."""
        out = await self.run_many([username])
        return out[0][1] if out else []


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
        url=site.url_for(username),
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


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

ANSI = {
    "green": "\033[32m",
    "red":   "\033[31m",
    "yellow":"\033[33m",
    "dim":   "\033[2m",
    "bold":  "\033[1m",
    "reset": "\033[0m",
}


def _c(color: bool, key: str) -> str:
    return ANSI[key] if color else ""


def _format_row(r: CheckResult, color: bool, show_variant: bool) -> str:
    """One line in the FOUND or UNKNOWN section.

    The variant tag is only printed when more than one variant ran — with
    `--exact`, it's just noise.
    """
    site = f"{_c(color,'bold')}{r.site:<14}{_c(color,'reset')}"
    target = r.final_url or r.url
    url_part = f"{_c(color,'dim')}{target}{_c(color,'reset')}"
    note_parts = []
    if r.status is not None:
        note_parts.append(f"http={r.status}")
    if r.error and r.error != r.reason:
        note_parts.append(r.error)
    if r.reason and r.reason != f"{r.status}":
        note_parts.append(r.reason)
    note = f" {_c(color,'dim')}({', '.join(note_parts)}){_c(color,'reset')}" if note_parts else ""
    tag = ""
    if show_variant and r.variant:
        tag = f"  {_c(color,'yellow')}[{r.variant}]{_c(color,'reset')}"
    return f"  {site} {url_part}{note}{tag}"


def _print_identity_summary(clusters, color: bool) -> None:
    """One-line-per-person view of the identity clusters.

    Only prints multi-member clusters — singletons are noise (every
    FOUND row would otherwise print as its own "person").
    """
    multi = [c for c in clusters if len(c.member_indexes) > 1]
    if not multi:
        return
    g, b, x, dim, accent = (
        _c(color, "green"), _c(color, "bold"), _c(color, "reset"),
        _c(color, "dim"), _c(color, "yellow"),
    )
    print(f"\n{b}[ IDENTITY ]{x}{b} {len(multi)}{x}  "
          f"{dim}(matched across multiple sites){x}")
    for i, c in enumerate(multi, 1):
        name = c.display_name or "(no name)"
        sites = ", ".join(c.sites)
        bits = []
        if c.total_followers is not None:
            bits.append(f"{_format_count(c.total_followers)} followers")
        if c.locations:
            bits.append("from " + c.locations[0])
        elif c.geo_hint and c.geo_hint.region:
            bits.append(f"likely {c.geo_hint.region}")
        if c.verified_on:
            bits.append("✓ verified")
        meta = " · ".join(bits)
        meta_part = f"  {dim}({meta}){x}" if meta else ""
        conf_part = f"{accent}({c.confidence:.2f}){x}"
        print(f"  {b}{name}{x} {conf_part} → {sites}{meta_part}")


def print_compact(
    grouped: list[tuple[str, list[CheckResult]]],
    elapsed: float,
    color: bool,
    found_only: bool,
) -> None:
    """One FOUND section, one UNKNOWN section, MISSING as a count.

    `grouped` is [(variant, [CheckResult, ...])] — but the user wants the
    output flattened across variants, with each row tagged by which variant
    produced it.
    """
    show_variant = len(grouped) > 1
    found: list[CheckResult] = []
    unknown: list[CheckResult] = []
    missing_count = 0
    for _, rs in grouped:
        for r in rs:
            if r.exists is True:
                found.append(r)
            elif r.exists is False:
                missing_count += 1
            else:
                unknown.append(r)

    sort_key = lambda r: (-r.reliability, r.site.lower(), r.variant or "")
    found.sort(key=sort_key)
    unknown.sort(key=sort_key)

    g, r_, y, b, x = (
        _c(color, "green"), _c(color, "red"), _c(color, "yellow"),
        _c(color, "bold"), _c(color, "reset"),
    )

    if found:
        print(f"\n{b}{g}[ FOUND ]{x}{b} {len(found)}{x}")
        for r in found:
            print(_format_row(r, color, show_variant))
    else:
        print(f"\n{b}{g}[ FOUND ]{x}{b} 0{x}")

    if not found_only:
        # Both [ ? ] and [MISSING] are shown as counts only — the per-row
        # detail is in the JSON/HTML/Markdown export. Keeps the terminal
        # readable on multi-variant runs where unknowns can hit the hundreds.
        print(f"\n{b}{y}[   ?   ]{x}{b} {len(unknown)}{x}  "
              f"{_c(color,'dim')}(use --export to see details){x}")
        print(f"{b}{r_}[MISSING]{x}{b} {missing_count}{x}")

    sys.stdout.flush()  # noqa: F841
    total = len(found) + len(unknown) + missing_count
    n_variants = len(grouped)
    suffix = f"across {n_variants} variant{'s' if n_variants != 1 else ''}"
    print(f"\n{_c(color,'dim')}{total} checks {suffix} in {elapsed:.1f}s{x}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _flatten(grouped: list[tuple[str, list[CheckResult]]]):
    found, unknown = [], []
    missing_count = 0
    for _, rs in grouped:
        for r in rs:
            if r.exists is True:
                found.append(r)
            elif r.exists is False:
                missing_count += 1
            else:
                unknown.append(r)
    sort_key = lambda r: (-r.reliability, r.site.lower(), r.variant or "")
    found.sort(key=sort_key)
    unknown.sort(key=sort_key)
    return found, unknown, missing_count


def _build_json_payload(grouped, raw, elapsed, clusters):
    found, unknown, missing_count = _flatten(grouped)
    return {
        "input": raw,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_seconds": round(elapsed, 2),
        "variants": [v for v, _ in grouped],
        "summary": {
            "found": len(found),
            "unknown": len(unknown),
            "missing": missing_count,
            "identities": len([c for c in clusters if len(c.member_indexes) > 1]),
        },
        "identities": [c.to_dict() for c in clusters],
        "found": [asdict(r) for r in found],
        "unknown": [asdict(r) for r in unknown],
    }


def export_json(grouped, raw, elapsed, path: Path, clusters=None) -> None:
    payload = _build_json_payload(grouped, raw, elapsed, clusters or [])
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def export_markdown(grouped, raw, elapsed, path: Path, clusters=None) -> None:
    found, unknown, missing_count = _flatten(grouped)
    clusters = clusters or []
    multi = [c for c in clusters if len(c.member_indexes) > 1]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Phantom report — `{raw}`",
        "",
        f"_Generated {ts} — {len(grouped)} variant(s) in {elapsed:.1f}s_",
        "",
        f"- **Found**: {len(found)}",
        f"- **Identified persons**: {len(multi)}",
        f"- **Unknown**: {len(unknown)}",
        f"- **Missing**: {missing_count}",
        "",
    ]
    if multi:
        lines += [f"## Identified persons ({len(multi)})", ""]
        for i, c in enumerate(multi, 1):
            name = c.display_name or "(no name)"
            lines.append(f"### Person {i}: {name}  *(confidence {c.confidence})*")
            lines.append("")
            lines.append(f"- **Sites**: {', '.join(c.sites)}")
            if c.locations:
                lines.append(f"- **Locations**: {', '.join(c.locations)}")
            if c.geo_hint and c.geo_hint.region and c.geo_hint.region not in c.locations:
                lines.append(
                    f"- **Likely region**: {c.geo_hint.region} "
                    f"_(conf {c.geo_hint.confidence}, {'; '.join(c.geo_hint.signals)})_"
                )
            if c.joined_oldest:
                lines.append(f"- **Active since**: {c.joined_oldest}")
            if c.total_followers is not None:
                lines.append(f"- **Followers (total)**: {c.total_followers:,}")
            if c.total_posts is not None:
                lines.append(f"- **Posts (total)**: {c.total_posts:,}")
            if c.verified_on:
                lines.append(f"- **Verified on**: {', '.join(c.verified_on)}")
            if c.rationale:
                lines.append(f"- **Reason**: {'; '.join(c.rationale)}")
            lines.append("")
    lines += [
        f"## Found ({len(found)})",
        "",
    ]
    if found:
        for r in found:
            target = r.final_url or r.url
            tag = f" — `{r.variant}`" if r.variant else ""
            lines.append(f"- [{r.site}]({target}){tag}")
    else:
        lines.append("_None._")
    lines += ["", f"## Unknown ({len(unknown)})", ""]
    if unknown:
        for r in unknown:
            target = r.final_url or r.url
            note = f" ({r.reason})" if r.reason else ""
            tag = f" — `{r.variant}`" if r.variant else ""
            lines.append(f"- [{r.site}]({target}){note}{tag}")
    else:
        lines.append("_None._")
    lines += ["", f"## Missing", "", f"{missing_count} sites cleanly returned not-found."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Phantom — {raw_html}</title>
<style>
  :root {{
    --bg: #0b0d12; --card: #141823; --card2: #1a1f2c; --line: #232836;
    --muted: #8a93a6; --text: #e7eaf3; --green: #5dd39e; --yellow: #f5c060;
    --red: #f25c54; --accent: #6aa6ff;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                   "Helvetica Neue", Arial, sans-serif;
  }}
  header {{
    padding: 32px 40px 20px; border-bottom: 1px solid var(--line);
  }}
  header h1 {{ margin: 0 0 6px; font-size: 22px; font-weight: 600; }}
  header .meta {{ color: var(--muted); font-size: 13px; }}
  header code {{
    background: var(--card); padding: 2px 8px; border-radius: 4px;
    color: var(--accent); font-size: 14px;
  }}
  .stats {{
    display: flex; gap: 16px; padding: 14px 40px;
    border-bottom: 1px solid var(--line);
    position: sticky; top: 0; z-index: 10;
    background: var(--bg);
    backdrop-filter: blur(8px);
  }}
  .stat {{
    background: var(--card); border: 1px solid var(--line);
    padding: 10px 16px; border-radius: 8px; min-width: 100px;
  }}
  .stat .n {{ font-size: 22px; font-weight: 600; }}
  .stat .label {{ font-size: 11px; color: var(--muted); text-transform: uppercase;
                  letter-spacing: 0.5px; }}
  .stat.found .n {{ color: var(--green); }}
  .stat.unknown .n {{ color: var(--yellow); }}
  .stat.missing .n {{ color: var(--red); }}
  .stat.identity .n {{ color: var(--accent); }}
  .id-card {{
    background: var(--card); border: 1px solid var(--line); border-radius: 14px;
    padding: 22px; margin-bottom: 16px; display: flex; gap: 24px;
    align-items: flex-start;
  }}
  .id-photos {{
    display: grid; grid-template-columns: repeat(2, 64px); gap: 6px;
    flex-shrink: 0;
  }}
  .photo-thumb {{
    width: 64px; height: 64px; border-radius: 10px;
    background: #0a0c11 center/cover no-repeat;
    border: 1px solid var(--line);
  }}
  .photo-thumb.empty {{
    display: flex; align-items: center; justify-content: center;
    color: var(--muted); font-size: 22px;
  }}
  .id-body {{ flex: 1; min-width: 0; }}
  .id-head {{ display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
  .id-head h3 {{ margin: 0; font-size: 19px; font-weight: 600; }}
  .conf {{ font-size: 11px; padding: 4px 10px; border-radius: 999px; font-weight: 700; }}
  .conf.high {{ background: rgba(93,211,158,.16); color: var(--green); }}
  .conf.med  {{ background: rgba(245,192,96,.16); color: var(--yellow); }}
  .conf.low  {{ background: rgba(138,147,166,.16); color: var(--muted); }}
  .id-stats {{
    display: flex; gap: 12px; margin-top: 14px; padding: 12px 0;
    border-top: 1px solid var(--line); border-bottom: 1px solid var(--line);
  }}
  .id-stats .col {{ flex: 1; }}
  .id-stats .n {{ font-size: 18px; font-weight: 700; }}
  .id-stats .l {{ font-size: 11px; color: var(--muted);
                  text-transform: uppercase; letter-spacing: 0.5px; }}
  .id-facts {{ list-style: none; padding: 0; margin: 12px 0 0;
               font-size: 13px; color: var(--muted); }}
  .id-facts li {{ margin: 4px 0; }}
  .id-facts b {{ color: var(--text); font-weight: 500; }}
  .id-rationale {{ margin-top: 10px; font-size: 11.5px;
                   color: var(--muted); font-style: italic; }}
  section {{ padding: 24px 40px; }}
  section h2 {{ margin: 0 0 14px; font-size: 16px; font-weight: 600; }}
  .grid {{
    display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 16px;
  }}
  .card {{
    background: var(--card); border: 1px solid var(--line); border-radius: 12px;
    overflow: hidden; display: flex; flex-direction: column;
    transition: border-color 120ms ease, transform 120ms ease;
  }}
  .card:hover {{ border-color: var(--accent); transform: translateY(-2px); }}
  .card a {{ color: inherit; text-decoration: none; }}
  .card-head {{
    position: relative;
    aspect-ratio: 1 / 1; background: #0a0c11 center/cover no-repeat;
    display: flex; align-items: flex-end; justify-content: flex-start;
  }}
  .card-head .badge {{
    position: absolute; top: 10px; right: 10px;
    background: rgba(11,13,18,.78); border: 1px solid rgba(255,255,255,.12);
    color: var(--text); padding: 4px 10px; border-radius: 999px;
    font-size: 11px; font-weight: 600; backdrop-filter: blur(6px);
  }}
  .card-head .verified {{
    position: absolute; top: 10px; left: 10px;
    background: rgba(106,166,255,.92); color: #0b0d12;
    padding: 3px 8px; border-radius: 999px;
    font-size: 11px; font-weight: 700;
  }}
  .card-head .private {{
    position: absolute; bottom: 10px; right: 10px;
    background: rgba(245,192,96,.92); color: #0b0d12;
    padding: 3px 8px; border-radius: 999px;
    font-size: 11px; font-weight: 700;
  }}
  .card-head .initial {{
    width: 100%; height: 100%; display: flex; align-items: center;
    justify-content: center; color: var(--muted); font-size: 56px; font-weight: 700;
  }}
  .card-body {{ padding: 14px 16px 16px; }}
  .card-body .name {{ font-size: 16px; font-weight: 600; line-height: 1.25;
                      word-break: break-word; }}
  .card-body .handle {{ color: var(--muted); font-size: 12px; margin-top: 2px; }}
  .card-body .bio {{ color: var(--text); font-size: 12.5px; margin-top: 10px;
                     opacity: 0.85; display: -webkit-box; -webkit-line-clamp: 3;
                     -webkit-box-orient: vertical; overflow: hidden;
                     word-break: break-word; }}
  .card-body .meta-row {{
    display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px;
    font-size: 11px; color: var(--muted);
  }}
  .card-body .meta-row span {{
    background: var(--card2); border: 1px solid var(--line);
    padding: 3px 8px; border-radius: 999px;
  }}
  .card-body .repo-row {{
    margin-top: 10px; font-size: 11.5px; color: var(--muted);
    display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
  }}
  .card-body .repo-row .repo {{
    background: rgba(106,166,255,.12); color: var(--accent);
    padding: 2px 8px; border-radius: 6px; font-family: ui-monospace, monospace;
  }}
  .alt-sites-row {{
    display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
    margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--line);
  }}
  .alt-sites-row .multi-badge {{
    font-size: 11px; font-weight: 600; padding: 3px 9px; border-radius: 999px;
    background: rgba(93,211,158,.16); color: var(--green);
  }}
  .alt-sites-row .alt-site {{
    font-size: 11px; padding: 3px 9px; border-radius: 999px;
    background: var(--card2); color: var(--text); text-decoration: none;
    border: 1px solid var(--line);
    transition: border-color 120ms ease, background 120ms ease;
  }}
  .alt-sites-row .alt-site:hover {{ border-color: var(--accent); background: rgba(106,166,255,.08); }}
  @media (max-width: 720px) {{
    header, .stats, section, footer {{ padding-left: 18px; padding-right: 18px; }}
    .stats {{ flex-wrap: wrap; }}
    .id-card {{ flex-direction: column; align-items: stretch; gap: 14px; }}
    .id-photos {{ grid-template-columns: repeat(4, 56px); }}
    .photo-thumb {{ width: 56px; height: 56px; }}
    .grid {{ grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 12px; }}
    .card-head {{ aspect-ratio: 4 / 3; }}
  }}
  .card-body .stats-row {{
    display: flex; gap: 12px; margin-top: 12px; padding-top: 12px;
    border-top: 1px solid var(--line);
  }}
  .card-body .stats-row .col {{ flex: 1; min-width: 0; }}
  .card-body .stats-row .n {{ font-weight: 600; font-size: 14px; }}
  .card-body .stats-row .l {{ font-size: 10.5px; color: var(--muted);
                              text-transform: uppercase; letter-spacing: 0.4px; }}
  .card-body .footer {{
    display: flex; align-items: center; justify-content: space-between;
    margin-top: 12px; gap: 8px;
  }}
  .card-body .variant {{
    display: inline-block; font-size: 11px;
    background: rgba(106,166,255,.12); color: var(--accent);
    padding: 3px 9px; border-radius: 999px;
  }}
  .card-body .open {{
    font-size: 11px; color: var(--accent); text-decoration: none;
    border: 1px solid rgba(106,166,255,.3); padding: 4px 10px;
    border-radius: 6px; transition: background 120ms ease;
  }}
  .card-body .open:hover {{ background: rgba(106,166,255,.12); }}
  .table {{
    width: 100%; border-collapse: collapse;
    background: var(--card); border-radius: 10px; overflow: hidden;
    border: 1px solid var(--line);
  }}
  .table th, .table td {{ padding: 10px 14px; text-align: left;
                          border-bottom: 1px solid var(--line); }}
  .table th {{ font-weight: 600; font-size: 12px; color: var(--muted);
               text-transform: uppercase; letter-spacing: 0.5px; }}
  .table tr:last-child td {{ border-bottom: 0; }}
  .table a {{ color: var(--accent); text-decoration: none; }}
  .table a:hover {{ text-decoration: underline; }}
  .pill {{ font-size: 11px; padding: 2px 8px; border-radius: 999px;
           background: rgba(245,192,96,.12); color: var(--yellow); }}
  footer {{ color: var(--muted); padding: 24px 40px; font-size: 12px;
            border-top: 1px solid var(--line); }}
</style>
</head>
<body>
<header>
  <h1>Phantom report</h1>
  <div class="meta">Searching for <code>{raw_html}</code> · {n_variants} variant(s) · {elapsed:.1f}s · {generated_at}</div>
</header>

<div class="stats">
  <div class="stat identity"><div class="n">{n_identities}</div><div class="label">Identified</div></div>
  <div class="stat found"><div class="n">{n_found}</div><div class="label">Found</div></div>
  <div class="stat unknown"><div class="n">{n_unknown}</div><div class="label">Unknown</div></div>
  <div class="stat missing"><div class="n">{n_missing}</div><div class="label">Missing</div></div>
</div>

{identity_section}

<section>
  <h2>Found ({n_found})</h2>
  {found_block}
</section>

<section>
  <h2>Unknown ({n_unknown})</h2>
  {unknown_block}
</section>

<footer>
  Phantom · {n_missing} sites cleanly returned not-found · variants tried:
  {variants_html}
</footer>
</body>
</html>
"""


def _format_count(n) -> str:
    """Human-friendly counts: 12345 -> '12.3K'. Used in profile cards."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if abs(n) >= 1_000_000_000:
        return f"{n/1_000_000_000:.1f}B".rstrip("0").rstrip(".")
    if abs(n) >= 1_000_000:
        return f"{n/1_000_000:.1f}M".rstrip("0").rstrip(".")
    if abs(n) >= 1_000:
        return f"{n/1_000:.1f}K".rstrip("0").rstrip(".")
    return str(n)


def _format_joined(s) -> Optional[str]:
    """Try to render a joined/created date as 'Mar 2024'. Best-effort only."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d.strftime("%b %Y")
    except Exception:
        try:
            d = datetime.strptime(str(s), "%a %b %d %H:%M:%S %z %Y")
            return d.strftime("%b %Y")
        except Exception:
            return None


def _html_grouped_card(group) -> str:
    """Render one card representing N results that share a person.

    A solo group falls through to the original `_html_card`. A merged
    group renders the richest member as the primary card with a "Found
    on N sites" badge and a strip of small site links underneath.
    """
    if len(group) == 1:
        return _html_card(group[0])

    # Pick the row with the most populated profile dict as the "primary".
    # Tie-broken by reliability so the most-trusted source wins.
    primary = max(
        group,
        key=lambda r: (len(r.profile or {}), r.reliability),
    )
    others = [r for r in group if r is not primary]
    sites_chips = "".join(
        f'<a class="alt-site" href="{html.escape(r.final_url or r.url, quote=True)}" '
        f'target="_blank" rel="noopener">{html.escape(r.site)}</a>'
        for r in [primary] + others
    )
    multi_badge = f'<span class="multi-badge">found on {len(group)} sites</span>'

    base = _html_card(primary)
    # Splice the multi-site row into the card-body before the footer.
    sites_block = (
        f'<div class="alt-sites-row">{multi_badge}{sites_chips}</div>'
    )
    base = base.replace(
        '<div class="footer">',
        f'{sites_block}<div class="footer">',
        1,
    )
    return base


def _html_card(r: CheckResult) -> str:
    """Render one FOUND profile as an info-rich card.

    Layout (top → bottom):
      [photo with site badge / verified / private overlays]
      display name
      @username (variant)
      bio (3 lines max)
      location · joined chips
      followers · following · posts · hearts row
      [variant pill]                     [open profile →]
    """
    p = r.profile or {}
    target = r.final_url or r.url
    site_badge = html.escape(r.site)

    # --- header (photo) ---
    photo_url = p.get("photo")
    initial = (r.site[:1] or "?").upper()
    if photo_url:
        head = (
            f'<div class="card-head" '
            f'style="background-image:url(\'{html.escape(photo_url, quote=True)}\')">'
        )
    else:
        head = '<div class="card-head"><div class="initial">' + html.escape(initial) + '</div>'
    head += f'<span class="badge">{site_badge}</span>'
    if p.get("verified"):
        head += '<span class="verified">VERIFIED</span>'
    if p.get("private"):
        head += '<span class="private">PRIVATE</span>'
    head += "</div>"

    # --- body ---
    display_name = p.get("display_name") or r.variant or r.site
    handle_bits = []
    if r.variant:
        handle_bits.append(f"@{html.escape(r.variant)}")
    handle_bits.append(html.escape(r.site))
    handle = '<div class="handle">' + " · ".join(handle_bits) + '</div>'

    bio_html = ""
    if p.get("bio"):
        bio_html = f'<div class="bio">{html.escape(p["bio"])}</div>'

    chips = []
    if p.get("location"):
        chips.append(f'📍 {html.escape(p["location"])}')
    if p.get("company"):
        chips.append(f'🏢 {html.escape(p["company"])}')
    joined = _format_joined(p.get("joined")) or p.get("joined")
    if joined:
        chips.append(f'📅 {html.escape(str(joined))}')
    if p.get("website"):
        chips.append(f'🔗 {html.escape(p["website"])}')
    if p.get("twitter_handle"):
        chips.append(f'🐦 @{html.escape(p["twitter_handle"])}')
    if p.get("steam_level") is not None:
        chips.append(f'🎮 lvl {p["steam_level"]}')
    if p.get("rating") is not None:
        chips.append(f'♟️ {p["rating"]}')
    if p.get("karma") is not None:
        chips.append(f'⭐ {_format_count(p["karma"])} karma')
    meta_row = ""
    if chips:
        meta_row = '<div class="meta-row">' + "".join(
            f"<span>{c}</span>" for c in chips
        ) + "</div>"

    # Pinned repos badge row (GitHub).
    pinned_html = ""
    if p.get("pinned_repos"):
        pins = "".join(
            f'<span class="repo">{html.escape(r)}</span>'
            for r in p["pinned_repos"][:6]
        )
        pinned_html = f'<div class="repo-row">📂 {pins}</div>'

    stats = []
    if "followers" in p:
        stats.append(("followers", p["followers"]))
    if "following" in p:
        stats.append(("following", p["following"]))
    if "posts" in p:
        stats.append(("posts", p["posts"]))
    if "hearts" in p:
        stats.append(("hearts", p["hearts"]))
    stats_row = ""
    if stats:
        cells = "".join(
            f'<div class="col"><div class="n">{html.escape(_format_count(v))}</div>'
            f'<div class="l">{html.escape(label)}</div></div>'
            for label, v in stats
        )
        stats_row = f'<div class="stats-row">{cells}</div>'

    variant_pill = (
        f'<span class="variant">{html.escape(r.variant)}</span>' if r.variant else ""
    )
    open_link = (
        f'<a class="open" href="{html.escape(target, quote=True)}" '
        f'target="_blank" rel="noopener">Open profile →</a>'
    )

    body = (
        '<div class="card-body">'
        f'<div class="name">{html.escape(display_name)}</div>'
        f"{handle}"
        f"{bio_html}"
        f"{meta_row}"
        f"{pinned_html}"
        f"{stats_row}"
        f'<div class="footer">{variant_pill}{open_link}</div>'
        "</div>"
    )

    return f'<div class="card">{head}{body}</div>'


def _html_unknown_row(r: CheckResult) -> str:
    target = r.final_url or r.url
    return (
        '<tr><td>{site}</td>'
        '<td><a href="{href}" target="_blank" rel="noopener">{url}</a></td>'
        '<td><span class="pill">{reason}</span></td>'
        '<td>{variant}</td></tr>'
    ).format(
        site=html.escape(r.site),
        href=html.escape(target, quote=True),
        url=html.escape(target),
        reason=html.escape(r.reason or "unknown"),
        variant=html.escape(r.variant or ""),
    )


def _html_identity_card(c, idx: int) -> str:
    """One "Identified person" panel. Built from an IdentityCluster."""
    name = c.display_name or f"Person {idx}"
    badge = ""
    if c.confidence >= 0.85:
        badge = '<span class="conf high">HIGH CONFIDENCE</span>'
    elif c.confidence >= 0.6:
        badge = '<span class="conf med">LIKELY MATCH</span>'
    else:
        badge = '<span class="conf low">CANDIDATE</span>'

    photos_html = "".join(
        f'<div class="photo-thumb" style="background-image:url(\'{html.escape(p, quote=True)}\')"></div>'
        for p in c.photos
    ) or '<div class="photo-thumb empty">?</div>'

    stats_pairs: list[tuple[str, str]] = []
    if c.total_followers is not None:
        stats_pairs.append(("Followers", _format_count(c.total_followers)))
    if c.total_following is not None:
        stats_pairs.append(("Following", _format_count(c.total_following)))
    if c.total_posts is not None:
        stats_pairs.append(("Posts", _format_count(c.total_posts)))

    stats_html = ""
    if stats_pairs:
        cells = "".join(
            f'<div class="col"><div class="n">{html.escape(v)}</div>'
            f'<div class="l">{html.escape(label)}</div></div>'
            for label, v in stats_pairs
        )
        stats_html = f'<div class="id-stats">{cells}</div>'

    bits: list[str] = []
    if c.locations:
        bits.append(
            "Location: " + ", ".join(html.escape(x) for x in c.locations)
        )
    geo = c.geo_hint
    if geo and geo.region and geo.region not in c.locations:
        sig = ", ".join(html.escape(s) for s in (geo.signals or []))
        bits.append(
            f"Likely region: <b>{html.escape(geo.region)}</b> "
            f"<span style='color:var(--muted)'>({sig}, conf {geo.confidence})</span>"
        )
    joined = _format_joined(c.joined_oldest)
    if joined:
        bits.append(f"Active since {html.escape(joined)}")
    if c.verified_on:
        bits.append("Verified on " + ", ".join(html.escape(s) for s in c.verified_on))
    if c.private_on:
        bits.append("Private on " + ", ".join(html.escape(s) for s in c.private_on))
    if c.variants:
        vstr = ", ".join(html.escape(v) for v in c.variants[:6])
        if len(c.variants) > 6:
            vstr += f", … (+{len(c.variants) - 6})"
        bits.append("Variants: " + vstr)
    bits.append(
        "Found on: " + ", ".join(f"<b>{html.escape(s)}</b>" for s in c.sites)
    )
    facts_html = (
        '<ul class="id-facts">'
        + "".join(f"<li>{b}</li>" for b in bits)
        + "</ul>"
    )

    rationale_html = ""
    if c.rationale:
        rationale_html = (
            '<div class="id-rationale">' + " · ".join(
                html.escape(r) for r in c.rationale
            ) + "</div>"
        )

    return (
        '<div class="id-card">'
        f'<div class="id-photos">{photos_html}</div>'
        '<div class="id-body">'
        f'<div class="id-head"><h3>{html.escape(name)}</h3>{badge}</div>'
        f"{stats_html}"
        f"{facts_html}"
        f"{rationale_html}"
        "</div></div>"
    )


def _photo_dedup_groups(found: list, clusters) -> list[list]:
    """Group FOUND results that share a perceptually-matched photo.

    `clusters` are IdentityCluster objects whose `member_indexes` point
    into `found`. We re-use those: any cluster of size ≥ 2 produces one
    grouped card; singletons render as individual cards as before.
    """
    if not clusters:
        return [[r] for r in found]

    used: set[int] = set()
    groups: list[list] = []
    for c in clusters:
        idxs = [i for i in c.member_indexes if 0 <= i < len(found)]
        if len(idxs) > 1:
            groups.append([found[i] for i in idxs])
            used.update(idxs)
    for i, r in enumerate(found):
        if i not in used:
            groups.append([r])
    return groups


def export_html(grouped, raw, elapsed, path: Path, clusters=None) -> None:
    found, unknown, missing_count = _flatten(grouped)
    clusters = clusters or []
    multi = [c for c in clusters if len(c.member_indexes) > 1]

    # Identity correlation runs on a flattened FOUND list. The order of
    # rows fed into build_identities was the same flatten() call as the
    # one we use here, so member_indexes line up.
    if multi:
        identity_block = "".join(
            _html_identity_card(c, i + 1) for i, c in enumerate(multi)
        )
        identity_section = (
            f'<section><h2>Identified person'
            f'{"s" if len(multi) != 1 else ""} ({len(multi)})</h2>'
            f'{identity_block}</section>'
        )
    else:
        identity_section = ""

    if found:
        groups = _photo_dedup_groups(found, clusters)
        found_block = '<div class="grid">' + "".join(
            _html_grouped_card(g) for g in groups
        ) + "</div>"
    else:
        found_block = '<p style="color:var(--muted)">No accounts found.</p>'

    if unknown:
        rows = "".join(_html_unknown_row(r) for r in unknown)
        unknown_block = (
            '<table class="table"><thead><tr>'
            "<th>Site</th><th>URL</th><th>Reason</th><th>Variant</th>"
            "</tr></thead><tbody>" + rows + "</tbody></table>"
        )
    else:
        unknown_block = '<p style="color:var(--muted)">No unknowns.</p>'

    variants_html = ", ".join(
        f"<code>{html.escape(v)}</code>" for v, _ in grouped
    )
    page = _HTML_TEMPLATE.format(
        raw_html=html.escape(raw),
        n_variants=len(grouped),
        elapsed=elapsed,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        n_found=len(found),
        n_identities=len(multi),
        n_unknown=len(unknown),
        n_missing=missing_count,
        identity_section=identity_section,
        found_block=found_block,
        unknown_block=unknown_block,
        variants_html=variants_html,
    )
    path.write_text(page, encoding="utf-8")


_FORMAT_ALIASES = {
    "html": ".html", "htm": ".html",
    "json": ".json",
    "md": ".md", "markdown": ".md", "txt": ".md",
}

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(raw: str) -> str:
    """Turn the raw input into a filesystem-safe slug for default export names.

    Multi-word inputs are joined with `_`; anything that isn't word-char,
    `.`, or `-` becomes `_`. Empty result falls back to "phantom".
    """
    slug = _SAFE_NAME_RE.sub("_", raw.strip()).strip("._-")
    return slug or "phantom"


def resolve_export_path(spec: str, raw: str) -> Path:
    """Decide where the export goes.

    Behaviour:
      - "html" / "json" / "md"   → `<input>_report.<ext>` in the cwd
      - "report.html"            → use as-is (has an extension)
      - "/tmp/out.html"          → use as-is (full path)
      - "reports/"               → directory, becomes
                                    "reports/<input>_report.json"
    """
    p = Path(spec).expanduser()
    if spec.endswith("/") or p.is_dir():
        return p / f"{_safe_filename(raw)}_report.json"
    if spec.lower() in _FORMAT_ALIASES:
        ext = _FORMAT_ALIASES[spec.lower()]
        return Path(f"{_safe_filename(raw)}_report{ext}")
    if p.suffix:
        return p
    # No extension and not a known format alias → treat as a basename and
    # default to JSON so the user gets a usable file.
    return p.with_suffix(".json")


def export_report(
    grouped: list[tuple[str, list[CheckResult]]],
    raw: str,
    elapsed: float,
    path: Path,
    clusters=None,
) -> None:
    """Dispatch by extension. Defaults to JSON if the suffix is unrecognised."""
    suffix = path.suffix.lower()
    if suffix == ".html" or suffix == ".htm":
        export_html(grouped, raw, elapsed, path, clusters)
    elif suffix == ".md" or suffix == ".markdown" or suffix == ".txt":
        export_markdown(grouped, raw, elapsed, path, clusters)
    else:
        export_json(grouped, raw, elapsed, path, clusters)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_HELP_DESCRIPTION = """\
Phantom — find a username across 60 hand-picked platforms with high accuracy
and pull whatever public profile data each platform exposes (display name,
bio, photo, follower / following / post counts, location, joined date,
verified flag).

A single input is automatically expanded to dozens of plausible variants
(separator insertion, smart word splits, number suffixes, prefix/suffix
combinations, first/last name permutations) and every variant is checked
against every site. Each FOUND result is tagged with the variant that
produced it.
"""

_HELP_EPILOG = """\
Examples:
  phantom <username>                       # full run with the variant engine
  phantom <username> --exact               # check the input verbatim, no variants
  phantom "<first> <last>"                 # name mode → firstlast, first.last, flast, ...
  phantom <username> --list-variants       # preview the variants and exit
  phantom <username> --max-variants 5      # cap at the first 5 variants
  phantom <username> --found-only          # hide the [ ? ] / [MISSING] sections
  phantom <username> --category social     # restrict to one category
  phantom <username> --min-reliability 85  # only the most trustworthy sites
  phantom <username> --export html         # auto-name to <username>_report.html
  phantom <username> --export json         # auto-name to <username>_report.json
  phantom <username> --export md           # auto-name to <username>_report.md
  phantom <username> --export reports/     # write to reports/<username>_report.json
  phantom <username> --export out.html     # write exactly to out.html
  phantom <username> --json                # JSON to stdout instead of file
  phantom <username> --no-cache            # ignore the on-disk cache for this run
  phantom <username> --no-retry            # don't retry transient failures
  phantom <username> --watch --quiet       # snapshot + diff (cron-friendly)
  phantom <username> --no-identity         # skip photo-hash cross-site merging

Output:
  [ FOUND ]  one row per hit, with the variant that found it
  [   ?   ]  count only — full detail is in the export
  [MISSING]  count only

Variants:
  - one word  → separators, smart splits, numbers, prefixes, suffixes
  - two words → firstlast, first.last, flast, lastfirst, ...

Categories:
  dev, social, gaming, media, forum, other

Notes:
  - The tool runs entirely on public data — no auth, no API keys, no cookies.
  - Bot-walled sites (LinkedIn, Reddit on some IPs) return [   ?   ].
  - For the most accurate results, use the default variant engine.
  - To make this command available everywhere, from inside the cloned dir:
      sudo ln -s "$PWD/phantom" /usr/local/bin/phantom
"""


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="phantom",
        description=_HELP_DESCRIPTION,
        epilog=_HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "username", nargs="+",
        help="username (or first + last name) to look up. Multi-word inputs "
             "are joined with a space and treated as a name.",
    )
    p.add_argument(
        "--sites", default=str(Path(__file__).with_name("sites.json")),
        help="path to sites.json (default: alongside this script)",
    )
    p.add_argument("--concurrency", type=int, default=25)
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument(
        "--min-reliability", type=int, default=0,
        help="skip sites with a reliability score below this threshold",
    )
    p.add_argument(
        "--category", action="append", default=None,
        help="restrict to a category (repeatable: dev, social, gaming, media, forum, other)",
    )
    p.add_argument(
        "--no-impersonate", action="store_true",
        help="disable curl_cffi browser impersonation, even if installed "
             "(sites flagged with tls_fingerprint will likely fail)",
    )
    p.add_argument(
        "--no-retry", action="store_true",
        help="disable the single-shot retry on transient failures (timeouts, "
             "5xx, transport errors). Off by default — retries on.",
    )
    p.add_argument(
        "--no-cache", action="store_true",
        help="disable the on-disk response cache "
             "(~/.cache/phantom/cache.json, 1h TTL). Cache is on by default.",
    )
    p.add_argument(
        "--no-identity", action="store_true",
        help="skip the identity-correlation step (downloading + hashing "
             "profile photos to merge cross-platform accounts).",
    )
    p.add_argument(
        "--watch", action="store_true",
        help="snapshot mode: persist this scan's FOUND set and diff against "
             "the previous snapshot for the same input. Designed for cron "
             "(see --quiet). Snapshots live in ~/.cache/phantom/snapshots/.",
    )
    p.add_argument(
        "--quiet", action="store_true",
        help="suppress the normal scan output. Combined with --watch, only "
             "the diff is printed (or nothing if there are no changes).",
    )
    p.add_argument(
        "--exact", action="store_true",
        help="check only the exact input — skip the variant generator",
    )
    p.add_argument(
        "--max-variants", type=int, default=0,
        help="cap the number of variants checked (0 = no cap)",
    )
    p.add_argument(
        "--list-variants", action="store_true",
        help="print the generated variants and exit (no network calls)",
    )
    p.add_argument("--found-only", action="store_true", help="only print hits")
    p.add_argument("--json", dest="as_json", action="store_true", help="emit JSON results to stdout")
    p.add_argument(
        "--export", metavar="FILE_OR_FORMAT",
        help="write a structured report. Pass a format ('html', 'json', 'md') "
             "and the file is auto-named '<input>_report.<ext>' in the cwd. "
             "Pass a path with extension (e.g. 'reports/out.html') to write "
             "exactly there.",
    )
    p.add_argument("--no-color", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    raw = " ".join(args.username).strip()
    if not raw:
        print("error: empty username", file=sys.stderr)
        return 2

    color = sys.stdout.isatty() and not args.no_color

    if args.exact:
        if not USERNAME_PATTERN.match(raw):
            print(f"error: invalid username '{raw}' (use without --exact to enable variants)", file=sys.stderr)
            return 2
        variants = [raw]
    else:
        variants = generate_variants(raw)
        if not variants:
            print(f"error: no valid variants generated from '{raw}'", file=sys.stderr)
            return 2

    if args.max_variants > 0:
        variants = variants[: args.max_variants]

    if args.list_variants:
        for v in variants:
            print(v)
        return 0

    sites_path = Path(args.sites)
    if not sites_path.is_file():
        print(f"error: sites file not found: {sites_path}", file=sys.stderr)
        return 2

    sites = load_sites(sites_path)
    sites = [s for s in sites if s.reliability >= args.min_reliability]
    if args.category:
        wanted = {c.lower() for c in args.category}
        sites = [s for s in sites if s.category.lower() in wanted]
    if not sites:
        print("error: no sites match the given filters", file=sys.stderr)
        return 2

    impersonate = not args.no_impersonate
    if any(s.needs_impersonation for s in sites) and not HAS_CURL_CFFI:
        print(
            "warning: curl_cffi is not installed; sites flagged with "
            "tls_fingerprint will likely return [   ?   ]. Install with: "
            "pip install curl_cffi",
            file=sys.stderr,
        )

    if len(variants) == 1:
        print(
            f"Phantom: searching for '{variants[0]}' across {len(sites)} sites...",
            file=sys.stderr,
        )
    else:
        print(
            f"Phantom: trying {len(variants)} variants of '{raw}' across "
            f"{len(sites)} sites = {len(variants) * len(sites)} requests "
            f"(use --exact to skip variants)",
            file=sys.stderr,
        )

    cache = ResponseCache(enabled=not args.no_cache)
    phantom = Phantom(
        sites,
        concurrency=args.concurrency,
        timeout=args.timeout,
        impersonate=impersonate,
        retry_on_transient=not args.no_retry,
        cache=cache,
    )

    async def _scan_and_correlate():
        results = await phantom.run_many(variants)
        # Flatten FOUND results (across all variants) and build identity
        # clusters. Skip if user disabled it — image fetches add a few
        # seconds and aren't always wanted.
        if args.no_identity:
            return results, []
        found_dicts: list[dict] = []
        for _, rs in results:
            for r in rs:
                if r.exists is True:
                    found_dicts.append(asdict(r))
        clusters = await build_identities(found_dicts)
        return results, clusters

    start = time.monotonic()
    grouped, clusters = asyncio.run(_scan_and_correlate())
    elapsed = time.monotonic() - start
    cache.save()

    if args.as_json:
        payload = _build_json_payload(grouped, raw, elapsed, clusters)
        print(json.dumps(payload, indent=2))
    elif not args.quiet:
        print_compact(grouped, elapsed, color, args.found_only)
        if clusters and not args.found_only:
            _print_identity_summary(clusters, color)

    # --- Watch mode: snapshot + diff -------------------------------------
    if args.watch:
        found_for_snapshot, _, _ = _flatten(grouped)
        snap = Snapshot.from_results(
            raw, [asdict(r) for r in found_for_snapshot]
        )
        history = load_history(raw)
        prev = history[-1] if history else None
        d = compute_diff(prev, snap)
        save_snapshot(snap)
        # Quiet mode: only output the diff (or nothing). Cron-friendly.
        if args.quiet:
            if not d.is_empty():
                print(render_diff_terminal(d, color))
        else:
            print()
            print(render_diff_terminal(d, color))

    if args.export:
        export_path = resolve_export_path(args.export, raw)
        if export_path.parent and not export_path.parent.exists():
            export_path.parent.mkdir(parents=True, exist_ok=True)
        export_report(grouped, raw, elapsed, export_path, clusters)
        print(
            f"{_c(color,'dim')}Report written to {export_path}{_c(color,'reset')}",
            file=sys.stderr,
        )

    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
