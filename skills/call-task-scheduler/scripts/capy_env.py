#!/usr/bin/env python3
"""Canonical credential loader for the Go Capy pipeline.

WHY: every pipeline script used to read API keys from a plaintext markdown file on a mapped Google
Drive (`G:\\Shared drives\\Capy Outreach\\global.env.md`). That read happens on EVERY invocation and
intermittently stalls ~30s or fails outright (`OSError [Errno 22]`) when Drive hiccups, crashing the
run. This loader reads the LOCAL file instead and only falls back to Drive if the local file is
missing/unreadable — so the happy path never touches Drive.

Source of truth: `~/.claude/global.env` (KEY=VALUE). Fallback: the Drive markdown (`**KEY:** value`).
Both formats are parsed, so it works against either file. Result is cached for the process.

NOTE: this file is the canonical copy under ai-sdr-manager/scripts/. Byte-identical copies live in the
other skills' scripts dirs (clickup, pipedrive, person-research, org-research, people-icp, root scripts)
because those dirs are run independently and import it as a sibling. Edit here, then re-sync the copies.

API:
    capy_env.load() -> dict      # cached; local-first, Drive-fallback
    capy_env.get(key, default)   # convenience
    capy_env.LOCAL_PATH, capy_env.DRIVE_PATH
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Allow an override for testing; otherwise the standard local location.
LOCAL_PATH = Path(os.environ.get("CAPY_ENV_FILE", "")) if os.environ.get("CAPY_ENV_FILE") \
    else Path.home() / ".claude" / "global.env"
DRIVE_PATH = Path(r"G:\Shared drives\Capy Outreach\global.env.md")

_MD = re.compile(r"\*\*([A-Z0-9_]+):\*\*\s*`?([^`\n]*)`?\s*$")
_KV = re.compile(r"^([A-Z0-9_]+)\s*=\s*(.*)$")

_CACHE: "dict[str, str] | None" = None


def _parse(text: str) -> "dict[str, str]":
    """Parse either format: markdown `**KEY:** value` OR `KEY=VALUE`. Skips comments/blank lines."""
    env: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        m = _MD.match(s)
        if m:
            env[m.group(1)] = m.group(2).strip()
            continue
        m = _KV.match(s)
        if m:
            env[m.group(1)] = m.group(2).strip().strip("`").strip()
    return env


def load(force: bool = False) -> "dict[str, str]":
    """Return the credential dict. Reads the LOCAL file; only falls back to Drive if local is
    missing/unreadable/empty. Cached per process (pass force=True to re-read)."""
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    env: dict[str, str] = {}
    try:
        if LOCAL_PATH.exists():
            env = _parse(LOCAL_PATH.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        env = {}
    if not env:  # local missing/unreadable/empty → fall back to the Drive copy (may be slow/flaky)
        try:
            env = _parse(DRIVE_PATH.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            env = {}
    _CACHE = env
    return env


def get(key: str, default: "str | None" = None) -> "str | None":
    return load().get(key, default)
