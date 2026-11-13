"""Duplicate files in your own folders (Downloads, Documents, Desktop by default).

Not part of the daily scan (it reads file contents): run "duplicates-find" from the panel.
Algorithm: group files by size -> hash the first 64 KB of same-size files -> full hash only for what still collides.
Hard links (same inode) are one file, not duplicates. Symlinks, app/library bundles, .git, node_modules, Library and
iCloud placeholders (dataless files: reading them would download them) are skipped.

Config: duplicate_roots (default ~/Downloads, ~/Documents, ~/Desktop), duplicate_min_mb (default 1).
"""
from __future__ import annotations

import hashlib
import os
import stat
import time

from .. import config, db, jobs, progress, trash
from ..util import human, move_to_trash

NAME = "duplicates"
KEY = "duplicates"
SCAN_LABEL = "Duplicate files"

DEFAULT_ROOTS = ["~/Downloads", "~/Documents", "~/Desktop"]
SKIP_DIRS = {".git", "node_modules", "Library", ".Trash", ".svn", ".hg"}
PACKAGE_EXT = (".app", ".photoslibrary", ".bundle", ".framework", ".plugin", ".kext", ".xcarchive",
               ".musiclibrary", ".fcpbundle", ".imovielibrary", ".tvlibrary", ".photolibrary", ".band",
               ".logicx", ".rtfd", ".pages", ".numbers", ".key", ".sparsebundle")
SF_DATALESS = 0x40000000  # macOS: file content lives in iCloud only
HEAD = 64 * 1024
CHUNK = 1024 * 1024
MAX_GROUPS = 500  # stored/sent to the dashboard (largest waste first); totals cover every group


def _cfg() -> tuple[list[str], int]:
    cfg = config.load()
    roots = cfg.get("duplicate_roots") or DEFAULT_ROOTS
    try:
        min_bytes = int(float(cfg.get("duplicate_min_mb", 1)) * 1024 * 1024)
    except (TypeError, ValueError):
        min_bytes = 1024 * 1024
    return [config.expand(r) for r in roots], max(1, min_bytes)


def _skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.lower().endswith(PACKAGE_EXT)


def _list_files(roots: list[str], min_bytes: int, log=print) -> dict[int, list[tuple[str, os.stat_result]]]:
    """{size: [(path, stat)]} of regular files >= min_bytes, one path per inode."""
    by_size: dict[int, list] = {}
    seen_inodes: set = set()
    seen_roots: set = set()
    count = 0
    for root in roots:
        try:
            rst = os.stat(root)
        except OSError:
            log(f"  {root}: not found, skipped")
            continue
        if not stat.S_ISDIR(rst.st_mode) or (rst.st_dev, rst.st_ino) in seen_roots:
            continue
        seen_roots.add((rst.st_dev, rst.st_ino))
        stack = [root]
        while stack:
            d = stack.pop()
            jobs.checkpoint()
            progress.update(current=d, counter=count)
            try:
                with os.scandir(d) as it:
                    entries = list(it)
            except OSError:
                continue
            for e in entries:
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        if not _skip_dir(e.name):
                            stack.append(e.path)
                        continue
                    if e.name.endswith(".icloud"):
                        continue
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_size < min_bytes:
                    continue
                if getattr(st, "st_flags", 0) & SF_DATALESS:
                    continue
                ino = (st.st_dev, st.st_ino)
                if ino in seen_inodes:  # hard link to a file already listed: same data, not a duplicate
                    continue
                seen_inodes.add(ino)
                count += 1
                by_size.setdefault(st.st_size, []).append((e.path, st))
    return by_size


def _hash(path: str, limit: int | None = None, on_bytes=None) -> str:
    h = hashlib.blake2b(digest_size=20)
    read = 0
    with open(path, "rb") as f:
        while True:
            want = CHUNK if limit is None else min(CHUNK, limit - read)
            if want <= 0:
                break
            b = f.read(want)
            if not b:
                break
            h.update(b)
            read += len(b)
            if on_bytes:
                on_bytes(len(b))
            jobs.checkpoint()
    return h.hexdigest()


def find(log=print) -> dict:
    roots, min_bytes = _cfg()
    t0 = time.time()
    progress.start(["Listing files", "Comparing file starts", "Hashing candidates"], title="Finding duplicate files",
                   weights=[0.2, 0.2, 0.6], counter_label="files", kb_label="read")
    log("searching " + ", ".join(roots) + f" for files of {human(min_bytes / 1024)} or more")
    by_size = _list_files(roots, min_bytes, log=log)
    files_scanned = sum(len(v) for v in by_size.values())
    same_size = [v for v in by_size.values() if len(v) > 1]
    log(f"{files_scanned:,} files, {sum(len(v) for v in same_size):,} share their size with another file")

    # Step 2: first 64 KB.
    progress.step(1, "Reading the first 64 KB of same-size files")
    todo = sum(len(v) for v in same_size)
    progress.update(items_total=todo)
    done = 0
    candidates: list[list[tuple[str, os.stat_result]]] = []
    for group in same_size:
        by_head: dict[str, list] = {}
        for path, st in group:
            try:
                by_head.setdefault(_hash(path, HEAD), []).append((path, st))
            except OSError as ex:
                log(f"  ! {path}: {ex}")
            done += 1
            progress.update(items_done=done, current=path)
        candidates += [v for v in by_head.values() if len(v) > 1]

    # Step 3: full hash of what still collides (files <= 64 KB are already fully compared).
    progress.step(2, "Hashing whole files")
    todo = sum(len(v) for v in candidates)
    progress.update(items_total=todo, kb=0)
    done, read_bytes = 0, 0

    def on_bytes(n: int) -> None:
        nonlocal read_bytes
        read_bytes += n
        progress.update(kb=read_bytes // 1024)

    groups = []
    for group in candidates:
        size = group[0][1].st_size
        by_full: dict[str, list] = {}
        for path, st in group:
            progress.update(current=path)
            try:
                hx = _hash(path, HEAD) if size <= HEAD else _hash(path, on_bytes=on_bytes)
                f = {"path": path, "mtime": int(st.st_mtime)}
                if st.st_nlink > 1:  # other hard links keep the data alive: trashing this one frees nothing
                    f["links"] = st.st_nlink
                by_full.setdefault(hx, []).append(f)
            except OSError as ex:
                log(f"  ! {path}: {ex}")
            done += 1
            progress.update(items_done=done)
        for hx, files in by_full.items():
            if len(files) > 1:
                files.sort(key=lambda f: f["mtime"], reverse=True)
                groups.append({"id": hx[:16], "hash": hx, "size": size, "files": files,
                               "wasted": size * (len(files) - 1)})

    groups.sort(key=lambda g: g["wasted"], reverse=True)
    total = sum(g["wasted"] for g in groups)
    res = {"ts": int(time.time()), "roots": roots, "min_mb": min_bytes / (1024 * 1024),
           "groups": groups[:MAX_GROUPS], "group_count": len(groups), "total_wasted": total,
           "hidden_wasted": total - sum(g["wasted"] for g in groups[:MAX_GROUPS]),
           "files_scanned": files_scanned, "duration": round(time.time() - t0, 1)}
    db.kv_set(KEY, res)
    log(f"{len(groups):,} groups of duplicates, {human(total / 1024)} wasted ({res['duration']} s)")
    return res


# ------------------------------------------------------------------ trashing
def _data() -> dict:
    d = db.kv_get(KEY) or {}
    d.pop("_ts", None)
    return d


def _save(d: dict) -> None:
    removed = 0
    keep = []
    for g in d.get("groups", []):
        g["wasted"] = g["size"] * max(0, len(g["files"]) - 1)
        if len(g["files"]) > 1:
            keep.append(g)
        else:
            removed += 1
    d["groups"] = sorted(keep, key=lambda g: g["wasted"], reverse=True)
    d["group_count"] = max(0, int(d.get("group_count", 0)) - removed)
    d["total_wasted"] = sum(g["wasted"] for g in keep) + max(0, int(d.get("hidden_wasted", 0)))
    db.kv_set(KEY, d)


def _same_copy(path: str, g: dict) -> os.stat_result | None:
    """stat of `path` if it is still a regular file with the group's size and content, else None."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size != g["size"]:
        return None
    try:
        hx = _hash(path, HEAD) if g["size"] <= HEAD else _hash(path)
    except OSError:
        return None
    return st if hx == g["hash"] else None


def _verified_others(g: dict, path: str, target_st: os.stat_result, log=print) -> list[str]:
    """Other copies of the group that still exist with identical content (and aren't the same inode)."""
    out = []
    for f in g["files"]:
        if f["path"] == path:
            continue
        st = _same_copy(f["path"], g)
        if st and (st.st_dev, st.st_ino) != (target_st.st_dev, target_st.st_ino):
            out.append(f["path"])
        else:
            log(f"  {f['path']} changed or is gone")
    return out


def _trash_one(path: str, g: dict, log=print) -> int:
    dest = move_to_trash(path)
    trash.remember(dest, path)
    kb = g["size"] // 1024
    log(f"moved to the Trash: {path}")
    db.log_action("duplicates-trash", path, True, kb)
    return kb


def _drop(d: dict, g: dict, gone: set) -> None:
    for x in d.get("groups", []):
        if x["id"] == g["id"]:
            x["files"] = [f for f in x["files"] if f["path"] not in gone]


def trash_file(path: str, log=print) -> int:
    """Move one duplicate to the Trash, only if another identical copy still exists."""
    d = _data()
    g = next((x for x in d.get("groups", []) if any(f["path"] == path for f in x["files"])), None)
    if not g:
        raise ValueError("This file isn't in the last duplicate search: search again")
    progress.start(["Checking copies"], title=f"Moving {os.path.basename(path)} to the Trash",
                   counter_label="", kb_label="")
    st = _same_copy(path, g)
    if not st:
        _drop(d, g, {path})
        _save(d)
        raise ValueError("The file changed or is gone since the search: not a duplicate any more")
    others = _verified_others(g, path, st, log=log)
    if not others:
        _drop(d, g, {f["path"] for f in g["files"] if f["path"] != path})
        _save(d)
        raise ValueError("Refusing: this is the last remaining copy (the others changed or are gone)")
    kb = _trash_one(path, g, log=log)
    _drop(d, g, {path})
    _save(d)
    trash.listing()
    return kb


def keep_newest(group_id: str, log=print) -> int:
    """Move every copy of a group but the newest to the Trash (each re-checked identical to the kept one)."""
    d = _data()
    g = next((x for x in d.get("groups", []) if x["id"] == group_id), None)
    if not g:
        raise ValueError("Unknown duplicate group: search again")
    progress.start(["Checking copies", "Moving to the Trash"], title="Keeping the newest copy",
                   counter_label="", kb_label="", items_total=len(g["files"]))
    alive = []
    for f in g["files"]:
        st = _same_copy(f["path"], g)
        if st:
            alive.append((st.st_mtime, f["path"], st))
        else:
            log(f"  {f['path']} changed or is gone: left alone")
    gone = {f["path"] for f in g["files"]} - {p for _m, p, _s in alive}
    if len(alive) < 2:
        _drop(d, g, gone)
        _save(d)
        raise ValueError("Nothing to do: fewer than two identical copies are left")
    alive.sort(key=lambda a: a[0], reverse=True)
    _m, kept, kept_st = alive[0]
    log(f"keeping {kept}")
    progress.step(1)
    progress.update(items_total=len(alive) - 1)
    freed = 0
    for i, (_m, p, st) in enumerate(alive[1:], 1):
        jobs.checkpoint()
        if (st.st_dev, st.st_ino) == (kept_st.st_dev, kept_st.st_ino):
            continue  # a hard link to the kept file: trashing it frees nothing
        freed += _trash_one(p, g, log=log)
        gone.add(p)
        progress.update(items_done=i, current=p)
    _drop(d, g, gone)
    _save(d)
    log(f"freed {human(freed)} (once the Trash is emptied)")
    trash.listing()
    return freed


ACTIONS = {
    "duplicates-find": {"label": lambda t: "Find duplicate files", "fn": find, "target": False, "lane": "heavy"},
    "duplicates-trash": {"label": lambda t: f"Move {os.path.basename(t or '')} to the Trash", "fn": trash_file,
                         "target": True, "lane": "heavy"},
    "duplicates-keep-newest": {"label": lambda t: f"Keep the newest copy ({t})", "fn": keep_newest,
                               "target": True, "lane": "heavy"},
}
