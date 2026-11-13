"""Background jobs with live logs, two lanes and cancellation.

Lanes: "heavy" jobs (scan, clean, brew, uninstall ...) run one at a time; "light" jobs (refresh a panel,
re-measure, git fetch ...) run one at a time in their own lane, so they never wait behind a long clean.

Cancellation is cooperative: cancel() sets a flag and kills the job's running child processes (du, brew,
git ...); long loops call checkpoint(), which raises Cancelled when the flag is set.
"""
from __future__ import annotations

import itertools
import os
import signal
import threading
import time
import traceback
from collections import OrderedDict

from . import config, progress

_ids = itertools.count(1)
_jobs: "OrderedDict[int, dict]" = OrderedDict()
_lock = threading.Lock()
_lane_locks = {"heavy": threading.Lock(), "light": threading.Lock()}
_tl = threading.local()
_procs: dict[int, set] = {}
_cancel: dict[int, threading.Event] = {}


class Cancelled(Exception):
    pass


# ------------------------------------------------------------------ helpers used by long-running code
def current_id() -> int | None:
    return getattr(_tl, "job_id", None)


def cancelled() -> bool:
    jid = current_id()
    ev = _cancel.get(jid) if jid else None
    return bool(ev and ev.is_set())


def checkpoint() -> None:
    """Raise Cancelled if the current job was cancelled. Call it in long loops."""
    if cancelled():
        raise Cancelled()


def track_process(p) -> None:
    """Register a child process of the current job so cancel() can kill it."""
    jid = current_id()
    if jid:
        with _lock:
            _procs.setdefault(jid, set()).add(p)
        if cancelled():
            _kill(p)


def untrack_process(p) -> None:
    jid = current_id()
    if jid:
        with _lock:
            _procs.get(jid, set()).discard(p)


def _kill(p) -> None:
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except Exception:
        try:
            p.terminate()
        except Exception:
            pass


# ------------------------------------------------------------------ jobs
def submit(kind: str, label: str, fn, *args, lane: str = "heavy") -> dict:
    """Queue fn(*args, log=...) in a thread. Returns the job (or the already-queued/running identical job)."""
    lane = lane if lane in _lane_locks else "heavy"
    with _lock:
        for j in _jobs.values():
            if j["kind"] == kind and j["label"] == label and j["status"] in ("queued", "running"):
                return _public(j)
        jid = next(_ids)
        job = {"id": jid, "kind": kind, "label": label, "lane": lane, "status": "queued", "log": [], "log_base": 0,
               "created": time.time(), "started": None, "ended": None, "result": None}
        _jobs[jid] = job
        _cancel[jid] = threading.Event()
        # Trim finished jobs only (never forget a queued/running one, or dedupe/busy() would miss it).
        for old in [k for k, j in _jobs.items() if j["status"] in ("done", "failed", "cancelled")][:max(0, len(_jobs) - 40)]:
            del _jobs[old]
            _cancel.pop(old, None)

    def log(msg: str = "") -> None:
        lines = str(msg).splitlines() or [""]
        progress.log_line(lines[-1])
        with _lock:
            job["log"].extend(lines)
            if len(job["log"]) > 5000:
                # Drop the oldest lines but keep offsets absolute so ?since= keeps working.
                del job["log"][:1000]
                job["log_base"] += 1000

    def runner():
        with _lane_locks[lane]:
            ev = _cancel.get(jid)
            if ev and ev.is_set():  # cancelled while queued
                with _lock:
                    job["status"], job["ended"] = "cancelled", time.time()
                return
            _tl.job_id = jid
            progress.bind(jid)
            with _lock:
                job["status"], job["started"] = "running", time.time()
            progress.start([label], title=label, generic=True, kind=kind, lane=lane, job_id=jid)
            try:
                result = fn(*args, log=log) if _accepts_log(fn) else fn(*args)
                status = "cancelled" if cancelled() else "done"
            except Cancelled:
                log("Cancelled.")
                result, status = None, "cancelled"
            except Exception as ex:
                if cancelled():
                    log(f"Cancelled ({ex}).")
                    result, status = None, "cancelled"
                else:
                    log(f"ERROR: {ex}")
                    log(traceback.format_exc())
                    result, status = None, "failed"
            finally:
                progress.finish()
                progress.bind(None)
                _tl.job_id = None
                with _lock:
                    _procs.pop(jid, None)
            with _lock:
                job["result"], job["status"], job["ended"] = result, status, time.time()
                text = "\n".join(job["log"])
            try:
                config.LOG_DIR.mkdir(parents=True, exist_ok=True)
                (config.LOG_DIR / f"job-{jid}-{kind}.log").write_text(text)
                _prune_logs()
            except Exception:
                pass

    threading.Thread(target=runner, daemon=True, name=f"job-{jid}").start()
    with _lock:
        return _public(job)


def cancel(jid: int) -> dict | None:
    with _lock:
        job = _jobs.get(jid)
        if not job or job["status"] not in ("queued", "running"):
            return _public(job) if job else None
        _cancel.setdefault(jid, threading.Event()).set()
        procs = list(_procs.get(jid, ()))
        if job["status"] == "queued":
            job["status"], job["ended"] = "cancelled", time.time()
        job["log"].append("Cancel requested ...")
    for p in procs:
        _kill(p)
    with _lock:
        return _public(job)


def _prune_logs(keep: int = 200) -> None:
    files = sorted(config.LOG_DIR.glob("job-*.log"), key=lambda p: p.stat().st_mtime)
    for p in files[:-keep]:
        p.unlink()


def _accepts_log(fn) -> bool:
    import inspect
    try:
        return "log" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _public(j: dict, since: int | None = 0) -> dict:
    """Snapshot of a job. Caller holds _lock. `since` is an absolute line offset (None = no log lines)."""
    d = {k: v for k, v in j.items() if k not in ("log", "log_base")}
    base = j["log_base"]
    d["log_len"] = base + len(j["log"])
    d["log"] = [] if since is None else j["log"][max(0, int(since) - base):]
    if not isinstance(d["result"], (int, float, str, type(None), bool)):
        d["result"] = None
    return d


def get(jid: int, since: int = 0) -> dict | None:
    with _lock:
        j = _jobs.get(jid)
        return _public(j, since) if j else None


def recent() -> list[dict]:
    with _lock:
        return [_public(j, None) for j in reversed(list(_jobs.values()))]


def busy(lane: str | None = None) -> bool:
    with _lock:
        return any(j["status"] in ("queued", "running") and (lane is None or j.get("lane") == lane)
                   for j in _jobs.values())
