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
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector

import apis
from enrich import extract_profile
from identity import _normalise_country, build_overall_and_clusters
import photo_deep
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

        # Cache only definitive verdicts (FOUND / MISSING). UNKNOWN means
        # we couldn't decide — usually a transient SPA-shell, login wall,
        # or rate-limit response — and locking that in for an hour blocks
        # legitimate retries. Transient transport errors are also skipped.
        if not _is_transient(result) and result.exists is not None:
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

        # Cache only definitive verdicts (FOUND / MISSING). UNKNOWN means
        # we couldn't decide — usually a transient SPA-shell, login wall,
        # or rate-limit response — and locking that in for an hour blocks
        # legitimate retries. Transient transport errors are also skipped.
        if not _is_transient(result) and result.exists is not None:
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
    # Show the canonical URL (the one we requested) — that's what reliably
    # opens the profile when clicked. Some sites (Instagram) drop the www
    # subdomain on redirect, and the redirected form trips their bot
    # detection when opened cold from a browser.
    target = r.url
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


def _print_identity_summary(overall, clusters, color: bool) -> None:
    """Print the overall identity summary + any photo-matched groups.

    The overall summary always prints when there's at least one FOUND;
    that's the bit that surfaces a region for users whose accounts
    don't share a photo. Photo-matched clusters print below as a
    secondary "definitely the same person on these sites" view.
    """
    g, b, x, dim, accent = (
        _c(color, "green"), _c(color, "bold"), _c(color, "reset"),
        _c(color, "dim"), _c(color, "yellow"),
    )

    if overall and len(overall.member_indexes) >= 1:
        print(f"\n{b}[ IDENTITY ]{x}  {dim}(aggregated from {len(overall.member_indexes)} account(s)){x}")
        if overall.display_name:
            print(f"  {b}Name{x}    {overall.display_name}")
        sites = ", ".join(overall.sites)
        print(f"  {b}Sites{x}   {sites}")
        loc_bits: list[str] = []
        if overall.locations:
            loc_bits.append(", ".join(overall.locations))
        if (
            overall.geo_hint and overall.geo_hint.region
            and overall.geo_hint.region not in (overall.locations or [])
        ):
            loc_bits.append(f"likely {overall.geo_hint.region} ({overall.geo_hint.confidence})")
        if loc_bits:
            print(f"  {b}Region{x}  " + " · ".join(loc_bits))
        stat_bits = []
        if overall.total_followers is not None:
            stat_bits.append(f"{_format_count(overall.total_followers)} followers")
        if overall.total_following is not None:
            stat_bits.append(f"{_format_count(overall.total_following)} following")
        if overall.total_posts is not None:
            stat_bits.append(f"{_format_count(overall.total_posts)} posts")
        if stat_bits:
            print(f"  {b}Stats{x}   " + " · ".join(stat_bits))
        if overall.verified_on:
            print(f"  {b}✓{x}       Verified on " + ", ".join(overall.verified_on))

    multi = [c for c in (clusters or []) if len(c.member_indexes) > 1]
    if multi:
        print(f"\n{b}[ PHOTO MATCH ]{x} {b}{len(multi)}{x}  "
              f"{dim}(same profile photo across multiple sites){x}")
        for c in multi:
            name = c.display_name or "(no name)"
            sites = ", ".join(c.sites)
            conf_part = f"{accent}({c.confidence:.2f}){x}"
            print(f"  {b}{name}{x} {conf_part} → {sites}")


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
    # `_flatten` runs the same-profile dedup so the terminal count
    # matches the exported report; otherwise `[ FOUND ] N` would still
    # include the alias duplicates (Facebook accepting several URL
    # patterns for one profile).
    found, unknown, missing_count = _flatten(grouped)

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

# Sites whose URL semantics make multiple URL patterns reliably resolve
# to one profile, so a same-site display_name match alone is safe to
# dedupe even when the canonical URL or photo couldn't be extracted.
# Facebook is the canonical example: `/john.smith`, `/john-smith`, and
# `/johnsmith` all point at the same person, never different real users.
_NAME_ONLY_DEDUP_SITES = frozenset({"Facebook"})


def _profile_dedup_key_parts(profile: dict, url: str, final_url: Optional[str], site: Optional[str] = None) -> Optional[tuple]:
    """Return a key identifying *which profile* this result is for, or
    None when we can't tell. Two FOUND results with the same key on the
    same site are the same person reached via different URL aliases —
    a Facebook account exposed at `/john.smith`, `/john-smith`, and
    `/johnsmith` returns the same profile body for all three.

    Priority:
      1. Platform-shipped numeric profile ID (Facebook's
         `al:ios:url` / `al:android:url`) — bullet-proof.
      2. Canonical URL from `og:url` (`profile.canonical_url`).
      3. The post-redirect final URL — works for sites that 30x to the
         normalised path.
      4. (display_name, photo) — same person if both match exactly.
      5. display_name alone — only on a small set of sites whose URL
         aliasing makes this safe (Facebook today). Falls back here
         when the platform served no canonical URL and we couldn't
         extract a photo.
    """
    p = profile or {}
    fb_id = p.get("fb_profile_id")
    if fb_id:
        return ("fb_id", str(fb_id))
    canonical = p.get("canonical_url")
    if canonical:
        return ("canonical", canonical.lower())
    final = (final_url or "").lower().rstrip("/").split("?", 1)[0]
    own = (url or "").lower().rstrip("/").split("?", 1)[0]
    if final and final != own:
        return ("final", final)
    name = (p.get("display_name") or "").strip().lower()
    photo = (p.get("photo") or "").strip()
    if name and photo:
        return ("name+photo", name, photo)
    if name and site in _NAME_ONLY_DEDUP_SITES:
        return ("name", name)
    return None


def _dedupe_same_site_profiles(found: list[CheckResult]) -> list[CheckResult]:
    """Merge FOUND results that point at the same profile on the same
    site — see `_profile_dedup_key_parts` for the matching rule.

    Kept entry is the one with the richest profile dict; merged variants
    get stashed on `profile["aliases"]` so the report can show every
    handle pattern that resolved to this profile.
    """
    by_key: dict[tuple, list[CheckResult]] = {}
    untouched: list[CheckResult] = []
    for r in found:
        key = _profile_dedup_key_parts(r.profile, r.url, r.final_url, r.site)
        if key is None:
            untouched.append(r)
            continue
        by_key.setdefault((r.site, *key), []).append(r)

    merged: list[CheckResult] = []
    for group in by_key.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        primary = max(
            group,
            key=lambda r: (len(r.profile or {}), -len(r.url or "")),
        )
        others = [g for g in group if g is not primary]
        primary.profile = {**(primary.profile or {})}
        primary.profile["aliases"] = [
            {"variant": o.variant, "url": o.url} for o in others
        ]
        merged.append(primary)
    return untouched + merged


def _dedupe_same_site_dicts(found_dicts: list[dict]) -> list[dict]:
    """Dict-level twin of `_dedupe_same_site_profiles`. Used before
    identity correlation so the photo-match cluster doesn't inflate
    when a single profile is reached via several URL patterns."""
    by_key: dict[tuple, list[dict]] = {}
    untouched: list[dict] = []
    for d in found_dicts:
        key = _profile_dedup_key_parts(
            d.get("profile") or {}, d.get("url") or "", d.get("final_url"),
            d.get("site"),
        )
        if key is None:
            untouched.append(d)
            continue
        by_key.setdefault((d.get("site"), *key), []).append(d)

    merged: list[dict] = []
    for group in by_key.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        primary = max(
            group,
            key=lambda d: (len(d.get("profile") or {}), -len(d.get("url") or "")),
        )
        others = [g for g in group if g is not primary]
        primary["profile"] = {**(primary.get("profile") or {})}
        primary["profile"]["aliases"] = [
            {"variant": o.get("variant"), "url": o.get("url")} for o in others
        ]
        merged.append(primary)
    return untouched + merged


def _run_api_subcommand(argv: list[str]) -> int:
    """Handle `phantom --api <cmd> [args...]`. Two subcommands today:

    - add SERVICE KEY: store a key (overwrites any existing one).
    - list: print configured services without revealing the keys.
    """
    usage = "usage: phantom --api {add SERVICE KEY | list}"
    if not argv:
        print(usage, file=sys.stderr)
        return 2
    cmd = argv[0].lower()
    if cmd == "add":
        if len(argv) != 3:
            print("usage: phantom --api add SERVICE KEY", file=sys.stderr)
            return 2
        try:
            path = apis.add(argv[1], argv[2])
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"saved {argv[1].lower()} key to {path}")
        return 0
    if cmd == "list":
        services = apis.list_services()
        if not services:
            print("no API keys configured.")
            print("add one with: phantom --api add SERVICE KEY")
            return 0
        print(f"Configured API keys ({apis.config_path()}):")
        for s in services:
            print(f"  {s:<10}  [configured]")
        return 0
    print(f"unknown --api subcommand: {argv[0]}", file=sys.stderr)
    print(usage, file=sys.stderr)
    return 2


# Domains whose accounts don't issue user emails (social/streaming/forum
# platforms). Hunter.io will happily search them and return spurious
# corporate or generic addresses, so skip the call entirely.
_HUNTER_DOMAIN_BLOCKLIST = frozenset({
    "instagram.com", "twitter.com", "x.com", "tiktok.com",
    "threads.net", "facebook.com", "youtube.com", "twitch.tv",
    "twitchtracker.com",
    "reddit.com", "pastebin.com", "pinterest.com", "tumblr.com",
    "soundcloud.com", "telegram.org", "discord.com", "linkedin.com",
})

# Minimum Hunter.io score to surface as a real match. Below this the
# result is recorded as low-confidence and the address is dropped.
_HUNTER_MIN_SCORE = 70


_NAME_TITLE_SEPARATOR_RE = re.compile(r"\s[-|·–—]\s|&")


def _looks_like_real_name(s: str) -> bool:
    """Cheap filter for Hunter.io: a real full name has at least two
    whitespace-separated parts with the first and last each ≥2 chars,
    AND no page-title-style separators (" - ", " | ", " · ", "&"). The
    last check catches og:title strings that platforms ship as display
    names — e.g. twitchtracker emits "<user> - Streamer Overview & Stats"
    which passes the word/length test but Hunter rejects with "Full name
    contains invalid characters".
    """
    text = s.strip()
    if _NAME_TITLE_SEPARATOR_RE.search(text):
        return False
    parts = text.split()
    if len(parts) < 2:
        return False
    return len(parts[0]) >= 2 and len(parts[-1]) >= 2


async def discover_emails(
    found: list["CheckResult"],
    api_key: str,
    timeout: float = 15.0,
) -> dict[str, dict]:
    """Query Hunter.io email-finder for each FOUND profile that has a
    display name. Uses the site's hostname as the company domain — Hunter
    expects an org domain, but we ship what the user has on hand and let
    the score speak for itself.

    Per-site outcome shapes:
      success     {email, score, domain}
      low score   {low_confidence: True, score, domain}  (email dropped)
      api error   {error, domain}
      pre-skip    {skipped: <reason>, domain?}

    Identical (full_name, domain) pairs are de-duplicated to one API
    call. Domains in _HUNTER_DOMAIN_BLOCKLIST (social/streaming
    platforms that don't issue user emails) are skipped before any
    network call. Successful results below _HUNTER_MIN_SCORE are
    discarded as low-confidence rather than surfaced as a match.
    """
    from urllib.parse import urlparse

    queue: list[tuple["CheckResult", str, str]] = []
    skipped: dict[str, dict] = {}
    for r in found:
        display = ((r.profile or {}).get("display_name") or "").strip()
        if not display:
            skipped[r.site] = {"skipped": "no display_name"}
            continue
        if not _looks_like_real_name(display):
            skipped[r.site] = {"skipped": "no real name detected"}
            continue
        host = (urlparse(r.url).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if not host:
            skipped[r.site] = {"skipped": "no domain"}
            continue
        if host in _HUNTER_DOMAIN_BLOCKLIST:
            skipped[r.site] = {"skipped": "social platform", "domain": host}
            continue
        queue.append((r, display, host))

    if not queue:
        return skipped

    cache: dict[tuple[str, str], dict] = {}
    out: dict[str, dict] = dict(skipped)
    sem = asyncio.Semaphore(5)

    async with aiohttp.ClientSession() as session:
        async def lookup(r, full_name, domain):
            cache_key = (full_name.lower(), domain)
            if cache_key in cache:
                return r.site, dict(cache[cache_key])
            params = {
                "domain": domain,
                "full_name": full_name,
                "api_key": api_key,
            }
            async with sem:
                try:
                    async with session.get(
                        "https://api.hunter.io/v2/email-finder",
                        params=params,
                        timeout=ClientTimeout(total=timeout),
                    ) as resp:
                        try:
                            payload = await resp.json(content_type=None)
                        except Exception:
                            payload = {}
                        if resp.status == 401:
                            info = {"error": "invalid Hunter.io API key (401)", "domain": domain}
                        elif resp.status == 429:
                            info = {"error": "Hunter.io rate-limited (429)", "domain": domain}
                        elif resp.status != 200:
                            err = "http " + str(resp.status)
                            errs = payload.get("errors") if isinstance(payload, dict) else None
                            if isinstance(errs, list) and errs and isinstance(errs[0], dict):
                                err = errs[0].get("details") or errs[0].get("id") or err
                            info = {"error": err, "domain": domain}
                        else:
                            data = (payload.get("data") if isinstance(payload, dict) else None) or {}
                            email = data.get("email") or None
                            score = data.get("score")
                            if (
                                email
                                and isinstance(score, (int, float))
                                and score < _HUNTER_MIN_SCORE
                            ):
                                info = {
                                    "low_confidence": True,
                                    "score": score,
                                    "domain": domain,
                                }
                            else:
                                info = {
                                    "email": email,
                                    "score": score,
                                    "domain": domain,
                                }
                except asyncio.TimeoutError:
                    info = {"error": "timeout", "domain": domain}
                except Exception as e:
                    info = {"error": type(e).__name__, "domain": domain}
            cache[cache_key] = info
            return r.site, dict(info)

        results = await asyncio.gather(*(lookup(r, n, d) for r, n, d in queue))

    for site, info in results:
        out[site] = info
    return out


def _attach_emails_to_found(
    grouped: list[tuple[str, list["CheckResult"]]],
    emails: dict[str, dict],
) -> int:
    """Stamp the email-finder result onto each FOUND result's profile
    dict so JSON export and HTML render pick it up uniformly. Returns
    the count of profiles with an actual email address attached."""
    n = 0
    for _, rs in grouped:
        for r in rs:
            if r.exists is not True:
                continue
            info = emails.get(r.site)
            if not info:
                continue
            if r.profile is None:
                r.profile = {}
            if info.get("email"):
                r.profile["email"] = info["email"]
                if info.get("score") is not None:
                    r.profile["email_score"] = info["score"]
                if info.get("domain"):
                    r.profile["email_domain"] = info["domain"]
                n += 1
            elif info.get("low_confidence"):
                r.profile["email_low_confidence"] = True
                if info.get("score") is not None:
                    r.profile["email_score"] = info["score"]
                if info.get("domain"):
                    r.profile["email_domain"] = info["domain"]
            elif info.get("error"):
                r.profile["email_error"] = info["error"]
    return n


def _print_deep_evidence(grouped, deep, color: bool) -> None:
    """Surface the deep-photo notes + reverse-image hits in the terminal."""
    notes = list(getattr(deep, "notes", []) or [])
    reverse_hits = getattr(deep, "reverse_hits", {}) or {}
    if not notes and not reverse_hits:
        return

    b, x, dim = _c(color, "bold"), _c(color, "reset"), _c(color, "dim")
    g, accent = _c(color, "green"), _c(color, "yellow")

    total_hits = sum(len(v) for v in reverse_hits.values())
    print(f"\n{b}[ DEEP PHOTO ]{x}  {dim}{total_hits} reverse hit(s){x}")
    if notes:
        for n in notes:
            print(f"  {dim}· {n}{x}")
    if reverse_hits:
        found, _, _ = _flatten(grouped)
        for idx, hits in sorted(reverse_hits.items(), key=lambda kv: int(kv[0])):
            try:
                idx_i = int(idx) if isinstance(idx, str) else idx
            except (TypeError, ValueError):
                continue
            pivot = (
                found[idx_i].site
                if 0 <= idx_i < len(found) else "?"
            )
            print(f"  {accent}↗{x} from {pivot}:")
            for h in hits[:8]:
                tag = f"{h.site}" if h.site else "?"
                handle = f"@{h.username}" if h.username else "(no handle)"
                print(f"    {tag:12} {handle:24} {dim}{h.url}{x}")


def _print_emails_section(
    found: list["CheckResult"],
    emails: dict[str, dict],
    color: bool,
) -> None:
    """Print a [ EMAILS ] block under the FOUND list with one line per
    site that produced an email or a per-site error/skip note."""
    if not emails:
        return
    rows = []
    n_emails = 0
    for r in found:
        info = emails.get(r.site)
        if not info:
            continue
        if info.get("email"):
            n_emails += 1
            score = info.get("score")
            tail = f" (score {score})" if score is not None else ""
            rows.append((r.site, info["email"] + tail, "ok"))
        elif info.get("low_confidence"):
            score = info.get("score")
            tail = f" (score {score})" if score is not None else ""
            rows.append((r.site, f"low confidence{tail}", "dim"))
        elif info.get("error"):
            rows.append((r.site, f"error: {info['error']}", "err"))
        elif info.get("skipped"):
            rows.append((r.site, f"skipped: {info['skipped']}", "dim"))
        else:
            rows.append((r.site, "no match", "dim"))

    if not rows:
        return
    b, x, dim, g, r_ = (
        _c(color, "bold"), _c(color, "reset"), _c(color, "dim"),
        _c(color, "green"), _c(color, "red"),
    )
    print(f"\n{b}[ EMAILS ]{x}{b} {n_emails}{x}  {dim}(via Hunter.io){x}")
    for site, msg, kind in rows:
        col = g if kind == "ok" else (r_ if kind == "err" else dim)
        print(f"  {b}{site:<14}{x} {col}{msg}{x}")


def _load_identity_hint(path: Path) -> Optional[dict]:
    """Read a previous Phantom JSON report and pull the bits we can use as
    a sanity filter for a fresh name-mode scan: a country (from geo_hint
    first, falling back to a normalisable item in `locations`), a bio
    language (the most common per-FOUND `language`), and a display name.

    Returns None if the file can't be parsed or carries no usable signal.
    Display name is informational only — filtering uses country/language.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: --identity-hint {path}: {e}", file=sys.stderr)
        return None

    overall = data.get("overall_identity") or data.get("identity") or {}

    country = None
    geo = overall.get("geo_hint") or {}
    if isinstance(geo, dict) and geo.get("region"):
        country = _normalise_country(geo["region"]) or geo["region"].strip()
    if not country:
        for loc in overall.get("locations") or []:
            country = _normalise_country(loc) if isinstance(loc, str) else None
            if country:
                break

    lang_counts: dict[str, int] = {}
    for f in data.get("found") or []:
        p = (f.get("profile") or {}) if isinstance(f, dict) else {}
        lang = p.get("language")
        if isinstance(lang, str) and lang.strip():
            lang_counts[lang] = lang_counts.get(lang, 0) + 1
    language = max(lang_counts, key=lang_counts.get) if lang_counts else None

    display_name = overall.get("display_name") or None

    if not (country or language):
        print(
            f"warning: --identity-hint {path} has no usable country or "
            f"language signal; nothing to filter on.",
            file=sys.stderr,
        )
        return None

    return {
        "country": country,
        "language": language,
        "display_name": display_name,
        "source": str(path),
    }


def _filter_results_by_hint(
    grouped: list[tuple[str, list["CheckResult"]]],
    hint: dict,
) -> int:
    """Reclassify FOUND hits whose profile country or language clearly
    contradicts the hint. The hit isn't deleted — its `exists` flips from
    True to None (UNKNOWN) and `reason` records the mismatch, so the row
    survives in the report for auditing while staying out of FOUND and
    out of the identity-correlation pool.

    Missing data is never treated as a contradiction: a profile with no
    location and no language is left alone.
    """
    expected_country = hint.get("country")
    expected_lang = hint.get("language")
    n_filtered = 0

    for _, rs in grouped:
        for r in rs:
            if r.exists is not True:
                continue
            profile = r.profile or {}
            mismatches: list[str] = []

            if expected_country:
                loc = profile.get("location")
                if isinstance(loc, str) and loc.strip():
                    observed = _normalise_country(loc)
                    if observed and observed.lower() != expected_country.lower():
                        mismatches.append(f"country={observed}≠{expected_country}")

            if expected_lang:
                lang = profile.get("language")
                if isinstance(lang, str) and lang.strip() and lang != expected_lang:
                    mismatches.append(f"lang={lang}≠{expected_lang}")

            if mismatches:
                r.exists = None
                tag = "filter:" + ",".join(mismatches)
                r.reason = f"{r.reason}+{tag}" if r.reason else tag
                n_filtered += 1

    return n_filtered


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
    found = _dedupe_same_site_profiles(found)
    sort_key = lambda r: (-r.reliability, r.site.lower(), r.variant or "")
    found.sort(key=sort_key)
    unknown.sort(key=sort_key)
    return found, unknown, missing_count


def _deep_evidence_to_dict(deep) -> Optional[dict]:
    if deep is None:
        return None
    return {
        "notes": list(getattr(deep, "notes", []) or []),
        "extra_edges": [
            {"i": i, "j": j, "rationale": why}
            for (i, j, why) in (getattr(deep, "extra_edges", []) or [])
        ],
        "reverse_hits": {
            str(idx): [
                {"site": h.site, "username": h.username, "url": h.url, "source": h.source}
                for h in hits
            ]
            for idx, hits in (getattr(deep, "reverse_hits", {}) or {}).items()
        },
    }


def _build_json_payload(grouped, raw, elapsed, overall, clusters, emails=None, deep_evidence=None):
    found, unknown, missing_count = _flatten(grouped)
    payload = {
        "input": raw,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_seconds": round(elapsed, 2),
        "variants": [v for v, _ in grouped],
        "summary": {
            "found": len(found),
            "unknown": len(unknown),
            "missing": missing_count,
            "photo_matches": len(
                [c for c in (clusters or []) if len(c.member_indexes) > 1]
            ),
        },
        "overall_identity": overall.to_dict() if overall else None,
        "photo_matched_clusters": [
            c.to_dict() for c in (clusters or []) if len(c.member_indexes) > 1
        ],
        "found": [asdict(r) for r in found],
        "unknown": [asdict(r) for r in unknown],
    }
    if emails:
        payload["emails"] = emails
    if deep_evidence is not None:
        payload["photo_deep"] = _deep_evidence_to_dict(deep_evidence)
    return payload


def export_json(grouped, raw, elapsed, path: Path, overall=None, clusters=None, emails=None, deep_evidence=None) -> None:
    payload = _build_json_payload(grouped, raw, elapsed, overall, clusters or [], emails, deep_evidence)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _md_identity_block(c, header: str) -> list[str]:
    """Render an identity cluster as a markdown block. Used for both the
    overall identity and photo-matched clusters — same shape, different
    headers."""
    lines = [header, ""]
    if c.display_name:
        lines.append(f"- **Display name**: {c.display_name}")
    lines.append(f"- **Sites** ({len(c.sites)}): {', '.join(c.sites)}")
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
    if c.total_following is not None:
        lines.append(f"- **Following (total)**: {c.total_following:,}")
    if c.total_posts is not None:
        lines.append(f"- **Posts (total)**: {c.total_posts:,}")
    if c.verified_on:
        lines.append(f"- **Verified on**: {', '.join(c.verified_on)}")
    if c.private_on:
        lines.append(f"- **Private on**: {', '.join(c.private_on)}")
    if c.rationale:
        lines.append(f"- **Reason**: {'; '.join(c.rationale)}")
    lines.append(f"- **Confidence**: {c.confidence}")
    lines.append("")
    return lines


def export_markdown(grouped, raw, elapsed, path: Path, overall=None, clusters=None) -> None:
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
        f"- **Photo-matched accounts**: {len(multi)}",
        f"- **Unknown**: {len(unknown)}",
        f"- **Missing**: {missing_count}",
        "",
    ]
    if overall and len(found) >= 2:
        lines += _md_identity_block(overall, "## Overall identity")
    if multi:
        lines += [f"## Photo-matched accounts ({len(multi)})", ""]
        for i, c in enumerate(multi, 1):
            lines += _md_identity_block(
                c, f"### Match {i}: {c.display_name or '(no name)'}"
            )
    lines += [
        f"## Found ({len(found)})",
        "",
    ]
    if found:
        for r in found:
            target = r.url  # canonical URL — see _format_row note
            tag = f" — `{r.variant}`" if r.variant else ""
            lines.append(f"- [{r.site}]({target}){tag}")
    else:
        lines.append("_None._")
    lines += ["", f"## Unknown ({len(unknown)})", ""]
    if unknown:
        for r in unknown:
            target = r.url
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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --paper:    #f5efe2;
    --paper-2:  #ebe1cd;
    --ink:      #1a1612;
    --muted:    #6b5f4d;
    --rule:     #c4b896;
    --border:   #d4c9b3;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ background: var(--paper); }}
  body {{
    margin: 0; color: var(--ink);
    font-family: 'IBM Plex Sans', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
    font-size: 14px; line-height: 1.55;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    min-height: 100vh;
  }}
  a {{ color: var(--ink); text-decoration: none; }}
  .serif {{ font-family: 'Instrument Serif', Georgia, 'Times New Roman', serif; }}
  .mono {{
    font-family: 'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace;
    font-feature-settings: "ss01", "ss02";
  }}

  .page {{
    max-width: 900px; margin: 0 auto;
    padding: 42px 38px;
  }}

  /* -------- Header -------- */
  header.top {{
    display: flex; align-items: flex-start; justify-content: space-between;
    gap: 24px;
    padding-bottom: 22px;
    border-bottom: 1px solid var(--ink);
  }}
  .brand {{
    display: inline-flex; align-items: center; gap: 9px;
    line-height: 1;
  }}
  .brand .ghost {{
    width: 22px; height: 22px; flex-shrink: 0;
  }}
  .brand .wordmark {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 30px; color: var(--ink);
    letter-spacing: -0.01em; line-height: 1;
  }}
  .file-meta {{
    text-align: right;
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 11px; color: var(--muted);
    line-height: 1.6;
    letter-spacing: 0.02em;
  }}
  .file-meta .num {{ color: var(--ink); font-weight: 500; }}

  /* -------- Section kicker -------- */
  .kicker {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 10px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.22em;
    color: var(--muted);
    margin-bottom: 14px;
  }}

  /* -------- Subject of inquiry -------- */
  section.subject {{
    margin-top: 30px;
  }}
  .subject-row {{
    display: flex; align-items: center; gap: 26px;
  }}
  .portrait {{
    width: 120px; height: 120px;
    border-radius: 6px;
    flex-shrink: 0;
    background: var(--paper-2) center/cover no-repeat;
    border: 1px solid var(--border);
    display: flex; align-items: center; justify-content: center;
    overflow: hidden;
  }}
  .portrait .letter {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 60px; color: var(--ink);
    line-height: 1; letter-spacing: -0.02em;
  }}
  .ident {{ flex: 1; min-width: 0; }}
  .ident .handle {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 72px; line-height: 1.05;
    letter-spacing: -0.02em; color: var(--ink);
    word-break: break-word;
  }}
  .ident .name-region {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-style: italic;
    font-size: 24px; line-height: 1.35;
    color: var(--muted);
    margin-top: 14px;
  }}

  /* -------- Stats row -------- */
  .stats {{
    margin-top: 30px;
    border-top: 1px solid var(--ink);
    border-bottom: 1px solid var(--ink);
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    align-items: center;
  }}
  .stat {{
    padding: 24px 18px;
    position: relative;
    text-align: left;
  }}
  .stat + .stat::before {{
    content: ""; position: absolute; left: 0; top: 14%; bottom: 14%;
    border-left: 1px dashed var(--rule);
  }}
  .stat .n {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 52px; line-height: 1.05;
    letter-spacing: -0.02em; color: var(--ink);
  }}
  .stat .l {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 10px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.18em;
    color: var(--muted);
    margin-top: 6px;
  }}

  /* -------- Detail rows -------- */
  section.details {{
    margin-top: 30px;
  }}
  .detail-row {{
    display: grid;
    grid-template-columns: 130px 1fr;
    gap: 20px;
    padding: 14px 0;
    border-bottom: 1px dashed var(--rule);
  }}
  .detail-row:first-child {{ padding-top: 0; }}
  .detail-row:last-child {{ border-bottom: 0; padding-bottom: 0; }}
  .detail-row .lbl {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 11px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.18em;
    color: var(--muted);
    align-self: center;
  }}
  .detail-row .val {{
    font-size: 15px; color: var(--ink);
    line-height: 1.5; word-break: break-word;
    align-self: center;
  }}
  .detail-row .val em {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-style: italic; color: var(--muted); font-size: 13px;
    margin-left: 6px;
  }}
  .alias-tag {{
    display: inline-block;
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 11px; font-weight: 500;
    background: var(--paper-2);
    color: var(--ink);
    padding: 3px 8px;
    border-radius: 3px;
    margin: 2px 4px 2px 0;
    border: 1px solid var(--border);
  }}

  /* -------- Discovered accounts -------- */
  section.accounts {{ margin-top: 30px; }}
  .accounts-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
  }}
  .acct {{
    background: var(--paper-2);
    border-radius: 6px;
    padding: 20px;
    display: flex;
    gap: 18px;
    border: 1px solid var(--border);
  }}
  .acct .photo {{
    width: 72px; height: 72px;
    border-radius: 6px;
    flex-shrink: 0;
    background: var(--paper) center/cover no-repeat;
    border: 1px solid var(--border);
    display: flex; align-items: center; justify-content: center;
    overflow: hidden;
  }}
  .acct .photo .letter {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 34px; color: var(--ink);
    line-height: 1; letter-spacing: -0.02em;
  }}
  .acct .body {{ flex: 1; min-width: 0; display: flex; flex-direction: column; }}
  .acct .display-name {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 22px; line-height: 1.2;
    color: var(--ink); letter-spacing: -0.01em;
    word-break: break-word;
  }}
  .acct .handle {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 14px; color: var(--muted);
    margin-top: 2px;
    word-break: break-all;
  }}
  .acct .bio {{
    font-size: 15px; color: var(--muted);
    margin-top: 12px;
    line-height: 1.6;
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }}
  .acct .footer {{
    display: flex; align-items: center; justify-content: space-between;
    margin-top: 16px; gap: 8px;
  }}
  .acct .platform-tag {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 12px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.10em;
    background: var(--paper);
    color: var(--ink);
    padding: 3px 8px;
    border-radius: 3px;
    border: 1px solid var(--border);
  }}
  .acct .url {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 13px; color: var(--muted);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    max-width: 60%;
    transition: color 0.15s;
  }}
  .acct .url:hover {{ color: var(--ink); }}
  .acct .url .arrow {{ margin-left: 3px; }}

  /* -------- Auxiliary panels (emails / deep / unknowns) -------- */
  section.aux {{ margin-top: 30px; }}
  .aux-panel {{
    background: var(--paper-2);
    border-radius: 6px;
    border: 1px solid var(--border);
    padding: 16px 18px;
  }}
  .aux-table {{
    width: 100%; border-collapse: collapse;
    font-size: 12px;
  }}
  .aux-table th, .aux-table td {{
    text-align: left;
    padding: 8px 10px;
    border-bottom: 1px dashed var(--rule);
    vertical-align: top;
  }}
  .aux-table tr:last-child td {{ border-bottom: 0; }}
  .aux-table th {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 10px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.14em;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
  }}
  .aux-table td {{ color: var(--ink); }}
  .aux-table .dim {{ color: var(--muted); }}
  .aux-table .err {{ color: #8a3a2e; }}
  .aux-table .platform-tag {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 10px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.10em;
    background: var(--paper);
    color: var(--ink);
    padding: 2px 7px; border-radius: 3px;
    border: 1px solid var(--border);
  }}
  .aux-table a {{ color: var(--ink); text-decoration: underline; text-decoration-color: var(--rule); text-underline-offset: 3px; }}
  .aux-table a:hover {{ text-decoration-color: var(--ink); }}
  .aux-notes {{
    display: flex; flex-wrap: wrap; gap: 6px;
    margin-bottom: 12px;
  }}
  .aux-notes .chip {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 10px; font-weight: 500;
    background: var(--paper);
    color: var(--muted);
    padding: 3px 8px; border-radius: 3px;
    border: 1px solid var(--border);
    letter-spacing: 0.04em;
  }}

  /* -------- Inconclusive collapsible -------- */
  details.unknown-fold {{ margin-top: 4px; }}
  details.unknown-fold > summary {{
    list-style: none; cursor: pointer; user-select: none;
    display: inline-flex; align-items: center; gap: 10px;
    padding: 9px 14px; border-radius: 4px;
    background: var(--paper-2);
    border: 1px solid var(--border);
    color: var(--ink); font-size: 12px;
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    text-transform: uppercase; letter-spacing: 0.10em;
    transition: background 0.15s, border-color 0.15s;
  }}
  details.unknown-fold > summary::-webkit-details-marker {{ display: none; }}
  details.unknown-fold > summary::before {{
    content: "›"; font-size: 14px; color: var(--muted);
    transition: transform 0.18s ease;
    line-height: 1;
  }}
  details.unknown-fold[open] > summary::before {{ transform: rotate(90deg); }}
  details.unknown-fold > summary:hover {{
    background: var(--paper); border-color: var(--ink);
  }}
  details.unknown-fold > .aux-panel {{ margin-top: 14px; }}

  /* -------- Footer -------- */
  footer.bottom {{
    margin-top: 38px;
    padding-top: 18px;
    border-top: 1px solid var(--ink);
    display: flex; justify-content: space-between; gap: 18px;
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 10px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.18em;
    color: var(--muted);
  }}
  footer.bottom a {{ color: var(--muted); }}
  footer.bottom a:hover {{ color: var(--ink); }}

  @media (max-width: 760px) {{
    .page {{ padding: 28px 18px; }}
    header.top {{ flex-direction: column; gap: 14px; }}
    .file-meta {{ text-align: left; }}
    .ident .handle {{ font-size: 40px; }}
    .ident .name-region {{ font-size: 18px; }}
    .stats {{ grid-template-columns: repeat(2, 1fr); }}
    .stat + .stat::before {{ display: none; }}
    .stat:nth-child(odd) {{ border-right: 1px dashed var(--rule); }}
    .stat:nth-child(n+3) {{ border-top: 1px dashed var(--rule); }}
    .detail-row {{ grid-template-columns: 1fr; gap: 6px; }}
    .accounts-grid {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>
<div class="page">

<header class="top">
  <div class="brand">
    <svg class="ghost" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <path d="M12 2.4 C6.8 2.4 4 5.8 4 10.6 L4 21.4 L6.6 19.4 L9 21.4 L12 19.4 L15 21.4 L17.4 19.4 L20 21.4 L20 10.6 C20 5.8 17.2 2.4 12 2.4 Z" fill="#1a1612"/>
      <circle cx="9.5" cy="10.6" r="1.3" fill="#f5efe2"/>
      <circle cx="14.5" cy="10.6" r="1.3" fill="#f5efe2"/>
    </svg>
    <span class="wordmark">Phantom</span>
  </div>
  <div class="file-meta">
    <div>File <span class="num">N° {file_number}</span></div>
    <div>{generated_date} · {generated_time} UTC</div>
    <div>Scan time {elapsed:.1f}s</div>
  </div>
</header>

<section class="subject">
  <div class="kicker">Subject of inquiry</div>
  <div class="subject-row">
    {subject_portrait}
    <div class="ident">
      <div class="handle">@{subject_handle}</div>
      <div class="name-region">{subject_name_region}</div>
    </div>
  </div>
</section>

<section class="stats">
  <div class="stat">
    <div class="n">{n_found}</div>
    <div class="l">Confirmed</div>
  </div>
  <div class="stat">
    <div class="n">{n_identities}</div>
    <div class="l">Photo match</div>
  </div>
  <div class="stat">
    <div class="n">{n_variants}</div>
    <div class="l">Aliases tested</div>
  </div>
  <div class="stat">
    <div class="n">{n_sites}</div>
    <div class="l">Sites scanned</div>
  </div>
</section>

<section class="details">
  {detail_rows}
</section>

<section class="accounts">
  <div class="kicker">Confirmed presence — {n_found} accounts</div>
  <div class="accounts-grid">{found_cards}</div>
</section>

{emails_section}
{deep_section}
{unknown_section}

<footer class="bottom">
  <div>Generated by Phantom</div>
  <div><a href="https://github.com/hamaffs/Phantom-" target="_blank" rel="noopener">github.com/hamaffs/Phantom-</a></div>
</footer>

</div>
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


def _html_card(r: CheckResult, photo_match: Optional[list] = None) -> str:
    """Render one FOUND profile as an editorial dossier account card.

    Layout (left to right):
      [56x56 photo]  display name (Instrument Serif)
                     @handle (Plex Mono muted)
                     bio (sans muted, clamped to 3 lines)
                     [platform tag]               [url ↗]
    """
    p = r.profile or {}
    target = r.url

    photo_url = p.get("photo")
    initial = (p.get("display_name") or r.variant or r.site or "?")[:1].upper()
    if photo_url:
        photo_html = (
            f'<div class="photo" style="background-image:url(\'{html.escape(photo_url, quote=True)}\')"></div>'
        )
    else:
        photo_html = (
            f'<div class="photo"><span class="letter">{html.escape(initial)}</span></div>'
        )

    display_name = p.get("display_name") or r.variant or r.site
    handle = f"@{r.variant}" if r.variant else r.site
    bio_html = (
        f'<div class="bio">{html.escape(p["bio"])}</div>' if p.get("bio") else ""
    )

    # Truncate URL for visual length while keeping it clickable.
    visible_url = target
    if len(visible_url) > 48:
        visible_url = visible_url[:46] + "…"

    body = (
        f'<div class="body">'
        f'<div class="display-name">{html.escape(display_name)}</div>'
        f'<div class="handle">{html.escape(handle)}</div>'
        f'{bio_html}'
        f'<div class="footer">'
        f'<span class="platform-tag">{html.escape(r.site)}</span>'
        f'<a class="url" href="{html.escape(target, quote=True)}" target="_blank" '
        f'rel="noopener" title="{html.escape(target)}">'
        f'{html.escape(visible_url)}<span class="arrow">↗</span></a>'
        f'</div>'
        f'</div>'
    )
    return f'<div class="acct">{photo_html}{body}</div>'


def _html_emails_section(found: list, emails: dict) -> str:
    """Per-site Hunter.io results — restyled for the editorial dossier."""
    if not emails:
        return ""
    rows: list[str] = []
    n_emails = 0
    for r in found:
        info = emails.get(r.site)
        if not info:
            continue
        site_cell = (
            f'<span class="platform-tag">{html.escape(r.site)}</span>'
        )
        if info.get("email"):
            n_emails += 1
            email = html.escape(info["email"])
            href = html.escape(info["email"], quote=True)
            score = info.get("score")
            score_part = f' <span class="dim">({score})</span>' if score is not None else ""
            cell = f'<a href="mailto:{href}">{email}</a>{score_part}'
        elif info.get("low_confidence"):
            score = info.get("score")
            tail = f" (score {score})" if score is not None else ""
            cell = f'<span class="dim">low confidence{html.escape(tail)}</span>'
        elif info.get("error"):
            cell = f'<span class="err">error: {html.escape(info["error"])}</span>'
        elif info.get("skipped"):
            cell = f'<span class="dim">skipped: {html.escape(info["skipped"])}</span>'
        else:
            cell = '<span class="dim">no match</span>'
        rows.append(f'<tr><td>{site_cell}</td><td>{cell}</td></tr>')
    if not rows:
        return ""
    table = (
        '<table class="aux-table"><thead><tr>'
        '<th>Site</th><th>Email</th></tr></thead><tbody>'
        + "".join(rows) + '</tbody></table>'
    )
    return (
        '<section class="aux">'
        f'<div class="kicker">Discovered emails — {n_emails}</div>'
        f'<div class="aux-panel">{table}</div>'
        '</section>'
    )


def _html_deep_section(found: list, deep) -> str:
    """Deep photo-matching evidence panel — editorial restyle."""
    if deep is None:
        return ""
    notes = list(getattr(deep, "notes", []) or [])
    reverse_hits = getattr(deep, "reverse_hits", {}) or {}
    if not notes and not reverse_hits:
        return ""

    notes_html = ""
    if notes:
        chips = "".join(
            f'<span class="chip">{html.escape(n)}</span>'
            for n in notes
        )
        notes_html = f'<div class="aux-notes">{chips}</div>'

    rows: list[str] = []
    total_hits = 0
    for idx_str, hits in sorted(reverse_hits.items(), key=lambda kv: int(kv[0])):
        try:
            idx = int(idx_str) if isinstance(idx_str, str) else idx_str
        except (TypeError, ValueError):
            continue
        pivot_site = (
            html.escape(found[idx].site)
            if 0 <= idx < len(found) else "?"
        )
        for h in hits:
            total_hits += 1
            site = html.escape(h.site or "?")
            handle = (
                f'<span class="dim">@{html.escape(h.username)}</span>'
                if h.username else '<span class="dim">—</span>'
            )
            url_safe = html.escape(h.url, quote=True)
            url_disp = html.escape(h.url[:54] + "…" if len(h.url) > 56 else h.url)
            src_tag = html.escape(h.source)
            rows.append(
                f'<tr><td><span class="platform-tag">{site}</span></td>'
                f'<td>{handle}</td>'
                f'<td><a href="{url_safe}" target="_blank" rel="noopener">{url_disp} ↗</a></td>'
                f'<td><span class="dim">{src_tag} from {pivot_site}</span></td></tr>'
            )

    table_html = ""
    if rows:
        table_html = (
            '<table class="aux-table"><thead><tr>'
            '<th>Site</th><th>Handle</th><th>URL</th><th>Source</th>'
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table>'
        )

    if not notes_html and not table_html:
        return ""

    return (
        '<section class="aux">'
        f'<div class="kicker">Reverse image findings — {total_hits}</div>'
        '<div class="aux-panel">'
        + notes_html + table_html +
        '</div></section>'
    )


def _html_unknown_row(r: CheckResult) -> str:
    target = r.url
    return (
        '<tr>'
        f'<td><span class="platform-tag">{html.escape(r.site)}</span></td>'
        f'<td><a href="{html.escape(target, quote=True)}" target="_blank" rel="noopener">'
        f'{html.escape(target)}</a></td>'
        f'<td><span class="dim">{html.escape(r.reason or "unknown")}</span></td>'
        f'<td><span class="dim">{html.escape(r.variant or "")}</span></td>'
        '</tr>'
    )


def _pick_subject_photo(overall, clusters, found) -> Optional[str]:
    """Pick the most representative photo for the dossier hero portrait.

    Preference order: largest photo-matched cluster's first photo →
    overall identity's first photo → first FOUND result with any photo.
    """
    multi = [c for c in (clusters or []) if len(c.member_indexes) > 1]
    if multi:
        biggest = max(multi, key=lambda c: len(c.member_indexes))
        if biggest.photos:
            return biggest.photos[0]
    if overall and getattr(overall, "photos", None):
        return overall.photos[0]
    for r in found:
        photo = (r.profile or {}).get("photo")
        if photo:
            return photo
    return None


def _subject_handle(raw: str, found) -> str:
    """Pick a single @handle to display as the subject identifier.

    For single-token input that's the input itself. For name-mode input
    ('first last'), pick the variant that produced the most FOUND
    accounts — that's the canonical handle the subject actually uses.
    """
    raw = raw.strip()
    if raw and " " not in raw:
        return raw
    counts: dict[str, int] = {}
    for r in found:
        v = (r.variant or "").strip()
        if v:
            counts[v] = counts.get(v, 0) + 1
    if counts:
        return max(counts.items(), key=lambda kv: kv[1])[0]
    return raw.replace(" ", "")


def _html_subject_portrait(photo_url: Optional[str], handle: str) -> str:
    """100×100 portrait — image if available, otherwise solid block with
    the first letter of the handle in serif."""
    if photo_url:
        return (
            f'<div class="portrait" '
            f'style="background-image:url(\'{html.escape(photo_url, quote=True)}\')">'
            f'</div>'
        )
    initial = (handle[:1] or "?").upper()
    return (
        f'<div class="portrait">'
        f'<span class="letter">{html.escape(initial)}</span>'
        f'</div>'
    )


def _format_footprint(overall) -> str:
    """Combine totals (followers / following / posts) into one line."""
    if overall is None:
        return "—"
    bits: list[str] = []
    if getattr(overall, "total_followers", None) is not None:
        bits.append(f"{_format_count(overall.total_followers)} followers")
    if getattr(overall, "total_following", None) is not None:
        bits.append(f"{_format_count(overall.total_following)} following")
    if getattr(overall, "total_posts", None) is not None:
        bits.append(f"{_format_count(overall.total_posts)} posts")
    return " · ".join(bits) if bits else "—"


def _format_region(overall) -> str:
    """Render region from locations + inferred geo hint."""
    if overall is None:
        return "—"
    parts: list[str] = []
    locs = list(getattr(overall, "locations", []) or [])
    if locs:
        parts.append(", ".join(locs))
    geo = getattr(overall, "geo_hint", None)
    if geo and getattr(geo, "region", None) and geo.region not in (locs or []):
        parts.append(
            f'<em>likely {html.escape(geo.region)} '
            f'({html.escape(getattr(geo, "confidence", "low"))})</em>'
        )
    return " · ".join(parts) if parts else "—"


def _build_detail_rows(overall, found, all_variants: list[str]) -> str:
    """Render the four detail rows: region, active since, footprint, aliases."""
    rows: list[tuple[str, str]] = []

    rows.append(("Region", _format_region(overall) or "—"))

    active_since = "—"
    if overall and getattr(overall, "joined_oldest", None):
        formatted = _format_joined(overall.joined_oldest)
        active_since = formatted or html.escape(str(overall.joined_oldest))
    rows.append(("Active since", active_since))

    rows.append(("Footprint", html.escape(_format_footprint(overall))))

    # Aliases: variants that actually surfaced a FOUND result, plus any
    # variants tested overall as light/dim chips. Confirmed first.
    confirmed = sorted({(r.variant or "").strip() for r in found if r.variant})
    confirmed = [v for v in confirmed if v]
    tags = "".join(
        f'<span class="alias-tag">{html.escape(v)}</span>'
        for v in confirmed
    )
    if not tags:
        tags = "—"
    rows.append(("Aliases", tags))

    return "".join(
        f'<div class="detail-row">'
        f'<div class="lbl">{html.escape(label)}</div>'
        f'<div class="val">{value}</div>'
        f'</div>'
        for label, value in rows
    )


def _html_unknown_section(unknown: list) -> str:
    """Restyled inconclusive collapsible. Empty string when no unknowns."""
    if not unknown:
        return ""
    rows = "".join(_html_unknown_row(r) for r in unknown)
    table = (
        '<table class="aux-table"><thead><tr>'
        '<th>Site</th><th>URL</th><th>Reason</th><th>Variant</th>'
        '</tr></thead><tbody>' + rows + '</tbody></table>'
    )
    n = len(unknown)
    plural = "s" if n != 1 else ""
    return (
        '<section class="aux">'
        f'<div class="kicker">Inconclusive — {n}</div>'
        '<details class="unknown-fold">'
        f'<summary>Show {n} inconclusive result{plural}</summary>'
        f'<div class="aux-panel">{table}</div>'
        '</details>'
        '</section>'
    )


def export_html(grouped, raw, elapsed, path: Path, overall=None, clusters=None, emails=None, deep_evidence=None) -> None:
    found, unknown, missing_count = _flatten(grouped)
    clusters = clusters or []
    multi = [c for c in clusters if len(c.member_indexes) > 1]

    # --- Subject hero ---
    subject_handle = _subject_handle(raw, found)
    portrait_url = _pick_subject_photo(overall, clusters, found)
    subject_portrait_html = _html_subject_portrait(portrait_url, subject_handle)

    # Italic name · region line under the @handle.
    nr_bits: list[str] = []
    if overall and getattr(overall, "display_name", None):
        nr_bits.append(html.escape(overall.display_name))
    region_text = ""
    if overall:
        if overall.locations:
            region_text = overall.locations[0]
        elif getattr(overall, "geo_hint", None) and overall.geo_hint.region:
            region_text = overall.geo_hint.region
    if region_text:
        nr_bits.append(html.escape(region_text))
    subject_name_region = " · ".join(nr_bits) if nr_bits else "&nbsp;"

    # --- Stats counts ---
    n_variants = len(grouped)
    n_sites = len(grouped[0][1]) if grouped and grouped[0][1] else 0

    # --- Detail rows + cards ---
    detail_rows_html = _build_detail_rows(
        overall, found, [v for v, _ in grouped]
    )
    if found:
        found_cards_html = "".join(_html_card(r) for r in found)
    else:
        found_cards_html = (
            '<div class="acct" style="grid-column:1/-1;justify-content:center">'
            '<div class="body"><div class="bio">No confirmed accounts.</div></div>'
            '</div>'
        )

    # --- Auxiliary panels ---
    emails_section = _html_emails_section(found, emails) if emails else ""
    deep_section = _html_deep_section(found, deep_evidence) if deep_evidence else ""
    unknown_section_html = _html_unknown_section(unknown)

    # --- File metadata ---
    now = datetime.now(timezone.utc)
    file_number = f"{random.randint(1000, 9999)}"
    generated_date = now.strftime("%b %d, %Y")
    generated_time = now.strftime("%H:%M")

    page = _HTML_TEMPLATE.format(
        raw_html=html.escape(raw),
        file_number=file_number,
        generated_date=generated_date,
        generated_time=generated_time,
        elapsed=elapsed,
        subject_portrait=subject_portrait_html,
        subject_handle=html.escape(subject_handle),
        subject_name_region=subject_name_region,
        n_found=len(found),
        n_identities=len(multi),
        n_variants=n_variants,
        n_sites=n_sites,
        detail_rows=detail_rows_html,
        found_cards=found_cards_html,
        emails_section=emails_section,
        deep_section=deep_section,
        unknown_section=unknown_section_html,
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
    overall=None,
    clusters=None,
    emails=None,
    deep_evidence=None,
) -> None:
    """Dispatch by extension. Defaults to JSON if the suffix is unrecognised."""
    suffix = path.suffix.lower()
    if suffix == ".html" or suffix == ".htm":
        export_html(grouped, raw, elapsed, path, overall, clusters, emails, deep_evidence)
    elif suffix == ".md" or suffix == ".markdown" or suffix == ".txt":
        export_markdown(grouped, raw, elapsed, path, overall, clusters)
    else:
        export_json(grouped, raw, elapsed, path, overall, clusters, emails, deep_evidence)


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
        "username", nargs="*",
        help="username (or first + last name) to look up. Multi-word inputs "
             "are joined with a space and treated as a name. Optional when "
             "using --api.",
    )
    p.add_argument(
        "--api", nargs="+", metavar="ARG",
        help="manage stored API keys instead of running a scan. "
             "Subcommands: 'add SERVICE KEY' to save a key, 'list' to show "
             "configured services. Keys live in ~/.config/phantom/apis.json.",
    )
    p.add_argument(
        "--email", action="store_true",
        help="for each FOUND profile, query Hunter.io email-finder using "
             "the profile's display name and the site's domain. Requires a "
             "Hunter.io key (set with: phantom --api add hunter YOUR_KEY).",
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
    p.add_argument(
        "--photo-deep", dest="photo_deep", action="store_true", default=True,
        help="use deep photo matching (DINOv2 image embeddings + Face++ "
             "face compare + Yandex reverse image search) on top of "
             "perceptual-hash clustering. ON by default. Each provider is "
             "skipped silently when its credentials aren't configured "
             "(see --api add huggingface / facepp_key / facepp_secret). "
             "Yandex reverse search needs no key.",
    )
    p.add_argument(
        "--no-photo-deep", dest="photo_deep", action="store_false",
        help="disable deep photo matching (--photo-deep is on by default)",
    )
    p.add_argument(
        "--identity-hint", metavar="REPORT.json",
        help="path to a previous Phantom JSON report. In name mode, FOUND "
             "hits whose profile location or bio language clearly contradict "
             "the hint's country / language are reclassified as UNKNOWN — "
             "cuts down on collisions with strangers who happen to share a "
             "name-derived handle.",
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

    if args.api:
        return _run_api_subcommand(args.api)

    raw = " ".join(args.username).strip()
    if not raw:
        print(
            "error: missing username. Pass a username (or first + last name), "
            "or use --api to manage stored keys.",
            file=sys.stderr,
        )
        return 2

    hunter_key: Optional[str] = None
    if args.email:
        hunter_key = apis.get("hunter")
        if not hunter_key:
            print(
                "warning: --email requires a Hunter.io API key. Add one with:\n"
                "  phantom --api add hunter YOUR_KEY\n"
                "Continuing the scan without email lookup.",
                file=sys.stderr,
            )

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

    is_name_mode = len(raw.split()) >= 2
    hint: Optional[dict] = None
    if args.identity_hint:
        if not is_name_mode:
            print(
                "warning: --identity-hint is ignored outside name mode "
                "(input must be two or more space-separated tokens).",
                file=sys.stderr,
            )
        else:
            hint_path = Path(args.identity_hint)
            if not hint_path.is_file():
                print(f"error: --identity-hint file not found: {hint_path}", file=sys.stderr)
                return 2
            hint = _load_identity_hint(hint_path)
            if hint:
                bits = []
                if hint.get("country"): bits.append(f"country={hint['country']}")
                if hint.get("language"): bits.append(f"language={hint['language']}")
                print(
                    f"identity hint: filtering FOUND hits against {', '.join(bits)} "
                    f"(from {hint['source']})",
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
        if hint:
            n_filtered = _filter_results_by_hint(results, hint)
            if n_filtered:
                print(
                    f"identity hint: discarded {n_filtered} FOUND hit"
                    f"{'s' if n_filtered != 1 else ''} (location or language "
                    f"mismatch).",
                    file=sys.stderr,
                )
        emails: dict[str, dict] = {}
        if hunter_key:
            found_for_email = [
                r for _, rs in results for r in rs if r.exists is True
            ]
            if found_for_email:
                emails = await discover_emails(found_for_email, hunter_key)
                _attach_emails_to_found(results, emails)
        if args.no_identity:
            return results, None, [], emails, None
        found_dicts: list[dict] = []
        for _, rs in results:
            for r in rs:
                if r.exists is True:
                    found_dicts.append(asdict(r))
        # Dedupe before correlation so a single profile reached via
        # several URL aliases doesn't inflate the photo-match cluster.
        found_dicts = _dedupe_same_site_dicts(found_dicts)
        deep_options = photo_deep.options_from_apis(enabled=args.photo_deep)
        overall, clusters, deep_evidence = await build_overall_and_clusters(
            found_dicts, deep_options=deep_options,
        )
        return results, overall, clusters, emails, deep_evidence

    start = time.monotonic()
    grouped, overall, clusters, emails, deep_evidence = asyncio.run(_scan_and_correlate())
    elapsed = time.monotonic() - start
    cache.save()

    if args.as_json:
        payload = _build_json_payload(grouped, raw, elapsed, overall, clusters, emails, deep_evidence)
        print(json.dumps(payload, indent=2))
    elif not args.quiet:
        print_compact(grouped, elapsed, color, args.found_only)
        if emails:
            found_for_print, _, _ = _flatten(grouped)
            _print_emails_section(found_for_print, emails, color)
        if not args.found_only:
            _print_identity_summary(overall, clusters, color)
            if deep_evidence is not None:
                _print_deep_evidence(grouped, deep_evidence, color)

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
        export_report(grouped, raw, elapsed, export_path, overall, clusters, emails, deep_evidence)
        print(
            f"{_c(color,'dim')}Report written to {export_path}{_c(color,'reset')}",
            file=sys.stderr,
        )

    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
