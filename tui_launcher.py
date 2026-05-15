"""Phantom interactive launcher (Option A).

When the user runs `phantom` with no arguments and stdout is a TTY, we
open this. It's a tiny REPL — banner on top, single-line input below,
keybinding hints at the bottom — that lets you queue scans, jump into
the full Textual TUI, run --self-check, and so on without retyping the
command each time.

Design:
- Zero external dependencies. Pure stdlib, works in any reasonable
  terminal (Python 3.11+ stdlib supports the ANSI codes we use).
- The "scan" action just rebuilds an argv list and calls `cli.main`
  in-process. That keeps a single source of truth for what each flag
  does, and any improvements to the scan engine show up here for free.
- Single-key shortcuts work without Enter where reasonable (E, F, S,
  T, Q, H) via raw-mode tty.

Keybindings (visible at the bottom of the screen):
    Enter   scan the current input
    [E]     toggle --export html (with file pick)
    [F]     open the flag menu
    [S]     --self-check
    [T]     launch the Textual full TUI
    [H]     help
    [Q]     quit
"""
from __future__ import annotations

import os
import re
import shutil
import sys

import termios
import tty
from pathlib import Path
from typing import Optional

from tui_assets import (
    ACCENT, BOLD, DIM, ERR, INK, MUTED, RESET,
    c, hr, key_hint, render_banner, render_section_rich, status_line,
)


def banner(color: bool = True) -> str:
    """Backwards-compat: existing code calls banner(); delegate to the
    new gradient-coloured renderer."""
    return render_banner()


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------

CLEAR = "\033[2J\033[H"           # clear screen + home cursor
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
CURSOR_UP = lambda n: f"\033[{n}A"  # noqa: E731
ERASE_LINE = "\033[2K\r"
# Bracketed / synchronized output (DECSCUSR mode 2026) — the terminal
# buffers everything between BEGIN and END and applies it in one
# repaint. Eliminates the I-beam cursor flicker that the user saw as
# stray `|` characters between sections during multi-step redraws.
SYNC_BEGIN = "\033[?2026h"
SYNC_END = "\033[?2026l"
# Alternate screen buffer (xterm extension, supported by every modern
# terminal: Konsole, kitty, iTerm2, Alacritty, WezTerm, Foot, GNOME
# Terminal, etc). Opens a fresh blank screen on enter, restores the
# original terminal contents on exit. Critically: keeps redraws OUT
# of the scrollback, so previous frames don't leak above the current
# one when the user scrolls up. Same mechanism used by vim / less /
# htop / man.
ALT_SCREEN_ON = "\033[?1049h"
ALT_SCREEN_OFF = "\033[?1049l"
# Solid background fill. When the user has Konsole / Alacritty / iTerm2
# with window opacity < 100%, the "empty" cells of the alt screen show
# whatever's behind the terminal (other windows, the desktop). Filling
# every visible row with a bg-coloured space row makes the alt screen
# fully opaque — same trick vim, less, and htop use. Colour 232 is the
# darkest non-black in the 256-colour palette; visually identical to
# black on any sane theme but explicitly OPAQUE.
BG_DARK = "\033[48;5;232m"
BG_RESET = "\033[49m"


def _solid_screen_fill() -> str:
    """Build a screen-spanning solid-bg fill. Goes BEFORE the actual
    content in each repaint so any cells the content doesn't write to
    still have an opaque background."""
    try:
        size = shutil.get_terminal_size((80, 24))
        cols, rows = size.columns, size.lines
    except Exception:
        cols, rows = 80, 24
    blank = " " * cols
    return "\033[H" + BG_DARK + ("\n".join([blank] * rows)) + "\033[H"


def term_width() -> int:
    try:
        return max(40, shutil.get_terminal_size((80, 24)).columns)
    except Exception:
        return 80


def _read_key() -> str:
    """Read a single keypress in raw mode. Returns the character or one
    of the synthetic names: ENTER, ESC, BACKSPACE, ARROW_*, EOF.

    Falls back to a blocking input() when stdin isn't a tty (unusual
    for the launcher but defensive)."""
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        return "EOF" if not line else line.rstrip("\n")
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    if ch == "\x03":
        raise KeyboardInterrupt
    if ch == "\r" or ch == "\n":
        return "ENTER"
    if ch == "\x7f" or ch == "\x08":
        return "BACKSPACE"
    if ch == "\x1b":
        # Possible escape sequence — read up to 2 more chars
        nxt = sys.stdin.read(1) if sys.stdin.readable() else ""
        if not nxt:
            return "ESC"
        if nxt == "[":
            arrow = sys.stdin.read(1) if sys.stdin.readable() else ""
            return {"A": "UP", "B": "DOWN", "C": "RIGHT", "D": "LEFT"}.get(
                arrow, "ESC"
            )
        return "ESC"
    return ch


def _line_edit(prompt: str, initial: str = "") -> Optional[str]:
    """Read a line with backspace + cursor, render the prompt, return
    the entered text. Returns None when Ctrl-C / ESC pressed."""
    buf = list(initial)
    sys.stdout.write(prompt + initial + SHOW_CURSOR)
    sys.stdout.flush()
    while True:
        try:
            k = _read_key()
        except KeyboardInterrupt:
            return None
        if k == "ENTER":
            sys.stdout.write("\n")
            return "".join(buf).strip()
        if k == "ESC":
            return None
        if k == "BACKSPACE":
            if buf:
                buf.pop()
                # Move cursor back, erase one char, move back again.
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            continue
        if k == "EOF":
            return None
        if k in ("UP", "DOWN", "LEFT", "RIGHT"):
            continue
        if len(k) == 1 and k.isprintable():
            buf.append(k)
            sys.stdout.write(k)
            sys.stdout.flush()


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class LauncherState:
    """All the toggleable flags the launcher exposes via [F].

    Each attr maps to an argparse flag. The launcher composes argv from
    these when the user presses Enter."""

    def __init__(self) -> None:
        self.handle: str = ""
        self.export_path: Optional[str] = None  # set via [E]
        self.exact: bool = False
        self.expand: bool = False
        self.wayback: bool = False
        self.github_deep: bool = False
        self.photo_ocr: bool = False
        self.no_cache: bool = False
        self.no_identity: bool = False
        self.found_only: bool = True

    def argv(self) -> list[str]:
        """Build the argv list for cli.main(). Mirrors the actual CLI."""
        out: list[str] = []
        if self.handle:
            # Allow URLs via --parse; otherwise positional.
            if "://" in self.handle:
                out += ["--parse", self.handle]
            else:
                out += [self.handle]
        if self.exact:
            out.append("--exact")
        if self.found_only:
            out.append("--found-only")
        if self.expand:
            out.append("--expand")
        if self.wayback:
            out.append("--wayback")
        if self.github_deep:
            out.append("--github-deep")
        if self.photo_ocr:
            out.append("--photo-ocr")
        if self.no_cache:
            out.append("--no-cache")
        if self.no_identity:
            out.append("--no-identity")
        if self.export_path:
            out += ["--export", self.export_path]
        return out


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

def _flag_status(state: LauncherState) -> str:
    """Compact summary of what's currently on."""
    on: list[str] = []
    if state.exact: on.append("exact")
    if state.expand: on.append("expand")
    if state.wayback: on.append("wayback")
    if state.github_deep: on.append("gh-deep")
    if state.photo_ocr: on.append("ocr")
    if state.no_cache: on.append("no-cache")
    if state.no_identity: on.append("no-identity")
    if state.found_only: on.append("found-only")
    if state.export_path:
        on.append(f"→ {state.export_path}")
    return ", ".join(on) if on else c("(defaults)", DIM)


def _flag_status_markup(state: LauncherState) -> str:
    """Same as _flag_status but uses Rich markup so it can sit inside a
    Rich Panel without throwing the panel's width calculation off."""
    on: list[str] = []
    if state.exact: on.append("exact")
    if state.expand: on.append("expand")
    if state.wayback: on.append("wayback")
    if state.github_deep: on.append("gh-deep")
    if state.photo_ocr: on.append("ocr")
    if state.no_cache: on.append("no-cache")
    if state.no_identity: on.append("no-identity")
    if state.found_only: on.append("found-only")
    if state.export_path:
        on.append(f"→ {state.export_path}")
    return ", ".join(on) if on else "[dim](defaults)[/dim]"


def draw_home(state: LauncherState) -> None:
    """Render the main launcher screen as one atomic write so the
    terminal can't show intermediate cursor positions mid-repaint.

    The bracketed-sync escapes around the write tell modern terminals
    (Konsole, kitty, iTerm2, Alacritty, Foot, WezTerm) to buffer the
    entire frame and apply it in a single flip — fixing the stray
    I-beam-cursor `|` artifacts visible in earlier builds.
    """
    # Rich markup, not raw ANSI — Rich's Panel measures markup widths
    # correctly but mis-measures embedded escape codes, which used to
    # make the right border float at a different column on each row.
    handle_cell = state.handle or "[dim](type below)[/dim]"
    export_cell = state.export_path or "[dim](none)[/dim]"
    status_lines = [
        f"  [bold]Handle[/bold]   {handle_cell}",
        f"  [bold]Flags [/bold]   {_flag_status_markup(state)}",
        f"  [bold]Export[/bold]   {export_cell}",
    ]
    section = render_section_rich("Target", status_lines)
    if not section:
        section = (
            hr(min(78, term_width())) + "\n"
            + "\n".join(status_lines) + "\n"
            + hr(min(78, term_width())) + "\n"
        )

    keys = "   ".join([
        key_hint("Enter", "scan"),
        key_hint("E", "export"),
        key_hint("F", "flags"),
        key_hint("S", "self-check"),
        key_hint("T", "TUI"),
        key_hint("H", "help"),
        key_hint("Q", "quit"),
    ])

    # Build the frame so that:
    #   1. The entire visible screen is first painted with a solid
    #      dark bg (so terminal transparency can't show through).
    #   2. The content is then written on top — but with `BG_DARK`
    #      re-set before every section so Rich Panel's internal
    #      `\033[0m` resets don't punch transparent holes between
    #      the panel and the surrounding text.
    frame = (
        SYNC_BEGIN
        + CLEAR
        + HIDE_CURSOR
        + _solid_screen_fill()
        + BG_DARK + render_banner() + BG_DARK + "\n\n"
        + BG_DARK + section + BG_DARK
        + "\n"
        + BG_DARK + "  " + keys + BG_DARK + "\n\n"
        + SYNC_END
    )
    sys.stdout.write(frame)
    sys.stdout.flush()


def screen_help() -> None:
    sys.stdout.write(CLEAR)
    sys.stdout.write(banner(color=True))
    sys.stdout.write("\n\n  " + c("Help", BOLD) + "\n")
    sys.stdout.write(hr(min(78, term_width())) + "\n\n")
    lines = [
        ("Target",       "Type a username (`hamaffs`) or paste a profile URL"),
        ("",             "(`https://github.com/user`). Then press Enter."),
        ("",             ""),
        ("[Enter]",      "Run the scan with the current flags."),
        ("[E]",          "Set an export path (`~/report.html`, `~/r.json`, etc.)."),
        ("[F]",          "Toggle scan flags (expand, wayback, github-deep, …)."),
        ("[S]",          "Run --self-check: probe canary handles, report drift."),
        ("[T]",          "Launch the full Textual TUI."),
        ("[H]",          "This help."),
        ("[Q]",          "Quit."),
        ("",             ""),
        ("Notes",        "All single-letter keys work without pressing Enter."),
        ("",             "ESC anywhere closes the current sub-screen."),
        ("",             "Ctrl-C exits."),
    ]
    for k, v in lines:
        sys.stdout.write(f"  {c(k, ACCENT, BOLD):<14}  {v}\n")
    sys.stdout.write("\n  " + key_hint("any key", "back") + "\n")
    sys.stdout.flush()
    _read_key()


def screen_flags(state: LauncherState) -> None:
    """Cycle a list of toggles via single-digit shortcuts."""
    toggles = [
        ("1", "exact",        "--exact: skip variant engine"),
        ("2", "expand",       "--expand: recursive cross-link discovery"),
        ("3", "wayback",      "--wayback: Wayback Machine lookup"),
        ("4", "github_deep",  "--github-deep: orgs + starred + commit email"),
        ("5", "photo_ocr",    "--photo-ocr: Tesseract on avatars"),
        ("6", "no_cache",     "--no-cache: skip the 1h response cache"),
        ("7", "no_identity",  "--no-identity: skip photo / cluster correlation"),
        ("8", "found_only",   "--found-only: hide [?] / [MISSING] counts"),
    ]
    while True:
        sys.stdout.write(CLEAR)
        sys.stdout.write(banner(color=True))
        sys.stdout.write("\n\n  " + c("Scan flags", BOLD) + "\n")
        sys.stdout.write(hr(min(78, term_width())) + "\n\n")
        for k, attr, label in toggles:
            on = getattr(state, attr)
            mark = c("●", ACCENT) if on else c("○", MUTED)
            sys.stdout.write(f"  [{c(k, BOLD)}]  {mark}  {label}\n")
        sys.stdout.write("\n  " + key_hint("digit", "toggle") + "   " +
                         key_hint("ESC", "back") + "\n")
        sys.stdout.flush()
        k = _read_key()
        if k in ("ESC", "ENTER", "q", "Q"):
            return
        for digit, attr, _ in toggles:
            if k == digit:
                setattr(state, attr, not getattr(state, attr))
                break


def screen_export(state: LauncherState) -> None:
    """Prompt for an export path. ESC keeps the current value."""
    sys.stdout.write(CLEAR)
    sys.stdout.write(banner(color=True))
    sys.stdout.write("\n\n  " + c("Export path", BOLD) + "\n")
    sys.stdout.write(hr(min(78, term_width())) + "\n\n")
    sys.stdout.write(
        "  Examples:  "
        + c("~/report.html", ACCENT) + ", "
        + c("~/report.pdf", ACCENT) + ", "
        + c("/tmp/r.csv", ACCENT) + ", "
        + c("(empty)", DIM) + " to clear\n\n"
    )
    current = state.export_path or ""
    prompt = "  " + c("path>", BOLD) + " "
    result = _line_edit(prompt, initial=current)
    if result is None:
        return
    state.export_path = os.path.expanduser(result) if result else None


# ---------------------------------------------------------------------------
# Action runners
# ---------------------------------------------------------------------------

def _run_scan(state: LauncherState) -> None:
    """Invoke cli.main with the launcher's composed argv. The scan
    streams output to stdout exactly as if the user had run it
    directly, so the existing terminal renderer just works."""
    if not state.handle:
        sys.stdout.write("\n  " + c("Enter a handle first.", ERR) + "\n\n")
        sys.stdout.write("  " + key_hint("any key", "back") + "\n")
        sys.stdout.flush()
        _read_key()
        return
    sys.stdout.write(SHOW_CURSOR)
    sys.stdout.write("\n")
    sys.stdout.flush()
    from cli import main as cli_main
    try:
        cli_main(state.argv())
    except SystemExit:
        pass
    except KeyboardInterrupt:
        sys.stdout.write("\n  " + c("Scan interrupted.", ERR) + "\n")
    sys.stdout.write("\n  " + key_hint("any key", "back") + "\n")
    sys.stdout.flush()
    _read_key()


def _run_self_check() -> None:
    sys.stdout.write(CLEAR)
    sys.stdout.write(banner(color=True))
    sys.stdout.write("\n\n  " + c("Self-check", BOLD) + " — probing canaries...\n\n")
    sys.stdout.flush()
    from cli import main as cli_main
    try:
        cli_main(["--self-check"])
    except SystemExit:
        pass
    except KeyboardInterrupt:
        sys.stdout.write("\n  " + c("Self-check interrupted.", ERR) + "\n")
    sys.stdout.write("\n  " + key_hint("any key", "back") + "\n")
    sys.stdout.flush()
    _read_key()


def _launch_textual(state: LauncherState) -> None:
    """Open the Textual full TUI (Option B). Falls back to a clean
    error message when Textual isn't installed."""
    try:
        from tui_app import run_tui
    except ImportError as e:
        sys.stdout.write(
            "\n  " + c("Textual TUI is not installed.", ERR) + "\n"
            "  Install with: " + c("pip install textual", ACCENT) + "\n"
            "  (" + str(e) + ")\n\n  "
            + key_hint("any key", "back") + "\n"
        )
        sys.stdout.flush()
        _read_key()
        return
    sys.stdout.write(SHOW_CURSOR)
    sys.stdout.write(CLEAR)
    sys.stdout.flush()
    run_tui(initial_handle=state.handle)
    # When the Textual app exits, we're back here.


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main_loop() -> int:
    state = LauncherState()
    # Enter the alternate screen so redraws never pollute scrollback.
    # On exit (finally block) we restore the user's original terminal
    # state — they'll see whatever was there before they ran `phantom`.
    sys.stdout.write(ALT_SCREEN_ON + HIDE_CURSOR)
    try:
        while True:
            # draw_home is wrapped in sync-output and ends with the
            # cursor hidden. Now build the prompt line and re-enable
            # the cursor right at its final position so the terminal
            # never paints an intermediate I-beam elsewhere.
            draw_home(state)
            prompt = "  " + c("phantom>", BOLD, ACCENT) + " "
            sys.stdout.write(prompt + state.handle + SHOW_CURSOR)
            sys.stdout.flush()
            # Read the first key. Letters that aren't shortcuts feed
            # into a normal line edit.
            try:
                k = _read_key()
            except KeyboardInterrupt:
                sys.stdout.write("\n")
                return 0
            if k == "ENTER":
                _run_scan(state)
                continue
            if k in ("Q", "q"):
                sys.stdout.write("\n")
                return 0
            if k in ("H", "h", "?"):
                screen_help()
                continue
            if k in ("F", "f"):
                screen_flags(state)
                continue
            if k in ("E", "e"):
                screen_export(state)
                continue
            if k in ("S", "s"):
                _run_self_check()
                continue
            if k in ("T", "t"):
                _launch_textual(state)
                continue
            if k == "BACKSPACE":
                state.handle = state.handle[:-1]
                continue
            if k in ("ESC", "EOF"):
                continue
            if len(k) == 1 and k.isprintable():
                # Start a new handle by replacing the existing one if
                # the user is mid-edit. Hand off to the line editor for
                # the rest of the input so they can keep typing.
                state.handle = ""
                sys.stdout.write("\r" + ERASE_LINE + prompt)
                sys.stdout.flush()
                line = _line_edit("", initial=k)
                if line is not None:
                    state.handle = line
    finally:
        # Restore terminal state. Order matters: show cursor + reset
        # styling, then drop the alternate screen so the user lands
        # back on their original shell prompt.
        sys.stdout.write(SHOW_CURSOR + RESET + ALT_SCREEN_OFF)
        sys.stdout.flush()


def is_interactive_session() -> bool:
    """Decide whether to open the launcher when invoked with no args.

    Opens only when both stdin and stdout are a real TTY. Piped /
    redirected runs keep the existing behaviour (the parser prints its
    usage error)."""
    return sys.stdin.isatty() and sys.stdout.isatty()
