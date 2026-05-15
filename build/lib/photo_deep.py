"""Deep photo-matching providers for Phantom.

Three optional providers that augment the built-in phash clustering when
`--photo-deep` is enabled (the default):

  1. Hugging Face Inference API + DINOv2 — semantic image embeddings.
     Catches "same logo recoloured", "same art different crop",
     "same photo with filter" — the cases where phash silently fails
     because the byte-level transformation is large but the visual
     content is identical.

  2. Face++ Compare — pairwise face matching. Only fires on avatars
     that contain a face (Face++'s detect step gates this for free).
     Merges accounts whose owners use very different selfies.

Each provider is independent and skipped when its credentials aren't
configured. Embeddings are cached per-URL in `~/.cache/phantom/embeds/`
so repeat runs cost nothing.

Reverse-image search (Yandex/Google scraping) was removed: the matches
were too often visually-similar but unrelated images (logos, default
avatars, font samples) rather than identity matches. Re-introduce only
when a face-anchored verification step is in place.

Public surface:

  - PhotoDeepOptions: bag of toggles + creds, built from CLI + apis.py
  - DeepEvidence: extra clustering edges + diagnostic notes
  - run_deep(found, photo_urls, photo_bytes, options) -> DeepEvidence

`run_deep` is async; call it from inside the existing identity build
pipeline after photo bytes are already fetched, so we don't re-download.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Any

import aiohttp


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Cosine similarity above which DINOv2 thinks two images are the same
# subject. DINOv2 base produces 768-dim normalised embeddings; 0.85 is
# the empirical floor where same-logo-different-colour reliably clusters
# without pulling in unrelated images.
_DINO_MATCH_COSINE = 0.85

# Face++ compare confidence — the API returns 0–100; anything above 80
# is "same person with high confidence" per their docs.
_FACEPP_MATCH_CONFIDENCE = 80.0

# Network budgets. Deep providers run in addition to phash, so they
# need their own (slightly looser) timeouts.
_HF_TIMEOUT = 30.0          # HF first call cold-starts the model
_FACEPP_TIMEOUT = 15.0
# HF model. DINOv2 base gives 768-dim embeddings via feature-extraction.
# As of late-2024/2025 HF retired the legacy `api-inference.huggingface.co`
# route for non-warm models and moved to a router that proxies to the
# `hf-inference` provider (or paid third-party providers). We try the
# router URL first; the legacy URL is kept as a last-ditch fallback for
# accounts still hitting the old endpoint.
_HF_MODEL = "facebook/dinov2-base"
_HF_URLS = (
    f"https://router.huggingface.co/hf-inference/models/{_HF_MODEL}",
    f"https://api-inference.huggingface.co/models/{_HF_MODEL}",
)

# Face++ region. `api-us` is the global endpoint; `api-cn` requires
# China-mainland account.
_FACEPP_DETECT_URL = "https://api-us.faceplusplus.com/facepp/v3/detect"
_FACEPP_COMPARE_URL = "https://api-us.faceplusplus.com/facepp/v3/compare"


# Concurrency caps. HF inference API rate-limits free tokens fairly
# aggressively; keep this conservative.
_HF_CONCURRENCY = 3
_FACEPP_CONCURRENCY = 2

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PhotoDeepOptions:
    enabled: bool = True
    hf_token: Optional[str] = None
    facepp_key: Optional[str] = None
    facepp_secret: Optional[str] = None

    @property
    def has_dino(self) -> bool:
        return bool(self.hf_token)

    @property
    def has_facepp(self) -> bool:
        return bool(self.facepp_key) and bool(self.facepp_secret)


@dataclass
class DeepEvidence:
    """Output of run_deep — edges to feed into clustering + side data."""
    # Pairs (i, j, rationale) that the clustering union-find should merge.
    extra_edges: list[tuple[int, int, str]] = field(default_factory=list)
    # Diagnostic notes shown in the report ("dino: 12 embeddings",
    # "facepp: skipped 6 non-face avatars", etc.).
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Embedding cache (per-URL, on-disk JSON)
# ---------------------------------------------------------------------------

def _cache_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))
    p = base / "phantom" / "embeds"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cache_key(url: str, model: str) -> Path:
    h = hashlib.sha256(f"{model}|{url}".encode()).hexdigest()[:24]
    return _cache_dir() / f"{h}.json"


def _cache_get(url: str, model: str) -> Optional[list[float]]:
    p = _cache_key(url, model)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, list) and all(isinstance(x, (int, float)) for x in data):
        return [float(x) for x in data]
    return None


def _cache_put(url: str, model: str, vec: list[float]) -> None:
    try:
        _cache_key(url, model).write_text(
            json.dumps(vec), encoding="utf-8",
        )
    except OSError:
        pass


# ---------------------------------------------------------------------------
# DINOv2 via Hugging Face Inference API
# ---------------------------------------------------------------------------

def _flatten_embedding(raw: Any) -> Optional[list[float]]:
    """HF returns embeddings as nested lists of varying depth depending on
    the model. DINOv2 returns either:
        - 1D: [768 floats]
        - 2D: [[768 floats], ...] where outer is patch tokens
        - 3D: [[[float, ...], ...]]
    We mean-pool across non-final dims to get one 768-dim CLS-style vector.
    """
    if isinstance(raw, list) and raw:
        # Detect depth.
        cur = raw
        depth = 0
        while isinstance(cur, list) and cur and isinstance(cur[0], list):
            cur = cur[0]
            depth += 1
        if depth == 0:
            return [float(x) for x in raw if isinstance(x, (int, float))]
        if depth == 1:
            # Mean-pool over outer (patch tokens).
            cols = list(zip(*raw))
            return [sum(c) / len(c) for c in cols]
        if depth == 2:
            # Strip the leading batch dim and mean-pool.
            inner = raw[0]
            if not inner:
                return None
            cols = list(zip(*inner))
            return [sum(c) / len(c) for c in cols]
    return None


def _l2_normalise(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec))
    if n == 0:
        return vec
    return [x / n for x in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))  # both pre-normalised


async def _hf_post_one(
    session: aiohttp.ClientSession,
    url: str,
    image_bytes: bytes,
    headers: dict,
) -> tuple[int, Any]:
    """Single POST + cold-start retry. Returns (status, body) where body
    is the parsed JSON on 200 or a short text snippet on error.
    """
    async with session.post(
        url,
        headers=headers,
        data=image_bytes,
        timeout=aiohttp.ClientTimeout(total=_HF_TIMEOUT),
    ) as resp:
        if resp.status == 503:
            await asyncio.sleep(min(20.0, float(resp.headers.get("Retry-After", 5))))
            async with session.post(
                url,
                headers=headers,
                data=image_bytes,
                timeout=aiohttp.ClientTimeout(total=_HF_TIMEOUT),
            ) as resp2:
                if resp2.status == 200:
                    return 200, await resp2.json()
                return resp2.status, (await resp2.text(errors="ignore"))[:200]
        if resp.status == 200:
            return 200, await resp.json()
        return resp.status, (await resp.text(errors="ignore"))[:200]


async def _hf_embed_one(
    session: aiohttp.ClientSession,
    image_bytes: bytes,
    token: str,
    diag: dict,
) -> Optional[list[float]]:
    """Call HF Inference API for one image. Tries the router URL first
    and falls back to the legacy URL only if the first one returns 404.
    On terminal failure, records the first failure reason into `diag`
    so the orchestrator can surface a single useful note.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
        "Accept": "application/json",
    }

    def _record(reason: str) -> None:
        if "first_failure" not in diag:
            diag["first_failure"] = reason

    last_status = None
    last_body: Any = None
    for url in _HF_URLS:
        try:
            status, body = await _hf_post_one(session, url, image_bytes, headers)
        except Exception as e:
            _record(f"network error: {type(e).__name__}: {e}")
            return None
        if status == 200:
            vec = _flatten_embedding(body)
            if not vec:
                _record(f"unexpected payload shape: {str(body)[:200]}")
                return None
            return _l2_normalise(vec)
        last_status, last_body = status, body
        # Only fall through to the next URL on routing-style failures.
        # Auth/quota errors apply to every URL.
        if status not in (404, 405):
            break

    snippet = last_body if isinstance(last_body, str) else str(last_body)[:200]
    _record(f"HTTP {last_status}: {snippet.strip()[:200]}")
    return None


async def compute_dino_embeddings(
    photo_urls: list[Optional[str]],
    photo_bytes: list[Optional[bytes]],
    token: str,
) -> tuple[list[Optional[list[float]]], dict]:
    """Embed each photo via HF + DINOv2. Cached by URL.

    Returns (embeddings, diag). `embeddings` is parallel to photo_urls;
    None entries indicate no photo, no bytes, or an embedding failure.
    `diag` carries the first failure reason for surfacing in notes.
    """
    out: list[Optional[list[float]]] = [None] * len(photo_urls)
    pending: list[int] = []
    diag: dict = {}

    for i, url in enumerate(photo_urls):
        if not url or not photo_bytes[i]:
            continue
        cached = _cache_get(url, _HF_MODEL)
        if cached:
            out[i] = cached
        else:
            pending.append(i)

    if not pending:
        return out, diag

    sem = asyncio.Semaphore(_HF_CONCURRENCY)
    async with aiohttp.ClientSession() as session:
        async def one(idx: int):
            async with sem:
                vec = await _hf_embed_one(session, photo_bytes[idx], token, diag)
            if vec:
                out[idx] = vec
                if photo_urls[idx]:
                    _cache_put(photo_urls[idx], _HF_MODEL, vec)

        await asyncio.gather(*(one(i) for i in pending))

    return out, diag


def dino_pairs(embeddings: list[Optional[list[float]]]) -> list[tuple[int, int, float]]:
    """Return (i, j, cosine) for every pair above the match threshold."""
    out: list[tuple[int, int, float]] = []
    n = len(embeddings)
    for i in range(n):
        if not embeddings[i]:
            continue
        for j in range(i + 1, n):
            if not embeddings[j]:
                continue
            c = _cosine(embeddings[i], embeddings[j])
            if c >= _DINO_MATCH_COSINE:
                out.append((i, j, c))
    return out


# ---------------------------------------------------------------------------
# Face++ pairwise compare
# ---------------------------------------------------------------------------

async def _facepp_detect(
    session: aiohttp.ClientSession,
    image_bytes: bytes,
    key: str,
    secret: str,
) -> bool:
    """True if Face++ found at least one face in `image_bytes`."""
    form = aiohttp.FormData()
    form.add_field("api_key", key)
    form.add_field("api_secret", secret)
    form.add_field("image_file", image_bytes, filename="x.jpg")
    try:
        async with session.post(
            _FACEPP_DETECT_URL,
            data=form,
            timeout=aiohttp.ClientTimeout(total=_FACEPP_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                return False
            payload = await resp.json()
    except Exception:
        return False
    return bool(payload.get("faces"))


async def _facepp_compare(
    session: aiohttp.ClientSession,
    bytes_a: bytes,
    bytes_b: bytes,
    key: str,
    secret: str,
) -> Optional[float]:
    """Returns Face++ confidence (0–100) for the pair, or None on error."""
    form = aiohttp.FormData()
    form.add_field("api_key", key)
    form.add_field("api_secret", secret)
    form.add_field("image_file1", bytes_a, filename="a.jpg")
    form.add_field("image_file2", bytes_b, filename="b.jpg")
    try:
        async with session.post(
            _FACEPP_COMPARE_URL,
            data=form,
            timeout=aiohttp.ClientTimeout(total=_FACEPP_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                return None
            payload = await resp.json()
    except Exception:
        return None
    conf = payload.get("confidence")
    if isinstance(conf, (int, float)):
        return float(conf)
    return None


async def compute_facepp_pairs(
    photo_bytes: list[Optional[bytes]],
    candidate_pairs: list[tuple[int, int]],
    key: str,
    secret: str,
) -> tuple[list[tuple[int, int, float]], int]:
    """Run Face++ Compare on every candidate pair where both bytes exist
    and both contain faces. Returns (matched_pairs, n_face_skipped).
    """
    if not candidate_pairs:
        return [], 0

    sem = asyncio.Semaphore(_FACEPP_CONCURRENCY)
    matched: list[tuple[int, int, float]] = []
    has_face: dict[int, bool] = {}
    n_skipped = 0

    async with aiohttp.ClientSession() as session:
        async def detect(idx: int):
            if idx in has_face:
                return
            if not photo_bytes[idx]:
                has_face[idx] = False
                return
            async with sem:
                has_face[idx] = await _facepp_detect(
                    session, photo_bytes[idx], key, secret,
                )

        # Detect faces only for indices appearing in candidate pairs.
        relevant = sorted({i for pair in candidate_pairs for i in pair})
        await asyncio.gather(*(detect(i) for i in relevant))

        async def compare(i: int, j: int):
            nonlocal n_skipped
            if not (has_face.get(i) and has_face.get(j)):
                n_skipped += 1
                return
            async with sem:
                conf = await _facepp_compare(
                    session, photo_bytes[i], photo_bytes[j], key, secret,
                )
            if conf is not None and conf >= _FACEPP_MATCH_CONFIDENCE:
                matched.append((i, j, conf))

        await asyncio.gather(*(compare(i, j) for i, j in candidate_pairs))

    return matched, n_skipped


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------

def _candidate_face_pairs(
    n: int,
    existing_edges: set[tuple[int, int]],
    dino_edges: list[tuple[int, int, float]],
) -> list[tuple[int, int]]:
    """Pick pairs worth Face++-comparing.

    Strategy: don't burn API calls on pairs already merged by phash or
    DINO. Send the rest (close-but-below DINO threshold or simply
    unmerged pairs in small clusters) so face recognition gets a shot
    at the photos that visual hashing didn't already settle.
    """
    chosen: list[tuple[int, int]] = []
    in_dino = {(i, j) for i, j, _ in dino_edges}
    # Candidates: everything dino didn't already promote, plus ALL pairs
    # if we have very few results (≤ 6) — small N, no reason to skip.
    full_pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    if n <= 6:
        for p in full_pairs:
            if p not in existing_edges and p not in in_dino:
                chosen.append(p)
        return chosen
    # For larger N, only pursue the top-K most visually similar pairs
    # to control cost. K = n * 2 keeps it bounded.
    # Caller already gives us dino edges only ABOVE threshold; we need
    # the close-but-below set, which we approximate by adding a few
    # random unmerged pairs. Without storing all cosines, fall back to
    # "every unmerged pair up to a cap".
    cap = max(20, n * 2)
    for p in full_pairs:
        if p in existing_edges or p in in_dino:
            continue
        chosen.append(p)
        if len(chosen) >= cap:
            break
    return chosen


async def run_deep(
    found: list[dict],
    photo_urls: list[Optional[str]],
    photo_bytes: list[Optional[bytes]],
    options: PhotoDeepOptions,
    existing_edges: Optional[set[tuple[int, int]]] = None,
) -> DeepEvidence:
    """Run every enabled deep provider and assemble evidence.

    `existing_edges` is the set of (i,j) pairs that phash already merged;
    used to avoid double-billing Face++ on pairs we already know about.
    """
    ev = DeepEvidence()
    if not options.enabled or not found:
        return ev

    existing_edges = existing_edges or set()

    # 1. DINOv2 semantic embedding match.
    dino_pairs_out: list[tuple[int, int, float]] = []
    if options.has_dino:
        embeds, dino_diag = await compute_dino_embeddings(
            photo_urls, photo_bytes, options.hf_token,
        )
        n_embed = sum(1 for e in embeds if e)
        if n_embed == 0 and dino_diag.get("first_failure"):
            ev.notes.append(
                f"dino: 0 embeddings ({dino_diag['first_failure'][:120]})"
            )
        else:
            ev.notes.append(f"dino: {n_embed} embedding(s)")
        dino_pairs_out = dino_pairs(embeds)
        for i, j, c in dino_pairs_out:
            if (i, j) not in existing_edges:
                ev.extra_edges.append(
                    (i, j, f"matching image content (dino cosine={c:.2f})")
                )

    # 2. Face++ pairwise on candidates not already merged.
    if options.has_facepp:
        candidate_pairs = _candidate_face_pairs(
            len(found), existing_edges, dino_pairs_out,
        )
        face_pairs, n_skipped = await compute_facepp_pairs(
            photo_bytes, candidate_pairs,
            options.facepp_key, options.facepp_secret,
        )
        ev.notes.append(
            f"facepp: {len(face_pairs)} match(es), {n_skipped} no-face skipped"
        )
        for i, j, conf in face_pairs:
            if (i, j) not in existing_edges:
                ev.extra_edges.append(
                    (i, j, f"matching face (face++ confidence={conf:.0f})")
                )

    return ev


# ---------------------------------------------------------------------------
# CLI helpers (called from checker.py)
# ---------------------------------------------------------------------------

def options_from_apis(enabled: bool) -> PhotoDeepOptions:
    """Build PhotoDeepOptions by reading every relevant key from apis.py."""
    try:
        import apis
    except Exception:
        return PhotoDeepOptions(enabled=enabled)
    return PhotoDeepOptions(
        enabled=enabled,
        hf_token=apis.get("huggingface"),
        facepp_key=apis.get("facepp_key"),
        facepp_secret=apis.get("facepp_secret"),
    )


def configured_summary(opts: PhotoDeepOptions) -> str:
    """One-line human-readable summary of which providers are wired up."""
    parts: list[str] = []
    parts.append("dino" + ("" if opts.has_dino else "(off)"))
    parts.append("facepp" + ("" if opts.has_facepp else "(off)"))
    parts.append("yandex")
    return ", ".join(parts)
