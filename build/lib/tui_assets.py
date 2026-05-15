"""ASCII art assets + Rich-based renderers for Phantom's TUI surfaces.

This module is shared between:
  - the dependency-light launcher (Option A) — uses raw ANSI when
    `rich` is unavailable, prettier output when it is
  - the full Textual app (Option B) — uses Rich for the inline banner
    inside the Header widget

Rich is part of Textual's dependency graph, so when Textual is
installed Rich is too. We import it lazily so the launcher can still
fall back to plain ANSI on a minimal install.
"""
from __future__ import annotations

import shutil
from typing import Optional


# ---------------------------------------------------------------------------
# ANSI helpers (kept as a fallback when Rich isn't installed)
# ---------------------------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
INK = "\033[97m"
# Monochrome white→grey palette — Phantom's brand is bone / paper, not
# warm colour. The gradient runs from bright white at the wordmark's
# top to dim grey at the bottom; the frame uses a mid-grey so it
# recedes behind the content.
ACCENT = "\033[38;5;255m"     # bright white
ACCENT2 = "\033[38;5;242m"    # mid grey (frame)
MUTED = "\033[38;5;245m"
ERR = "\033[38;5;167m"
OK = "\033[38;5;108m"


def c(text: str, *codes: str) -> str:
    """Wrap text in ANSI codes, ensuring a reset at the end."""
    return "".join(codes) + text + RESET


def term_width() -> int:
    try:
        return max(40, shutil.get_terminal_size((80, 24)).columns)
    except Exception:
        return 80


# ---------------------------------------------------------------------------
# Banner art
# ---------------------------------------------------------------------------
#
# 7-row ghost paired with a 7-row PHANTOM wordmark in "ANSI Shadow"
# style. Each was hand-trimmed so the baselines align vertically when
# pasted next to each other with a single space separator.

_GHOST_RAW = [
    "   ▄▄▄▄▄▄▄",
    " ▄█████████▄",
    "███ ●   ● ███",
    "█████████████",
    "█████████████",
    "▀█▀▄▀█▀▄▀█▀▄▀",
]

_WORDMARK_RAW = [
    "██████╗ ██╗  ██╗ █████╗ ███╗   ██╗████████╗ ██████╗ ███╗   ███╗",
    "██╔══██╗██║  ██║██╔══██╗████╗  ██║╚══██╔══╝██╔═══██╗████╗ ████║",
    "██████╔╝███████║███████║██╔██╗ ██║   ██║   ██║   ██║██╔████╔██║",
    "██╔═══╝ ██╔══██║██╔══██║██║╚██╗██║   ██║   ██║   ██║██║╚██╔╝██║",
    "██║     ██║  ██║██║  ██║██║ ╚████║   ██║   ╚██████╔╝██║ ╚═╝ ██║",
    "╚═╝     ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝   ╚═╝    ╚═════╝ ╚═╝     ╚═╝",
]

# Normalise every row to the longest row's width, then append a single
# blank padding row of the same width. Computed at import time so the
# banner is structurally incapable of having mismatched row widths.
GHOST_WIDTH = max(len(line) for line in _GHOST_RAW)
WORDMARK_WIDTH = max(len(line) for line in _WORDMARK_RAW)
GHOST_LINES = [line.ljust(GHOST_WIDTH) for line in _GHOST_RAW] + [" " * GHOST_WIDTH]
WORDMARK_LINES = [line.ljust(WORDMARK_WIDTH) for line in _WORDMARK_RAW] + [" " * WORDMARK_WIDTH]


# Compact 3-row variant for the live-app header — same ghost shape but
# squashed so it fits in a Textual `Header`.
GHOST_COMPACT = [
    " ▄▄▄▄▄ ",
    "█ ●  ● █",
    " ▀▀▀▀▀ ",
]


VERSION = "v1.0"

# Author + repo links surfaced in the banner tagline. The two URLs
# become OSC 8 terminal hyperlinks — clickable in Konsole, iTerm2,
# Alacritty, kitty, WezTerm, Foot, and most other modern terminals.
GITHUB_URL = "https://github.com/hamaffs"
INSTAGRAM_URL = "https://www.instagram.com/hama_ffs/"


def _osc_link(url: str, text: str) -> str:
    """OSC 8 terminal hyperlink.

    Uses BEL (`\\x07`) as the string terminator rather than ST
    (`\\033\\\\`). Both are valid per the OSC 8 spec, but BEL is the
    older form and has wider terminal-emulator support — notably,
    Konsole's hyperlink parser sometimes refuses ST-terminated OSC 8
    sequences while still accepting BEL-terminated ones. We default
    to BEL so links are clickable on the broadest set of terminals
    (Konsole, kitty, iTerm2, Alacritty, WezTerm, Foot, …).
    """
    return f"\033]8;;{url}\x07{text}\033]8;;\x07"


def render_tagline_ansi() -> str:
    """Plain-ANSI tagline: `by @hamaffs` in soft green, then clickable
    `github` and `instagram` links underlined in white."""
    underline_on, underline_off = "\033[4m", "\033[24m"
    by = "\033[38;5;108mby @hamaffs\033[0m"
    github = f"{underline_on}{_osc_link(GITHUB_URL, 'github')}{underline_off}"
    insta = f"{underline_on}{_osc_link(INSTAGRAM_URL, 'instagram')}{underline_off}"
    sep = "\033[38;5;240m·\033[0m"
    return f"{by}  {sep}  {github}  {sep}  {insta}"


def render_tagline_rich_markup() -> str:
    """Same tagline for the Textual app, in Rich markup form."""
    return (
        f"[color(108)]by @hamaffs[/]  "
        f"[color(240)]·[/]  "
        f"[link={GITHUB_URL}]github[/link]  "
        f"[color(240)]·[/]  "
        f"[link={INSTAGRAM_URL}]instagram[/link]"
    )


# Visible (non-ANSI) length of the tagline, used for centring.
TAGLINE_VISIBLE = "by @hamaffs  ·  github  ·  instagram"


# ---------------------------------------------------------------------------
# Rich-based premium banner (used when Rich is available — i.e. when
# Textual is installed, which is true for almost everyone)
# ---------------------------------------------------------------------------

def _try_rich():
    """Return Rich's classes or None if Rich isn't installed."""
    try:
        from rich.align import Align
        from rich.console import Console, Group
        from rich.panel import Panel
        from rich.text import Text
        from rich import box
        return {
            "Align": Align, "Console": Console, "Group": Group,
            "Panel": Panel, "Text": Text, "box": box,
        }
    except ImportError:
        return None


def _gradient_text(lines: list[str], rich_mod: dict, colors: list[str]):
    """Apply a vertical gradient to a multi-line ASCII block via Rich
    Text. Each line gets the next colour in `colors`, cycling if there
    are more lines than colours."""
    Text = rich_mod["Text"]
    out = Text()
    for i, line in enumerate(lines):
        color = colors[i % len(colors)]
        out.append(line + "\n", style=f"bold {color}")
    return out


def render_banner_rich(version: str = VERSION) -> Optional[str]:
    """Render the framed banner manually with ANSI + box-drawing chars.

    We deliberately don't use Rich's Panel for the banner because Rich
    will wrap the wordmark at line breaks when the terminal isn't wide
    enough — which mangles the ASCII art. By drawing the frame
    ourselves we control exactly how much horizontal space is used and
    can crop / shrink gracefully when the terminal is narrow.

    Returns the rendered string. Name retains "_rich" suffix for the
    public surface — `Rich` was the original plan; the rename to a
    manual frame is implementation detail."""
    # Vertical white→grey gradient, brightest at the top.
    GRAD = [255, 255, 252, 250, 248, 245, 243]

    # Compute the natural inner width — ghost + 2 spaces + wordmark.
    inner_width = len(GHOST_LINES[0]) + 2 + len(WORDMARK_LINES[0])
    available = max(60, term_width() - 4)
    # If the terminal is wider than we need, frame at the natural width;
    # if narrower, fall back to plain unframed banner so wrapping can't
    # break the art.
    if available < inner_width + 4:
        return render_banner_ansi()

    pad_left = 2
    pad_right = 2
    box_inner = inner_width + pad_left + pad_right
    # Solid bg on the borders + every interior cell. Without explicit
    # bg, terminals with window opacity < 100% (Konsole's default in
    # many themes) show whatever's behind through the "blank" cells of
    # the alt screen. The bg here matches BG_DARK in tui_launcher.
    BG = "\033[48;5;232m"
    box_top = f"{BG}\033[38;5;242m╭{'─' * box_inner}╮\033[0m"
    box_bot = f"{BG}\033[38;5;242m╰{'─' * box_inner}╯\033[0m"
    box_side = f"{BG}\033[38;5;242m│\033[0m"
    blank_line = f"{box_side}{BG}{' ' * box_inner}{box_side}"

    def amber(n: int) -> str:
        # Foreground + bg in one escape so each cell is opaque.
        return f"\033[1;38;5;{n};48;5;232m"

    def colored_row(i: int) -> str:
        color = amber(GRAD[i])
        g = f"{color}{GHOST_LINES[i]}{RESET}"
        w = f"{color}{WORDMARK_LINES[i]}{RESET}"
        return (
            f"{box_side}"
            f"{' ' * pad_left}"
            f"{g}  {w}"
            f"{' ' * pad_right}"
            f"{box_side}"
        )

    def center_line(text: str, visible: Optional[str] = None,
                    style: str = "") -> str:
        """Center a line by its visible width. `text` may contain ANSI
        / OSC sequences that aren't visible; pass `visible` separately
        to compute the correct padding."""
        bare_len = len(visible if visible is not None else text)
        total_pad = inner_width - bare_len
        left = max(0, total_pad // 2)
        right = max(0, total_pad - left)
        colored = f"{style}{text}{RESET}" if style else text
        return (
            f"{box_side}"
            f"{' ' * (pad_left + left)}"
            f"{colored}"
            f"{' ' * (pad_right + right)}"
            f"{box_side}"
        )

    out = [box_top, blank_line]
    for i in range(7):
        out.append(colored_row(i))
    out.append(blank_line)
    out.append(center_line(render_tagline_ansi(), visible=TAGLINE_VISIBLE))
    out.append(center_line(version, style=DIM))
    out.append(blank_line)
    out.append(box_bot)
    return "\n".join(out)


def render_section_rich(title: str, lines: list[str], width: Optional[int] = None) -> Optional[str]:
    """Render a titled section panel for the launcher's home screen
    (Target / Flags / Export). Falls back to None when Rich isn't
    available."""
    rich_mod = _try_rich()
    if rich_mod is None:
        return None
    from io import StringIO
    Console = rich_mod["Console"]
    Panel = rich_mod["Panel"]
    Text = rich_mod["Text"]
    box = rich_mod["box"]

    # Parse Rich markup so width calculation matches what's drawn.
    # Passing raw ANSI here would confuse Rich's width measurer and
    # leave the right border floating at a different column on each
    # row (the bug that produced the misaligned Target panel).
    body = Text("\n").join(Text.from_markup(line) for line in lines)
    panel = Panel(
        body,
        title=f"[bold white]{title}[/]",
        title_align="left",
        box=box.ROUNDED,
        border_style="color(240)",
        padding=(0, 2),
        width=width or min(100, term_width()),
        # Solid bg on the panel itself — without this, Rich emits a
        # reset that lets terminal transparency bleed through the
        # panel's interior cells. color(232) matches the launcher's
        # BG_DARK so the banner + section share one continuous bg.
        style="on color(232)",
    )
    buf = StringIO()
    Console(file=buf, force_terminal=True,
            color_system="truecolor",
            width=min(100, term_width())).print(panel)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Plain-ANSI fallback
# ---------------------------------------------------------------------------

def render_banner_ansi() -> str:
    """Pure-ANSI fallback banner — no frame (so it fits any width), but
    the same per-row white→grey gradient as the framed version. Every
    coloured cell sets bg=232 so terminal transparency can't punch
    holes through the art."""
    GRAD = [255, 255, 252, 250, 248, 245, 243]
    lines: list[str] = []
    for i in range(7):
        color = f"\033[1;38;5;{GRAD[i]};48;5;232m"
        g = f"{color}{GHOST_LINES[i]}{RESET}"
        w = f"{color}{WORDMARK_LINES[i]}{RESET}"
        lines.append(g + "\033[48;5;232m  \033[0m" + w)
    lines.append("")
    lines.append("\033[48;5;232m  " + render_tagline_ansi() + "\033[0m")
    lines.append("\033[48;5;232m  " + c(VERSION, DIM) + "\033[0m")
    return "\n".join(lines)


def render_banner() -> str:
    """Top-level: prefer Rich, fall back to ANSI."""
    out = render_banner_rich()
    if out is not None:
        return out.rstrip("\n")
    return render_banner_ansi()


# ---------------------------------------------------------------------------
# Box-drawing helpers shared with the launcher
# ---------------------------------------------------------------------------

def key_hint(key: str, label: str, color: bool = True) -> str:
    """`[X] label` keybinding chip."""
    if color:
        return f"{c('[' + key + ']', BOLD, ACCENT)} {c(label, MUTED)}"
    return f"[{key}] {label}"


def hr(width: int = 78, color: bool = True) -> str:
    line = "─" * width
    return c(line, DIM) if color else line


def status_line(parts: list[str], color: bool = True) -> str:
    sep = c("·", DIM) if color else "·"
    return f" {sep} ".join(parts)
