"""Spotlight index: status, size (admin), current exclusions (admin) and suggested folders to exclude.

Suggestions are folders that hold many indexed items but that you never need in Spotlight / Finder search:
Git clones (search code in the IDE / git grep), Docker data, build caches and toolchains. The number of
indexed items per folder comes from `mdfind -count -onlyin` (no admin needed), so a suggestion only appears
when Spotlight really indexes that folder.

macOS has no supported command to add a folder to Spotlight's Privacy list, so "Exclude" is a guided shortcut:
the dashboard copies the path, reveals the folder in Finder and opens Spotlight settings (drag it into the list).
Reading the index size / exclusion list and rebuilding the index need admin rights: they go through the standard
macOS password prompt (osascript ... with administrator privileges), only when you click the button.
"""
from __future__ import annotations

import json
import os
import re
import time

from .. import config, db, jobs, progress
from ..util import IS_MAC, du_kb, human, run_out

NAME = "spotlight"
KEY = "spotlight"
SCAN_LABEL = "Spotlight"
ADMIN_KEY = "spotlight_admin"
INDEX_DIR = "/System/Volumes/Data/.Spotlight-V100"
SETTINGS_URL = "x-apple.systempreferences:com.apple.Spotlight-Settings.extension"
MIN_ITEMS = 500          # don't suggest folders with fewer indexed items than this


def _candidates() -> list[dict]:
    """(path, kind, reason) for folders worth excluding; only existing ones are kept later."""
    home = os.path.expanduser("~")
    out = []
    git = db.kv_get("git") or {}
    repos = git.get("items") or []
    try:
        from .. import gitrepos
        roots = gitrepos.roots()
    except Exception:
        roots = []
    for r in roots:
        n = sum(1 for x in repos if x.get("path", "").startswith(r.rstrip("/") + "/"))
        out.append({"path": r, "kind": "git", "reason": f"{n} Git repositories" if n else "Code projects",
                    "why": "Search code in your IDE or with git grep; Spotlight results there are mostly noise."})
    dev = [
        ("~/Library/Containers/com.docker.docker", "docker", "Docker Desktop data", "Container layers and the VM disk; nothing to search."),
        ("~/.orbstack", "docker", "OrbStack data", "Container data; nothing to search."),
        ("~/.colima", "docker", "Colima data", "Container data; nothing to search."),
        ("~/.m2", "build", "Maven repository", "Downloaded jars and poms."),
        ("~/.gradle", "build", "Gradle caches", "Downloaded dependencies and build caches."),
        ("~/.npm", "build", "npm cache", "Package tarballs."),
        ("~/.cache", "build", "Tool caches (~/.cache)", "uv, pip, pre-commit … caches."),
        ("~/Library/Developer", "build", "Xcode data", "DerivedData, simulators, device support."),
        ("~/.nvm", "toolchain", "nvm Node versions", "Node installs and their global modules."),
        ("~/.local/share/mise", "toolchain", "mise tool versions", "Node, Java, Python … installed by mise."),
        ("~/.pyenv", "toolchain", "pyenv Python versions", "Python installs and virtualenvs."),
        ("~/.sdkman", "toolchain", "SDKMAN candidates", "JDKs, Maven, Gradle installs."),
        ("~/.rustup", "toolchain", "Rust toolchains", "Toolchains and docs."),
        ("~/.cargo", "toolchain", "Cargo registry", "Downloaded crates."),
        ("~/go", "toolchain", "Go workspace", "Module cache."),
        ("~/Library/Application Support/JetBrains", "build", "JetBrains IDE data", "Plugins and IDE system files."),
    ]
    for p, kind, reason, why in dev:
        out.append({"path": os.path.expanduser(p), "kind": kind, "reason": reason, "why": why})
    bp = config.brew_prefix()
    if bp and bp.startswith("/opt/"):
        out.append({"path": bp, "kind": "toolchain", "reason": "Homebrew", "why": "Installed packages; apps you launch are found anyway."})
    seen, uniq = set(), []
    for c in out:
        rp = os.path.realpath(c["path"])
        if os.path.isdir(c["path"]) and rp not in seen and rp != home:
            seen.add(rp)
            uniq.append(c)
    return uniq


def _indexed_count(path: str) -> int | None:
    """How many items Spotlight has indexed under `path` (None if mdfind is unavailable / failed)."""
    if not IS_MAC:
        return None
    code, out, _err = run_out(["mdfind", "-onlyin", path, "-count", "kMDItemFSName == '*'"], timeout=60)
    m = re.search(r"\d+", out or "")
    return int(m.group()) if code == 0 and m else None


def _snapshot_kb(path: str) -> int | None:
    rows = db.query("SELECT id FROM snapshots WHERE duration_s>0 ORDER BY ts DESC LIMIT 1")
    if not rows:
        return None
    r = db.query("SELECT kb FROM sizes WHERE snapshot_id=? AND path=?", (rows[0]["id"], path))
    return r[0]["kb"] if r else None


def _status() -> dict:
    if not IS_MAC:
        return {"available": False}
    code, out, err = run_out(["mdutil", "-s", "/"], timeout=30)
    text = (out or err or "").strip()
    enabled = None
    if re.search(r"Indexing enabled", text, re.I):
        enabled = True
    elif re.search(r"Indexing (and searching )?disabled", text, re.I):
        enabled = False
    return {"available": code == 0 or bool(text), "enabled": enabled, "status_text": text[-300:]}


def _excluded(path: str, exclusions: list[str]) -> bool:
    rp = os.path.realpath(path)
    for e in exclusions:
        e = os.path.realpath(e)
        if rp == e or rp.startswith(e.rstrip("/") + "/"):
            return True
    return False


def scan(log=print) -> dict:
    res = _status()
    admin = db.kv_get(ADMIN_KEY) or {}
    exclusions = admin.get("exclusions") or []
    cands = _candidates()
    progress.update(items_total=len(cands), items_done=0)
    sugg = []
    for i, c in enumerate(cands, 1):
        jobs.checkpoint()
        progress.update(current=c["path"], items_done=i - 1)
        n = _indexed_count(c["path"])
        # 0 indexed items in a non-empty folder = it's in the Privacy list (macOS doesn't let us read that list:
        # the index folder is protected even for admins, unless Terminal has Full Disk Access).
        c.update(indexed=n, kb=_snapshot_kb(c["path"]),
                 excluded=_excluded(c["path"], exclusions) or (n == 0 and any(os.scandir(c["path"]))))
        if c["excluded"] or n is None or n >= MIN_ITEMS:
            sugg.append(c)
    sugg.sort(key=lambda c: (c["excluded"], -(c["indexed"] or 0)))
    core = os.path.expanduser("~/Library/Metadata/CoreSpotlight")
    res.update({
        "user_index_kb": du_kb(core) if os.path.isdir(core) else None, "user_index_path": core,
        "suggestions": sugg, "index_kb": admin.get("index_kb"), "exclusions": exclusions,
        "admin_ts": admin.get("ts"), "admin_error": admin.get("error"), "partial": admin.get("partial"),
        "indexed_total": sum(c["indexed"] or 0 for c in sugg if not c["excluded"]),
        "ts": int(time.time()),
    })
    log(f"Spotlight {'enabled' if res.get('enabled') else 'status unknown' if res.get('enabled') is None else 'disabled'}; "
        f"{len([s for s in sugg if not s['excluded']])} folders suggested for exclusion")
    db.kv_set(KEY, res)
    return res


# ------------------------------------------------------------------ admin actions
def _admin(shell_cmd: str, timeout: int = 300) -> tuple[int, str]:
    """Run a shell command with admin rights through the standard macOS password prompt."""
    if '"' in shell_cmd or "\\" in shell_cmd:
        raise ValueError("unsafe command")
    script = f'do shell script "{shell_cmd}" with administrator privileges'
    code, out, err = run_out(["osascript", "-e", script], timeout=timeout)
    return code, (out or "") + (err or "")


def inspect(log=print) -> dict:
    """Read the index size and the Privacy exclusion list (admin prompt)."""
    log("Asking for your admin password to read Spotlight's index folder …")
    # 2>&1 so a refusal ("Operation not permitted") is visible; do shell script turns \n into \r.
    cmd = (f"du -sk {INDEX_DIR} 2>&1; echo ---DH---; "
           f"plutil -convert json -o - {INDEX_DIR}/VolumeConfiguration.plist 2>&1")
    code, out = _admin(cmd)
    out = out.replace("\r", "\n")
    rec = {"ts": int(time.time())}
    if code != 0:
        msg = "cancelled" if "-128" in out else out.strip()[-300:]
        rec["error"] = msg
        log(f"Couldn't read the index: {msg}")
    else:
        size_part, _, json_part = out.partition("---DH---")
        log("du: " + (size_part.strip()[-300:] or "(no output)"))
        m = re.search(r"^\s*(\d+)\s+/", size_part, re.M)
        rec["index_kb"] = int(m.group(1)) if m else None
        conf = {}
        j = json_part.strip()
        if j.startswith("{"):
            try:
                conf = json.loads(j)
            except ValueError:
                conf = {}
        else:
            log("plutil: " + (j[-300:] or "(no output)"))
        rec["exclusions"] = [e for e in (conf.get("Exclusions") or []) if isinstance(e, str)]
        errs = [l.strip() for l in (size_part + "\n" + (j if not j.startswith("{") else "")).splitlines()
                if re.search(r"not permitted|denied|No such file", l)]
        if errs and rec["index_kb"] is not None:
            rec["partial"] = True   # du counted only what it could read
            rec["error"] = errs[0]
        if rec["index_kb"] is None:
            rec.pop("index_kb")
            rec["error"] = errs[0] if errs else "du gave no size"
            log(f"Couldn't measure the index: {rec['error']}")
        else:
            log(f"Index size: {human(rec['index_kb'])}; {len(rec['exclusions'])} folder(s) excluded")
    prev = db.kv_get(ADMIN_KEY) or {}
    prev.pop("_ts", None)
    if "error" in rec:  # keep the last good numbers
        prev.update(rec)
        rec = prev
    db.kv_set(ADMIN_KEY, rec)
    scan(log=log)
    if rec.get("error") and not rec.get("index_kb"):
        raise RuntimeError(rec["error"])
    return rec


def rebuild(log=print) -> None:
    """Erase and rebuild the index of the startup disk (admin prompt)."""
    log("Asking for your admin password to rebuild the Spotlight index (mdutil -E /) …")
    code, out = _admin("mdutil -E /")
    log(out.strip() or f"exit {code}")
    if code != 0:
        raise RuntimeError("cancelled" if "-128" in out else "mdutil failed")
    db.log_action("spotlight-rebuild", "/", True, 0, "index erased; macOS re-indexes in the background")
    log("Done. macOS rebuilds the index in the background (mds_stores may use CPU for a while).")
    adm = db.kv_get(ADMIN_KEY) or {}
    adm.pop("_ts", None)
    adm.update(rebuilt=int(time.time()))
    db.kv_set(ADMIN_KEY, adm)


def open_settings(log=print) -> None:
    run_out(["open", SETTINGS_URL], timeout=15)
    log("Opened Spotlight settings: use “Search Privacy…” and drag the folder in (or click + and press ⌘⇧G).")


ACTIONS = {
    "spotlight-inspect": {"label": lambda t: "Read Spotlight index size & exclusions", "fn": inspect,
                          "target": False, "lane": "light"},
    "spotlight-rebuild": {"label": lambda t: "Rebuild the Spotlight index", "fn": rebuild,
                          "target": False, "lane": "light"},
    "spotlight-open-settings": {"label": lambda t: "Open Spotlight settings", "fn": open_settings,
                                "target": False, "lane": "light"},
}


def alerts(cfg) -> list[dict]:
    adm = db.kv_get(ADMIN_KEY) or {}
    kb = adm.get("index_kb") or 0
    if kb >= cfg.get("spotlight_alert_gb", 10) * 1024 ** 2:
        return [{"id": f"spotlight:{int(time.time() // (7 * 86400))}", "level": "notice", "notify": True,
                 "title": f"Spotlight index is {human(kb)}: exclude dev folders and rebuild it", "section": "storage"}]
    return []
