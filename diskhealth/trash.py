"""Trash management: list ~/.Trash, empty it (all or some items) with live progress, put back items we trashed."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from . import db, progress
from .util import DeleteTracker, du_kb, human

TRASH = Path.home() / ".Trash"
ORIGINS_KEY = "trash_origins"   # {path in Trash: original path} for items Disk Health moved there


def remember(trash_path: str, original: str) -> None:
    o = db.kv_get(ORIGINS_KEY) or {}
    o.pop("_ts", None)
    o[trash_path] = original
    db.kv_set(ORIGINS_KEY, o)


def _origins() -> dict:
    o = db.kv_get(ORIGINS_KEY) or {}
    o.pop("_ts", None)
    return o


def listing(limit: int = 300) -> dict:
    """Items in the Trash, biggest first. Needs Full Disk Access (macOS protects ~/.Trash)."""
    if not TRASH.is_dir():
        return {"items": [], "total_kb": 0, "count": 0, "readable": False}
    try:
        entries = [e for e in os.scandir(TRASH) if e.name != ".DS_Store"]
    except PermissionError:
        return {"items": [], "total_kb": 0, "count": 0, "readable": False,
                "error": "No permission to read the Trash: grant Full Disk Access to DiskHealth.app."}
    origins = _origins()
    items = []
    for e in entries:
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        p = e.path
        is_dir = e.is_dir(follow_symlinks=False)
        items.append({"path": p, "name": e.name, "kb": du_kb(p) if is_dir else (st.st_blocks // 2),
                      "is_dir": is_dir, "trashed": int(st.st_ctime),  # ctime ≈ when it was moved to the Trash
                      "origin": origins.get(p)})
    items.sort(key=lambda i: i["kb"], reverse=True)
    # Forget origins of items no longer in the Trash.
    live = {i["path"] for i in items}
    if any(k not in live for k in origins):
        db.kv_set(ORIGINS_KEY, {k: v for k, v in origins.items() if k in live})
    res = {"items": items[:limit], "total_kb": sum(i["kb"] for i in items), "count": len(items),
           "readable": True, "ts": int(time.time())}
    db.kv_set("trash", res)
    return res


def _in_trash(p: str) -> bool:
    rp = os.path.realpath(os.path.dirname(p))
    return rp == os.path.realpath(TRASH) and os.path.basename(p) not in ("", ".", "..")


def empty(paths: list[str] | None = None, log=print) -> int:
    """Permanently delete the given Trash items (all of them if paths is None)."""
    current = listing(limit=100000)
    targets = current["items"] if paths is None else [i for i in current["items"] if i["path"] in set(paths)]
    targets = [t for t in targets if _in_trash(t["path"])]
    expected = sum(t["kb"] for t in targets)
    title = "Emptying the Trash" if paths is None else f"Deleting {len(targets)} item{'s' if len(targets) != 1 else ''} from the Trash"
    progress.start(["Deleting"], title=title, expected_kb=expected, expected_from="Trash size",
                   counter_label="files deleted", kb_label="freed", items_total=len(targets))
    freed = files = 0

    def report(t: DeleteTracker, current: str) -> None:
        progress.update(kb=freed + t.freed_kb, counter=files + t.files, current=current)

    for i, item in enumerate(targets, 1):
        t = DeleteTracker(report)
        t.remove(item["path"])
        freed += t.freed_kb
        files += t.files
        log(f"  {item['name']}: {human(t.freed_kb)} in {t.files:,} files")
        for er in t.errors[:5]:
            log(f"  ! {er}")
        if len(t.errors) > 5:
            log(f"  ! ... {len(t.errors) - 5} more (files in use or locked)")
        progress.update(items_done=i)
    log(f"freed {human(freed)}")
    db.log_action("empty-trash", "" if paths is None else ", ".join(t["name"] for t in targets)[:300], True, freed)
    listing()
    return freed


def put_back(trash_path: str, log=print) -> str:
    origin = _origins().get(trash_path)
    if not origin:
        raise ValueError("Unknown original location: use Finder's 'Put Back' for this item")
    if not _in_trash(trash_path) or not os.path.lexists(trash_path):
        raise ValueError("Item is no longer in the Trash")
    if os.path.lexists(origin):
        raise ValueError(f"Something already exists at {origin}")
    os.makedirs(os.path.dirname(origin), exist_ok=True)
    shutil.move(trash_path, origin)
    log(f"restored {origin}")
    db.log_action("put-back", origin, True)
    listing()
    return origin
