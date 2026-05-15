"""HTML dossier exporter. The big editorial layout — Instrument Serif +
IBM Plex stack, photo-match card, subject hero, account grid, theme
toggle.
"""
from __future__ import annotations

import html
import json
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from confidence import TIER_IMPOSTOR, TIER_LIKELY, TIER_VERIFIED
from dedupe import _flatten
from disambiguation import LABEL_LOW, LABEL_PRIMARY, LABEL_SECONDARY
from models import CheckResult, _photo_to_data_uri


def _format_count(n) -> str:
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


_HTML_TEMPLATE = """<!doctype html>
<html lang="en" data-theme="{theme}">
<head>
<script>(function(){{try{{var t=localStorage.getItem('phantom-theme');if(t)document.documentElement.setAttribute('data-theme',t);}}catch(e){{}}}})();</script>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Phantom — {raw_html}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:        #f5efe2;
    --paper:     #f5efe2;
    --paper-2:   #ebe1cd;
    --ink:       #1a1612;
    --muted:     #6b5f4d;
    --rule:      #c4b896;
    --border:    #d4c9b3;
    --btn-bg:    #1a1612;
    --btn-text:  #f5efe2;
    --btn-hover: #2e2620;
    --err:       #8a3a2e;
    --tier-verified: #3d6b4a;
  }}
  [data-theme="dark"] {{
    --bg:        #16140f;
    --paper:     #2a2620;
    --paper-2:   #23201a;
    --ink:       #f0e8d8;
    --muted:     #9a8d75;
    --rule:      #3a342a;
    --tier-verified: #6aaa7c;
    --border:    #3a342a;
    --btn-bg:    #f0e8d8;
    --btn-text:  #16140f;
    --btn-hover: #d8ccb8;
    --err:       #d4826f;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ background: var(--bg); }}
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
  .ghost path {{ fill: var(--ink); }}
  .ghost circle {{ fill: var(--bg); }}

  /* -------- Theme toggle -------- */
  .header-right {{
    display: flex; align-items: flex-start; gap: 12px;
  }}
  .theme-toggle {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 13px;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--muted);
    padding: 5px 9px;
    border-radius: 4px;
    cursor: pointer;
    line-height: 1;
    flex-shrink: 0;
    margin-top: 1px;
  }}
  .theme-toggle:hover {{
    border-color: var(--ink);
    color: var(--ink);
    background: var(--paper-2);
  }}

  .page {{
    max-width: 900px; margin: 0 auto;
    padding: 42px 36px;
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
    width: 130px; height: 130px;
    border-radius: 6px;
    flex-shrink: 0;
    background: var(--paper-2) center/cover no-repeat;
    border: 1px solid var(--border);
    display: flex; align-items: center; justify-content: center;
    overflow: hidden;
  }}
  .portrait .letter {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 66px; color: var(--ink);
    line-height: 1; letter-spacing: -0.02em;
  }}
  .ident {{ flex: 1; min-width: 0; }}
  .ident .handle {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 96px; line-height: 1.04;
    letter-spacing: -0.03em; color: var(--ink);
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
    font-size: 56px; line-height: 1.05;
    letter-spacing: -0.02em; color: var(--ink);
  }}
  .stat .l {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 11px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.18em;
    color: var(--muted);
    margin-top: 6px;
  }}

  /* -------- Photo-match + Subject details combo row -------- */
  section.combo {{
    margin-top: 30px;
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr);
    gap: 28px;
    align-items: start;
  }}
  /* When the photo-match column is absent, let subject-details span. */
  section.combo > :only-child {{ grid-column: 1 / -1; }}

  .photo-match {{
    background: var(--paper-2);
    border-radius: 8px;
    padding: 22px;
    border: 1px solid var(--border);
    display: flex; flex-direction: column;
    min-width: 0;
  }}
  .pm-photos {{
    display: flex; align-items: center; justify-content: center;
    gap: 16px;
    margin-top: 4px;
    margin-bottom: 18px;
  }}
  .pm-thumb {{
    width: 84px; height: 84px;
    border-radius: 6px;
    flex-shrink: 0;
    background: var(--paper) center/cover no-repeat;
    border: 1px solid var(--border);
  }}
  .pm-arrow {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 16px;
    color: var(--muted);
    line-height: 1;
  }}
  .pm-divider {{
    border-top: 1px dashed var(--rule);
    padding-top: 12px;
  }}
  .pm-meta {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 11px;
    color: var(--muted);
    line-height: 1.55;
  }}
  .pm-meta + .pm-meta {{ margin-top: 4px; }}
  .pm-site {{ color: var(--ink); font-weight: 400; }}

  /* Right column flat list — no card background; just an offset to
     visually align its kicker with the card's interior kicker. */
  .subject-details {{
    padding-top: 22px;
    min-width: 0;
  }}
  .subject-details > .kicker {{ margin-bottom: 14px; }}

  /* -------- Detail rows -------- */
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

  /* -------- Identity cluster grouping -------- */
  .cluster-group {{ margin-bottom: 26px; }}
  .cluster-group:last-child {{ margin-bottom: 0; }}
  .cluster-header {{
    display: flex; align-items: baseline; gap: 10px;
    margin-bottom: 12px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }}
  .cluster-label {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 10px; font-weight: 500;
    text-transform: uppercase; letter-spacing: 0.18em;
    padding: 2px 7px; border-radius: 3px;
    border: 1px solid var(--border);
  }}
  .cluster-label-primary {{
    color: var(--tier-verified); border-color: var(--tier-verified);
  }}
  .cluster-label-secondary {{
    color: var(--muted); border-color: var(--border);
  }}
  .cluster-label-low {{
    color: var(--muted); border-color: var(--border); opacity: 0.6;
  }}
  .cluster-name {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 17px; color: var(--ink); flex: 1;
  }}
  .cluster-meta {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 10px; color: var(--muted); white-space: nowrap;
  }}

  .accounts-grid {{
    display: grid;
    /* minmax(0, 1fr) — without it, `1fr` resolves to minmax(auto, 1fr),
       and `auto` is min-content. A long URL or display-name inside a
       card then forces its grid cell wider than 50%, blowing the whole
       grid past the 900px container. */
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 14px;
    width: 100%;
  }}

  /* -------- Account filter (inline, header of accounts section) -------- */
  .acct-filter-row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 18px;
    margin-bottom: 14px;
  }}
  .acct-filter-row .kicker {{ margin-bottom: 0; }}
  .acct-filter {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 12px;
    background: transparent;
    border: 1px solid var(--border);
    color: var(--ink);
    padding: 6px 10px;
    border-radius: 4px;
    min-width: 180px;
    max-width: 280px;
    flex: 1 1 220px;
  }}
  .acct-filter::placeholder {{ color: var(--muted); }}
  .acct-filter:focus {{
    outline: none;
    border-color: var(--ink);
  }}
  .acct.is-filtered-out {{ display: none; }}
  .filter-empty {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 11px;
    color: var(--muted);
    text-align: center;
    padding: 20px;
    display: none;
  }}
  .accounts.is-empty .filter-empty {{ display: block; }}

  /* -------- Identity graph -------- */
  .identity-graph-section {{
    margin-top: 30px;
  }}
  .graph-panel {{
    position: relative;
    background: var(--paper-2);
    border: 1px solid var(--border);
    border-radius: 6px;
    overflow: hidden;
    /* Pan-and-zoom workspace: bigger so it feels like an investigation
       canvas, not a thumbnail. */
    height: 640px;
  }}
  .identity-graph {{
    display: block;
    width: 100%;
    height: 100%;
    cursor: grab;
    background:
      radial-gradient(circle at 1px 1px, var(--rule) 1px, transparent 1px);
    background-size: 30px 30px;
    background-position: 0 0;
    touch-action: none;  /* let JS handle pinch/pan */
  }}
  .identity-graph.is-panning {{ cursor: grabbing; }}

  /* Curved bezier edges replace straight lines. Stroke colour by kind. */
  .identity-graph .edge {{
    stroke-width: 1.2;
    stroke-opacity: 0.55;
    fill: none;
    pointer-events: none;
  }}
  .identity-graph .edge.cluster {{ stroke: var(--ink); stroke-width: 1.5; stroke-opacity: 0.7; }}
  .identity-graph .edge.photo   {{ stroke: var(--tier-verified); stroke-width: 1.4; stroke-opacity: 0.85; }}
  .identity-graph .edge.link    {{ stroke: var(--muted); stroke-dasharray: 5 4; stroke-opacity: 0.65; }}

  /* Node group: avatar circle + score ring + outer stroke + label */
  .identity-graph .node {{ cursor: grab; }}
  .identity-graph .node:active {{ cursor: grabbing; }}
  .identity-graph .node-bg {{
    fill: var(--paper);
    stroke: var(--ink);
    stroke-width: 1.8;
    transition: stroke-width 0.12s;
  }}
  .identity-graph .node-avatar {{
    /* image fill is set inline per node */
    pointer-events: none;
  }}
  .identity-graph .node-letter {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 18px;
    fill: var(--ink);
    text-anchor: middle;
    dominant-baseline: central;
    pointer-events: none;
    user-select: none;
  }}
  .identity-graph .node.verified .node-bg {{
    stroke: var(--tier-verified);
    stroke-width: 2.4;
  }}
  .identity-graph .node.primary .node-bg {{
    stroke-width: 2.8;
  }}
  .identity-graph .node.primary .node-glow {{
    /* Glow rendered as a soft tinted ring underneath via SVG filter */
    filter: url(#phantom-glow);
  }}
  .identity-graph .node.impostor .node-bg {{
    stroke: var(--err);
    stroke-dasharray: 3 2;
  }}
  /* Score ring — partial arc, color graded by tier. Kept subtle so it
     reads as metadata, not the focal point. */
  .identity-graph .score-ring {{
    fill: none;
    stroke-width: 1.8;
    stroke-linecap: round;
    pointer-events: none;
    opacity: 0.75;
  }}
  .identity-graph .score-ring.tier-verified {{ stroke: var(--tier-verified); }}
  .identity-graph .score-ring.tier-likely   {{ stroke: var(--muted); }}
  .identity-graph .score-ring.tier-impostor {{ stroke: var(--err); opacity: 0.5; }}
  .identity-graph .score-track {{
    fill: none;
    stroke: var(--border);
    stroke-width: 1.8;
    opacity: 0.25;
    pointer-events: none;
  }}
  .identity-graph .node-label {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 11px;
    fill: var(--ink);
    pointer-events: none;
    user-select: none;
    text-anchor: middle;
    paint-order: stroke fill;
    stroke: var(--paper-2);
    stroke-width: 3px;
    stroke-linejoin: round;
  }}
  .identity-graph .node:hover .node-bg {{
    stroke-width: 3.4;
  }}
  .identity-graph .node.dragging .node-bg {{
    stroke-width: 4;
  }}
  .identity-graph .node.dimmed {{
    opacity: 0.16;
  }}
  .identity-graph .edge.dimmed {{
    opacity: 0.06;
  }}

  /* ---- Side panel (slides in from right on node click) ---- */
  .graph-side-panel {{
    position: absolute;
    top: 0; right: 0; bottom: 0;
    width: 340px;
    max-width: 70%;
    background: var(--paper);
    border-left: 1px solid var(--border);
    box-shadow: -8px 0 22px rgba(0, 0, 0, 0.08);
    transform: translateX(100%);
    transition: transform 0.22s cubic-bezier(.4,.2,.2,1);
    z-index: 4;
    overflow-y: auto;
    padding: 22px 22px 26px;
    font-family: 'IBM Plex Sans', -apple-system, system-ui, sans-serif;
  }}
  .graph-side-panel.is-open {{
    transform: translateX(0);
  }}
  .gsp-close {{
    position: absolute;
    top: 8px; right: 12px;
    background: transparent;
    border: none;
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 20px;
    color: var(--muted);
    cursor: pointer;
    line-height: 1;
    padding: 4px 8px;
  }}
  .gsp-close:hover {{ color: var(--ink); }}
  .gsp-avatar {{
    width: 96px; height: 96px;
    border-radius: 50%;
    background: var(--paper-2) center/cover no-repeat;
    border: 1.5px solid var(--border);
    margin: 0 auto 14px;
    display: flex; align-items: center; justify-content: center;
    overflow: hidden;
  }}
  .gsp-avatar .gsp-letter {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 48px; color: var(--ink); line-height: 1;
  }}
  .gsp-platform {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 10px;
    color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.16em;
    text-align: center;
    margin-bottom: 4px;
  }}
  .gsp-name {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 26px;
    text-align: center;
    color: var(--ink);
    line-height: 1.2;
    margin-bottom: 4px;
  }}
  .gsp-handle {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 12px;
    color: var(--muted);
    text-align: center;
    margin-bottom: 18px;
  }}
  .gsp-bio {{
    font-size: 13px;
    color: var(--ink);
    line-height: 1.5;
    padding: 10px 0;
    margin-bottom: 14px;
    border-top: 1px dashed var(--rule);
    border-bottom: 1px dashed var(--rule);
  }}
  .gsp-bio:empty {{ display: none; }}
  .gsp-stats {{
    display: flex; justify-content: space-around;
    margin-bottom: 18px;
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 11px;
    color: var(--muted);
  }}
  .gsp-stat {{ text-align: center; }}
  .gsp-stat .n {{
    display: block;
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 20px;
    color: var(--ink);
    line-height: 1.1;
  }}
  .gsp-stat .l {{ font-size: 9px; text-transform: uppercase; letter-spacing: 0.14em; }}
  .gsp-score-row {{
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px 12px;
    background: var(--paper-2);
    border-radius: 4px;
    margin-bottom: 14px;
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 11px;
  }}
  .gsp-score-num {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 30px;
    line-height: 1;
    color: var(--ink);
  }}
  .gsp-evidence {{
    margin-bottom: 18px;
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 11px;
  }}
  .gsp-evidence-title {{
    font-size: 10px;
    color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.16em;
    margin-bottom: 8px;
  }}
  .gsp-evd-row {{
    display: flex; gap: 10px; padding: 3px 0;
    line-height: 1.45;
  }}
  .gsp-evd-w {{
    flex: 0 0 32px; text-align: right;
    font-variant-numeric: tabular-nums; font-weight: 500;
  }}
  .gsp-evd-w.pos {{ color: var(--tier-verified); }}
  .gsp-evd-w.neg {{ color: var(--err); }}
  .gsp-actions {{
    display: flex; gap: 8px;
  }}
  .gsp-btn {{
    flex: 1;
    display: inline-block;
    text-align: center;
    padding: 10px;
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 12px;
    text-decoration: none;
    border-radius: 3px;
    background: var(--btn-bg);
    color: var(--btn-text);
    border: 1px solid var(--btn-bg);
    cursor: pointer;
  }}
  .gsp-btn.secondary {{
    background: transparent;
    color: var(--ink);
    border-color: var(--border);
  }}
  .gsp-btn:hover {{ background: var(--btn-hover); }}
  .gsp-btn.secondary:hover {{ border-color: var(--ink); background: var(--paper-2); }}
  /* Graph toolbar — sits over the SVG, top-right. */
  .graph-toolbar {{
    position: absolute;
    top: 10px; right: 10px;
    display: flex;
    gap: 4px;
    z-index: 2;
  }}
  .graph-toolbar button {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 12px;
    background: var(--paper);
    color: var(--ink);
    border: 1px solid var(--border);
    width: 28px; height: 28px;
    cursor: pointer;
    line-height: 1;
    padding: 0;
    border-radius: 3px;
  }}
  .graph-toolbar button:hover {{
    background: var(--paper-2);
    border-color: var(--ink);
  }}
  .graph-hint {{
    position: absolute;
    bottom: 8px; left: 12px;
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 10px;
    color: var(--muted);
    pointer-events: none;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    opacity: 0.65;
  }}
  .graph-legend {{
    display: flex;
    flex-wrap: wrap;
    gap: 18px;
    margin-top: 12px;
    padding: 12px 4px 0;
    border-top: 1px dashed var(--rule);
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 10px;
    color: var(--muted);
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }}
  .lg-row {{ display: inline-flex; align-items: center; gap: 7px; }}
  .lg-line {{ display: inline-block; width: 22px; height: 2px; }}
  .lg-line.lg-cluster {{ background: var(--ink); }}
  .lg-line.lg-photo   {{ background: var(--tier-verified); }}
  .lg-line.lg-link    {{ background: var(--muted); height: 0; border-top: 2px dashed var(--muted); }}
  .lg-dot {{
    display: inline-block; width: 10px; height: 10px;
    border-radius: 50%; border: 1.6px solid var(--ink);
    background: var(--paper);
  }}
  .lg-dot.lg-verified {{ background: var(--tier-verified); border-color: var(--tier-verified); }}
  .lg-dot.lg-primary  {{ border-width: 3px; }}

  .acct {{
    background: var(--paper-2);
    border-radius: 6px;
    padding: 20px 20px 24px;
    display: flex;
    flex-direction: column;
    gap: 14px;
    border: 1px solid var(--border);
    min-width: 0;
    overflow: hidden;
  }}
  .acct-head {{
    display: flex;
    gap: 18px;
    align-items: flex-start;
    min-width: 0;
  }}
  .acct-head-text {{
    flex: 1;
    min-width: 0;
  }}
  .acct .photo {{
    width: 64px; height: 64px;
    border-radius: 6px;
    flex-shrink: 0;
    background: var(--paper) center/cover no-repeat;
    border: 1px solid var(--border);
    display: flex; align-items: center; justify-content: center;
    overflow: hidden;
  }}
  .acct .photo .letter {{
    font-family: 'Instrument Serif', Georgia, serif;
    font-size: 30px; color: var(--ink);
    line-height: 1; letter-spacing: -0.02em;
  }}
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
    line-height: 1.6;
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }}
  .acct-details {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 13px;
    color: var(--muted);
    line-height: 1.7;
    word-break: break-word;
    margin: 4px 0;
  }}
  .acct-details b.verified-tag {{
    font-weight: 500;
    color: var(--ink);
  }}
  .acct-meta-row {{
    display: flex; align-items: center; gap: 6px;
  }}
  .tier-dot {{
    width: 7px; height: 7px;
    border-radius: 50%; flex-shrink: 0;
    display: inline-block; vertical-align: middle;
  }}
  .tier-verified {{ background: var(--tier-verified); }}
  .score-chip {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 10px; color: var(--muted);
    margin-left: auto;
    letter-spacing: 0.04em;
  }}

  /* -------- Facts strip (location, website, email, category, wayback) -------- */
  .acct .facts {{
    display: flex;
    flex-direction: column;
    gap: 5px;
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 11px;
    color: var(--ink);
    margin-top: -2px;
  }}
  .acct .fact-row {{
    display: flex;
    gap: 10px;
    align-items: baseline;
    line-height: 1.4;
  }}
  .acct .fact-k {{
    flex: 0 0 90px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.10em;
    font-size: 9.5px;
  }}
  .acct .fact-v {{
    flex: 1; min-width: 0;
    word-break: break-word;
  }}
  .acct .fact-link {{
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--rule);
    text-underline-offset: 2px;
  }}
  .acct .fact-link:hover {{
    text-decoration-color: var(--ink);
  }}

  /* -------- Linked accounts chips -------- */
  .acct .link-chips {{
    display: flex;
    flex-direction: column;
    gap: 6px;
  }}
  .acct .link-chips-k {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 9.5px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.10em;
  }}
  .acct .link-chips-row {{
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
  }}
  .acct .link-chip {{
    display: inline-block;
    padding: 3px 8px;
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 10.5px;
    background: var(--paper);
    color: var(--ink);
    border: 1px solid var(--border);
    border-radius: 3px;
    text-decoration: none;
    line-height: 1.3;
  }}
  .acct .link-chip:hover {{
    border-color: var(--ink);
    background: var(--paper-2);
  }}

  /* -------- GitHub deep block -------- */
  .acct .gh-deep {{
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 10px 11px;
    background: var(--paper);
    border: 1px solid var(--border);
    border-radius: 4px;
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 11px;
  }}
  .acct .gh-deep-row {{
    display: flex;
    flex-direction: column;
    gap: 4px;
  }}
  .acct .gh-deep-k {{
    font-size: 9.5px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.10em;
  }}
  .acct .gh-deep-v {{ color: var(--ink); }}
  .acct .gh-deep-src {{ color: var(--muted); font-size: 10px; }}
  .acct .gh-deep-note {{ color: var(--muted); font-style: italic; }}
  .acct .gh-deep-fold {{ background: transparent; border: none; padding: 0; margin: 0; }}
  .acct .gh-deep-fold > summary {{
    cursor: pointer;
    color: var(--muted);
    font-size: 9.5px;
    text-transform: uppercase;
    letter-spacing: 0.10em;
    padding: 0;
    list-style: none;
    user-select: none;
  }}
  .acct .gh-deep-fold > summary::-webkit-details-marker {{ display: none; }}
  .acct .gh-deep-fold > summary::before {{
    content: "▸ ";
    display: inline-block;
    transition: transform 0.15s ease;
  }}
  .acct .gh-deep-fold[open] > summary::before {{ transform: rotate(90deg); }}
  .acct .gh-deep-list {{
    list-style: none;
    margin: 6px 0 0;
    padding: 0;
    display: flex; flex-direction: column; gap: 5px;
  }}
  .acct .gh-deep-list li {{ line-height: 1.4; }}
  .acct .gh-deep-list a {{
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--rule);
  }}
  .acct .gh-repo-desc {{
    display: block;
    color: var(--muted);
    font-size: 10.5px;
    margin-top: 1px;
  }}
  .acct .gh-repo-stars {{ color: var(--ink); }}

  /* -------- Confirmed-missing dossier section -------- */
  .confirmed-missing-section {{
    margin-top: 26px;
  }}
  .confirmed-missing-list {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    padding: 12px;
    background: var(--paper-2);
    border: 1px solid var(--border);
    border-radius: 6px;
  }}
  .confirmed-missing-list .mtag {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 11px;
    padding: 3px 9px;
    background: var(--paper);
    color: var(--muted);
    border: 1px solid var(--border);
    border-radius: 3px;
    line-height: 1.3;
  }}

  /* -------- Evidence trace ("Why this score") -------- */
  .acct .evidence {{
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 11px;
    background: var(--paper);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0;
    margin: 0 0 -4px 0;
  }}
  .acct .evidence > summary {{
    list-style: none;
    cursor: pointer;
    padding: 7px 11px;
    color: var(--muted);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    font-size: 10px;
    user-select: none;
  }}
  .acct .evidence > summary::-webkit-details-marker {{ display: none; }}
  .acct .evidence > summary::before {{
    content: "▸ ";
    display: inline-block;
    transition: transform 0.15s ease;
  }}
  .acct .evidence[open] > summary::before {{
    transform: rotate(90deg);
  }}
  .acct .evidence:hover > summary {{ color: var(--ink); }}
  .acct .evd-body {{
    padding: 4px 11px 11px;
    border-top: 1px dashed var(--rule);
  }}
  .acct .evd-row {{
    display: flex;
    align-items: baseline;
    gap: 12px;
    padding: 4px 0;
    line-height: 1.45;
  }}
  .acct .evd-weight {{
    flex: 0 0 36px;
    text-align: right;
    font-weight: 500;
    font-variant-numeric: tabular-nums;
  }}
  .acct .evd-pos {{ color: var(--tier-verified); }}
  .acct .evd-neg {{ color: var(--err); }}
  .acct .evd-label {{
    color: var(--ink);
    font-size: 11px;
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
  .acct .open-btn {{
    display: block;
    width: 100%;
    text-align: center;
    box-sizing: border-box;
    font-family: 'IBM Plex Mono', ui-monospace, monospace;
    font-size: 13px; font-weight: 500;
    letter-spacing: 0.04em;
    background: var(--btn-bg);
    color: var(--btn-text);
    padding: 14px 16px;
    border-radius: 6px;
    border: 0;
    cursor: pointer;
    text-decoration: none;
    transition: background 0.15s ease;
    margin-top: auto;
  }}
  .acct .open-btn:hover {{ background: var(--btn-hover); }}
  .acct .open-btn .arrow {{ margin-left: 4px; }}

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
  .aux-table .err {{ color: var(--err); }}
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
    .header-right {{ justify-content: flex-end; }}
    .file-meta {{ text-align: left; }}
    .portrait {{ width: 96px; height: 96px; }}
    .portrait .letter {{ font-size: 50px; }}
    .ident .handle {{ font-size: 52px; letter-spacing: -0.025em; }}
    .ident .name-region {{ font-size: 18px; margin-top: 10px; }}
    .stats {{ grid-template-columns: repeat(2, 1fr); }}
    .stat + .stat::before {{ display: none; }}
    .stat:nth-child(odd) {{ border-right: 1px dashed var(--rule); }}
    .stat:nth-child(n+3) {{ border-top: 1px dashed var(--rule); }}
    .stat .n {{ font-size: 40px; }}
    section.combo {{ grid-template-columns: 1fr; gap: 18px; }}
    .subject-details {{ padding-top: 0; }}
    .detail-row {{ grid-template-columns: 1fr; gap: 6px; }}
    .accounts-grid {{ grid-template-columns: 1fr; }}
  }}
</style>
<script>
document.addEventListener('DOMContentLoaded', function() {{
  var btn = document.getElementById('theme-toggle');
  if (!btn) return;
  function applyTheme(t) {{
    document.documentElement.setAttribute('data-theme', t);
    btn.textContent = t === 'dark' ? '☾' : '☀';
  }}
  var init;
  try {{ init = localStorage.getItem('phantom-theme'); }} catch(e) {{}}
  if (!init) init = document.documentElement.getAttribute('data-theme') || 'light';
  applyTheme(init);
  btn.addEventListener('click', function() {{
    var cur = document.documentElement.getAttribute('data-theme') || 'light';
    var next = cur === 'dark' ? 'light' : 'dark';
    applyTheme(next);
    try {{ localStorage.setItem('phantom-theme', next); }} catch(e) {{}}
  }});
}});
</script>
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
  <div class="header-right">
    {toggle_button}
    <div class="file-meta">
      <div>File <span class="num">N° {file_number}</span></div>
      <div>{generated_date} · {generated_time} UTC</div>
      <div>Scan time {elapsed:.1f}s</div>
    </div>
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

<section class="combo">
  {photo_match_block}
  <div class="subject-details">
    <div class="kicker">Subject details</div>
    {detail_rows}
  </div>
</section>

{graph_section}

<section class="accounts">
  <div class="acct-filter-row">
    <div class="kicker">Confirmed presence — {n_found} accounts</div>
    <input class="acct-filter" type="search" placeholder="filter accounts…" aria-label="Filter accounts">
  </div>
  {found_section}
  <div class="filter-empty">No accounts match your filter.</div>
</section>

{confirmed_missing_section}

{emails_section}

<footer class="bottom">
  <div>Generated by Phantom</div>
  <div><a href="https://github.com/hamaffs/Phantom-" target="_blank" rel="noopener">github.com/hamaffs/Phantom-</a></div>
</footer>

<script>
(function() {{
  var input = document.querySelector('.acct-filter');
  if (!input) return;
  var section = document.querySelector('section.accounts');
  var cards = Array.prototype.slice.call(document.querySelectorAll('section.accounts .acct'));
  // Build a lowercase haystack per card once at load. Filter is substring,
  // not regex — fast and predictable.
  var index = cards.map(function(c) {{ return c.textContent.toLowerCase(); }});
  function apply() {{
    var q = input.value.trim().toLowerCase();
    var any = false;
    for (var i = 0; i < cards.length; i++) {{
      var match = !q || index[i].indexOf(q) !== -1;
      cards[i].classList.toggle('is-filtered-out', !match);
      if (match) any = true;
    }}
    section.classList.toggle('is-empty', !any && !!q);
  }}
  input.addEventListener('input', apply);
}})();
</script>

<script>
// Identity graph — interactive force-directed surveillance map with pan /
// zoom / drag, real profile photos as nodes, curved bezier edges, soft
// glow on the primary identity, score rings around every avatar, and a
// side panel that slides in when you click a node. Static — no
// animations on the edges; this is an investigation map, not a display.
(function() {{
  var dataEl = document.getElementById('identity-graph-data');
  if (!dataEl) return;
  var svg = document.querySelector('.identity-graph');
  if (!svg) return;
  var viewport = svg.querySelector('.viewport');
  var edgesG = svg.querySelector('.edges');
  var nodesG = svg.querySelector('.nodes');
  if (!viewport || !edgesG || !nodesG) return;
  var raw;
  try {{ raw = JSON.parse(dataEl.textContent); }} catch (e) {{ return; }}
  var nodes = raw.nodes, edges = raw.edges;
  if (!nodes || nodes.length < 2) return;

  var NS = 'http://www.w3.org/2000/svg';
  var panel = svg.parentNode;
  var sidePanelEl = panel.querySelector('#graph-side-panel');
  var sidePanelBody = sidePanelEl && sidePanelEl.querySelector('.gsp-body');

  // World-coord bounds the simulation lives in. We resize the SVG's
  // viewBox to match the actual panel size so 1 SVG-unit ≈ 1 CSS pixel
  // at zoom = 1 — makes drag math straightforward.
  var W = 0, H = 0;
  function syncSize() {{
    var rect = panel.getBoundingClientRect();
    W = Math.max(400, rect.width);
    H = Math.max(300, rect.height);
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  }}
  syncSize();
  window.addEventListener('resize', syncSize);

  // Two-island layout: split nodes by `bucket` (confirmed vs unrelated)
  // and give each island its own gravity target. Confirmed sits in the
  // left ~60% of the canvas, unrelated packs into the right ~25% —
  // visually two distinct webs instead of one mashed-together blob.
  var confirmed = nodes.filter(function(n) {{ return n.bucket === 'confirmed'; }});
  var unrelated = nodes.filter(function(n) {{ return n.bucket !== 'confirmed'; }});
  var hasTwoIslands = confirmed.length > 0 && unrelated.length > 0;
  // Centroid targets. If there's only one bucket, fall back to canvas centre.
  var confCx = hasTwoIslands ? W * 0.42 : W / 2;
  var confCy = H * 0.50;
  var unrelCx = hasTwoIslands ? W * 0.82 : W / 2;
  var unrelCy = H * 0.50;
  // Seed each island on its own circle.
  function _seed(group, cx, cy, rBase) {{
    var n = group.length;
    if (!n) return;
    var R = rBase * (1 + Math.min(1, n / 18));
    for (var i = 0; i < n; i++) {{
      var nd = group[i];
      var ang = (i / n) * Math.PI * 2;
      nd.x = cx + R * Math.cos(ang) + (Math.random() - 0.5) * 12;
      nd.y = cy + R * Math.sin(ang) + (Math.random() - 0.5) * 12;
      nd.vx = 0; nd.vy = 0;
      nd.pinned = false;
      nd._gx = cx;  // per-node gravity target X
      nd._gy = cy;  // per-node gravity target Y
    }}
  }}
  _seed(confirmed, confCx, confCy, Math.min(W * 0.20, H * 0.32));
  _seed(unrelated, unrelCx, unrelCy, Math.min(W * 0.10, H * 0.22));
  var nodeById = {{}};
  for (var i2 = 0; i2 < nodes.length; i2++) nodeById[nodes[i2].id] = nodes[i2];

  // Neighbours map (for hover-highlight).
  var neighbours = {{}};
  for (var ne = 0; ne < edges.length; ne++) {{
    var e = edges[ne];
    (neighbours[e.source] = neighbours[e.source] || []).push(e.target);
    (neighbours[e.target] = neighbours[e.target] || []).push(e.source);
  }}

  // For each (source, target) pair, we may have multiple kinds of edge
  // (cluster + photo + cross-link). Number them 0,1,2,... so the curve
  // generator can fan them out perpendicular to the line.
  var edgeIndex = {{}};
  for (var ei = 0; ei < edges.length; ei++) {{
    var ed = edges[ei];
    var key = ed.source + '/' + ed.target;
    if (!edgeIndex[key]) edgeIndex[key] = [];
    edgeIndex[key].push(ei);
  }}
  for (var ei2 = 0; ei2 < edges.length; ei2++) {{
    var ed2 = edges[ei2];
    var siblings = edgeIndex[ed2.source + '/' + ed2.target];
    ed2._offsetIdx = siblings.indexOf(ei2);
    ed2._offsetCount = siblings.length;
  }}

  // ---- Build SVG elements ------------------------------------------
  // (Cluster halos were removed — they dominated the canvas. Cluster
  // membership is still readable via the cluster-edge stroke colour and
  // the primary-identity glow on individual nodes.)

  // Edges as <path> so we can use quadratic beziers — static, no
  // animation. Real OSINT tools (Maltego, i2 Analyst's Notebook) are
  // visually still; movement reads as a toy.
  var edgeEls = [];
  for (var eii = 0; eii < edges.length; eii++) {{
    var ed3 = edges[eii];
    var path = document.createElementNS(NS, 'path');
    path.setAttribute('class', 'edge ' + (ed3.kind || 'cluster'));
    path.setAttribute('data-src', ed3.source);
    path.setAttribute('data-dst', ed3.target);
    edgesG.appendChild(path);
    edgeEls.push(path);
  }}

  // Nodes — each is a <g> with: background ring + score track + score
  // arc + clipped avatar (or letter fallback) + label.
  var nodeEls = [];
  for (var ni = 0; ni < nodes.length; ni++) {{
    var nd = nodes[ni];
    var g = document.createElementNS(NS, 'g');
    var classes = ['node'];
    if (nd.verified) classes.push('verified');
    if (nd.is_primary) classes.push('primary');
    if (nd.tier === 'possible_impostor') classes.push('impostor');
    if (nd.tier === 'verified_identity') classes.push('tier-verified');
    g.setAttribute('class', classes.join(' '));
    g.setAttribute('data-id', nd.id);

    // Radius scales with score so important nodes are visibly bigger.
    var rBase = 16 + Math.max(0, Math.min(100, nd.score)) * 0.10;
    nd._r = rBase;

    // Score ring (background track + foreground arc). Drawn slightly
    // outside the avatar so it reads as a "halo".
    var rRing = rBase + 5;
    var track = document.createElementNS(NS, 'circle');
    track.setAttribute('r', rRing);
    track.setAttribute('class', 'score-track');
    g.appendChild(track);

    // Score arc: dashed-circumference trick.
    var circ = 2 * Math.PI * rRing;
    var pct = Math.max(0, Math.min(100, nd.score)) / 100;
    var arc = document.createElementNS(NS, 'circle');
    arc.setAttribute('r', rRing);
    arc.setAttribute('transform', 'rotate(-90)');
    var arcClass = 'score-ring ' +
      (nd.tier === 'verified_identity' ? 'tier-verified' :
       nd.tier === 'possible_impostor' ? 'tier-impostor' : 'tier-likely');
    arc.setAttribute('class', arcClass);
    arc.setAttribute('stroke-dasharray', (circ * pct) + ' ' + circ);
    g.appendChild(arc);

    // Avatar circle. Either an embedded image (via a per-node <pattern>)
    // or a letter placeholder.
    var bg = document.createElementNS(NS, 'circle');
    bg.setAttribute('r', rBase);
    bg.setAttribute('class', 'node-bg');
    g.appendChild(bg);

    if (nd.photo) {{
      // Unique pattern id per node so multiple avatars can coexist.
      var patId = 'phantom-pat-' + nd.id;
      var defs = svg.querySelector('defs');
      var pat = document.createElementNS(NS, 'pattern');
      pat.setAttribute('id', patId);
      pat.setAttribute('patternUnits', 'objectBoundingBox');
      pat.setAttribute('width', '1');
      pat.setAttribute('height', '1');
      var img = document.createElementNS(NS, 'image');
      img.setAttribute('href', nd.photo);
      img.setAttribute('preserveAspectRatio', 'xMidYMid slice');
      img.setAttribute('width', rBase * 2);
      img.setAttribute('height', rBase * 2);
      pat.appendChild(img);
      defs.appendChild(pat);

      var avatar = document.createElementNS(NS, 'circle');
      avatar.setAttribute('r', rBase - 1);
      avatar.setAttribute('class', 'node-avatar');
      avatar.setAttribute('fill', 'url(#' + patId + ')');
      g.appendChild(avatar);
    }} else {{
      // Letter placeholder — first character of display name or site.
      var letter = (nd.display_name || nd.site || '?').slice(0, 1).toUpperCase();
      var t = document.createElementNS(NS, 'text');
      t.setAttribute('class', 'node-letter');
      t.textContent = letter;
      g.appendChild(t);
    }}

    var lab = document.createElementNS(NS, 'text');
    lab.setAttribute('class', 'node-label');
    lab.setAttribute('y', rBase + 16);
    lab.textContent = nd.site;
    g.appendChild(lab);

    nodesG.appendChild(g);
    nodeEls.push(g);
    nd._g = g;
  }}

  // ---- Pan + zoom state ---------------------------------------------
  var view = {{ tx: 0, ty: 0, scale: 1 }};
  var MIN_SCALE = 0.25, MAX_SCALE = 4;

  function applyView() {{
    viewport.setAttribute(
      'transform',
      'translate(' + view.tx + ',' + view.ty + ') scale(' + view.scale + ')'
    );
  }}

  function clientToWorld(clientX, clientY) {{
    // Convert a pointer event's client coords into SVG world coords,
    // taking the panel offset and the current view transform into account.
    var rect = panel.getBoundingClientRect();
    var sx = (clientX - rect.left) * (W / rect.width);
    var sy = (clientY - rect.top) * (H / rect.height);
    return {{
      x: (sx - view.tx) / view.scale,
      y: (sy - view.ty) / view.scale,
    }};
  }}

  function zoomAt(clientX, clientY, factor) {{
    var newScale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, view.scale * factor));
    if (newScale === view.scale) return;
    // Keep the world point under the cursor stationary while scaling.
    var w = clientToWorld(clientX, clientY);
    view.scale = newScale;
    // After scaling, the world point would have moved — adjust translate
    // to bring it back to the same client position.
    var rect = panel.getBoundingClientRect();
    var sx = (clientX - rect.left) * (W / rect.width);
    var sy = (clientY - rect.top) * (H / rect.height);
    view.tx = sx - w.x * view.scale;
    view.ty = sy - w.y * view.scale;
    applyView();
    kick();
  }}

  // Wheel zoom (cursor-centred).
  svg.addEventListener('wheel', function(ev) {{
    ev.preventDefault();
    var factor = ev.deltaY < 0 ? 1.15 : (1 / 1.15);
    zoomAt(ev.clientX, ev.clientY, factor);
  }}, {{ passive: false }});

  // Toolbar buttons.
  var toolbar = panel.querySelector('.graph-toolbar');
  if (toolbar) {{
    toolbar.addEventListener('click', function(ev) {{
      var btn = ev.target.closest('button');
      if (!btn) return;
      var action = btn.getAttribute('data-graph-zoom');
      var rect = panel.getBoundingClientRect();
      var cx = rect.left + rect.width / 2;
      var cy = rect.top + rect.height / 2;
      if (action === 'in') zoomAt(cx, cy, 1.3);
      else if (action === 'out') zoomAt(cx, cy, 1 / 1.3);
      else if (action === 'reset') {{
        view.tx = 0; view.ty = 0; view.scale = 1;
        applyView();
        // Unpin everything so the sim re-centres.
        for (var i = 0; i < nodes.length; i++) nodes[i].pinned = false;
        kick();
      }}
    }});
  }}

  // ---- Pointer-based pan + node drag --------------------------------
  // Single unified pointer handler covers mouse, pen, and touch (Pointer
  // Events are well-supported now and trackpad pinch usually arrives as
  // wheel events with ctrlKey set, handled above).
  var dragState = null;  // {{ kind: 'pan'|'node', startX, startY, ... }}

  function onPointerDown(ev) {{
    if (ev.button !== undefined && ev.button !== 0) return;
    var hitNode = ev.target.closest && ev.target.closest('.node');
    if (hitNode) {{
      var id = +hitNode.getAttribute('data-id');
      var nd = nodeById[id];
      var w = clientToWorld(ev.clientX, ev.clientY);
      dragState = {{
        kind: 'node',
        node: nd,
        offX: nd.x - w.x,
        offY: nd.y - w.y,
        startX: ev.clientX, startY: ev.clientY,
        moved: false,
      }};
      nd.pinned = true;
      hitNode.classList.add('dragging');
    }} else {{
      dragState = {{
        kind: 'pan',
        startX: ev.clientX, startY: ev.clientY,
        startTx: view.tx, startTy: view.ty,
        moved: false,
      }};
      svg.classList.add('is-panning');
    }}
    try {{ svg.setPointerCapture(ev.pointerId); }} catch (e) {{}}
  }}

  function onPointerMove(ev) {{
    if (!dragState) return;
    var dx = ev.clientX - dragState.startX;
    var dy = ev.clientY - dragState.startY;
    if (Math.abs(dx) + Math.abs(dy) > 3) dragState.moved = true;
    if (dragState.kind === 'pan') {{
      // Convert client deltas into SVG-world deltas via the viewBox ratio.
      var rect = panel.getBoundingClientRect();
      view.tx = dragState.startTx + dx * (W / rect.width);
      view.ty = dragState.startTy + dy * (H / rect.height);
      applyView();
    }} else if (dragState.kind === 'node') {{
      var w = clientToWorld(ev.clientX, ev.clientY);
      dragState.node.x = w.x + dragState.offX;
      dragState.node.y = w.y + dragState.offY;
      dragState.node.vx = 0;
      dragState.node.vy = 0;
      place();
      kick();
    }}
  }}

  function onPointerUp(ev) {{
    if (!dragState) return;
    var was = dragState;
    svg.classList.remove('is-panning');
    if (was.kind === 'node') {{
      was.node._g.classList.remove('dragging');
      // A click that didn't drag opens the side panel.
      if (!was.moved) {{
        openSidePanel(was.node);
        // Releasing a click un-pins the node so the sim can re-flow it.
        was.node.pinned = false;
        kick();
      }}
    }}
    dragState = null;
    try {{ svg.releasePointerCapture(ev.pointerId); }} catch (e) {{}}
  }}

  // ---- Side panel ---------------------------------------------------
  function _esc(s) {{
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }}
  function _fmtNum(n) {{
    if (n === null || n === undefined) return '—';
    if (Math.abs(n) >= 1e9) return (n/1e9).toFixed(1).replace(/\\.0$/, '') + 'B';
    if (Math.abs(n) >= 1e6) return (n/1e6).toFixed(1).replace(/\\.0$/, '') + 'M';
    if (Math.abs(n) >= 1e3) return (n/1e3).toFixed(1).replace(/\\.0$/, '') + 'K';
    return String(n);
  }}
  function openSidePanel(nd) {{
    if (!sidePanelEl || !sidePanelBody) {{
      // Fallback to the old scroll-to-card behaviour.
      var card = document.getElementById('acct-' + nd.id);
      if (card) card.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
      return;
    }}
    var avatarHtml = nd.photo
      ? '<div class="gsp-avatar" style="background-image:url(' + JSON.stringify(nd.photo) + ')"></div>'
      : '<div class="gsp-avatar"><span class="gsp-letter">'
        + _esc((nd.display_name || nd.site || '?').slice(0, 1).toUpperCase())
        + '</span></div>';
    var statsRow = '';
    var statBits = [];
    if (nd.followers != null) statBits.push(['Followers', _fmtNum(nd.followers)]);
    if (nd.following != null) statBits.push(['Following', _fmtNum(nd.following)]);
    if (nd.posts     != null) statBits.push(['Posts',     _fmtNum(nd.posts)]);
    if (statBits.length) {{
      statsRow = '<div class="gsp-stats">' + statBits.map(function(s) {{
        return '<div class="gsp-stat"><span class="n">' + _esc(s[1]) + '</span>'
             + '<span class="l">' + _esc(s[0]) + '</span></div>';
      }}).join('') + '</div>';
    }}
    var signals = nd.signals || [];
    var evdRows = signals.map(function(s) {{
      var w = +s.weight;
      var sign = w >= 0 ? '+' : '';
      var cls = w >= 0 ? 'pos' : 'neg';
      return '<div class="gsp-evd-row"><span class="gsp-evd-w ' + cls + '">'
           + sign + w + '</span><span>' + _esc(s.label) + '</span></div>';
    }}).join('');
    var evdBlock = evdRows
      ? '<div class="gsp-evidence">'
        + '<div class="gsp-evidence-title">Why this score</div>'
        + evdRows + '</div>'
      : '';
    var locLine = nd.location
      ? '<div class="gsp-platform" style="margin-top:8px">' + _esc(nd.location) + '</div>'
      : '';
    var tierBadge = nd.tier
      ? '<span style="text-transform:uppercase;letter-spacing:0.1em">'
        + _esc(nd.tier.replace(/_/g, ' ')) + '</span>'
      : '';
    sidePanelBody.innerHTML = ''
      + avatarHtml
      + '<div class="gsp-platform">' + _esc(nd.site) + (nd.verified ? ' · verified ✓' : '') + '</div>'
      + '<div class="gsp-name">' + _esc(nd.display_name || nd.handle || nd.site) + '</div>'
      + (nd.handle ? '<div class="gsp-handle">@' + _esc(nd.handle) + '</div>' : '')
      + locLine
      + (nd.bio ? '<div class="gsp-bio">' + _esc(nd.bio) + '</div>' : '')
      + statsRow
      + '<div class="gsp-score-row">'
        + tierBadge
        + '<span class="gsp-score-num">' + (nd.score == null ? '—' : nd.score) + '</span>'
      + '</div>'
      + evdBlock
      + '<div class="gsp-actions">'
        + (nd.url
            ? '<a class="gsp-btn" target="_blank" rel="noopener" href="'
              + _esc(nd.url) + '">Open profile ↗</a>'
            : '')
        + '<button class="gsp-btn secondary" data-gsp-to-card data-card-id="acct-' + nd.id
          + '">Show in cards</button>'
      + '</div>';
    sidePanelEl.classList.add('is-open');
    sidePanelEl.setAttribute('aria-hidden', 'false');
  }}
  function closeSidePanel() {{
    if (!sidePanelEl) return;
    sidePanelEl.classList.remove('is-open');
    sidePanelEl.setAttribute('aria-hidden', 'true');
  }}
  if (sidePanelEl) {{
    sidePanelEl.addEventListener('click', function(ev) {{
      if (ev.target.closest('[data-gsp-close]')) {{ closeSidePanel(); return; }}
      var jump = ev.target.closest('[data-gsp-to-card]');
      if (jump) {{
        var cardId = jump.getAttribute('data-card-id');
        var card = cardId && document.getElementById(cardId);
        if (card) {{
          card.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
          card.style.transition = 'outline 0.3s';
          card.style.outline = '2px solid var(--ink)';
          setTimeout(function() {{ card.style.outline = ''; }}, 1200);
        }}
        closeSidePanel();
      }}
    }});
    // Close on outside click (anywhere on the SVG that isn't a node).
    svg.addEventListener('pointerdown', function(ev) {{
      if (!sidePanelEl.classList.contains('is-open')) return;
      if (ev.target.closest('.node')) return;
      closeSidePanel();
    }});
    // ESC also closes.
    window.addEventListener('keydown', function(ev) {{
      if (ev.key === 'Escape') closeSidePanel();
    }});
  }}

  svg.addEventListener('pointerdown', onPointerDown);
  svg.addEventListener('pointermove', onPointerMove);
  svg.addEventListener('pointerup', onPointerUp);
  svg.addEventListener('pointercancel', onPointerUp);

  // ---- Hover-highlight (no drag) ------------------------------------
  function setHighlight(focusId) {{
    var allowed = null;
    if (focusId !== null) {{
      allowed = {{}}; allowed[focusId] = true;
      var nb = neighbours[focusId] || [];
      for (var i = 0; i < nb.length; i++) allowed[nb[i]] = true;
    }}
    for (var n = 0; n < nodeEls.length; n++) {{
      var el = nodeEls[n];
      var id = +el.getAttribute('data-id');
      el.classList.toggle('dimmed', allowed !== null && !allowed[id]);
    }}
    for (var e = 0; e < edgeEls.length; e++) {{
      var ee = edgeEls[e];
      var src = +ee.getAttribute('data-src');
      var dst = +ee.getAttribute('data-dst');
      var keep = (allowed === null) || (src === focusId || dst === focusId);
      ee.classList.toggle('dimmed', !keep);
    }}
  }}
  nodesG.addEventListener('mouseover', function(ev) {{
    if (dragState) return;
    var g = ev.target.closest('.node');
    if (!g) return;
    setHighlight(+g.getAttribute('data-id'));
  }});
  nodesG.addEventListener('mouseout', function(ev) {{
    if (dragState) return;
    if (ev.relatedTarget && ev.relatedTarget.closest && ev.relatedTarget.closest('.node')) return;
    setHighlight(null);
  }});

  // ---- Continuous simulation ----------------------------------------
  // Parameters tuned so a 15-30 node graph settles with breathing room
  // between every pair. Once layout finishes, ALL nodes get pinned
  // automatically — from then on the only way anything moves is a
  // manual drag. This matches the "static investigation map" model
  // (Maltego/i2 Analyst's Notebook) the user asked for.
  var DAMPING = 0.86;
  // Repulsion is stronger here than before — cliques pull aggressively
  // toward their centroid and we want them spread regardless.
  var REPULSION = 7000 + nodes.length * 350;
  // Spring force is divided per-edge by sqrt(node degree) inside step(),
  // so densely-connected nodes don't get yanked inward by every edge.
  var SPRING_K = 0.024;
  var SPRING_LEN = 200 + Math.min(140, nodes.length * 5);
  var GRAVITY = 0.012;         // per-bucket gravity — stronger so an
                               // impostor with a real-photo edge to the
                               // confirmed web still stays in its island
  var ENERGY_FLOOR = 0.012;
  var WAKE_TICKS = 100;
  var ticksLeft = 480;        // longer initial settle — bigger forces
  var initialSettleDone = false;

  // Pre-compute node degree (count of edges touching the node) so we
  // can divide spring force by sqrt(degree). Otherwise a node with 12
  // edges feels 12× the inward spring force and the clique collapses.
  var degree = {{}};
  for (var de = 0; de < edges.length; de++) {{
    var ed4 = edges[de];
    degree[ed4.source] = (degree[ed4.source] || 0) + 1;
    degree[ed4.target] = (degree[ed4.target] || 0) + 1;
  }}
  for (var nid in nodeById) {{
    nodeById[nid]._springScale = 1 / Math.sqrt(Math.max(1, degree[nid] || 1));
  }}

  function kick() {{ if (ticksLeft < WAKE_TICKS) ticksLeft = WAKE_TICKS; }}

  function place() {{
    // Nodes.
    for (var i = 0; i < nodes.length; i++) {{
      var n = nodes[i];
      n._g.setAttribute('transform', 'translate(' + n.x + ',' + n.y + ')');
    }}
    // Edges as quadratic beziers. Parallel edges between the same pair
    // get a perpendicular offset so they fan out instead of overlapping.
    for (var j = 0; j < edges.length; j++) {{
      var e = edges[j];
      var s = nodeById[e.source], d = nodeById[e.target];
      var dx = d.x - s.x, dy = d.y - s.y;
      var len = Math.sqrt(dx * dx + dy * dy) || 0.001;
      // Perpendicular unit vector
      var nx = -dy / len, ny = dx / len;
      // Curve depth: gently bowed by default; more bow for sibling edges
      // so they don't overlap.
      var bow = 18;
      var off = 0;
      if (e._offsetCount > 1) {{
        off = (e._offsetIdx - (e._offsetCount - 1) / 2) * 22;
      }}
      var cx = (s.x + d.x) / 2 + nx * (bow + off);
      var cy = (s.y + d.y) / 2 + ny * (bow + off);
      var pathD = 'M ' + s.x + ' ' + s.y
                + ' Q ' + cx + ' ' + cy + ' ' + d.x + ' ' + d.y;
      edgeEls[j].setAttribute('d', pathD);
    }}
  }}


  function step() {{
    var totalKE = 0;
    for (var i = 0; i < nodes.length; i++) {{
      var n = nodes[i];
      if (n.pinned) continue;
      // Repulsion against every other node.
      for (var k = 0; k < nodes.length; k++) {{
        if (i === k) continue;
        var m = nodes[k];
        var dx = n.x - m.x, dy = n.y - m.y;
        var d2 = dx * dx + dy * dy + 0.01;
        var f = REPULSION / d2;
        var dist = Math.sqrt(d2);
        n.vx += (dx / dist) * f * 0.04;
        n.vy += (dy / dist) * f * 0.04;
      }}
      // Gravity toward per-node target (each bucket has its own centroid).
      n.vx += ((n._gx || W / 2) - n.x) * GRAVITY;
      n.vy += ((n._gy || H / 2) - n.y) * GRAVITY;
    }}
    for (var j = 0; j < edges.length; j++) {{
      var e = edges[j];
      var s = nodeById[e.source], d = nodeById[e.target];
      var dx = d.x - s.x, dy = d.y - s.y;
      var dist = Math.sqrt(dx * dx + dy * dy) || 0.001;
      var k2 = (e.kind === 'cluster') ? SPRING_K * 1.4 : SPRING_K;
      // Divide spring force by sqrt(degree) — without this, dense
      // cliques pull every member to the centre because each node has
      // 10+ springs all pulling inward simultaneously.
      var fSpring = (dist - SPRING_LEN) * k2;
      var ux = dx / dist, uy = dy / dist;
      if (!s.pinned) {{
        s.vx += ux * fSpring * (s._springScale || 1);
        s.vy += uy * fSpring * (s._springScale || 1);
      }}
      if (!d.pinned) {{
        d.vx -= ux * fSpring * (d._springScale || 1);
        d.vy -= uy * fSpring * (d._springScale || 1);
      }}
    }}
    for (var n2 = 0; n2 < nodes.length; n2++) {{
      var nn = nodes[n2];
      if (nn.pinned) continue;
      nn.x += nn.vx; nn.y += nn.vy;
      nn.vx *= DAMPING; nn.vy *= DAMPING;
      totalKE += nn.vx * nn.vx + nn.vy * nn.vy;
    }}
    return totalKE / Math.max(1, nodes.length);
  }}

  function pinAll() {{
    for (var i = 0; i < nodes.length; i++) nodes[i].pinned = true;
  }}
  function unpinAll() {{
    for (var i = 0; i < nodes.length; i++) nodes[i].pinned = false;
  }}

  // Main rAF loop.
  //   Initial phase: runs the simulation for ticksLeft frames, settles
  //   the layout into the two-island shape. After settling, every node
  //   is auto-pinned so dragging one no longer yanks its neighbours
  //   along — they stay put unless the user moves them individually.
  //   Drag phase: re-renders positions only for the dragged node;
  //   no simulation forces apply.
  function loop() {{
    if (initialSettleDone) {{
      // Already settled — only re-render if a drag is in progress, and
      // even then we only move the dragged node (no simulation).
      if (dragState && dragState.kind === 'node') place();
      // Idle: stop the loop until the user does something.
      return;
    }}
    var ke = step();
    place();
    ticksLeft--;
    if (ticksLeft <= 0 || ke <= ENERGY_FLOOR) {{
      // Settled — freeze everything in place.
      initialSettleDone = true;
      pinAll();
      return;
    }}
    requestAnimationFrame(loop);
  }}
  requestAnimationFrame(loop);

  // Drag-loop driver: keeps redrawing while the user holds a node.
  // When the drag ends the loop stops; static state is restored.
  var dragLoopActive = false;
  function startDragLoop() {{
    if (dragLoopActive) return;
    dragLoopActive = true;
    (function tick() {{
      if (!dragState || dragState.kind !== 'node') {{
        dragLoopActive = false;
        return;
      }}
      place();
      requestAnimationFrame(tick);
    }})();
  }}
  svg.addEventListener('pointerdown', function(ev) {{
    if (ev.target.closest && ev.target.closest('.node')) startDragLoop();
  }}, true);

  // Reset button restarts the settle phase (unpins everything, resets
  // velocities, re-runs the sim).
  function restartSettle() {{
    initialSettleDone = false;
    ticksLeft = 480;
    unpinAll();
    for (var i = 0; i < nodes.length; i++) {{
      nodes[i].vx = 0; nodes[i].vy = 0;
    }}
    requestAnimationFrame(loop);
  }}
  // Override the existing reset behaviour to call restartSettle().
  if (toolbar) {{
    toolbar.addEventListener('click', function(ev) {{
      var btn = ev.target.closest('button');
      if (btn && btn.getAttribute('data-graph-zoom') === 'reset') {{
        restartSettle();
      }}
    }});
  }}
}})();
</script>

</div>
</body>
</html>
"""
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
def _format_profile_details(profile: dict) -> str:
    """Render the per-card details strip — followers / following / posts /
    hearts / joined / verified / public-private — bullet-separated.

    Each field is skipped silently if the platform didn't surface it.
    Returns the empty string when nothing's available so the card can
    omit the row entirely. Numeric counts go through `_format_count`
    (12345 → '12.3K'); joined dates go through `_format_joined`. The
    verified flag renders bolder than the rest of the strip; private/
    public is shown as plain text.
    """
    p = profile or {}
    bits: list[str] = []

    if p.get("followers") is not None:
        bits.append(f"{html.escape(_format_count(p['followers']))} followers")
    if p.get("following") is not None:
        bits.append(f"{html.escape(_format_count(p['following']))} following")
    if p.get("posts") is not None:
        bits.append(f"{html.escape(_format_count(p['posts']))} posts")
    if p.get("hearts") is not None:
        bits.append(f"{html.escape(_format_count(p['hearts']))} hearts")

    joined = _format_joined(p.get("joined")) or p.get("joined")
    if joined:
        bits.append(f"joined {html.escape(str(joined))}")

    if p.get("verified") is True:
        bits.append('<b class="verified-tag">verified ✓</b>')

    if p.get("private") is True:
        bits.append("private")
    elif p.get("private") is False:
        bits.append("public")

    if not bits:
        return ""
    return f'<div class="acct-details">{" · ".join(bits)}</div>'
def _platform_domain(url: str) -> Optional[str]:
    """Return the canonical platform short-name for a known social URL,
    or None for unknown hosts.

    Used to label `linked_accounts` chips with a recognisable platform
    name rather than a raw domain ("Twitter" instead of "twitter.com").
    """
    m = re.search(r"https?://(?:www\.)?([^/]+)/", url + "/")
    if not m:
        return None
    host = m.group(1).lower()
    # Compact lookup. Add to this list when a new platform shows up
    # in linked_accounts harvesting.
    map_ = {
        "twitter.com": "Twitter", "x.com": "X",
        "instagram.com": "Instagram",
        "tiktok.com": "TikTok",
        "threads.net": "Threads", "threads.com": "Threads",
        "youtube.com": "YouTube", "youtu.be": "YouTube",
        "twitch.tv": "Twitch",
        "github.com": "GitHub",
        "facebook.com": "Facebook", "fb.com": "Facebook",
        "reddit.com": "Reddit",
        "linkedin.com": "LinkedIn",
        "linktr.ee": "Linktree", "beacons.ai": "Beacons",
        "bio.link": "Bio.link", "carrd.co": "Carrd",
        "t.me": "Telegram", "telegram.me": "Telegram",
        "medium.com": "Medium", "dev.to": "Dev.to",
        "behance.net": "Behance", "dribbble.com": "Dribbble",
        "soundcloud.com": "SoundCloud", "bandcamp.com": "Bandcamp",
        "mixcloud.com": "Mixcloud", "spotify.com": "Spotify",
        "keybase.io": "Keybase",
        "bsky.app": "Bluesky",
        "patreon.com": "Patreon",
        "ko-fi.com": "Ko-fi", "buymeacoffee.com": "BuyMeACoffee",
        "huggingface.co": "HuggingFace",
        "pinterest.com": "Pinterest",
    }
    if host in map_:
        return map_[host]
    # Mastodon instances vary — soft-match.
    if "mastodon" in host or host.endswith(".social"):
        return "Mastodon"
    return None


def _html_facts_block(profile: dict) -> str:
    """Render the per-card "facts" strip — location, website, business
    email, category. Empty fields are skipped silently. Returns the
    empty string when no facts are available so the card omits the
    block entirely."""
    p = profile or {}
    rows: list[str] = []

    loc = p.get("location")
    if isinstance(loc, str) and loc.strip():
        rows.append(
            f'<div class="fact-row"><span class="fact-k">Location</span>'
            f'<span class="fact-v">{html.escape(loc.strip())}</span></div>'
        )
    site = p.get("website")
    if isinstance(site, str) and site.strip():
        # Display abbreviated URL; full URL goes in title attribute and href.
        clean = re.sub(r"^https?://(?:www\.)?", "", site.strip()).rstrip("/")
        href = site.strip()
        if not href.startswith(("http://", "https://")):
            href = "https://" + href
        rows.append(
            f'<div class="fact-row"><span class="fact-k">Website</span>'
            f'<a class="fact-v fact-link" href="{html.escape(href, quote=True)}" '
            f'target="_blank" rel="noopener noreferrer">{html.escape(clean[:60])}</a></div>'
        )
    email = p.get("email")
    if isinstance(email, str) and email.strip():
        rows.append(
            f'<div class="fact-row"><span class="fact-k">Email</span>'
            f'<a class="fact-v fact-link" href="mailto:{html.escape(email.strip(), quote=True)}">'
            f'{html.escape(email.strip())}</a></div>'
        )
    cat = p.get("category")
    if isinstance(cat, str) and cat.strip():
        rows.append(
            f'<div class="fact-row"><span class="fact-k">Category</span>'
            f'<span class="fact-v">{html.escape(cat.strip())}</span></div>'
        )
    wb = p.get("wayback_first_snapshot")
    if isinstance(wb, str) and wb.strip():
        archive_url = p.get("wayback_archive_url") or ""
        count = p.get("wayback_snapshot_count")
        count_text = (
            f' · {count:,} snapshots' if isinstance(count, int) and count > 1
            else ""
        )
        value = f"{html.escape(wb)}{html.escape(count_text)}"
        if archive_url:
            value = (
                f'<a class="fact-link" href="{html.escape(archive_url, quote=True)}" '
                f'target="_blank" rel="noopener noreferrer">{value}</a>'
            )
        rows.append(
            f'<div class="fact-row"><span class="fact-k">First archived</span>'
            f'<span class="fact-v">{value}</span></div>'
        )

    if not rows:
        return ""
    return f'<div class="facts">{"".join(rows)}</div>'


def _html_linked_chips(profile: dict, current_url: str) -> str:
    """Render `linked_accounts` as small clickable platform chips.

    Chips drop any URL whose host matches the current account's host —
    a Twitter card linking back to twitter.com is noise.
    """
    p = profile or {}
    links = p.get("linked_accounts") or []
    if not isinstance(links, list) or not links:
        return ""
    cur_host_m = re.search(r"https?://(?:www\.)?([^/]+)", current_url or "")
    cur_host = cur_host_m.group(1).lower() if cur_host_m else ""
    chips: list[str] = []
    seen: set[str] = set()
    for u in links:
        if not isinstance(u, str) or not u.strip():
            continue
        u = u.strip()
        if not u.startswith(("http://", "https://")):
            u = "https://" + u
        host_m = re.search(r"https?://(?:www\.)?([^/]+)", u)
        host = host_m.group(1).lower() if host_m else ""
        if host == cur_host:
            continue
        key = u.lower()
        if key in seen:
            continue
        seen.add(key)
        label = _platform_domain(u) or host or "Link"
        chips.append(
            f'<a class="link-chip" target="_blank" rel="noopener noreferrer" '
            f'href="{html.escape(u, quote=True)}" title="{html.escape(u)}">'
            f'{html.escape(label)}</a>'
        )
        if len(chips) >= 10:
            break
    if not chips:
        return ""
    return (
        f'<div class="link-chips">'
        f'<span class="link-chips-k">Linked accounts</span>'
        f'<div class="link-chips-row">{"".join(chips)}</div>'
        f'</div>'
    )


def _html_github_deep_block(profile: dict) -> str:
    """Render GitHub-deep enrichment: orgs, starred repos, commit email.

    Empty when no github-deep fields are present (typical — only the
    GitHub account gets this data).
    """
    p = profile or {}
    has_anything = any(
        p.get(k) for k in (
            "organizations", "recent_starred",
            "commit_email", "commit_email_note",
        )
    )
    if not has_anything:
        return ""
    parts: list[str] = []

    orgs = p.get("organizations") or []
    if isinstance(orgs, list) and orgs:
        chips = "".join(
            f'<a class="link-chip" target="_blank" rel="noopener noreferrer" '
            f'href="https://github.com/{html.escape(o, quote=True)}">'
            f'{html.escape(o)}</a>'
            for o in orgs[:12] if isinstance(o, str) and o
        )
        if chips:
            parts.append(
                f'<div class="gh-deep-row">'
                f'<span class="gh-deep-k">Organizations</span>'
                f'<div class="link-chips-row">{chips}</div>'
                f'</div>'
            )

    starred = p.get("recent_starred") or []
    if isinstance(starred, list) and starred:
        items = []
        for repo in starred[:5]:
            if not isinstance(repo, dict):
                continue
            name = repo.get("name") or ""
            if not name:
                continue
            desc = (repo.get("description") or "")[:100]
            stars = repo.get("stars")
            stars_tag = f' · ★ {stars:,}' if isinstance(stars, int) else ""
            items.append(
                f'<li><a target="_blank" rel="noopener noreferrer" '
                f'href="https://github.com/{html.escape(name, quote=True)}">'
                f'{html.escape(name)}</a>'
                f'<span class="gh-repo-desc">{html.escape(desc)}'
                f'<span class="gh-repo-stars">{html.escape(stars_tag)}</span></span></li>'
            )
        if items:
            parts.append(
                f'<details class="gh-deep-fold">'
                f'<summary>Recently starred ({len(starred)})</summary>'
                f'<ul class="gh-deep-list">{"".join(items)}</ul>'
                f'</details>'
            )

    email = p.get("commit_email")
    note = p.get("commit_email_note")
    src = p.get("commit_email_source")
    if email:
        link_html = (
            f'<a class="fact-link" href="{html.escape(src, quote=True)}" '
            f'target="_blank" rel="noopener noreferrer" '
            f'title="Leaked from commit .patch">.patch</a>'
            if src else ""
        )
        parts.append(
            f'<div class="gh-deep-row">'
            f'<span class="gh-deep-k">Commit email</span>'
            f'<span class="gh-deep-v">'
            f'<a class="fact-link" href="mailto:{html.escape(email, quote=True)}">{html.escape(email)}</a>'
            f' <span class="gh-deep-src">{link_html}</span>'
            f'</span></div>'
        )
    elif note:
        parts.append(
            f'<div class="gh-deep-row">'
            f'<span class="gh-deep-k">Commit email</span>'
            f'<span class="gh-deep-v gh-deep-note">{html.escape(note)}</span>'
            f'</div>'
        )

    if not parts:
        return ""
    return f'<div class="gh-deep">{"".join(parts)}</div>'


def _html_card(r: CheckResult, photo_match: Optional[list] = None,
               tier: Optional[str] = None, photo_bytes_map: Optional[dict] = None,
               card_id: Optional[str] = None) -> str:
    """Render one FOUND profile as an editorial dossier account card.

    Vertical flow:
      ┌────────────────────────────────────┐
      │ [photo]  display name              │  ← header row
      │          @handle                   │
      │  bio                               │
      │  details · followers · joined …    │
      │  [platform tag]  [●] [score N]     │
      │ ┌────────────────────────────────┐ │
      │ │       Open profile  ↗          │ │  ← full-width CTA
      │ └────────────────────────────────┘ │
      └────────────────────────────────────┘
    """
    p = r.profile or {}
    target = r.url

    photo_url = p.get("photo")
    # Suppress platform-default placeholder avatars (Instagram gradient,
    # Facebook silhouette URL fragment, etc.) so the card falls back to
    # the letter glyph — same rule the identity graph uses.
    if photo_url and _photo_is_likely_default(photo_url, photo_bytes_map):
        photo_url = None
    initial = (p.get("display_name") or r.variant or r.site or "?")[:1].upper()
    if photo_url:
        src = _photo_to_data_uri(photo_url, photo_bytes_map) or html.escape(photo_url, quote=True)
        photo_html = (
            f'<div class="photo" style="background-image:url(\'{src}\')"></div>'
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
    details_html = _format_profile_details(p)

    head = (
        '<div class="acct-head">'
        f'{photo_html}'
        '<div class="acct-head-text">'
        f'<div class="display-name">{html.escape(display_name)}</div>'
        f'<div class="handle">{html.escape(handle)}</div>'
        '</div>'
        '</div>'
    )

    # Tier dot: small green circle for verified identity.
    effective_tier = tier or r.tier
    dot_html = (
        '<span class="tier-dot tier-verified" title="Verified identity"></span>'
        if effective_tier == TIER_VERIFIED else ""
    )
    # Score chip.
    score_html = (
        f'<span class="score-chip">{r.score}</span>'
        if r.score is not None else ""
    )
    meta_row = (
        '<div class="acct-meta-row">'
        f'<span class="platform-tag">{html.escape(r.site)}</span>'
        f'{dot_html}{score_html}'
        '</div>'
    )

    button = (
        f'<a class="open-btn" href="{html.escape(target, quote=True)}" '
        f'target="_blank" rel="noopener noreferrer" '
        f'title="{html.escape(target)}">'
        f'Open profile <span class="arrow">↗</span></a>'
    )

    # Evidence trace — every signal that fired in confidence.score_result,
    # rendered as a collapsible "Why this score" block under the card. This
    # is the kind of judgment Maigret literally doesn't produce: instead of
    # just showing a number, we show *why* that number.
    evidence_html = ""
    signals = getattr(r, "signals", None) or []
    if signals:
        rows = []
        for s in signals:
            w = int(s.get("weight", 0))
            sign = "+" if w >= 0 else ""
            klass = "evd-pos" if w >= 0 else "evd-neg"
            rows.append(
                f'<div class="evd-row">'
                f'<span class="evd-weight {klass}">{sign}{w}</span>'
                f'<span class="evd-label">{html.escape(str(s.get("label", "")))}</span>'
                f'</div>'
            )
        evidence_html = (
            '<details class="evidence">'
            '<summary>Why this score</summary>'
            f'<div class="evd-body">{"".join(rows)}</div>'
            '</details>'
        )

    # Facts strip (location, website, business email, category, wayback)
    # and linked-accounts chips. Both only render when there's data.
    facts_html = _html_facts_block(p)
    linked_html = _html_linked_chips(p, r.url or "")
    # GitHub deep-dive block (orgs, starred repos, commit email).
    # No-op for non-GitHub accounts.
    gh_deep_html = _html_github_deep_block(p)

    id_attr = f' id="{html.escape(card_id, quote=True)}"' if card_id else ""
    return (
        f'<div class="acct"{id_attr}>'
        f'{head}{bio_html}{details_html}'
        f'{facts_html}{linked_html}{gh_deep_html}'
        f'{meta_row}{evidence_html}{button}'
        '</div>'
    )
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
# Sites whose users typically post real selfies as the avatar — break
# ties in favour of these when several photos contain a detected face.
_SELFIE_SITES = frozenset({
    "Behance", "Instagram", "Twitter", "Threads", "Facebook", "LinkedIn",
})
# Sites that commonly carry logos or stylised avatars rather than faces.
_LOGO_SITES = frozenset({
    "GitHub", "Pastebin", "Disqus", "Pinterest",
})


def _site_priority(site: str) -> int:
    """Lower = more preferred when several face photos tie on cluster size."""
    if site in _SELFIE_SITES:
        return 0
    if site in _LOGO_SITES:
        return 2
    return 1


def _pick_subject_photo(overall, clusters, found, face_map=None) -> Optional[str]:
    """Pick the dossier hero portrait with face-aware priority.

    Order (highest priority first):
      (a) Photos with a detected human face — sort by cluster size desc,
          then by site priority asc (Behance/IG/Twitter/Threads/FB/
          LinkedIn beat GitHub/Pastebin/Disqus/Pinterest beat neutral).
      (b) No face detected anywhere, BUT a selfie-site photo is
          available — Behance, Instagram, etc. are very likely real
          even when Haar misses the face (off-angle, small crop, hat,
          glasses, partial occlusion). Prefer the selfie-site photo
          over a logo-site or generic-avatar photo. Sort by cluster
          size desc, then site priority asc.
      (c) No selfie-site photo either → largest photo-matched cluster's
          representative photo (the user's chosen self-representation,
          logo or otherwise).
      (d) No clusters → first FOUND profile with any photo.
      (e) Nothing → None (caller renders the letter placeholder).

    Only drives the big hero portrait. Per-account 64×64 cards keep
    showing whatever each platform exposed. Logs every candidate +
    selection reason to stderr so future mismatches are debuggable.
    """
    face_map = face_map or {}

    # Cluster-coverage map: photo URL → max number of sites in any
    # cluster that includes that photo. Used by (a) and (b) sorts.
    coverage: dict[str, int] = {}
    for c in (clusters or []):
        size = len(getattr(c, "sites", None) or c.member_indexes)
        for url in (getattr(c, "photos", []) or []):
            if size > coverage.get(url, 0):
                coverage[url] = size

    # Build a candidate list from FOUND profiles' photos.
    candidates: list[dict] = []
    seen_urls: set[str] = set()
    for r in found:
        url = (r.profile or {}).get("photo")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        candidates.append({
            "url": url,
            "site": r.site,
            "has_face": bool(face_map.get(url)),
            "cluster_size": coverage.get(url, 0),
            "is_selfie_site": r.site in _SELFIE_SITES,
            "is_logo_site": r.site in _LOGO_SITES,
        })

    # Debug logging: every candidate + their decision-relevant fields.
    if candidates:
        print(
            f"[portrait] evaluating {len(candidates)} candidate(s):",
            file=sys.stderr,
        )
        for c in candidates:
            url_short = c["url"] if len(c["url"]) <= 70 else c["url"][:67] + "…"
            tag = ""
            if c["is_selfie_site"]:
                tag = " (selfie-site)"
            elif c["is_logo_site"]:
                tag = " (logo-site)"
            print(
                f"[portrait]   {c['site']:13} face={'yes' if c['has_face'] else 'no ':<3} "
                f"cluster={c['cluster_size']}{tag} {url_short}",
                file=sys.stderr,
            )

    def _log(reason: str, url: str) -> str:
        url_short = url if len(url) <= 70 else url[:67] + "…"
        print(f"[portrait] selected ({reason}): {url_short}", file=sys.stderr)
        return url

    if not candidates:
        if overall and getattr(overall, "photos", None):
            return _log("overall.photos[0], no candidates", overall.photos[0])
        print("[portrait] selected: none (no photos available)", file=sys.stderr)
        return None

    # (a) face-bearing photos: cluster size desc, site priority asc.
    face_cands = [c for c in candidates if c["has_face"]]
    if face_cands:
        face_cands.sort(
            key=lambda c: (-c["cluster_size"], _site_priority(c["site"]))
        )
        return _log("face detected", face_cands[0]["url"])

    # (b) No face anywhere — but selfie-site photos are still very
    # likely real (Haar misses faces routinely on creative profile
    # angles). Prefer them over logo-leaning sites.
    selfie_cands = [c for c in candidates if c["is_selfie_site"]]
    if selfie_cands:
        selfie_cands.sort(
            key=lambda c: (-c["cluster_size"], _site_priority(c["site"]))
        )
        return _log("selfie-site (no face detected)", selfie_cands[0]["url"])

    # (c) largest photo-matched cluster's representative photo.
    multi = [c for c in (clusters or []) if len(c.member_indexes) > 1]
    if multi:
        biggest = max(multi, key=lambda c: len(c.member_indexes))
        if biggest.photos:
            return _log("largest cluster", biggest.photos[0])
    if overall and getattr(overall, "photos", None):
        return _log("overall.photos[0]", overall.photos[0])

    # (d) first FOUND profile with any photo.
    return _log("first candidate", candidates[0]["url"])
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
def _html_subject_portrait(photo_url: Optional[str], handle: str, photo_bytes_map: Optional[dict] = None) -> str:
    """100×100 portrait — image if available, otherwise solid block with
    the first letter of the handle in serif."""
    if photo_url:
        src = _photo_to_data_uri(photo_url, photo_bytes_map) or html.escape(photo_url, quote=True)
        return (
            f'<div class="portrait" '
            f'style="background-image:url(\'{src}\')">'
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
# Words that show up inside platform-decorated display names but aren't
# part of the user's actual name. Used to detect the "noise" tier of
# display_name strings (e.g. "Hamaffs's Pastebin - Pastebin.com").
_NAME_NOISE_TOKENS = frozenset({
    "pastebin", "github", "youtube", "twitter", "twitch", "tiktok",
    "instagram", "threads", "facebook", "linkedin", "reddit",
    "soundcloud", "bandcamp", "mixcloud", "behance", "dribbble",
    "medium", "linktree", "beacons", "huggingface", "pillowfort",
    "myanimelist", "lichess", "spotify", "discogs", "spotify",
    "profile", "channel", "user", "account", "official", "verified",
    "stats", "overview", "streamer", "creator", "page",
})


def _name_token_ok(tok: str) -> bool:
    """A real-name token is mostly alphabetic, no digits, no underscores,
    starts uppercase, length 2-24. Allows apostrophes and hyphens for
    names like O'Brien or Jean-Pierre. Allows non-ASCII letters."""
    if not tok or not (2 <= len(tok) <= 24):
        return False
    if any(c.isdigit() or c == "_" for c in tok):
        return False
    if not tok[0].isupper():
        return False
    # 80% of characters must be alphabetic (accents/letters across scripts)
    letters = sum(1 for c in tok if c.isalpha())
    if letters / len(tok) < 0.8:
        return False
    return True


def _classify_name(s: str, variant_set: set[str]) -> str:
    """Return one of 'real_name', 'nickname', 'username', 'noise'.

    Heuristic:
      - real_name: 2+ tokens, each token passes _name_token_ok, AND
        the whole string contains no noise tokens (platform names,
        decorator words like 'Profile'). Examples: "Alex Stevens",
        "Jean-Pierre Dupont", "Maria O'Brien".
      - nickname: single token that passes _name_token_ok, isn't in
        the variant set we already tested, and isn't a noise token.
        Examples: "Hama", "PewDiePie".
      - username: matches (case-insensitive) one of the searched
        variants — same data we already have on the handle line.
      - noise: everything else (platform-decorated titles, empty,
        too long, all caps, contains a platform name).
    """
    if not s:
        return "noise"
    s = s.strip()
    if not s or len(s) > 60:
        return "noise"

    lower = s.lower()
    # Reject anything containing a platform/decorator noise token.
    for noise in _NAME_NOISE_TOKENS:
        if noise in lower:
            return "noise"

    # Same as one of the variants we already tested.
    if lower in variant_set:
        return "username"

    # Tokenise on whitespace.
    tokens = s.split()
    if len(tokens) >= 2 and all(_name_token_ok(t) for t in tokens):
        return "real_name"
    if len(tokens) == 1 and _name_token_ok(tokens[0]):
        return "nickname"
    return "noise"


def _detect_subject_names(
    found: list, variant_set: set[str],
) -> tuple[str, str]:
    """Walk every FOUND profile's display_name + first bio line and
    return `(real_name, nickname)` — either may be empty when no
    confident match exists.

    Strategy: count how many primary-cluster accounts surface each
    distinct real-name / nickname string. Most-recurring wins.
    Ties broken by longest (more specific → less likely to be noise).
    """
    real_counts: dict[str, int] = {}
    nick_counts: dict[str, int] = {}

    for r in found:
        if r.exists is not True:
            continue
        # Skip impostor-tier results — their names are misleading.
        if getattr(r, "tier", None) == "possible_impostor":
            continue
        # Skip non-primary-identity members when we have a cluster.
        if r.is_primary_identity is False:
            continue
        p = r.profile or {}
        candidates = []
        dn = p.get("display_name")
        if isinstance(dn, str) and dn.strip():
            candidates.append(dn.strip())
        # Bios sometimes carry the real name as the first / only line —
        # YouTube About-page descriptions are the canonical example.
        # Limit to the first 60 chars to avoid pulling in entire bios.
        bio = p.get("bio")
        if isinstance(bio, str) and bio.strip():
            first_line = bio.strip().splitlines()[0].strip()[:60]
            if first_line and first_line != (dn or "").strip():
                candidates.append(first_line)
        for s in candidates:
            cls = _classify_name(s, variant_set)
            if cls == "real_name":
                real_counts[s] = real_counts.get(s, 0) + 1
            elif cls == "nickname":
                nick_counts[s] = nick_counts.get(s, 0) + 1

    def pick(d: dict) -> str:
        if not d:
            return ""
        # Tie-break: count desc, length desc (more specific), alpha asc.
        return max(d.items(), key=lambda kv: (kv[1], len(kv[0]), -ord(kv[0][0])))[0]

    real = pick(real_counts)
    nick = pick(nick_counts)
    # Don't double-show — if real_name already starts with the nickname
    # (e.g. real "Hama Affs" and nick "Hama"), drop the nickname.
    if real and nick and (nick.lower() in real.lower() or real.lower().startswith(nick.lower())):
        nick = ""
    return real, nick


def _build_detail_rows(overall, found, all_variants: list[str]) -> str:
    """Render the dossier detail rows: real name + nickname (when
    detected), region, active since, footprint, aliases."""
    rows: list[tuple[str, str]] = []

    # Real-name / nickname detection runs first because the analyst
    # needs the human-readable identifier at a glance — that's what
    # they'll search for next.
    variant_set = {(v or "").lower().strip() for v in all_variants}
    real_name, nickname = _detect_subject_names(found, variant_set)
    if real_name:
        rows.append(("Real name", html.escape(real_name)))
    if nickname:
        rows.append(("Nickname", html.escape(nickname)))

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
def _html_photo_match_card(found, clusters, photo_bytes_map=None) -> str:
    """Render the editorial Photo Match card or empty string.

    Picks the highest-confidence photo-matched cluster (must have ≥ 2
    members) and surfaces two of its photos side by side with a ↔
    glyph between them, plus the cluster's hamming distance and
    confidence score below a dashed divider. When no cluster qualifies
    we return an empty string and CSS lets the right column span the
    full row.
    """
    multi = [c for c in (clusters or []) if len(c.member_indexes) > 1]
    if not multi:
        return ""
    best = max(multi, key=lambda c: getattr(c, "confidence", 0) or 0)

    # Walk the FOUND list once, keep the first 2 (site, photo) pairs
    # whose site is in this cluster's site set. Indexing the cluster
    # member dicts directly isn't possible from here — `member_indexes`
    # references the dedupped found_dicts list at correlation time.
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    cluster_sites = set(getattr(best, "sites", []) or [])
    for r in found:
        if r.site in cluster_sites and r.site not in seen:
            photo = (r.profile or {}).get("photo")
            if photo:
                pairs.append((r.site, photo))
                seen.add(r.site)
        if len(pairs) >= 2:
            break

    if len(pairs) < 2:
        return ""

    site1, photo1 = pairs[0]
    site2, photo2 = pairs[1]

    # Pull a hamming distance out of the cluster's rationale strings
    # (the phash matcher records "matching profile photo (hamming=N)").
    # If the merge came from DINO/Face++ instead, hamming is None and
    # we drop that part of the metadata line.
    hamming = None
    for note in (getattr(best, "rationale", None) or []):
        m = re.search(r"hamming=(\d+)", note)
        if m:
            hamming = int(m.group(1))
            break

    confidence = getattr(best, "confidence", 0) or 0
    meta_bits: list[str] = []
    if hamming is not None:
        meta_bits.append(f"Hamming distance: {hamming}")
    meta_bits.append(f"Confidence: {confidence:.2f}")
    meta_line = " · ".join(meta_bits)

    src1 = _photo_to_data_uri(photo1, photo_bytes_map) or html.escape(photo1, quote=True)
    src2 = _photo_to_data_uri(photo2, photo_bytes_map) or html.escape(photo2, quote=True)
    return (
        '<aside class="photo-match">'
        '<div class="kicker">Photo match</div>'
        '<div class="pm-photos">'
        f'<div class="pm-thumb" style="background-image:url(\'{src1}\')"></div>'
        '<div class="pm-arrow">↔</div>'
        f'<div class="pm-thumb" style="background-image:url(\'{src2}\')"></div>'
        '</div>'
        '<div class="pm-divider">'
        f'<div class="pm-meta">Same profile photo confirmed across '
        f'<span class="pm-site">{html.escape(site1)}</span> and '
        f'<span class="pm-site">{html.escape(site2)}</span></div>'
        f'<div class="pm-meta">{html.escape(meta_line)}</div>'
        '</div>'
        '</aside>'
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
_PLACEHOLDER_CACHE: dict[bytes, bool] = {}


def _photo_is_likely_default(
    url: str, photo_bytes_map: Optional[dict],
) -> bool:
    """True when a photo URL is almost certainly a platform "no PFP"
    placeholder rather than a real user photo.

    Two-tier detection:
      1. URL fragment match — catches platforms that name their defaults
         (Facebook /img/f-default, Twitter default_profile, Gravatar
         identicon, etc.) via confidence._DEFAULT_AVATAR_FRAGMENTS.
      2. Image variance — Instagram serves its default-PFP gradient via
         the same CDN URL shape as real photos. A blanket size threshold
         was wrong: Threads's actual user logos (e.g. small text logos)
         can be smaller than Instagram's gradient placeholder. Instead
         we resize the image to 16×16 grayscale and measure pixel
         variance. Placeholder gradients/silhouettes have variance well
         under 200; real photos and logos sit at 1000+. Cached by bytes
         so repeat checks are free.
    """
    if not url:
        return False
    from confidence import _is_default_avatar
    if _is_default_avatar(url):
        return True
    if not photo_bytes_map or url not in photo_bytes_map:
        return False
    data = photo_bytes_map.get(url) or b""
    if not data:
        return False
    # Absurdly small bytes are always placeholders (sub-1KB files are
    # almost certainly a transparent pixel or 1-color JPG).
    if len(data) < 800:
        return True
    cached = _PLACEHOLDER_CACHE.get(data)
    if cached is not None:
        return cached
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(data)).convert("L").resize((16, 16))
        px = list(img.getdata())
        mean = sum(px) / len(px)
        variance = sum((p - mean) ** 2 for p in px) / len(px)
        # 200 is a safe floor: Instagram's gradient sits around 30-80;
        # the dimmest real logos (faint outlines, monochrome wordmarks)
        # come in at 600+. A solid silhouette comes in around 100-150
        # near the edges. 200 cleanly separates real from placeholder
        # in every sample we've checked.
        is_placeholder = variance < 200
    except Exception:
        # If PIL can't decode (rare), don't suppress — keep the photo.
        is_placeholder = False
    _PLACEHOLDER_CACHE[data] = is_placeholder
    return is_placeholder


def _build_graph_data(
    found: list, dis_clusters: Optional[list], clusters: Optional[list],
    photo_bytes_map: Optional[dict] = None,
) -> Optional[dict]:
    """Build {nodes, edges} JSON for the embedded identity graph.

    Nodes: every FOUND result, with site, score, tier, cluster_id, card_id,
    photo URL (data-URI for CORP-restricted CDNs), bio snippet, and the
    full evidence trace so the side panel can render without re-querying.
    Edges: derived from three independent signals — disambiguation cluster
    membership, photo-hash cluster membership, and cross-link via bio/website
    domain mentions. Each edge carries a `kind` so the JS can colour-code
    by signal type. Cliques over 8 members collapse to a star pattern
    (every member connects to the highest-scoring node) to keep the graph
    readable.

    Returns None when there are fewer than 2 FOUND results — no graph
    to draw.
    """
    if len(found) < 2:
        return None

    nodes = []
    for i, r in enumerate(found):
        p = r.profile or {}
        photo_url = p.get("photo") or ""
        # Suppress platform-default placeholder avatars so the node falls
        # back to the letter glyph — consistent with how Facebook /
        # Twitter / GitHub defaults already render. Catches Instagram's
        # gradient placeholder (which has no telltale URL marker) via the
        # byte-size heuristic.
        if photo_url and _photo_is_likely_default(photo_url, photo_bytes_map):
            photo_url = ""
        # CORP-restricted CDNs (Instagram, Facebook) won't load cross-origin
        # in the file:// scheme — fall back to a base64 data URI built from
        # the bytes we already fetched for the dossier.
        embedded = _photo_to_data_uri(photo_url, photo_bytes_map) if photo_url else None
        if embedded:
            photo_url = embedded
        bio_snippet = (p.get("bio") or "").strip()
        if len(bio_snippet) > 160:
            bio_snippet = bio_snippet[:160].rstrip() + "…"
        # Bucket separates the canvas into "confirmed" (primary identity
        # AND not impostor-tier) vs "unrelated" (everything else —
        # secondary clusters, low-confidence clusters, AND any
        # possible_impostor node even if disambiguation lumped it into
        # the primary cluster). Tier wins over cluster membership: a
        # red-dashed impostor node always lives on the unrelated side,
        # never inside the confirmed web. This matches the user's
        # explicit ask: "the ones in red should be in another web alone
        # on the side."
        is_impostor = (r.tier == TIER_IMPOSTOR)
        bucket = (
            "confirmed"
            if (r.is_primary_identity and not is_impostor)
            else "unrelated"
        )
        nodes.append({
            "id": i,
            "site": r.site,
            "card_id": f"acct-{i}",
            "score": int(r.score or 0),
            "tier": r.tier or "",
            "cluster_id": r.identity_id if r.identity_id is not None else -1,
            "is_primary": bool(r.is_primary_identity),
            "verified": bool(p.get("verified")),
            "bucket": bucket,
            "display_name": (p.get("display_name") or r.variant or r.site)[:40],
            "handle": r.variant or "",
            "url": r.url or "",
            "photo": photo_url,
            "bio": bio_snippet,
            "followers": p.get("followers"),
            "following": p.get("following"),
            "posts": p.get("posts"),
            "location": p.get("location") or "",
            "signals": list(getattr(r, "signals", []) or []),
        })

    # Use a (lo, hi, kind) tuple → strongest-kind wins on duplicate pairs.
    EDGE_PRIORITY = {"cluster": 3, "photo": 2, "link": 1}
    edges_by_pair: dict[tuple[int, int], str] = {}

    def _add_edge(a: int, b: int, kind: str) -> None:
        if a == b:
            return
        lo, hi = (a, b) if a < b else (b, a)
        existing = edges_by_pair.get((lo, hi))
        if (existing is None
                or EDGE_PRIORITY.get(kind, 0)
                > EDGE_PRIORITY.get(existing, 0)):
            edges_by_pair[(lo, hi)] = kind

    def _connect_members(member_indices: list[int], kind: str) -> None:
        """Connect cluster members as a full clique — every node to every
        other. This is what makes the graph look like a real OSINT
        investigation web instead of a star with spokes. Previous version
        collapsed big clusters to a star pattern for readability, but
        density IS the readability cue here: a tight clique means "same
        identity, high confidence."
        """
        valid = [i for i in member_indices if 0 <= i < len(found)]
        if len(valid) < 2:
            return
        for i, a in enumerate(valid):
            for b in valid[i + 1:]:
                _add_edge(a, b, kind)

    # 1. Disambiguation clusters — strongest signal.
    if dis_clusters:
        for c in dis_clusters:
            if c.size >= 2:
                _connect_members(list(c.member_indices), "cluster")

    # 2. Photo-hash clusters from identity.py — these refer to member
    #    indexes into the dedupped found_dicts list, which is *not* the
    #    same as our `found` list. We approximate by matching site name
    #    + photo URL.
    if clusters:
        photo_url_to_node: dict[str, list[int]] = {}
        for i, r in enumerate(found):
            url = (r.profile or {}).get("photo")
            if url:
                photo_url_to_node.setdefault(url, []).append(i)
        for c in clusters or []:
            if len(c.member_indexes) < 2:
                continue
            matched: list[int] = []
            for url in (getattr(c, "photos", []) or []):
                if url in photo_url_to_node:
                    matched.extend(photo_url_to_node[url])
            _connect_members(list(set(matched)), "photo")

    # 3. Cross-link via bio/website domain mention.
    import re as _re
    host_by_node: dict[int, str] = {}
    for i, r in enumerate(found):
        m = _re.search(r"https?://([^/]+)", r.url or "")
        if m:
            host_by_node[i] = m.group(1).lower()
    for i, r in enumerate(found):
        p = r.profile or {}
        haystack = ((p.get("bio") or "") + " " + (p.get("website") or "")).lower()
        if not haystack.strip():
            continue
        for j, other_host in host_by_node.items():
            if i == j or not other_host:
                continue
            if other_host in haystack:
                _add_edge(i, j, "link")

    # Drop spurious cross-bucket cluster edges: when disambiguation
    # bundles an impostor (red dashed) node into the primary cluster, it
    # generates "same identity" edges from that node to every real
    # account. Those edges pull the impostor back into the main web no
    # matter how strongly the bucket gravity pushes it away. We treat
    # cross-bucket cluster edges as noise (the disambiguation algorithm
    # was over-eager) and drop them entirely. Photo-match and bio-link
    # edges across buckets are kept — those are genuine signals worth
    # surfacing ("impostor stole your photo" is meaningful).
    bucket_by_id = {n["id"]: n["bucket"] for n in nodes}
    edges = []
    for (lo, hi), kind in edges_by_pair.items():
        if (kind == "cluster"
                and bucket_by_id.get(lo) != bucket_by_id.get(hi)):
            continue
        edges.append({"source": lo, "target": hi, "kind": kind})
    return {"nodes": nodes, "edges": edges}


def _html_identity_graph(graph_data: Optional[dict]) -> str:
    """Render the identity-graph section. Vanilla-SVG, no external libs.

    Returns empty string when there isn't enough to draw. Otherwise
    produces:
      - kicker label
      - SVG container with embedded data + JS force-directed sim
      - legend
    """
    if not graph_data or len(graph_data["nodes"]) < 2:
        return ""
    # JSON-serialise the data into a script tag the page-level JS will read.
    # Using a typed `<script type=application/json>` block keeps it inert
    # for browsers' JS parsers, so no escaping of `</script>` is needed
    # beyond the standard fix.
    payload = json.dumps(graph_data, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")
    n_nodes = len(graph_data["nodes"])
    n_edges = len(graph_data["edges"])
    return (
        '<section class="identity-graph-section">'
        '<div class="kicker">'
        f'Identity graph &mdash; {n_nodes} accounts, {n_edges} connection'
        f'{"s" if n_edges != 1 else ""}'
        '</div>'
        '<div class="graph-panel">'
        '<svg class="identity-graph" '
        'preserveAspectRatio="xMidYMid meet">'
        '<defs>'
        '<filter id="phantom-glow" x="-50%" y="-50%" width="200%" height="200%">'
        '<feGaussianBlur stdDeviation="3" result="blur"/>'
        '<feMerge>'
        '<feMergeNode in="blur"/><feMergeNode in="blur"/>'
        '<feMergeNode in="SourceGraphic"/>'
        '</feMerge>'
        '</filter>'
        '</defs>'
        '<g class="viewport">'
        '<g class="edges"></g>'
        '<g class="nodes"></g>'
        '</g>'
        '</svg>'
        '<div class="graph-toolbar">'
        '<button data-graph-zoom="in" title="Zoom in">+</button>'
        '<button data-graph-zoom="out" title="Zoom out">&minus;</button>'
        '<button data-graph-zoom="reset" title="Reset view">&#9711;</button>'
        '</div>'
        '<div class="graph-hint">drag • scroll to zoom • click node for details</div>'
        '<div class="graph-side-panel" id="graph-side-panel" aria-hidden="true">'
        '<button class="gsp-close" data-gsp-close title="Close">&times;</button>'
        '<div class="gsp-body"></div>'
        '</div>'
        '</div>'
        '<div class="graph-legend">'
        '<span class="lg-row"><span class="lg-line lg-cluster"></span>same identity</span>'
        '<span class="lg-row"><span class="lg-line lg-photo"></span>same profile photo</span>'
        '<span class="lg-row"><span class="lg-line lg-link"></span>cross-linked in bio</span>'
        '<span class="lg-row"><span class="lg-dot lg-verified"></span>verified</span>'
        '<span class="lg-row"><span class="lg-dot lg-primary"></span>primary identity</span>'
        '</div>'
        f'<script type="application/json" id="identity-graph-data">{payload}</script>'
        '</section>'
    )


def _html_cluster_section(found: list, dis_clusters: list, photo_bytes_map: Optional[dict] = None) -> str:
    """Build the found_section HTML grouped by identity cluster."""
    parts: list[str] = []

    LABEL_MAP = {
        LABEL_PRIMARY:   ("cluster-label-primary",   "Primary Identity"),
        LABEL_SECONDARY: ("cluster-label-secondary", "Secondary Cluster"),
        LABEL_LOW:       ("cluster-label-low",        "Unrelated Matches"),
    }

    # Stable ID per result so the identity graph can scroll-to-card on click.
    card_id_by_index: dict[int, str] = {i: f"acct-{i}" for i in range(len(found))}

    primary_and_secondary = [c for c in dis_clusters
                             if c.label in (LABEL_PRIMARY, LABEL_SECONDARY)]
    low_clusters = [c for c in dis_clusters if c.label == LABEL_LOW]

    for c in primary_and_secondary:
        css_cls, label_text = LABEL_MAP.get(c.label, ("cluster-label-low", c.label))
        meta_parts = [f"{c.size} account{'s' if c.size != 1 else ''}"]
        if c.location:
            meta_parts.append(html.escape(c.location))
        name_html = (
            f'<span class="cluster-name">{html.escape(c.display_name)}</span>'
            if c.display_name else ""
        )
        header = (
            f'<div class="cluster-header">'
            f'<span class="cluster-label {css_cls}">{label_text}</span>'
            f'{name_html}'
            f'<span class="cluster-meta">{html.escape(" · ".join(meta_parts))}</span>'
            f'</div>'
        )
        member_pairs = [(i, found[i]) for i in c.member_indices if i < len(found)]
        member_pairs.sort(key=lambda ir: (
            0 if ir[1].tier == TIER_VERIFIED else 1,
            -(ir[1].score or 0),
        ))
        cards_html = "".join(
            _html_card(
                r,
                tier=TIER_VERIFIED if r.tier == TIER_VERIFIED else None,
                photo_bytes_map=photo_bytes_map,
                card_id=card_id_by_index.get(i),
            )
            for i, r in member_pairs
        )
        parts.append(
            f'<div class="cluster-group">'
            f'{header}'
            f'<div class="accounts-grid">{cards_html}</div>'
            f'</div>'
        )

    # Low-confidence clusters go in a collapsible
    if low_clusters:
        total_low = sum(c.size for c in low_clusters)
        inner_html = ""
        for c in low_clusters:
            member_pairs = [(i, found[i]) for i in c.member_indices if i < len(found)]
            inner_html += "".join(
                _html_card(
                    r,
                    photo_bytes_map=photo_bytes_map,
                    card_id=card_id_by_index.get(i),
                )
                for i, r in member_pairs
            )
        parts.append(
            f'<details class="unknown-fold" style="margin-top:18px">'
            f'<summary>Unrelated matches ({total_low})</summary>'
            f'<div class="aux-panel" style="margin-top:14px">'
            f'<div class="accounts-grid">{inner_html}</div>'
            f'</div></details>'
        )

    return "\n".join(parts) if parts else (
        '<div class="accounts-grid">'
        '<div class="acct" style="grid-column:1/-1;justify-content:center">'
        '<div class="body"><div class="bio">No confirmed accounts.</div></div>'
        '</div></div>'
    )
def export_html(grouped, raw, elapsed, path: Path, overall=None, clusters=None, emails=None, deep_evidence=None, face_map=None, dark=False, include_toggle=True, dis_clusters=None, photo_bytes_map=None) -> None:
    found, _, missing_count = _flatten(grouped)
    clusters = clusters or []
    multi = [c for c in clusters if len(c.member_indexes) > 1]

    # When disambiguation has run, use primary-cluster members for the subject
    # overview so stats reflect the real person, not all matched accounts.
    primary_cluster = None
    if dis_clusters:
        primary_cluster = next(
            (c for c in dis_clusters if c.label == LABEL_PRIMARY), None
        )
    if primary_cluster:
        primary_found = [found[i] for i in primary_cluster.member_indices if i < len(found)]
    else:
        primary_found = found

    # --- Subject hero ---
    subject_handle = _subject_handle(raw, primary_found)
    portrait_url = _pick_subject_photo(overall, clusters, primary_found, face_map)
    subject_portrait_html = _html_subject_portrait(portrait_url, subject_handle, photo_bytes_map)

    if primary_cluster and primary_cluster.display_name:
        subject_name_region = html.escape(primary_cluster.display_name)
    elif overall and getattr(overall, "display_name", None):
        subject_name_region = html.escape(overall.display_name)
    else:
        subject_name_region = "&nbsp;"

    # --- Stats counts ---
    n_variants = len(grouped)
    n_sites = len(grouped[0][1]) if grouped and grouped[0][1] else 0

    # --- Combo section: photo-match card + subject details (primary only) ---
    photo_match_block = _html_photo_match_card(primary_found, clusters, photo_bytes_map)
    detail_rows_html = _build_detail_rows(
        overall, primary_found, [v for v, _ in grouped]
    )

    # --- Account cards: cluster-grouped or tier-grouped ---
    # Build a stable index→card_id map so the identity graph can target cards
    # whichever rendering path we take.
    card_id_by_index: dict[int, str] = {i: f"acct-{i}" for i in range(len(found))}
    index_by_result: dict[int, int] = {id(r): i for i, r in enumerate(found)}

    def _card(r, **kw):
        cid = card_id_by_index.get(index_by_result.get(id(r), -1))
        return _html_card(r, photo_bytes_map=photo_bytes_map, card_id=cid, **kw)

    if found:
        if dis_clusters:
            found_section_html = _html_cluster_section(found, dis_clusters, photo_bytes_map)
        else:
            scored_html = any(r.tier is not None for r in found)
            if scored_html:
                v_cards = [r for r in found if r.tier == TIER_VERIFIED]
                l_cards = [r for r in found if r.tier == TIER_LIKELY]
                i_cards = [r for r in found if r.tier == TIER_IMPOSTOR]
                l_cards += [r for r in found if r.tier is None]
            else:
                v_cards, l_cards, i_cards = [], found, []
            primary_html = (
                "".join(_card(r, tier=TIER_VERIFIED) for r in v_cards)
                + "".join(_card(r) for r in l_cards)
            )
            found_section_html = f'<div class="accounts-grid">{primary_html}</div>'
            if i_cards:
                impostor_inner = "".join(_card(r) for r in i_cards)
                found_section_html += (
                    '<details class="unknown-fold" style="margin-top:18px">'
                    f'<summary>Possible impostors ({len(i_cards)})</summary>'
                    '<div class="aux-panel" style="margin-top:14px">'
                    f'<div class="accounts-grid">{impostor_inner}</div>'
                    '</div></details>'
                )
    else:
        found_section_html = (
            '<div class="accounts-grid">'
            '<div class="acct" style="grid-column:1/-1;justify-content:center">'
            '<div class="body"><div class="bio">No confirmed accounts.</div></div>'
            '</div></div>'
        )

    # --- Identity graph -----------------------------------------------
    # Nodes = found accounts, edges = signal links (cluster / photo /
    # cross-link). Click a node to scroll to its card. Section is empty
    # when there are <2 accounts (nothing meaningful to draw).
    graph_data = _build_graph_data(found, dis_clusters, clusters, photo_bytes_map)
    graph_section = _html_identity_graph(graph_data)

    # --- Confirmed-missing section ---
    # Collect every MISSING result whose tier is `confirmed_missing`
    # (set by confidence.annotate_missing) and surface them as a chip
    # list. The OSINT signal is "we're SURE this handle is not on
    # these platforms" — a finding on its own, distinct from "we
    # don't know."
    confirmed_missing_sites = sorted({
        r.site
        for _, rs in grouped
        for r in rs
        if r.exists is False and r.tier == "confirmed_missing"
    })
    if confirmed_missing_sites:
        chips = "".join(
            f'<span class="mtag">{html.escape(s)}</span>'
            for s in confirmed_missing_sites
        )
        confirmed_missing_section = (
            f'<section class="confirmed-missing-section">'
            f'<div class="kicker">'
            f'Confirmed missing &mdash; {len(confirmed_missing_sites)} platform'
            f'{"s" if len(confirmed_missing_sites) != 1 else ""}'
            f'</div>'
            f'<div class="confirmed-missing-list">{chips}</div>'
            f'</section>'
        )
    else:
        confirmed_missing_section = ""

    # --- Auxiliary panels ---
    emails_section = _html_emails_section(found, emails) if emails else ""

    # --- Theme ---
    theme = "dark" if dark else "light"
    toggle_button = (
        '<button id="theme-toggle" class="theme-toggle" title="Toggle theme">☀</button>'
        if include_toggle else ""
    )

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
        photo_match_block=photo_match_block,
        detail_rows=detail_rows_html,
        graph_section=graph_section,
        found_section=found_section_html,
        confirmed_missing_section=confirmed_missing_section,
        emails_section=emails_section,
        theme=theme,
        toggle_button=toggle_button,
    )
    path.write_text(page, encoding="utf-8")
