"""Configuration: defaults merged with ~/Library/Application Support/disk-health/config.json."""
from __future__ import annotations

import json
import os
import secrets
import shutil
from pathlib import Path

HOME = Path.home()
DATA_DIR = Path(os.environ.get("DISK_HEALTH_DATA", HOME / "Library/Application Support/disk-health"))
DB_PATH = DATA_DIR / "disk-health.sqlite3"
CONFIG_PATH = DATA_DIR / "config.json"
TOKEN_PATH = DATA_DIR / "token"
LOG_DIR = DATA_DIR / "logs"
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

DEFAULTS = {
    "port": 7777,
    # Folders scanned with `du` for growth tracking.
    "scan_roots": ["~", "/Applications", "/Library", "{brew_prefix}"],
    # Depth (relative to each root) kept in the history, and minimum size kept.
    "scan_depth": 5,
    "scan_min_mb": 50,
    "scan_exclude": ["~/Library/Mobile Documents", "~/Library/CloudStorage"],
    # Scheduling (hours).
    "scan_every_hours": 24,
    "brew_check_every_hours": 24,
    "free_sample_every_minutes": 60,
    # Alert thresholds.
    "free_warn_percent": 15,
    "free_critical_percent": 8,
    "growth_alert_gb_7d": 2.0,
    "growth_alert_ratio_7d": 0.25,
    "cache_alert_gb": 20,
    "trash_alert_gb": 2,
    "mise_prune_alert_gb": 1,
    # Git: folders searched for clones (default: ~/projects, ~/dev, ~/git, ~/IdeaProjects ...), "stale" threshold.
    "git_roots": [],
    "git_stale_days": 90,
    "unused_app_days": 90,
    "unused_tool_days": 120,
    # brew / oh-my-zsh
    "brew_cleanup_prune_days": 30,
    "omz_dir": "~/.oh-my-zsh",
    "zsh_history": "~/.zsh_history",
    # Extra cache entries, same shape as caches.REGISTRY items.
    "extra_caches": [],
    # Update check (daily). Git checkouts use `git fetch`; zip installs need update_url (see features/updates.py).
    "update_check": True,
    "update_url": "",
    "update_page": "",
}


def brew_prefix() -> str:
    for p in ("/opt/homebrew", "/usr/local/Homebrew", "/usr/local"):
        if Path(p, "bin/brew").exists():
            return "/usr/local" if p.startswith("/usr/local") else p
    b = shutil.which("brew")
    return str(Path(b).resolve().parent.parent) if b else ""


def expand(p: str) -> str:
    p = p.replace("{brew_prefix}", brew_prefix())
    return os.path.expanduser(p)


def load() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except Exception as e:  # keep running with defaults
            print(f"[config] ignoring invalid {CONFIG_PATH}: {e}")
    return cfg


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def token() -> str:
    ensure_dirs()
    if not TOKEN_PATH.exists() or not TOKEN_PATH.read_text().strip():
        # Created 0600 from the start (no window where another user could read it).
        fd = os.open(str(TOKEN_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
        os.chmod(TOKEN_PATH, 0o600)
    return TOKEN_PATH.read_text().strip()
