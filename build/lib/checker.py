"""Phantom CLI entrypoint shim.

Real code lives in the focused modules: models / cache / scanner / terminal
/ dedupe / emails / hints / exporters / cli. This file is kept so the
existing `phantom` bash wrapper (which calls `python checker.py`) keeps
working without modification, and so external scripts that import names
from `checker` continue to resolve.
"""
from __future__ import annotations

import sys

from cli import main
from models import (
    BOT_TITLE_HINTS,
    DEFAULT_HEADERS,
    USERNAME_PATTERN,
    CheckResult,
    Site,
    evaluate,
    load_sites,
)
from scanner import HAS_CURL_CFFI, Phantom
from cache import ResponseCache
from terminal import print_compact, print_clustered
from exporters import export_report, resolve_export_path

__all__ = [
    "BOT_TITLE_HINTS",
    "DEFAULT_HEADERS",
    "HAS_CURL_CFFI",
    "Phantom",
    "ResponseCache",
    "CheckResult",
    "Site",
    "USERNAME_PATTERN",
    "evaluate",
    "export_report",
    "load_sites",
    "main",
    "print_compact",
    "print_clustered",
    "resolve_export_path",
]


if __name__ == "__main__":
    raise SystemExit(main())
