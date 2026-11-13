"""Space map: a nested size tree (for the dashboard's treemap) built from the latest folder-size snapshot.

No scan of its own: it reads the `sizes` rows the daily `du` snapshot already stored.

GET /api/spacemap?path=<abs path>&depth=3&compare_days=7
    path          optional; default: a virtual root whose children are the scan roots
    depth         levels below `path` to include (1..4, default 3)
    compare_days  optional; adds delta_kb per node vs the snapshot closest to that many days ago

Each node: {path, name, kb, children: [...], delta_kb?}. Children are sorted by size; past the top
MAX_CHILDREN they are folded, together with the part of the parent that no stored child explains
(folders under scan_min_mb and loose files), into one synthetic node {"other": true, "count": n}
so children always add up to their parent.
"""
from __future__ import annotations

import os

from .. import db, scanner

NAME = "spacemap"
MAX_CHILDREN = 40
MAX_DEPTH = 4
OTHER_NAME = "(other / smaller items)"


def _int(v, default, lo, hi):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _rows(snap_id: int, path: str | None) -> dict[str, int]:
    """{path: kb} of a snapshot: everything (virtual root) or `path` and everything below it, in one query."""
    if not path:
        rows = db.query("SELECT path, kb FROM sizes WHERE snapshot_id=?", (snap_id,))
    else:
        prefix = path.rstrip("/") + "/"
        # Range query (see scanner.children): every "<path>/..." sorts in [prefix, "<path>0").
        rows = db.query("SELECT path, kb FROM sizes WHERE snapshot_id=? AND (path=? OR (path>=? AND path<?))",
                        (snap_id, path, prefix, prefix[:-1] + "0"))
    return {r["path"]: r["kb"] for r in rows}


def _parent(p: str) -> str:
    return os.path.dirname(p.rstrip("/")) or "/"


def _build(sizes: dict[str, int], top: str | None, roots: list[str], depth: int) -> dict | None:
    kids: dict[str, list[str]] = {}
    for p in sizes:
        if p != "/" and p not in roots:
            kids.setdefault(_parent(p), []).append(p)

    def node(p: str, level: int) -> dict:
        n = {"path": p, "name": os.path.basename(p.rstrip("/")) or p, "kb": sizes[p]}
        if p in roots:
            n["root"] = True
        if level < depth:
            n["children"] = _children(p, n["kb"], kids.get(p, []), level)
        return n

    def _children(p: str, kb: int, ch: list[str], level: int) -> list[dict]:
        ch = sorted(ch, key=lambda c: sizes[c], reverse=True)
        shown, rest = ch[:MAX_CHILDREN], ch[MAX_CHILDREN:]
        out = [node(c, level + 1) for c in shown]
        other_kb = max(0, kb - sum(sizes[c] for c in shown))
        if out and other_kb > 0:  # a folder with no stored subfolder stays a leaf
            out.append({"path": p.rstrip("/") + "/", "name": OTHER_NAME, "kb": other_kb, "other": True,
                        "count": len(rest)})
        return out

    if top:
        if top not in sizes:
            return None
        return node(top, 0)
    present = [r for r in roots if r in sizes]
    tree = {"path": "", "name": "All roots", "kb": sum(sizes[r] for r in present), "virtual": True}
    if depth > 0:
        tree["children"] = sorted((node(r, 1) for r in present), key=lambda n: n["kb"], reverse=True)
    return tree


def _add_deltas(n: dict, old: dict[str, int], measured_roots: set) -> None:
    """delta_kb vs the baseline; a folder absent from the baseline counts from 0 when its scan root was
    measured then (it was under the size threshold), else unknown (None)."""
    if n.get("virtual"):
        n["delta_kb"] = None
    elif not n.get("other"):
        if n["path"] in old:
            n["delta_kb"] = n["kb"] - old[n["path"]]
        else:
            r = max((r for r in measured_roots if n["path"] == r or n["path"].startswith(r.rstrip("/") + "/")),
                    key=len, default=None)
            n["delta_kb"] = n["kb"] if r else None
            if r:
                n["new"] = True
    ch = n.get("children") or []
    for c in ch:
        if not c.get("other"):
            _add_deltas(c, old, measured_roots)
    for c in ch:
        if c.get("other"):
            known = [s["delta_kb"] for s in ch if not s.get("other")]
            c["delta_kb"] = (n["delta_kb"] - sum(known)) if n.get("delta_kb") is not None and None not in known \
                else None
    if n.get("virtual"):
        ds = [c["delta_kb"] for c in ch]
        n["delta_kb"] = sum(ds) if ds and None not in ds else None


def spacemap(q: dict) -> dict:
    depth = _int(q.get("depth"), 3, 1, MAX_DEPTH)
    days = _int(q.get("compare_days"), 0, 0, 3650) if q.get("compare_days") else 0
    path = (q.get("path") or "").strip()
    if path:
        path = os.path.normpath(path) if path.startswith("/") else ""
        if not path:
            return {"ready": True, "found": False, "reason": "Path must be absolute", "tree": None}
        if path.startswith("//"):
            path = "/" + path.lstrip("/")
    snaps = scanner.snapshots()
    if not snaps:
        return {"ready": False, "reason": "No scan yet", "tree": None}
    latest = snaps[-1]
    roots = [r for r in (latest["roots"] or "").split(",") if r]
    sizes = _rows(latest["id"], path or None)
    tree = _build(sizes, path or None, roots, depth)
    out = {"ready": True, "found": tree is not None, "ts": latest["ts"], "path": path, "depth": depth,
           "roots": roots, "tree": tree, "compare": None}
    if tree is None:
        out["reason"] = "Not in the latest scan (smaller than the scan threshold, or outside the scan roots)"
        return out
    if path:
        out["root"] = max((r for r in roots if path == r or path.startswith(r.rstrip("/") + "/")), key=len,
                          default=None)
    if days:
        base = scanner._baseline(snaps, days)
        if base:
            old = _rows(base["id"], path or None)
            if path and out.get("root") and out["root"] != path:
                # the scan root itself is outside the prefix query; one extra lookup tells if it was measured
                old_root = db.query("SELECT kb FROM sizes WHERE snapshot_id=? AND path=?", (base["id"], out["root"]))
                measured = {out["root"]} if old_root else set()
            else:
                measured = {r for r in roots if r in old}
            _add_deltas(tree, old, measured)
            out["compare"] = {"days": days, "from_ts": base["ts"], "to_ts": latest["ts"],
                              "window_days": round((latest["ts"] - base["ts"]) / 86400, 1)}
        else:
            out["compare"] = {"days": days, "from_ts": None, "reason": "Need a second scan to compare"}
    return out


ROUTES = {"/api/spacemap": spacemap}
