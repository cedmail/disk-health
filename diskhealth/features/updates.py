"""Update check: tells you (alert + header badge) when a newer Disk Health is available.

- Homebrew install: compares with the tap's formula version (`brew info`); command: brew upgrade disk-health.
  After the upgrade the service restarts itself on the new code (server.scheduler_tick).

- Git checkout: `git fetch` once a day (non-interactive: never prompts for a password) and counts the
  commits you don't have yet. Update command: git pull && ./install.sh.
- Release zip install: set "update_url" in config.json to a URL that returns the latest version, either
  plain text ("1.2.0", e.g. the raw VERSION file of the repo) or GitHub's releases/latest JSON
  ("tag_name"). Optional "update_page": where to download it (defaults to the release's html_url).
Nothing is downloaded or installed automatically.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request

from .. import PROJECT_DIR, __version__, brew_opt_dir, config, db
from ..util import run_out

NAME = "updates"
KEY = "updates"
CHECK_EVERY_H = 24
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oConnectTimeout=10",
            "GIT_ASKPASS": "true"}
_busy = threading.Lock()


def _vtuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", (v or "").split("-")[0])[:3]) or (0,)


def _git(*args, timeout=30):
    return run_out(["git", "-C", str(PROJECT_DIR), *args], timeout=timeout, env=_GIT_ENV)


def _git_usable() -> bool:
    """False when git is only Apple's stub (calling it would pop up the Command Line Tools installer)."""
    g = shutil.which("git")
    if not g:
        return False
    if os.path.realpath(g) == "/usr/bin/git":
        try:
            return subprocess.run(["xcode-select", "-p"], capture_output=True, timeout=10).returncode == 0
        except Exception:
            return False
    return True


def _check_git(log) -> dict:
    rec = {"mode": "git", "current": __version__}
    if not _git_usable():
        rec["error"] = "git not available"
        return rec
    code, out, err = _git("fetch", "--quiet", "--tags", timeout=90)
    if code != 0:
        rec["error"] = (err or out or f"git fetch exit {code}").strip().splitlines()[-1][:200]
        log(f"git fetch failed: {rec['error']}")
        return rec
    code, out, _ = _git("rev-list", "--count", "HEAD..@{u}")
    if code != 0:
        rec["error"] = "no upstream branch (git branch --set-upstream-to=origin/main)"
        return rec
    behind = int(out.strip() or 0)
    rec["behind"] = behind
    code, out, _ = _git("show", "@{u}:VERSION")
    latest = out.strip() if code == 0 and out.strip() else None
    rec["latest"] = latest or __version__
    rec["available"] = behind > 0
    rec["command"] = f'cd "{PROJECT_DIR}" && git pull && ./install.sh'
    log(f"{behind} new commit(s) upstream" + (f", version {latest}" if latest else ""))
    return rec


def _check_url(url: str, page: str, log) -> dict:
    rec = {"mode": "url", "current": __version__}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"disk-health/{__version__}",
                                                   "Accept": "application/vnd.github+json, text/plain"})
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read(64 * 1024).decode("utf-8", "replace").strip()
    except Exception as ex:
        rec["error"] = str(ex)[:200]
        log(f"Update check failed: {rec['error']}")
        return rec
    latest, html = None, None
    if body.startswith("{"):
        try:
            j = json.loads(body)
            latest, html = j.get("tag_name") or j.get("version"), j.get("html_url")
        except ValueError:
            pass
    else:
        latest = body.split()[0] if body else None
    if not latest:
        rec["error"] = "no version found at update_url"
        return rec
    latest = latest.lstrip("vV")
    rec.update(latest=latest, available=_vtuple(latest) > _vtuple(__version__), page=page or html or url)
    log(f"Latest version {latest} (installed {__version__})")
    return rec


_BREW_ENV = {"HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_ENV_HINTS": "1", "HOMEBREW_NO_COLOR": "1",
             "NONINTERACTIVE": "1"}


def _check_brew(log) -> dict:
    """Installed from the Homebrew tap: compare with the formula version. The tap itself is refreshed by
    `brew update`, which the daily Homebrew check runs."""
    rec = {"mode": "brew", "current": __version__, "command": "brew upgrade disk-health"}
    code, out, err = run_out(["brew", "info", "--json=v2", "disk-health"], timeout=120, env=_BREW_ENV)
    try:
        f = (json.loads(out).get("formulae") or [{}])[0]
    except ValueError:
        lines = (err or out or "").strip().splitlines()
        rec["error"] = lines[-1][:200] if lines else f"brew info exit {code}"
        log(f"brew info failed: {rec['error']}")
        return rec
    latest = str((f.get("versions") or {}).get("stable") or "")
    if not latest:
        rec["error"] = "formula has no stable version"
        return rec
    rec.update(latest=latest, available=_vtuple(latest) > _vtuple(__version__), tap=f.get("tap") or "")
    log(f"Formula {f.get('full_name', 'disk-health')} {latest} (running {__version__})")
    return rec


def check(log=print) -> dict:
    """Check now (also the "Check for updates" action)."""
    with _busy:
        cfg = config.load()
        if brew_opt_dir() is not None:
            rec = _check_brew(log)
        elif (PROJECT_DIR / ".git").exists():
            rec = _check_git(log)
        elif cfg.get("update_url"):
            rec = _check_url(cfg["update_url"], cfg.get("update_page", ""), log)
        else:
            rec = {"mode": "none", "current": __version__,
                   "hint": 'Installed from a zip: set "update_url" in config.json to get update alerts.'}
            log(rec["hint"])
        db.kv_set(KEY, rec)
        return rec


def tick(now, cfg) -> None:
    if not cfg.get("update_check", True) or _busy.locked():
        return
    if now - db.kv_ts(KEY) >= CHECK_EVERY_H * 3600:
        prev = db.kv_get(KEY) or {}
        prev.pop("_ts", None)
        db.kv_set(KEY, dict(prev, current=__version__))   # stamp first: retry only tomorrow
        threading.Thread(target=check, kwargs={"log": lambda *a: None}, daemon=True, name="update-check").start()


ACTIONS = {
    "updates-check": {"label": lambda t: "Check for Disk Health updates", "fn": check, "target": False,
                      "lane": "light"},
}


def alerts(cfg) -> list[dict]:
    rec = db.kv_get(KEY) or {}
    if not rec.get("available"):
        return []
    what = f"Disk Health {rec['latest']} is available" if rec.get("latest") and rec["latest"] != __version__ \
        else f"Disk Health update available ({rec.get('behind', '?')} new commits)"
    a = {"id": f"update:{rec.get('latest')}:{rec.get('behind', 0)}", "level": "notice", "notify": True,
         "title": f"{what} (you have {__version__})", "section": "updates"}
    if rec.get("command"):
        a["cmd"] = rec["command"]
    if str(rec.get("page", "")).startswith(("https://", "http://")):
        a["url"] = rec["page"]
    return [a]
