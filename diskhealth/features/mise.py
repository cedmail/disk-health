"""mise (https://mise.jdx.dev): tool versions installed by mise, what's outdated, what `mise prune` would free.

Shown inside the Homebrew panel. mise is run without shell activation, from your home folder (global config)
and from each project mise has seen (its tracked configs), so project mise.toml ranges are respected:
`mise upgrade` installs the newest version *within* the requested range (node = "20" -> latest 20.x) and
never rewrites a config (no --bump). Nothing runs automatically except the read-only checks of the daily scan.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

from .. import config, db, jobs, progress
from ..util import IS_MAC, du_kb, human, run_out, stream

NAME = "mise"
KEY = "mise"
SCAN_LABEL = "mise"
ENV = {"NO_COLOR": "1", "MISE_COLOR": "0", "CLICOLOR": "0"}
# Only for commands acting on your own global setup (prune, self-update). Never for project folders: MISE_YES
# would also answer "trust this config?" for you; an untrusted project config just fails and is skipped.
ENV_YES = dict(ENV, MISE_YES="1")
MAX_PROJECTS = 60


# ------------------------------------------------------------------ locations
def mise_bin() -> str | None:
    return shutil.which("mise")


def _xdg(env: str, xdg: str, default: str) -> Path:
    if os.environ.get(env):
        return Path(os.environ[env])
    if os.environ.get(xdg):
        return Path(os.environ[xdg]) / "mise"
    return Path.home() / default


def data_dir() -> Path:
    return _xdg("MISE_DATA_DIR", "XDG_DATA_HOME", ".local/share/mise")


def state_dir() -> Path:
    return _xdg("MISE_STATE_DIR", "XDG_STATE_HOME", ".local/state/mise")


def cache_dir() -> Path:
    return _xdg("MISE_CACHE_DIR", "XDG_CACHE_HOME", "Library/Caches/mise" if IS_MAC else ".cache/mise")


def installs_dir() -> Path:
    return Path(os.environ.get("MISE_INSTALLS_DIR") or data_dir() / "installs")


def _global_config_dir() -> str:
    return os.path.realpath(os.environ.get("MISE_CONFIG_DIR") or Path.home() / ".config/mise")


def tracked_projects(limit: int = MAX_PROJECTS) -> list[str]:
    """Project folders whose mise config mise has loaded (state/tracked-configs), most recently used first."""
    d = state_dir() / "tracked-configs"
    found: dict[str, float] = {}
    try:
        entries = list(os.scandir(d))
    except OSError:
        return []
    gdir = _global_config_dir()
    for e in entries:
        target = os.path.realpath(e.path)
        if not os.path.isfile(target) or target.startswith(gdir + "/"):
            continue
        proj = os.path.dirname(target)
        while os.path.basename(proj) in (".config", ".mise", "mise") and proj != "/":
            proj = os.path.dirname(proj)   # proj/.config/mise.toml, proj/.mise/config.toml, .config/mise/config.toml
        if os.path.realpath(proj) == os.path.realpath(Path.home()):
            continue
        try:
            found[proj] = max(found.get(proj, 0), os.stat(target).st_mtime)
        except OSError:
            pass
    return sorted(found, key=lambda p: -found[p])[:limit]


def installed() -> list[dict]:
    """[{tool, version, path}] from the installs folder (mise's own 'latest'/'20' symlinks are skipped)."""
    out = []
    root = installs_dir()
    try:
        tools = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        return []
    for t in tools:
        try:
            vers = sorted(v for v in t.iterdir() if v.is_dir() and not v.is_symlink() and not v.name.startswith("."))
        except OSError:
            continue
        for v in vers:
            out.append({"tool": t.name, "version": v.name, "path": str(v)})
    return out


# ------------------------------------------------------------------ mise commands
def _mise(args: list[str], cwd: str | None = None, timeout: int = 120) -> tuple[int, str, str]:
    b = mise_bin()
    if not b:
        return 127, "", "mise not found"
    return run_out([b, *args], timeout=timeout, env=ENV, cwd=cwd or str(Path.home()))


def _json(out: str):
    out = (out or "").strip()
    i = min([x for x in (out.find("{"), out.find("[")) if x >= 0], default=-1)
    if i < 0:
        return None
    try:
        return json.loads(out[i:])
    except ValueError:
        return None


def outdated(projects: list[str], log=print) -> list[dict]:
    """`mise outdated --json` in ~ (global tools) and in each tracked project; merged by tool + range."""
    merged: dict[tuple, dict] = {}
    dirs = [str(Path.home())] + projects
    progress.update(items_total=len(dirs), items_done=0, counter_label="configs checked")
    for i, d in enumerate(dirs, 1):
        jobs.checkpoint()
        progress.update(current=d, items_done=i - 1)
        code, out, err = _mise(["outdated", "--json"], cwd=d, timeout=90)
        data = _json(out)
        if not isinstance(data, dict):
            if code not in (0, 1) and err.strip():
                log(f"  ! {d}: {err.strip().splitlines()[-1][:160]}")
            continue
        for name, r in data.items():
            if not isinstance(r, dict):
                continue
            src = (r.get("source") or {}).get("path") or ""
            k = (r.get("name") or name, str(r.get("requested") or ""), str(r.get("current") or ""))
            e = merged.setdefault(k, {"name": k[0], "requested": k[1], "current": k[2],
                                      "latest": str(r.get("latest") or ""), "sources": [], "dirs": []})
            if src and src not in e["sources"]:
                e["sources"].append(src)
            if d not in e["dirs"]:
                e["dirs"].append(d)
    return sorted(merged.values(), key=lambda e: (e["name"], e["requested"]))


_PRUNE_LINE = re.compile(r"(\S+)@(\S+)\s+\[dry-?run\]\s+remove\s+(.+)$", re.I)


def prunable() -> list[dict]:
    """Versions `mise prune` would delete: no tracked config (nor a running process) uses them."""
    items: dict[str, dict] = {}
    code, out, err = _mise(["prune", "--tools", "--dry-run"], timeout=120)
    for line in (out + "\n" + err).splitlines():
        m = _PRUNE_LINE.search(line.strip())
        if not m:
            continue
        tool, ver, paths = m.group(1), m.group(2), m.group(3)
        if tool.lower() == "mise":                # "mise node@20.0.0 [dryrun] remove …"
            continue
        first = os.path.expanduser(paths.split(",")[0].strip())
        items[f"{tool}@{ver}"] = {"tool": tool, "version": ver, "path": first}
    if not items and code == 0:
        # Older output formats: fall back to `mise ls --prunable --json`.
        c2, o2, _e = _mise(["ls", "--prunable", "--json"], timeout=60)
        data = _json(o2)
        if isinstance(data, dict):
            for tool, recs in data.items():
                for r in recs if isinstance(recs, list) else []:
                    v = str(r.get("version") or "")
                    if v:
                        items[f"{tool}@{v}"] = {"tool": tool, "version": v,
                                                "path": r.get("install_path") or str(installs_dir() / tool / v)}
    res = []
    for it in items.values():
        jobs.checkpoint()
        if not os.path.isdir(it["path"]):
            continue                              # already gone
        it["kb"] = du_kb(it["path"])
        res.append(it)
    return sorted(res, key=lambda x: -x["kb"])


_PROBLEM_SKIP = re.compile(r"activat|\bPATH\b|shims", re.I)


def doctor() -> dict:
    """`mise doctor` problems, minus the ones caused by running outside an activated shell."""
    code, out, err = _mise(["doctor"], timeout=90)
    text = out + "\n" + err
    probs = []
    m = re.search(r"\d+\s+problems?\s+found:?\s*\n(.*)", text, re.S | re.I)
    if m:
        cur = []
        for line in m.group(1).splitlines():
            if re.match(r"\s*\d+\.\s", line):
                if cur:
                    probs.append(" ".join(cur))
                cur = [re.sub(r"^\s*\d+\.\s*", "", line).strip()]
            elif line.strip() and cur:
                cur.append(line.strip())
            elif not line.strip() and cur and len(cur) > 3:
                break
        if cur:
            probs.append(" ".join(cur))
    probs = [p[:300] for p in probs if not _PROBLEM_SKIP.search(p)]
    return {"ok": not probs, "problems": probs, "exit": code}


def _install_method(b: str) -> str:
    real = os.path.realpath(b)
    bp = config.brew_prefix()
    if bp and (real.startswith(bp + "/") or "/Cellar/" in real):
        return "Homebrew"
    return "standalone"


# ------------------------------------------------------------------ scan
def scan(log=print) -> dict:
    b = mise_bin()
    if not b:
        res = {"available": False}
        db.kv_set(KEY, res)
        log("mise not installed")
        return res
    _c, out, _e = _mise(["--version"], timeout=30)
    ver = (out.split() or ["?"])[0]
    method = _install_method(b)
    log(f"mise {ver} ({method}, {b})")
    projects = tracked_projects()
    log(f"Checking outdated tools in ~ and {len(projects)} project(s) mise knows …")
    od = outdated(projects, log)
    jobs.checkpoint()
    log("Looking for versions no config uses (mise prune --dry-run) …")
    pr = prunable()
    jobs.checkpoint()
    inst = installed()
    per_tool: dict[str, dict] = {}
    progress.update(items_total=len(inst), items_done=0, counter_label="versions measured")
    for i, it in enumerate(inst, 1):
        jobs.checkpoint()
        progress.update(current=it["path"], items_done=i - 1)
        it["kb"] = du_kb(it["path"])
        t = per_tool.setdefault(it["tool"], {"tool": it["tool"], "versions": [], "kb": 0})
        t["versions"].append(it["version"])
        t["kb"] += it["kb"]
    cache = cache_dir()
    res = {
        "available": True, "version": ver, "bin": b, "install_method": method,
        "self_update": method != "Homebrew",
        "data_dir": str(data_dir()), "installs_kb": sum(i["kb"] for i in inst), "versions": len(inst),
        "tools": sorted(per_tool.values(), key=lambda t: -t["kb"]),
        "cache_dir": str(cache), "cache_kb": du_kb(str(cache)) if cache.is_dir() else 0,
        "projects": projects, "outdated": od,
        "prunable": pr, "prunable_kb": sum(p["kb"] for p in pr),
        "doctor": doctor(), "ts": int(time.time()),
    }
    log(f"{len(inst)} installed versions ({human(res['installs_kb'])}); {len(od)} outdated; "
        f"{len(pr)} prunable ({human(res['prunable_kb'])}); cache {human(res['cache_kb'])}")
    db.kv_set(KEY, res)
    return res


def _refresh(log):
    log("Refreshing mise status …")
    try:
        scan(log=lambda *_: None)
    except jobs.Cancelled:
        raise
    except Exception as ex:
        log(f"! refresh failed: {ex}")


# ------------------------------------------------------------------ actions
def upgrade(log=print) -> None:
    """`mise upgrade` (within each config's range) in ~ and in every project that has outdated tools."""
    prev = db.kv_get(KEY) or {}
    dirs = [str(Path.home())]
    for e in prev.get("outdated") or []:
        for d in e.get("dirs") or []:
            if d not in dirs and os.path.isdir(d):
                dirs.append(d)
    failed = 0
    progress.update(items_total=len(dirs), items_done=0, counter_label="configs upgraded")
    for i, d in enumerate(dirs, 1):
        jobs.checkpoint()
        progress.update(current=d, items_done=i - 1)
        log(f"$ cd {d} && mise upgrade")
        code = stream([mise_bin() or "mise", "upgrade"], lambda l: log("  " + l), timeout=3600, env=ENV, cwd=d)
        if code != 0:
            failed += 1
            log(f"! mise upgrade exited with {code} in {d}")
    db.log_action("mise-upgrade", "~", failed == 0, 0, f"{len(dirs)} config(s), {failed} failed")
    _refresh(log)
    if failed:
        raise RuntimeError(f"mise upgrade failed in {failed} folder(s)")


def prune(log=print) -> int:
    prev = db.kv_get(KEY) or {}
    before = du_kb(str(installs_dir())) if installs_dir().is_dir() else 0
    log("$ mise prune --tools")
    code = stream([mise_bin() or "mise", "prune", "--tools"], lambda l: log("  " + l), timeout=1800, env=ENV_YES,
                  cwd=str(Path.home()))
    after = du_kb(str(installs_dir())) if installs_dir().is_dir() else 0
    freed = max(0, before - after)
    log(f"Freed {human(freed)} ({len(prev.get('prunable') or [])} version(s) were prunable)")
    db.log_action("mise-prune", str(installs_dir()), code == 0, freed, "mise prune --tools")
    _refresh(log)
    if code != 0:
        raise RuntimeError(f"mise prune exited with {code}")
    return freed


def self_update(log=print) -> None:
    b = mise_bin()
    if b and _install_method(b) == "Homebrew":
        raise ValueError("mise is installed with Homebrew: brew upgrade (Run brewed) updates it")
    log("$ mise self-update --yes")
    code = stream([b or "mise", "self-update", "--yes"], lambda l: log("  " + l), timeout=600, env=ENV_YES)
    db.log_action("mise-self-update", b or "mise", code == 0, 0, "")
    _refresh(log)
    if code != 0:
        raise RuntimeError(f"mise self-update exited with {code}")


ACTIONS = {
    "mise-upgrade": {"label": lambda t: "mise upgrade (within requested ranges)", "fn": upgrade, "target": False,
                     "lane": "heavy"},
    "mise-prune": {"label": lambda t: "mise prune (unused tool versions)", "fn": prune, "target": False,
                   "lane": "heavy"},
    "mise-self-update": {"label": lambda t: "mise self-update", "fn": self_update, "target": False,
                         "lane": "heavy"},
}


def alerts(cfg) -> list[dict]:
    r = db.kv_get(KEY) or {}
    if not r.get("available"):
        return []
    out = []
    kb = r.get("prunable_kb") or 0
    if kb >= cfg.get("mise_prune_alert_gb", 1) * 1024 ** 2:
        n = len(r.get("prunable") or [])
        out.append({"id": f"mise-prune:{int(time.time() // (7 * 86400))}", "level": "notice", "notify": True,
                    "title": f"mise: {n} tool version{'s' if n != 1 else ''} no project uses ({human(kb)}): "
                             f"mise prune frees it", "section": "brew"})
    probs = (r.get("doctor") or {}).get("problems") or []
    if probs:
        out.append({"id": f"mise-doctor:{len(probs)}", "level": "info", "notify": False,
                    "title": f"mise doctor: {len(probs)} problem{'s' if len(probs) != 1 else ''}", "section": "brew"})
    return out
