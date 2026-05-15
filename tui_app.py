"""Phantom full Textual TUI (Option B).

A live in-terminal investigation surface. Three panels:
  - left:    found accounts table, updating as the scan progresses
  - right:   activity log (every per-site request as it happens)
  - footer:  keybindings, live elapsed timer

Keybindings (visible in the footer at all times):
    Enter   open the selected account's details overlay
    E       export the current results to ~/<handle>_dossier.html
    F       filter the found list by free-text substring
    Q       quit

The app reuses Phantom's existing scan engine — `scanner.Phantom` and
the per-site extractors are unchanged. We just instantiate them with a
small adapter that publishes per-result events to the UI's reactive
state, instead of writing rows to stdout.
"""
from __future__ import annotations

import asyncio
import time
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

# Textual is an optional dep; tui_launcher.py guards the import.
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import (
    DataTable, Footer, Header, Input, Label, Log, Static,
)

from rich.text import Text as RichText

from tui_assets import (
    GHOST_LINES, WORDMARK_LINES, render_tagline_rich_markup,
)


# Tier → rich-color-token map (Textual respects Rich markup in cells).
_TIER_BADGE = {
    "verified_identity": "[b green]VERIFIED[/]",
    "likely_match":      "[b yellow]LIKELY[/]",
    "possible_impostor": "[b red]IMPOSTOR[/]",
}


# ---------------------------------------------------------------------------
# Data adapter
# ---------------------------------------------------------------------------

@dataclass
class ScanRow:
    """One row in the found-accounts table."""
    site: str
    handle: str
    score: int
    tier: str
    display_name: str
    url: str


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class BannerHeader(Static):
    """Ghost + PHANTOM wordmark at the top of the app, with the
    clickable-link tagline below. Uses the exact same 7-row art as
    the launcher so the two surfaces feel like one product.
    """

    DEFAULT_CSS = """
    BannerHeader {
        height: 9;
        padding: 1 2 0 2;
        background: $surface-darken-1;
        content-align: left middle;
    }
    """

    def render(self):
        text = RichText()
        grad = [
            "color(255)", "color(255)", "color(252)", "color(250)",
            "color(248)", "color(245)", "color(243)",
        ]
        for i in range(7):
            g = GHOST_LINES[i]
            w = WORDMARK_LINES[i]
            text.append(g, style=f"bold {grad[i]}")
            text.append("  ")
            text.append(w, style=f"bold {grad[i]}")
            text.append("\n")
        # Tagline as Rich markup so the OSC-8 links + green styling
        # work natively in Textual's renderer.
        text.append(RichText.from_markup(render_tagline_rich_markup()))
        return text


class DetailOverlay(Static):
    """Modal-ish overlay that shows full profile data for one result.

    Toggled by pressing Enter on a row. Press Enter or Esc to dismiss.
    """

    DEFAULT_CSS = """
    DetailOverlay {
        display: none;
        layer: overlay;
        offset: 8 4;
        width: 70%;
        height: 70%;
        padding: 2 3;
        background: $panel;
        border: thick $accent;
    }
    DetailOverlay.-visible { display: block; }
    """

    def __init__(self) -> None:
        super().__init__("", id="detail-overlay")
        self._row: Optional[dict] = None

    def show(self, row_data: dict) -> None:
        self._row = row_data
        p = row_data.get("profile") or {}
        lines: list[str] = []
        lines.append(f"[b]{row_data.get('site')}[/b]   [dim]{row_data.get('url')}[/dim]")
        lines.append("")
        if p.get("display_name"):
            lines.append(f"[b]Name[/b]      {p['display_name']}")
        if row_data.get("variant"):
            lines.append(f"[b]Handle[/b]    @{row_data['variant']}")
        if p.get("bio"):
            lines.append(f"[b]Bio[/b]       {p['bio']}")
        if p.get("location"):
            lines.append(f"[b]Location[/b]  {p['location']}")
        for k in ("followers", "following", "posts"):
            if p.get(k) is not None:
                lines.append(f"[b]{k.title():9}[/b] {p[k]:,}")
        if p.get("joined"):
            lines.append(f"[b]Joined[/b]    {p['joined']}")
        if p.get("verified"):
            lines.append("[b]Verified ✓[/b]")
        if p.get("linked_accounts"):
            lines.append("")
            lines.append(f"[b]Linked accounts[/b]")
            for u in p["linked_accounts"][:10]:
                lines.append(f"  • {u}")
        if row_data.get("signals"):
            lines.append("")
            lines.append(f"[b]Why this score[/b]")
            for s in row_data["signals"]:
                w = s.get("weight", 0)
                sign = "+" if w >= 0 else ""
                col = "green" if w >= 0 else "red"
                lines.append(
                    f"  [{col}]{sign}{w:>3}[/]  {s.get('label', '')}"
                )
        lines.append("")
        lines.append("[dim][Enter / Esc] close[/dim]")
        self.update("\n".join(lines))
        self.add_class("-visible")

    def hide(self) -> None:
        self.remove_class("-visible")


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------

class PhantomTUI(App):
    """Phantom's full Textual TUI. Reactive state drives the table +
    activity log. The scan itself runs in a background asyncio task
    so the UI stays responsive."""

    CSS = """
    Screen {
        background: $surface-darken-2;
    }
    #search-row {
        height: 3;
        padding: 0 2;
    }
    Input {
        width: 1fr;
        border: round white 30%;
    }
    Input:focus {
        border: round white;
    }
    #status-row {
        height: 1;
        padding: 0 3;
        color: $text-muted;
        text-style: italic;
    }
    #main-row {
        height: 1fr;
        padding: 0 1;
    }
    #found-pane {
        width: 60%;
    }
    #activity-pane {
        width: 40%;
    }
    #found-table-container, #activity-container {
        height: 1fr;
        border: round white 25%;
    }
    #found-table-container {
        border-title-color: white;
        border-title-style: bold;
    }
    #activity-container {
        border-title-color: white;
        border-title-style: bold;
    }
    DataTable {
        height: 1fr;
        background: transparent;
    }
    DataTable > .datatable--cursor {
        background: white 15%;
        color: $text;
    }
    DataTable > .datatable--header {
        background: $surface;
        color: white;
        text-style: bold;
    }
    Log {
        height: 1fr;
        background: transparent;
        scrollbar-size: 0 1;
    }
    Footer {
        background: $surface-darken-1;
    }
    """

    BINDINGS = [
        Binding("enter", "open_detail", "Details", show=True, priority=False),
        Binding("e", "export", "Export", show=True),
        Binding("f", "filter", "Filter", show=True),
        Binding("s", "scan", "Scan", show=True),
        Binding("q", "quit", "Quit", show=True),
        Binding("escape", "close_overlay", "", show=False),
    ]

    # Reactive state pieces.
    handle: reactive[str] = reactive("", init=False)
    found_count: reactive[int] = reactive(0)
    elapsed: reactive[float] = reactive(0.0)
    scanning: reactive[bool] = reactive(False)
    last_export: reactive[str] = reactive("")

    def __init__(self, initial_handle: str = ""):
        super().__init__()
        self._scan_task: Optional[asyncio.Task] = None
        self._timer_task: Optional[asyncio.Task] = None
        self._start_ts: float = 0.0
        # raw FOUND dicts keyed by row index, for the detail overlay
        self._row_data: dict[int, dict] = {}
        self._filter: str = ""
        self.handle = initial_handle

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield BannerHeader()
        with Horizontal(id="search-row"):
            yield Input(
                placeholder="◇  handle or profile URL · Enter to scan",
                id="handle-input",
                value=self.handle,
            )
        yield Label("", id="status-row")
        with Horizontal(id="main-row"):
            with Vertical(id="found-pane"):
                with Vertical(id="found-table-container"):
                    table = DataTable(id="found-table", zebra_stripes=True)
                    table.cursor_type = "row"
                    table.add_columns("Site", "Handle", "Score", "Tier")
                    yield table
            with Vertical(id="activity-pane"):
                with Vertical(id="activity-container"):
                    yield Log(id="activity-log", highlight=False, max_lines=500)
        yield DetailOverlay()
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Phantom"
        self.sub_title = "OSINT username investigation"
        # Title-text on the panels — Textual's reactive `border_title`.
        self.query_one("#found-table-container").border_title = " FOUND ACCOUNTS "
        self.query_one("#activity-container").border_title = " ACTIVITY LOG "
        self.set_interval(0.25, self._refresh_status)
        self.query_one("#handle-input", Input).focus()

    # ----- key actions ----------------------------------------------------

    @on(Input.Submitted, "#handle-input")
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        self.handle = event.value.strip()
        if self.handle:
            self.action_scan()

    def action_scan(self) -> None:
        if self.scanning:
            self.bell()
            return
        if not self.handle:
            inp = self.query_one("#handle-input", Input)
            self.handle = inp.value.strip()
        if not self.handle:
            self.bell()
            self._set_status("Type a handle and press Enter.")
            return
        self._reset_results()
        self._scan_task = asyncio.create_task(self._run_scan())

    def action_quit(self) -> None:
        if self._scan_task and not self._scan_task.done():
            self._scan_task.cancel()
        self.exit(0)

    def action_close_overlay(self) -> None:
        ov = self.query_one(DetailOverlay)
        if ov.has_class("-visible"):
            ov.hide()
            return
        # If no overlay is open, escape acts like quit.
        self.action_quit()

    def action_open_detail(self) -> None:
        table = self.query_one("#found-table", DataTable)
        if not table.row_count:
            return
        row_idx = table.cursor_row
        data = self._row_data.get(row_idx)
        if not data:
            return
        self.query_one(DetailOverlay).show(data)

    def action_export(self) -> None:
        if not self.handle:
            self._set_status("Nothing to export — run a scan first.")
            return
        if self.scanning:
            self._set_status("Wait for the scan to finish before exporting.")
            return
        # Run the scan again with --export so the existing dossier
        # builder produces a proper HTML report. (We can't easily
        # serialize the in-memory partial results to HTML without
        # re-running the identity / disambiguation pipeline.)
        out_path = Path.home() / f"{self.handle}_report.html"
        self._set_status(f"Building {out_path}...")
        asyncio.create_task(self._run_export(str(out_path)))

    def action_filter(self) -> None:
        inp = self.query_one("#handle-input", Input)
        if inp.placeholder.startswith("handle"):
            inp.placeholder = "filter found list (esc to clear)"
            inp.value = ""
            inp.focus()
            self._filter_mode = True
        else:
            self._filter_mode = False
            inp.placeholder = "handle or profile URL"
            inp.value = self.handle
            self._filter = ""
            self._refilter_table()

    @on(Input.Changed, "#handle-input")
    def _on_input_changed(self, event: Input.Changed) -> None:
        if getattr(self, "_filter_mode", False):
            self._filter = event.value.lower()
            self._refilter_table()

    # ----- scan runner ----------------------------------------------------

    def _set_status(self, text: str) -> None:
        try:
            self.query_one("#status-row", Label).update(text)
        except Exception:
            pass

    def _reset_results(self) -> None:
        self._row_data.clear()
        self.found_count = 0
        self.elapsed = 0.0
        self._start_ts = time.monotonic()
        table = self.query_one("#found-table", DataTable)
        table.clear()
        log = self.query_one("#activity-log", Log)
        log.clear()

    def _refresh_status(self) -> None:
        if self.scanning:
            self.elapsed = time.monotonic() - self._start_ts
        parts = [
            f"target: {self.handle or '(none)'}",
            f"found: {self.found_count}",
            f"elapsed: {self.elapsed:.1f}s",
            "scanning" if self.scanning else "idle",
        ]
        if self.last_export:
            parts.append(f"exported → {self.last_export}")
        self._set_status("    ".join(parts))

    async def _run_scan(self) -> None:
        """Run the full Phantom pipeline — variants + scan + scoring +
        disambiguation + photo correlation — so the TUI's results match
        what `phantom <handle>` produces from the command line. Found
        rows stream into the table as the scanner returns them; tier /
        score cells are filled in after the post-scan pipeline runs.
        """
        self.scanning = True
        log = self.query_one("#activity-log", Log)
        table = self.query_one("#found-table", DataTable)
        from cache import ResponseCache
        from models import load_sites
        from scanner import Phantom

        try:
            from cli import _default_sites_path
            sites_path = Path(_default_sites_path())
            sites = load_sites(sites_path)
        except Exception as e:
            log.write_line(f"error: {e}")
            self.scanning = False
            return

        # Variant resolution mirrors cli.main: --parse-style URL input
        # collapses to one exact handle; bare handles go through the
        # variant generator (30+ candidates for a typical 7-char name).
        if "://" in self.handle:
            from expand import _extract_one
            v = _extract_one(self.handle)
            if not v:
                log.write_line(
                    f"could not extract a handle from {self.handle!r}"
                )
                self.scanning = False
                return
            log.write_line(f"parsed handle: {v}")
            raw = v
            variants = [v]
        else:
            raw = self.handle
            from variants import generate as generate_variants
            variants = generate_variants(self.handle)
            if not variants:
                log.write_line(f"no valid variants for {self.handle!r}")
                self.scanning = False
                return

        phantom = Phantom(
            sites,
            cache=ResponseCache(enabled=False),
        )
        n_req = len(variants) * len(sites)
        log.write_line(
            f"scanning {len(variants)} variant"
            f"{'s' if len(variants) != 1 else ''} × {len(sites)} sites "
            f"= {n_req} requests..."
        )

        # (site, variant) → table row index. Used after the post-scan
        # pipeline runs so we can refresh the placeholder score / tier
        # cells in-place rather than rebuilding the table.
        row_for: dict[tuple[str, str], int] = {}

        def _stream_row(r) -> None:
            """Scanner per-result callback. Adds a row immediately for
            FOUND results with placeholder score / tier — those get
            filled in once disambiguation has run on the full set."""
            if r.exists is not True:
                return
            p = r.profile or {}
            disp = (p.get("display_name") or r.variant or r.site)[:30]
            row_idx = table.row_count
            table.add_row(
                RichText.from_markup(f"[b]{r.site}[/]"),
                RichText.from_markup(f"[dim]@[/]{r.variant or '?'}"),
                RichText.from_markup("[dim]…[/]"),
                RichText.from_markup("[dim]…[/]"),
            )
            self._row_data[row_idx] = {
                "site": r.site, "url": r.url, "variant": r.variant,
                "profile": p, "signals": getattr(r, "signals", None),
                "score": None, "tier": None,
            }
            row_for[(r.site, r.variant or "")] = row_idx
            self.found_count += 1
            log.write_line(f"  ✓ {r.site}  →  {disp}")

        try:
            results = await phantom.run_many(variants, on_result=_stream_row)
        except asyncio.CancelledError:
            log.write_line("scan cancelled")
            raise
        except Exception as e:
            log.write_line(f"error: {type(e).__name__}: {e}")
            self.scanning = False
            return

        log.write_line(
            f"scan complete — running correlation on "
            f"{self.found_count} found accounts..."
        )

        # ----- post-scan pipeline (mirrors cli.main) ---------------------
        # Same order as cli.main: photo correlation → confidence
        # scoring → disambiguation. Each step mutates the CheckResult
        # objects in place; we then refresh the streamed rows from
        # those (now-enriched) objects.
        from confidence import score_all
        from dedupe import _dedupe_same_site_dicts
        from identity import build_overall_and_clusters
        import disambiguation as _disambiguation
        import photo_deep

        flat_found = [r for _, rs in results for r in rs if r.exists is True]
        overall, clusters = None, []
        try:
            if flat_found:
                found_dicts = [asdict(r) for r in flat_found]
                found_dicts = _dedupe_same_site_dicts(found_dicts)
                deep_options = photo_deep.options_from_apis(enabled=True)
                overall, clusters, _de, _fm, _pbm = \
                    await build_overall_and_clusters(
                        found_dicts, deep_options=deep_options,
                    )
        except Exception as e:
            log.write_line(f"correlation skipped: {type(e).__name__}: {e}")

        subject_name = getattr(overall, "display_name", None) or ""
        if flat_found:
            score_all(flat_found, clusters or [], subject_name, raw,
                      expand_source_map={})

        if flat_found:
            try:
                dis_clusters = _disambiguation.disambiguate(
                    flat_found, clusters or [], raw,
                )
                _disambiguation.attach_identity_fields(
                    flat_found, dis_clusters,
                )
            except Exception as e:
                log.write_line(
                    f"disambiguation skipped: {type(e).__name__}: {e}"
                )

        # ----- refresh table cells with final tier / score ---------------
        # Two passes:
        #   1. fill in score / tier cells from the post-pipeline results
        #   2. drop IMPOSTOR rows so the TUI matches the HTML export
        #      (which excludes inconclusive / impostor hits from the
        #      main account cards). Without this Facebook + Pillowfort
        #      light up with ~10 same-photo variants because their
        #      presence_text rules ("first_name", etc.) match for any
        #      URL — the score system correctly demotes them to
        #      score=15 / IMPOSTOR but they still clutter the table.
        impostor_rows: list[int] = []
        for r in flat_found:
            row_idx = row_for.get((r.site, r.variant or ""))
            if row_idx is None:
                continue
            if r.tier == "possible_impostor":
                impostor_rows.append(row_idx)
                continue
            score = r.score
            if score is not None:
                color = (
                    "green" if score >= 55
                    else "yellow" if score >= 20
                    else "red"
                )
                score_cell = RichText.from_markup(f"[b {color}]{score:>3}[/]")
            else:
                score_cell = RichText.from_markup("[dim]?[/]")
            badge = _TIER_BADGE.get(r.tier, "[dim]—[/]")
            try:
                row_key = table.coordinate_to_cell_key((row_idx, 2)).row_key
                col_keys = list(table.columns.keys())
                table.update_cell(row_key, col_keys[2], score_cell)
                table.update_cell(
                    row_key, col_keys[3], RichText.from_markup(badge),
                )
            except Exception:
                pass
            d = self._row_data.get(row_idx)
            if d is not None:
                d["score"] = score
                d["tier"] = r.tier

        # Remove impostor rows from highest index downward so earlier
        # indices stay valid. DataTable's `remove_row` takes a row key,
        # which we resolve via the same coordinate-to-key lookup the
        # refresh loop uses.
        for row_idx in sorted(impostor_rows, reverse=True):
            try:
                row_key = table.coordinate_to_cell_key((row_idx, 0)).row_key
                table.remove_row(row_key)
            except Exception:
                pass
            self._row_data.pop(row_idx, None)
            self.found_count -= 1

        if impostor_rows:
            log.write_line(
                f"filtered {len(impostor_rows)} impostor"
                f"{'s' if len(impostor_rows) != 1 else ''} "
                f"(same-photo variants on loose-rule sites)"
            )

        log.write_line(
            f"done — {self.found_count} found in "
            f"{time.monotonic() - self._start_ts:.1f}s"
        )
        self.scanning = False

    async def _run_export(self, out_path: str) -> None:
        """Re-run the full scan (variants + correlation + scoring) and
        write the HTML dossier. Mirrors what `phantom <handle> --export`
        produces from the command line — no --exact, so variants are
        generated the same way the live TUI scan generates them."""
        from cli import main as cli_main
        argv = [self.handle, "--export", out_path, "--quiet"]
        await asyncio.to_thread(cli_main, argv)
        self.last_export = out_path
        self._set_status(f"Exported to {out_path}")

    def _refilter_table(self) -> None:
        """Hide rows that don't match the current filter string."""
        table = self.query_one("#found-table", DataTable)
        # DataTable doesn't natively hide rows, so we rebuild from
        # _row_data. Cheap for our row counts (under a hundred).
        cursor = table.cursor_row
        table.clear()
        for idx, data in sorted(self._row_data.items()):
            blob = (
                str(data.get("site", "")) + " "
                + str(data.get("variant", "")) + " "
                + str((data.get("profile") or {}).get("display_name", ""))
            ).lower()
            if self._filter and self._filter not in blob:
                continue
            score = data.get("score")
            score_color = (
                "green" if (score or 0) >= 55
                else "yellow" if (score or 0) >= 20
                else "red"
            )
            score_cell = (
                RichText.from_markup(f"[b {score_color}]{score:>3}[/]")
                if score is not None else RichText.from_markup("[dim]?[/]")
            )
            badge = _TIER_BADGE.get(data.get("tier"), "[dim]—[/]")
            table.add_row(
                RichText.from_markup(f"[b]{data.get('site', '')}[/]"),
                RichText.from_markup(f"[dim]@[/]{data.get('variant') or '?'}"),
                score_cell,
                RichText.from_markup(badge),
            )
        try:
            table.move_cursor(row=min(cursor, table.row_count - 1))
        except Exception:
            pass


def run_tui(initial_handle: str = "") -> None:
    """Top-level entry — used by both `phantom --tui` and the launcher's
    [T] keybinding."""
    PhantomTUI(initial_handle=initial_handle).run()
