"""Third HTTP backend — headless Chromium via Playwright.

Used for sites that defeat curl_cffi's TLS impersonation: Cloudflare
interactive challenges, JS-only SPAs that need a real browser to render
the SSR fallback, and platforms that watch for behavioural signals beyond
the TLS handshake (mouse events, animation frames, etc.).

The backend manages a single long-lived browser instance with multiple
pages so per-request startup cost (~1s for a fresh browser) is paid once.
Each request gets its own incognito context so cookies / storage don't
leak between sites in a multi-variant scan.

Use cases:
- Sites flagged `protection: ["js_challenge"]` in sites.json route here
  automatically.
- `--js-render` on the CLI forces *every* site through this backend (slow,
  but useful for debugging or testing on networks where everything else
  is blocked).

Trade-offs vs. curl_cffi:
- Slower: ~1-3s per page-load vs. ~200ms for an HTTP request.
- Heavier: ~200MB resident for the browser.
- More realistic: real Chrome, real JS execution, real rendering.

The backend defers Playwright import until first use — `phantom` without
js_challenge sites and without `--js-render` never pays the cost.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional


# Default cap on concurrent in-flight pages. Each page costs ~50MB and
# CPU spikes during JS execution, so we keep this small even when the
# global scanner concurrency is high.
DEFAULT_JS_CONCURRENCY = 3

# Default navigation timeout in ms. Note this is independent of the
# scanner's timeout — that one applies on top via asyncio.wait_for.
_NAV_TIMEOUT_MS = 20_000

# What we consider "page is settled enough to read" — `networkidle` waits
# for 500ms of zero in-flight requests, which is the right point to grab
# rendered content for most SPAs. `domcontentloaded` is too early
# (Cloudflare challenge hasn't resolved); `load` is too late (waits on
# images and analytics).
_WAIT_UNTIL = "domcontentloaded"


class PlaywrightFetcher:
    """Lazy-initialised Playwright wrapper.

    Lifecycle:
        f = PlaywrightFetcher(concurrency=3)
        await f.start()                # spawns the browser
        status, body, final_url = await f.fetch(url)
        await f.close()                # cleans up

    `start()` is idempotent — safe to call multiple times. `close()` is
    also idempotent and is a no-op when the backend was never started.
    """

    def __init__(
        self,
        *,
        concurrency: int = DEFAULT_JS_CONCURRENCY,
        timeout_seconds: float = 25.0,
        wait_for_selector: Optional[str] = None,
    ) -> None:
        self.concurrency = concurrency
        self.timeout_seconds = timeout_seconds
        self.wait_for_selector = wait_for_selector
        self._pw = None              # the playwright runtime
        self._browser = None         # the long-lived browser instance
        self._sem: Optional[asyncio.Semaphore] = None
        self._started = False
        self._start_lock = asyncio.Lock()

    async def start(self) -> None:
        """Spawn the browser if it isn't running yet."""
        if self._started:
            return
        async with self._start_lock:
            if self._started:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as e:
                raise RuntimeError(
                    "playwright is not installed — run "
                    "`pip install playwright && playwright install chromium`"
                ) from e
            self._pw = await async_playwright().start()
            # Headless Chromium with a couple of stability flags. We avoid
            # `--no-sandbox` to keep the security model intact (sandbox is
            # only an issue when running as root in a container).
            self._browser = await self._pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            self._sem = asyncio.Semaphore(self.concurrency)
            self._started = True

    async def close(self) -> None:
        """Tear the browser down. Safe to call if never started."""
        if not self._started:
            return
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()
            self._browser = None
            self._pw = None
            self._started = False

    async def fetch(
        self,
        url: str,
        *,
        headers: Optional[dict] = None,
        user_agent: Optional[str] = None,
    ) -> tuple[int, str, str]:
        """Fetch `url` through headless Chromium.

        Returns `(status, body, final_url)`. Status is the HTTP status of
        the main navigation response (the page itself, not subresources).
        Body is the rendered HTML after `domcontentloaded`. `final_url`
        is the URL after any redirects.

        Raises:
            asyncio.TimeoutError: navigation didn't finish in time
            RuntimeError: any other Playwright error
        """
        await self.start()
        assert self._sem is not None and self._browser is not None

        async with self._sem:
            # Fresh incognito context per request so cookies / storage
            # don't leak across sites. Cheap (~50ms) compared to launching
            # the browser itself.
            ctx_kwargs: dict = {}
            if user_agent:
                ctx_kwargs["user_agent"] = user_agent
            if headers:
                ctx_kwargs["extra_http_headers"] = headers
            context = await self._browser.new_context(**ctx_kwargs)
            page = await context.new_page()
            try:
                response = await asyncio.wait_for(
                    page.goto(
                        url,
                        wait_until=_WAIT_UNTIL,
                        timeout=_NAV_TIMEOUT_MS,
                    ),
                    timeout=self.timeout_seconds,
                )
                # When a site uses a JS challenge, the initial response is
                # the challenge page (200, body is JS). After it resolves
                # the page navigates to the real content. Allow a short
                # quiet period for that to happen.
                if self.wait_for_selector:
                    try:
                        await page.wait_for_selector(
                            self.wait_for_selector,
                            timeout=5_000,
                        )
                    except Exception:
                        pass  # selector optional; fall through to body grab
                else:
                    # Default settle: give the page 800ms to finish any
                    # post-load JS that swaps the body. Tuned conservatively.
                    await page.wait_for_timeout(800)
                body = await page.content()
                status = response.status if response else 0
                final_url = page.url
                return status, body, final_url
            finally:
                await context.close()
