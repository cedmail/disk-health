"""Local Git clones (GitHub first): size, last sync with the remote, last commit, uncommitted/unpushed work.

"Last sync" = the last time the clone talked to its remote: newest of .git/FETCH_HEAD (fetch/pull) and the
remote-tracking reflogs (push/fetch). It is read from files, so it never needs the network.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from . import config, db, progress
from .util import du_kb, human, move_to_trash, run_out, stream

DEFAULT_ROOTS = ["~/projects", "~/Projects", "~/dev", "~/git", "~/github", "~/GitHub", "~/IdeaProjects", "~/src",
                 "~/work", "~/code", "~/Documents/GitHub", "~/Developer"]
SKIP_DIRS = {"node_modules", "target", "build", "dist", ".gradle", ".idea", "vendor", ".venv", "venv", "Library",
             ".Trash", ".cache", "Pods"}
GIT_ENV = {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C",
           "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=10"}


def _git(repo: str, *args: str, timeout: int = 30) -> tuple[int, str]:
    code, out, _err = run_out(["git", "-C", repo, "-c", "core.fsmonitor=false", *args], timeout=timeout, env=GIT_ENV)
    return code, out.strip()


def roots() -> list[str]:
    cfg = config.load()
    rs = [config.expand(r) for r in cfg.get("git_roots") or DEFAULT_ROOTS]
    real, out = set(), []
    for r in rs:
        if os.path.isdir(r):
            rp = os.path.realpath(r)
            if rp not in real:  # ~/projects and ~/Projects are the same folder on macOS
                real.add(rp)
                out.append(r)
    # Drop roots nested in another root.
    return [r for r in out if not any(o != r and os.path.realpath(r).startswith(os.path.realpath(o) + "/") for o in out)]


def find_repos(max_depth: int = 5) -> list[str]:
    found = []
    for root in roots():
        base = root.rstrip("/").count("/")
        for top, dirs, _files in os.walk(root):
            if ".git" in dirs or os.path.isfile(os.path.join(top, ".git")):
                found.append(top)
                dirs[:] = []  # don't descend into a repo (submodules are part of it)
                continue
            if top.count("/") - base >= max_depth:
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
    return sorted(set(found))


def _gitdir(repo: str) -> Path:
    g = Path(repo, ".git")
    if g.is_file():  # worktree / submodule: "gitdir: <path>"
        m = re.match(r"gitdir:\s*(.+)", g.read_text().strip())
        if m:
            p = Path(m.group(1))
            return p if p.is_absolute() else (Path(repo) / p).resolve()
    return g


def _last_sync(gd: Path) -> int | None:
    cands = [gd / "FETCH_HEAD"]
    logs = gd / "logs" / "refs" / "remotes"
    if logs.is_dir():
        cands += [p for p in logs.rglob("*") if p.is_file()]
    ts = [p.stat().st_mtime for p in cands if p.exists()]
    return int(max(ts)) if ts else None


def _github_slug(url: str) -> str | None:
    m = re.search(r"github\.com[:/]+([^/]+)/([^/\s]+?)(?:\.git)?/?$", url or "")
    return f"{m.group(1)}/{m.group(2)}" if m else None


def inspect(repo: str) -> dict:
    gd = _gitdir(repo)
    _c, url = _git(repo, "remote", "get-url", "origin")
    if _c != 0:
        _c2, remotes = _git(repo, "remote")
        first = (remotes.split() or [""])[0]
        url = _git(repo, "remote", "get-url", first)[1] if first else ""
    _c, branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    _c, last_commit = _git(repo, "log", "-1", "--format=%ct")
    _c, status = _git(repo, "status", "--porcelain", "--untracked-files=normal", timeout=60)
    dirty = len([l for l in status.splitlines() if l.strip()])
    ahead = behind = None
    code, ab = _git(repo, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if code == 0 and re.fullmatch(r"\d+\s+\d+", ab):
        behind, ahead = (int(x) for x in ab.split())
    _c, stashes = _git(repo, "stash", "list")
    slug = _github_slug(url)
    total_kb = du_kb(repo)
    git_kb = du_kb(str(gd)) if gd.is_dir() else 0
    return {
        "path": repo, "name": os.path.basename(repo), "remote": url or None, "github": slug,
        "host": "github" if slug else ("other" if url else "none"),
        "branch": branch or None, "last_sync": _last_sync(gd) if gd.is_dir() else None,
        "last_commit": int(last_commit) if last_commit.isdigit() else None,
        "dirty": dirty, "ahead": ahead, "behind": behind, "has_upstream": ahead is not None,
        "stashes": len([l for l in stashes.splitlines() if l.strip()]),
        "kb": total_kb, "git_kb": git_kb,
    }


def scan(progress_log=print) -> dict:
    if not run_out(["git", "--version"], timeout=10)[0] == 0:
        res = {"available": False, "items": [], "ts": int(time.time())}
        db.kv_set("git", res)
        return res
    progress_log("  looking for clones in " + ", ".join(r.replace(str(config.HOME), "~") for r in roots()))
    repos = find_repos()
    progress_log(f"  {len(repos)} repositories found")
    progress.update(items_total=len(repos), items_done=0)
    items = []
    for i, r in enumerate(repos, 1):
        progress.update(current=r, items_done=i - 1)
        try:
            items.append(inspect(r))
        except Exception as ex:
            progress_log(f"  ! {r}: {ex}")
    progress.update(items_done=len(repos))
    items.sort(key=lambda x: x["kb"], reverse=True)
    stale_days = config.load().get("git_stale_days", 90)
    now = time.time()
    res = {
        "available": True, "items": items, "roots": roots(), "ts": int(now),
        "count": len(items), "github_count": sum(1 for x in items if x["github"]),
        "total_kb": sum(x["kb"] for x in items), "git_kb": sum(x["git_kb"] for x in items),
        "stale": sum(1 for x in items if (x["last_sync"] or 0) < now - stale_days * 86400),
        "dirty_count": sum(1 for x in items if x["dirty"]),
        "unpushed_count": sum(1 for x in items if (x["ahead"] or 0) > 0),
        "stale_days": stale_days,
    }
    db.kv_set("git", res)
    return res


def _known(path: str) -> dict:
    item = next((x for x in (db.kv_get("git") or {}).get("items", []) if x["path"] == path), None)
    if not item:
        raise ValueError("Unknown repository (refresh the Git panel first)")
    return item


def _update(path: str, log=print) -> None:
    data = db.kv_get("git") or {}
    data.pop("_ts", None)
    items = [x for x in data.get("items", []) if x["path"] != path]
    if os.path.isdir(path):
        items.append(inspect(path))
        log(f"  now {human(items[-1]['kb'])}")
    items.sort(key=lambda x: x["kb"], reverse=True)
    data["items"] = items
    data.update(count=len(items), total_kb=sum(x["kb"] for x in items), git_kb=sum(x["git_kb"] for x in items),
                github_count=sum(1 for x in items if x["github"]),
                dirty_count=sum(1 for x in items if x["dirty"]),
                unpushed_count=sum(1 for x in items if (x["ahead"] or 0) > 0))
    db.kv_set("git", data)


def gc(path: str, log=print) -> int:
    """git gc: repack objects and drop unreachable ones; shrinks .git."""
    _known(path)
    before = du_kb(str(_gitdir(path)))
    log(f"$ git gc --prune=now   ({path})")
    stream(["git", "-C", path, "gc", "--prune=now"], log, timeout=3600, env=GIT_ENV)
    freed = max(0, before - du_kb(str(_gitdir(path))))
    log(f"freed {human(freed)}")
    db.log_action("git-gc", path, True, freed)
    _update(path, log)
    return freed


def fetch(path: str, log=print) -> None:
    """git fetch --all --prune: updates 'last sync' and ahead/behind. Uses your SSH keys / credential helper."""
    _known(path)
    log(f"$ git fetch --all --prune   ({path})")
    code = stream(["git", "-C", path, "fetch", "--all", "--prune"], log, timeout=600, env=GIT_ENV)
    if code:
        log(f"(exit {code}: if it asks for credentials, run `git fetch` once in Terminal, or check ssh-agent)")
    _update(path, log)


def remove(path: str, log=print) -> int:
    """Move a clone to the Trash; refuses if it holds work that exists nowhere else."""
    item = inspect(path) if os.path.isdir(path) else _known(path)
    _known(path)
    problems = []
    if item["dirty"]:
        problems.append(f"{item['dirty']} uncommitted change(s)")
    if (item["ahead"] or 0) > 0:
        problems.append(f"{item['ahead']} unpushed commit(s)")
    if not item["has_upstream"] and item["remote"]:
        problems.append("current branch has no upstream (may be unpushed)")
    if not item["remote"]:
        problems.append("no remote at all")
    if item["stashes"]:
        problems.append(f"{item['stashes']} stash(es)")
    if problems:
        raise ValueError("Not removed, it has local work: " + "; ".join(problems))
    from . import trash
    dest = move_to_trash(path)
    trash.remember(dest, path)
    log(f"→ Trash: {path}  ({human(item['kb'])}); re-clone any time from {item['remote']}")
    db.log_action("git-remove", path, True, item["kb"])
    _update(path, log)
    trash.listing()
    return item["kb"]
