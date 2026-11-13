"""Disk scanning: free-space samples, folder-size snapshots (via du) and growth analysis."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time

from . import config, db, progress as live
from .util import IS_MAC, parse_du, run_out


# ---------------------------------------------------------------- free space
def sample_free(record: bool = True) -> dict:
    u = shutil.disk_usage("/System/Volumes/Data" if os.path.exists("/System/Volumes/Data") else "/")
    row = {"ts": int(time.time()), "total": u.total, "used": u.used, "free": u.free}
    if record:
        db.execute("INSERT OR REPLACE INTO free_samples VALUES (?,?,?,?)", (row["ts"], u.total, u.used, u.free))
    return row


def free_history(days: int = 90) -> list[dict]:
    since = int(time.time()) - days * 86400
    rows = db.query("SELECT ts, free, total FROM free_samples WHERE ts>=? ORDER BY ts", (since,))
    # Thin to at most ~4 points per day for the chart.
    out, last = [], 0
    for r in rows:
        if r["ts"] - last >= 6 * 3600 or r is rows[-1]:
            out.append(r)
            last = r["ts"]
    return out


# ---------------------------------------------------------------- snapshots
def _du_cmd(root: str, depth: int, excludes: list[str]) -> list[str]:
    # Note: BSD `du -I mask` ignores every file/folder with that *name* anywhere under root, not just that path
    # (e.g. "CloudStorage"); acceptable for the default excludes, which have distinctive names.
    cmd = ["nice", "-n", "15", "du", "-k", "-d", str(depth)]
    for ex in excludes:
        if ex.startswith(root.rstrip("/") + "/"):
            cmd += (["-I", os.path.basename(ex)] if IS_MAC else [f"--exclude={ex}"])
    return cmd + [root]


def _stream_du(cmd: list[str], root: str, done_kb: int, timeout: int = 3600) -> tuple[int, dict[str, int]]:
    """Run du, feeding live progress as folders complete. Returns (exit code, {path: kb})."""
    sizes: dict[str, int] = {}
    pending: dict[str, int] = {}   # parent -> kb of its completed children already counted
    running = 0                    # KB of completed subtrees (no double counting)
    base_depth = root.rstrip("/").count("/")
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, encoding="utf-8", errors="replace", start_new_session=True)
    except FileNotFoundError:
        return 127, sizes
    from . import jobs
    jobs.track_process(p)
    timer = threading.Timer(timeout, lambda: os.killpg(p.pid, signal.SIGKILL))
    timer.daemon = True
    timer.start()
    last_push = 0.0
    n = 0
    try:
        for line in p.stdout:  # type: ignore[union-attr]
            kb_s, sep, path = line.rstrip("\n").partition("\t")
            kb_s = kb_s.strip()
            if not sep or not kb_s.isdigit() or not path:
                continue
            kb = int(kb_s)
            if path != root:
                path = path.rstrip("/")
            sizes[path] = kb
            n += 1
            running += kb - pending.pop(path, 0)
            parent = os.path.dirname(path)
            pending[parent] = pending.get(parent, 0) + kb
            rel_depth = path.count("/") - base_depth
            if rel_depth == 1:
                live.add_biggest(path, kb)
            now = time.time()
            if now - last_push > 0.4:
                live.update(current=path, dirs=n, counter=n, kb=done_kb + min(running, sizes.get(root, running)))
                last_push = now
        code = p.wait()
    finally:
        timer.cancel()
        jobs.untrack_process(p)
    jobs.checkpoint()
    if code in (-9, 137):
        code = 124
    live.update(dirs=n, counter=n, kb=done_kb + sizes.get(root, running))
    return code, sizes


def take_snapshot(progress=print) -> int:
    cfg = config.load()
    depth, min_kb = int(cfg["scan_depth"]), int(cfg["scan_min_mb"]) * 1024
    excludes = [config.expand(e) for e in cfg["scan_exclude"]]
    roots = [r.rstrip("/") or "/" for r in (config.expand(x) for x in cfg["scan_roots"]) if r and os.path.isdir(r)]
    roots = list(dict.fromkeys(roots))  # de-duplicate, keep order
    # Drop roots nested in another root (e.g. ~ when /Users is also a root)
    roots = [r for r in roots if not any(r != o and r.startswith(o.rstrip("/") + "/") for o in roots)]
    t0 = time.time()
    snap_id = db.execute("INSERT INTO snapshots (ts, duration_s, roots) VALUES (?,?,?)",
                         (int(t0), 0, ",".join(roots))).lastrowid
    rows = []
    # Expected size, for the progress bar: previous complete scan of these roots, else used disk space.
    prev = snapshots()
    expected = 0
    if prev:
        tot = {r["path"]: r["kb"] for r in db.query(
            f"SELECT path, kb FROM sizes WHERE snapshot_id=? AND path IN ({','.join('?' * len(roots))})",
            (prev[-1]["id"], *roots))}
        expected = sum(tot.values()) if len(tot) == len(roots) else 0
    if not expected:
        expected = sample_free(record=False)["used"] // 1024
    live.update(expected_kb=expected, root_count=len(roots), expected_from="previous scan" if prev else "used space")
    done_kb = 0
    for i, root in enumerate(roots):
        progress(f"scanning {root} (depth {depth}) ...")
        live.update(root=root, root_index=i)
        # du exits non-zero on any unreadable folder (TCC-protected ones are common): parse output regardless.
        code, sizes = _stream_du(_du_cmd(root, depth, excludes), root, done_kb)
        if code == 124:
            progress(f"  ! du timed out on {root}; keeping partial results")
        done_kb += sizes.get(root, 0)
        progress(f"  {len(sizes)} folders read, {done_kb / 1024 / 1024:.1f} GB counted so far")
        base_depth = root.rstrip("/").count("/")
        n = 0
        for path, kb in sizes.items():
            if kb < min_kb and path != root:
                continue
            rows.append((snap_id, path, path.rstrip("/").count("/") - base_depth, kb))
            n += 1
        progress(f"  {n} folders >= {cfg['scan_min_mb']} MB recorded")
    if not rows:
        db.execute("DELETE FROM snapshots WHERE id=?", (snap_id,))
        raise RuntimeError("du returned nothing; snapshot discarded")
    db.executemany("INSERT INTO sizes VALUES (?,?,?,?)", rows)
    # duration_s > 0 marks the snapshot complete (see snapshots()), so never store 0.
    db.execute("UPDATE snapshots SET duration_s=? WHERE id=?", (max(0.1, round(time.time() - t0, 1)), snap_id))
    progress(f"snapshot #{snap_id} done in {time.time() - t0:.0f}s")
    return snap_id


def snapshots() -> list[dict]:
    return db.query("SELECT id, ts, duration_s, roots FROM snapshots WHERE duration_s>0 ORDER BY ts")


def _sizes(snap_id: int) -> dict[str, int]:
    return {r["path"]: r["kb"] for r in db.query("SELECT path, kb FROM sizes WHERE snapshot_id=?", (snap_id,))}


def _baseline(snaps: list[dict], days: int) -> dict | None:
    latest = snaps[-1]
    target = latest["ts"] - days * 86400 + 6 * 3600  # 6h slack
    older = [s for s in snaps[:-1] if s["ts"] <= target]
    if older:
        return older[-1]
    return snaps[0] if len(snaps) > 1 else None


def growth(days: int = 7, limit: int = 25) -> dict:
    cfg = config.load()
    snaps = snapshots()
    if not snaps:
        return {"ready": False, "items": [], "reason": "No scan yet"}
    latest = snaps[-1]
    base = _baseline(snaps, days)
    if not base:
        return {"ready": False, "items": [], "reason": "Need a second scan to compare", "latest_ts": latest["ts"]}
    cur, old = _sizes(latest["id"]), _sizes(base["id"])
    min_kb = int(cfg["scan_min_mb"]) * 1024
    roots = [r for r in (latest["roots"] or "").split(",") if r]

    def root_of(p: str) -> str | None:
        return max((r for r in roots if p == r or p.startswith(r.rstrip("/") + "/")), key=len, default=None)

    cands = []
    for path, kb in cur.items():
        before = old.get(path)
        if before is None:
            # A folder is only "new" if its scan root was measured in the baseline; otherwise (root added to
            # the config, or du failed/timed out on it last time) everything under it would look like growth.
            r = root_of(path)
            if r is None or r not in old:
                continue
        delta = kb - (before if before is not None else 0)
        if delta >= max(min_kb, 100 * 1024):
            cands.append({"path": path, "kb": kb, "before_kb": before, "delta_kb": delta, "new": before is None})
    cands.sort(key=lambda c: c["delta_kb"], reverse=True)
    # Keep the most specific folder: drop a parent when one child explains >=80% of its growth.
    keep = []
    for c in cands:
        prefix = c["path"].rstrip("/") + "/"
        if any(o["path"].startswith(prefix) and o["delta_kb"] >= 0.8 * c["delta_kb"] for o in cands):
            continue
        keep.append(c)
    keep = keep[:limit]
    # Sparklines over the last 30 snapshots.
    recent = snaps[-30:]
    ids = ",".join(str(s["id"]) for s in recent)
    for c in keep:
        hist = {r["snapshot_id"]: r["kb"] for r in db.query(
            f"SELECT snapshot_id, kb FROM sizes WHERE path=? AND snapshot_id IN ({ids})", (c["path"],))}
        c["series"] = [[s["ts"], hist.get(s["id"])] for s in recent]
        ratio = c["delta_kb"] / c["before_kb"] if c["before_kb"] else 1.0
        scale = days / 7.0
        c["abnormal"] = (c["delta_kb"] >= cfg["growth_alert_gb_7d"] * scale * 1024 ** 2
                         and ratio >= cfg["growth_alert_ratio_7d"])
    window_days = (latest["ts"] - base["ts"]) / 86400
    return {"ready": True, "items": keep, "from_ts": base["ts"], "to_ts": latest["ts"],
            "window_days": round(window_days, 1), "partial": window_days < days - 0.5}


def children(path: str | None = None) -> dict:
    """Folders in the latest snapshot directly under `path` (or the scan roots)."""
    snaps = snapshots()
    if not snaps:
        return {"path": path, "items": []}
    sid = snaps[-1]["id"]
    if not path:
        roots = [r for r in (snaps[-1]["roots"] or "").split(",") if r] or [""]
        rows = db.query(f"SELECT path, kb FROM sizes WHERE snapshot_id=? AND path IN ({','.join('?' * len(roots))})",
                        (sid, *roots))
    else:
        path = path.rstrip("/") or "/"
        prefix = path.rstrip("/") + "/"
        # Range query instead of LIKE: exact and case-sensitive, no wildcard escaping, and uses the path index.
        # Every string starting with "<path>/" sorts in [prefix, "<path>0") since "0" follows "/".
        rows = db.query("SELECT path, kb FROM sizes WHERE snapshot_id=? AND path>=? AND path<?",
                        (sid, prefix, prefix[:-1] + "0"))
        rows = [r for r in rows if r["path"].startswith(prefix) and r["path"] != prefix
                and "/" not in r["path"][len(prefix):]]
    total = max([r["kb"] for r in rows] + [1]) if not path else (
        (db.query("SELECT kb FROM sizes WHERE snapshot_id=? AND path=?", (sid, path)) or [{"kb": 0}])[0]["kb"])
    rows.sort(key=lambda r: r["kb"], reverse=True)
    return {"path": path, "total_kb": total, "items": rows[:40], "ts": snaps[-1]["ts"]}


def prune_old(keep_days: int = 400) -> None:
    cutoff = int(time.time()) - keep_days * 86400
    old = [r["id"] for r in db.query("SELECT id FROM snapshots WHERE ts<?", (cutoff,))]
    for sid in old:
        db.execute("DELETE FROM sizes WHERE snapshot_id=?", (sid,))
        db.execute("DELETE FROM snapshots WHERE id=?", (sid,))
    db.execute("DELETE FROM free_samples WHERE ts<?", (cutoff,))


def local_snapshots() -> list[str]:
    """APFS local Time Machine snapshots (they hold space Finder calls 'purgeable')."""
    if not IS_MAC:
        return []
    _c, out, _e = run_out(["tmutil", "listlocalsnapshots", "/"], timeout=60)
    return [l.strip() for l in out.splitlines() if l.strip().startswith("com.apple")]
