"""Phantom CLI entrypoint — argparse, --api subcommand, main()."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Optional

import apis
import confidence as _confidence
import disambiguation as _disambiguation
import photo_deep
from cache import ResponseCache
from dedupe import _dedupe_same_site_dicts, _flatten
from emails import _attach_emails_to_found, _print_emails_section, discover_emails
from exporters import export_report, resolve_export_path
from exporters.json_export import _build_json_payload
from hints import _filter_results_by_hint, _load_identity_hint
from identity import build_overall_and_clusters
from models import USERNAME_PATTERN, load_sites
from scanner import HAS_CURL_CFFI, Phantom
from terminal import _c, _print_identity_summary, print_compact
from variants import generate as generate_variants
from watch import (
    Snapshot, diff as compute_diff, load_history, render_diff_terminal,
    save_snapshot,
)


def _default_sites_path() -> str:
    """Locate sites.json across two install shapes.

    1. Dev / git-clone install: sites.json sits next to cli.py.
    2. pip install: sites.json is shipped to `share/phantom/` via
       pyproject.toml's [tool.setuptools.data-files] block. Relative
       to the running interpreter that's `<sys.prefix>/share/phantom/
       sites.json` (or `~/.local/share/phantom/sites.json` for a
       --user install).

    Return the first one that exists; fall back to the dev-layout
    string so the error message points somewhere obvious if neither
    is present.
    """
    here = Path(__file__).with_name("sites.json")
    if here.is_file():
        return str(here)
    # pip install --user → ~/.local/share/phantom/sites.json
    # pip install (venv) → <venv>/share/phantom/sites.json
    for prefix in (sys.prefix, sys.exec_prefix):
        if prefix:
            candidate = Path(prefix) / "share" / "phantom" / "sites.json"
            if candidate.is_file():
                return str(candidate)
    # Last resort: pip's user-site share
    import site
    user_base = getattr(site, "USER_BASE", None)
    if user_base:
        candidate = Path(user_base) / "share" / "phantom" / "sites.json"
        if candidate.is_file():
            return str(candidate)
    return str(here)  # fall through; the error message will surface this


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
        "--sites", default=_default_sites_path(),
        help="path to sites.json (default: alongside this script, or "
             "<venv>/share/phantom/sites.json when pip-installed)",
    )
    p.add_argument("--concurrency", type=int, default=25)
    p.add_argument(
        "--per-host-concurrency", type=int, default=3, metavar="N",
        help="cap on simultaneous in-flight requests to any single host "
             "(default: 3). Prevents rate-limit throttling on Instagram, "
             "TikTok, etc. when running many variants against few hosts.",
    )
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
        "--country", action="append", default=None, metavar="CC",
        help="restrict to sites tagged with this country (ISO 3166-1 alpha-2 "
             "lowercase, e.g. --country us). 'global' matches platforms with "
             "no geographic affinity. Repeatable. Sites without a country tag "
             "are kept unless --strict-tags is set.",
    )
    p.add_argument(
        "--language", action="append", default=None, metavar="LANG",
        help="restrict to sites tagged with this language (ISO 639-1, e.g. "
             "--language en). 'global' matches multi-lingual platforms. "
             "Repeatable.",
    )
    p.add_argument(
        "--content-type", action="append", default=None, metavar="TYPE",
        help="restrict to sites of this content type (photo, text, code, "
             "audio, video, links, mixed). Repeatable.",
    )
    p.add_argument(
        "--strict-tags", action="store_true",
        help="when --country / --language / --content-type is set, drop sites "
             "with no tag set on that field. Default: keep untagged sites.",
    )
    p.add_argument(
        "--no-impersonate", action="store_true",
        help="disable curl_cffi browser impersonation, even if installed "
             "(sites flagged with tls_fingerprint will likely fail)",
    )
    p.add_argument(
        "--proxy", metavar="URL",
        help="route all HTTP requests through a proxy. Accepted schemes: "
             "http://, https://, socks5://, socks5h://. Example: "
             "--proxy socks5://127.0.0.1:9050 (Tor). The same URL is used "
             "for both the aiohttp and curl_cffi backends.",
    )
    p.add_argument(
        "--js-render", action="store_true",
        help="force every site through the Playwright (headless Chromium) "
             "backend. Off by default — only sites flagged with "
             "protection=js_challenge in sites.json use Playwright. "
             "Slower (~2s per page), but cracks Cloudflare turnstile and "
             "JS-only SPAs. Requires `playwright install chromium`.",
    )
    p.add_argument(
        "--js-concurrency", type=int, default=3, metavar="N",
        help="max in-flight Playwright pages (default: 3). Each page costs "
             "~50MB RAM and CPU during JS execution; keep small.",
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
        "--resume", action="store_true",
        help="resume an interrupted scan: extend the cache TTL to 7 days and "
             "reuse every prior verdict, then refetch only the requests that "
             "didn't complete. Reports the reuse ratio at the end.",
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
    p.add_argument(
        "--expand", action="store_true",
        help="after the first scan, harvest @handles from linked accounts "
             "(Keybase proofs, Dev.to / About.me sameAs, Linktree links, "
             "GitHub blog / x_handle, etc.) and run additional rounds. "
             "Depth, handle count, and wall time are bounded by the flags "
             "below.",
    )
    p.add_argument(
        "--expand-depth", type=int, default=2, metavar="N",
        help="how many recursion rounds to run when --expand is set "
             "(default: 2, max: 4). Each round's discoveries feed the "
             "next round's harvest. Confidence-boosted: handles from a "
             "Keybase proof start with +30, JSON-LD sameAs +15, Linktree +5.",
    )
    p.add_argument(
        "--expand-max-handles", type=int, default=50, metavar="N",
        help="cap on total new handles scanned across all expand rounds "
             "(default: 50). Stops a runaway when a linktree user lists "
             "every social platform on Earth.",
    )
    p.add_argument(
        "--expand-max-time", type=float, default=300.0, metavar="SECONDS",
        help="cap on wall-clock seconds spent in expansion rounds "
             "(default: 300). Once exceeded, no further rounds start.",
    )
    p.add_argument(
        "--parse", metavar="URL",
        help="parse a profile URL to extract the username and scan that "
             "(implies --exact). Supports the same platforms the cross-link "
             "expander knows about. Mutually replaces the positional "
             "username argument.",
    )
    p.add_argument(
        "--wayback", action="store_true",
        help="after the scan, query the Wayback Machine for each FOUND "
             "account's oldest archived snapshot. Surfaces historical "
             "evidence (account age, deletion dates) and stamps "
             "wayback_first_snapshot + wayback_archive_url onto each "
             "profile dict. Free public API, no auth required.",
    )
    p.add_argument(
        "--tui", action="store_true",
        help="open the full Textual TUI (live results, in-terminal "
             "dossier explorer). Running `phantom` with no args opens "
             "the simpler ASCII launcher instead.",
    )
    p.add_argument(
        "--self-check", dest="self_check", action="store_true",
        help="probe a curated canary handle on each site (tests/"
             "canaries.json) and report sites that have drifted. Doesn't "
             "take a username argument. Use to catch silent site-API "
             "changes before they bite a real scan.",
    )
    p.add_argument(
        "--self-check-verbose", action="store_true",
        help="when used with --self-check, print every result row not "
             "just the drifted ones.",
    )
    p.add_argument(
        "--github-deep", action="store_true",
        help="when GitHub is FOUND, fetch deep public data: organizations, "
             "recently-starred repos, verified linked social accounts, "
             "and a commit-email leak via .patch download (catches ~70%% "
             "of accounts that haven't enabled email-privacy). Adds "
             "4–6 unauthenticated GitHub API requests per GitHub hit.",
    )
    p.add_argument(
        "--photo-ocr", action="store_true",
        help="run Tesseract OCR over each FOUND avatar and feed any "
             "handle-shaped text back into the variant queue. Catches "
             "users who put their handle on their PFP. Requires "
             "`apt install tesseract-ocr && pip install pytesseract`. "
             "Implies --expand (the recovered handles only matter if "
             "we re-scan with them).",
    )
    p.add_argument("--found-only", action="store_true", help="only print hits")
    p.add_argument(
        "--show-all", action="store_true",
        help="include 'possible impostor' accounts in terminal output "
             "(default: show count only)",
    )
    p.add_argument(
        "--no-cluster", action="store_true",
        help="disable identity disambiguation clustering; show flat tier-based "
             "output instead of grouped-by-identity output",
    )
    p.add_argument("--json", dest="as_json", action="store_true", help="emit JSON results to stdout")
    p.add_argument(
        "--export", metavar="FILE_OR_FORMAT",
        help="write a structured report. Pass a format ('html', 'json', 'md', 'pdf') "
             "and the file is auto-named '<input>_report.<ext>' in the cwd. "
             "Pass a path with extension (e.g. 'reports/out.html') to write "
             "exactly there.",
    )
    _theme = p.add_mutually_exclusive_group()
    _theme.add_argument(
        "--dark", action="store_true",
        help="use dark theme for HTML/PDF exports (default is light)",
    )
    _theme.add_argument(
        "--light", action="store_true",
        help="use light theme for HTML/PDF exports (default)",
    )
    p.add_argument("--no-color", action="store_true")
    return p.parse_args(argv)
def main(argv: Optional[list[str]] = None) -> int:
    # --- TUI mode short-circuit -------------------------------------
    # `phantom` with no args (and a real TTY) opens the interactive
    # ASCII-art launcher. `phantom --tui` opens the full Textual app.
    # We branch BEFORE argparse so the launcher doesn't trigger the
    # `error: missing username` path.
    raw_argv = sys.argv[1:] if argv is None else argv
    if raw_argv == ["--tui"]:
        try:
            from tui_app import run_tui
        except ImportError as e:
            print(
                "error: --tui requires the textual library.\n"
                "  pip install textual\n"
                f"  ({e})",
                file=sys.stderr,
            )
            return 2
        run_tui()
        return 0
    if not raw_argv:
        from tui_launcher import is_interactive_session, main_loop
        if is_interactive_session():
            return main_loop()
        # Non-interactive (piped) with no args: fall through so argparse
        # prints the usual usage error.

    args = parse_args(argv)

    if args.api:
        return _run_api_subcommand(args.api)

    # --- --self-check -------------------------------------------------
    # Probe a curated canary handle on each site. Branches early — no
    # username required, returns immediately with the drift count.
    if args.self_check:
        from self_check import run_self_check
        sites_path = Path(args.sites)
        if not sites_path.is_file():
            print(f"error: sites file not found: {sites_path}", file=sys.stderr)
            return 2
        drifted = asyncio.run(run_self_check(
            sites_path,
            concurrency=args.concurrency,
            timeout=args.timeout,
            impersonate=not args.no_impersonate,
            verbose=args.self_check_verbose,
        ))
        # Non-zero exit when any canary drifted, so CI / scripts catch it.
        return 1 if drifted else 0

    # --- --parse URL --------------------------------------------------
    # When --parse is set, derive the username from a profile URL via the
    # same platform→handle table the expander uses. Forces --exact so the
    # variant engine doesn't blow up an already-specific handle. Setting
    # both --parse and a positional username is an error.
    if args.parse:
        if args.username:
            print(
                "error: --parse URL replaces the positional username argument; "
                "don't pass both.",
                file=sys.stderr,
            )
            return 2
        from expand import _extract_one
        parsed_handle = _extract_one(args.parse)
        if not parsed_handle:
            print(
                f"error: could not extract a handle from {args.parse!r}. "
                f"The URL must point at a known platform's profile page "
                f"(e.g. https://github.com/<user>). See expand.py for the "
                f"recognised list.",
                file=sys.stderr,
            )
            return 2
        args.username = [parsed_handle]
        args.exact = True
        print(
            f"parse: extracted handle {parsed_handle!r} from {args.parse}",
            file=sys.stderr,
        )

    # Clamp expand-depth to the documented max so a user passing
    # --expand-depth 99 doesn't spin off into 99 rounds.
    if args.expand_depth < 1:
        args.expand_depth = 1
    elif args.expand_depth > 4:
        print(
            f"warning: --expand-depth clamped from {args.expand_depth} to 4 "
            f"(beyond that, latency and noise outweigh new findings)",
            file=sys.stderr,
        )
        args.expand_depth = 4

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

    def _tag_filter(attr: str, wanted: set[str]) -> None:
        nonlocal sites
        sites = [
            s for s in sites
            if (getattr(s, attr) and getattr(s, attr).lower() in wanted)
            or (not args.strict_tags and getattr(s, attr) is None)
        ]

    if args.country:
        _tag_filter("country", {c.lower() for c in args.country})
    if args.language:
        _tag_filter("language", {c.lower() for c in args.language})
    if args.content_type:
        _tag_filter("content_type", {c.lower() for c in args.content_type})

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

    from cache import _RESUME_TTL, _CACHE_TTL
    cache = ResponseCache(
        enabled=not args.no_cache,
        ttl_seconds=_RESUME_TTL if args.resume else _CACHE_TTL,
    )
    if args.resume and cache.enabled:
        prior_entries = len(cache._mem)
        print(
            f"resume: loaded {prior_entries} cached verdict"
            f"{'s' if prior_entries != 1 else ''} (7d TTL)",
            file=sys.stderr,
        )
    if args.proxy:
        print(f"proxy: routing through {args.proxy}", file=sys.stderr)
    phantom = Phantom(
        sites,
        concurrency=args.concurrency,
        per_host_concurrency=args.per_host_concurrency,
        timeout=args.timeout,
        impersonate=impersonate,
        retry_on_transient=not args.no_retry,
        cache=cache,
        proxy=args.proxy,
        js_render=args.js_render,
        js_concurrency=args.js_concurrency,
    )

    # Lives outside _scan_and_correlate so it survives to the scoring step.
    # Maps lower(handle) → source_kind for every handle the expansion
    # discovered, used by score_all() to apply a starting boost.
    expand_source_map: dict[str, str] = {}

    async def _scan_and_correlate():
        results = await phantom.run_many(variants)

        # --- Cross-link expansion ---------------------------------------
        # After the first scan completes, walk every FOUND profile's
        # linked-account fields and queue any handles we haven't tried
        # yet. Iterates up to --expand-depth rounds (default 2). Each
        # round's discoveries feed the next round's harvest. Hard caps on
        # total handles and wall-clock time prevent runaways.
        if args.expand:
            from expand import SOURCE_WEIGHTS, discover_new_handles
            tested: set[str] = set(variants)
            expand_start = time.monotonic()
            for round_no in range(1, args.expand_depth + 1):
                elapsed_so_far = time.monotonic() - expand_start
                if elapsed_so_far > args.expand_max_time:
                    print(
                        f"expand: stopping (time limit {args.expand_max_time}s "
                        f"reached after round {round_no - 1})",
                        file=sys.stderr,
                    )
                    break
                if len(tested) - len(variants) >= args.expand_max_handles:
                    print(
                        f"expand: stopping (handle limit "
                        f"{args.expand_max_handles} reached)",
                        file=sys.stderr,
                    )
                    break

                found_now = [
                    r for _, rs in results for r in rs if r.exists is True
                ]
                new_pairs = discover_new_handles(found_now, tested)
                if not new_pairs:
                    if round_no == 1:
                        # First round with nothing to expand — silent exit,
                        # the user already knows expand was requested.
                        pass
                    else:
                        print(
                            f"expand: round {round_no} found no new handles — "
                            f"stopping",
                            file=sys.stderr,
                        )
                    break

                # Respect the global cap when slicing this round.
                remaining = args.expand_max_handles - (len(tested) - len(variants))
                new_pairs = new_pairs[: max(0, remaining)]
                new_handles = [h for h, _ in new_pairs]
                for h, src in new_pairs:
                    expand_source_map[h.lower()] = src
                    tested.add(h)

                round_t0 = time.monotonic()
                preview = ", ".join(new_handles[:6])
                tail = "…" if len(new_handles) > 6 else ""
                print(
                    f"expand: round {round_no} discovered "
                    f"{len(new_handles)} new handle"
                    f"{'s' if len(new_handles) != 1 else ''} "
                    f"({preview}{tail}) — scanning",
                    file=sys.stderr,
                )
                expand_results = await phantom.run_many(new_handles)
                results.extend(expand_results)
                round_dt = time.monotonic() - round_t0
                round_found = sum(
                    1 for _, rs in expand_results for r in rs if r.exists is True
                )
                print(
                    f"expand: round {round_no} done in {round_dt:.1f}s — "
                    f"{round_found} new FOUND",
                    file=sys.stderr,
                )

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

        # --- GitHub deep-dive ----------------------------------------
        if args.github_deep:
            from github_deep import enrich_grouped as gh_enrich
            n_gh = await gh_enrich(results)
            if n_gh:
                print(
                    f"github-deep: enriched {n_gh} GitHub account"
                    f"{'s' if n_gh != 1 else ''} with orgs / starred "
                    f"repos / commit email",
                    file=sys.stderr,
                )

        # --- Wayback Machine historical lookup ------------------------
        if args.wayback:
            from wayback import attach_wayback_to_found, lookup_many
            urls = [
                r.url for _, rs in results for r in rs
                if r.exists is True and r.url
            ]
            urls = list(dict.fromkeys(urls))  # de-dup preserving order
            if urls:
                print(
                    f"wayback: querying {len(urls)} URL"
                    f"{'s' if len(urls) != 1 else ''} against the archive...",
                    file=sys.stderr,
                )
                wb = await lookup_many(urls)
                if wb:
                    n_attached = attach_wayback_to_found(results, wb)
                    print(
                        f"wayback: {n_attached} account"
                        f"{'s' if n_attached != 1 else ''} have archived "
                        f"snapshots",
                        file=sys.stderr,
                    )
                else:
                    print("wayback: no snapshots found", file=sys.stderr)
        if args.no_identity:
            return results, None, [], emails, None, {}, None
        found_dicts: list[dict] = []
        for _, rs in results:
            for r in rs:
                if r.exists is True:
                    found_dicts.append(asdict(r))
        # Dedupe before correlation so a single profile reached via
        # several URL aliases doesn't inflate the photo-match cluster.
        found_dicts = _dedupe_same_site_dicts(found_dicts)
        deep_options = photo_deep.options_from_apis(enabled=args.photo_deep)
        overall, clusters, deep_evidence, face_map, photo_bytes_map = await build_overall_and_clusters(
            found_dicts, deep_options=deep_options,
        )

        # --- Profile-photo OCR ---------------------------------------
        # After photo correlation populates photo_bytes_map, OCR every
        # avatar and feed any handle-shaped text into a single extra
        # scan round. Skipped silently when Tesseract isn't installed.
        if args.photo_ocr and photo_bytes_map:
            from photo_ocr import available as ocr_available
            from photo_ocr import discover_handles as ocr_discover
            if not ocr_available():
                print(
                    "photo-ocr: tesseract not installed — skipping. "
                    "Install with `apt install tesseract-ocr && "
                    "pip install pytesseract`.",
                    file=sys.stderr,
                )
            else:
                # Build the set of already-tested handles (originals +
                # anything --expand already pulled in).
                tested_so_far = set(variants)
                for _, rs in results:
                    for r in rs:
                        if r.variant:
                            tested_so_far.add(r.variant)
                new_handles = ocr_discover(photo_bytes_map, tested_so_far)
                # Respect the same handle cap --expand uses.
                if new_handles:
                    cap = max(0, args.expand_max_handles - (
                        len(tested_so_far) - len(variants)
                    ))
                    new_handles = new_handles[:cap]
                if new_handles:
                    preview = ", ".join(new_handles[:6])
                    tail = "…" if len(new_handles) > 6 else ""
                    print(
                        f"photo-ocr: extracted {len(new_handles)} new "
                        f"handle{'s' if len(new_handles) != 1 else ''} "
                        f"from avatars ({preview}{tail}) — scanning",
                        file=sys.stderr,
                    )
                    for h in new_handles:
                        expand_source_map.setdefault(h.lower(), "bio_link")
                    ocr_results = await phantom.run_many(new_handles)
                    results.extend(ocr_results)
                    n_new_found = sum(
                        1 for _, rs in ocr_results for r in rs if r.exists is True
                    )
                    print(
                        f"photo-ocr: {n_new_found} new FOUND from "
                        f"avatar-derived handles",
                        file=sys.stderr,
                    )
                    # Re-run identity correlation so the new FOUND
                    # accounts cluster with the rest.
                    new_dicts = [
                        asdict(r)
                        for _, rs in ocr_results
                        for r in rs if r.exists is True
                    ]
                    if new_dicts:
                        merged = _dedupe_same_site_dicts(
                            found_dicts + new_dicts,
                        )
                        overall, clusters, deep_evidence, face_map, photo_bytes_map = \
                            await build_overall_and_clusters(
                                merged, deep_options=deep_options,
                            )

        return results, overall, clusters, emails, deep_evidence, face_map, photo_bytes_map

    start = time.monotonic()
    grouped, overall, clusters, emails, deep_evidence, face_map, photo_bytes_map = asyncio.run(_scan_and_correlate())
    elapsed = time.monotonic() - start
    cache.save()
    if args.resume and cache.enabled:
        total = cache.disk_hits + cache.fresh_writes
        if total:
            pct = 100 * cache.disk_hits / total
            print(
                f"resume: reused {cache.disk_hits}/{total} request"
                f"{'s' if total != 1 else ''} from cache ({pct:.0f}%)",
                file=sys.stderr,
            )

    # Compute confidence scores and attach tier labels to every FOUND result.
    # Done once here so all output paths (terminal, HTML, JSON, Markdown) share
    # the same scores without recomputing.
    _found_for_scoring, _, _ = _flatten(grouped)
    if _found_for_scoring:
        subject_name = getattr(overall, "display_name", None) or ""
        # Convert source-kind labels into integer boosts for the scorer.
        boost_map: dict[str, int] = {}
        if expand_source_map:
            from expand import SOURCE_WEIGHTS
            for handle_lower, source in expand_source_map.items():
                boost_map[handle_lower] = SOURCE_WEIGHTS.get(source, 0)
        _confidence.score_all(
            _found_for_scoring, clusters or [], subject_name, raw,
            expand_source_map=boost_map,
        )

    # Annotate MISSING results with their confidence tier. Mirror of the
    # FOUND scoring above: every cleanly-missing result on a reliable
    # site gets `tier = confirmed_missing`, everything else gets
    # `uncertain_missing`. Exports + terminal can now distinguish "we're
    # sure this handle isn't on Twitter" from "we couldn't tell".
    all_missing = [r for _, rs in grouped for r in rs if r.exists is False]
    if all_missing:
        _confidence.annotate_missing(all_missing)

    # Identity disambiguation — cluster found accounts into distinct identity groups.
    # Skipped when --no-cluster, --no-identity, or there are no found results.
    _dis_clusters = None
    no_cluster = getattr(args, "no_cluster", False)
    if not no_cluster and not args.no_identity and _found_for_scoring:
        _dis_clusters = _disambiguation.disambiguate(
            _found_for_scoring, clusters or [], raw
        )
        _disambiguation.attach_identity_fields(_found_for_scoring, _dis_clusters)

    if args.as_json:
        payload = _build_json_payload(grouped, raw, elapsed, overall, clusters, emails,
                                      deep_evidence, dis_clusters=_dis_clusters)
        print(json.dumps(payload, indent=2))
    elif not args.quiet:
        print_compact(grouped, elapsed, color, args.found_only,
                      show_all=getattr(args, "show_all", False),
                      dis_clusters=_dis_clusters)
        if emails:
            found_for_print, _, _ = _flatten(grouped)
            _print_emails_section(found_for_print, emails, color)
        # Identity summary only when clustering is off (clustering replaces it).
        if not args.found_only and no_cluster:
            _print_identity_summary(overall, clusters, color)

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
        export_report(grouped, raw, elapsed, export_path, overall, clusters, emails, deep_evidence, face_map, dark=args.dark, dis_clusters=_dis_clusters, photo_bytes_map=photo_bytes_map)
        print(
            f"{_c(color,'dim')}Report written to {export_path}{_c(color,'reset')}",
            file=sys.stderr,
        )

    sys.stdout.flush()
    return 0
