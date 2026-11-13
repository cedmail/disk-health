"""Live progress of running jobs (scan, clean, brew ...), shared with the dashboard and the extension.

Each job has its own progress state; the job runner binds the current thread to its job id, so code anywhere
(scanner, caches, features) can call start()/update()/step() without knowing which job it runs in.
Every job gets a "generic" progress (title + latest log line); long jobs call start() for real numbers.
"""
from __future__ import annotations

import threading
import time

_lock = threading.Lock()
_states: dict[int, dict] = {}
_tl = threading.local()


def bind(job_id: int | None) -> None:
    _tl.job_id = job_id or 0


def _key() -> int:
    return getattr(_tl, "job_id", 0) or 0


def start(steps: list[str], title: str = "Scanning", weights: list[float] | None = None, generic: bool = False,
          **extra) -> None:
    k = _key()
    with _lock:
        prev = _states.get(k, {})
        st = {"active": True, "started": prev.get("started") if prev.get("active") else time.time(),
              "title": title, "steps": steps, "step": 0,
              "weights": weights or [1 / len(steps)] * len(steps), "generic": generic,
              "job_id": prev.get("job_id") or k or None, "kind": prev.get("kind"), "lane": prev.get("lane", "heavy"),
              "detail": "", "current": "", "dirs": 0, "kb": 0, "expected_kb": 0,
              "counter": 0, "counter_label": "", "kb_label": "counted",
              "root": "", "root_index": 0, "root_count": 0, "biggest": [], "items_done": 0, "items_total": 0}
        st["started"] = st["started"] or time.time()
        st.update(extra)
        _states[k] = st


def step(i: int, detail: str = "") -> None:
    with _lock:
        _states.setdefault(_key(), {}).update({"step": i, "detail": detail, "current": "", "items_done": 0,
                                               "items_total": 0})


def update(**kw) -> None:
    with _lock:
        _states.setdefault(_key(), {}).update(kw)


def log_line(line: str) -> None:
    """Called for every job log line: shown as 'current' for jobs without detailed progress."""
    with _lock:
        s = _states.get(_key())
        if s and s.get("active") and s.get("generic") and line.strip():
            s["current"] = line.strip()[:300]


def add_biggest(path: str, kb: int, keep: int = 8) -> None:
    with _lock:
        s = _states.setdefault(_key(), {})
        b = [x for x in s.get("biggest", []) if x["path"] != path] + [{"path": path, "kb": kb}]
        b.sort(key=lambda x: x["kb"], reverse=True)
        s["biggest"] = b[:keep]


def finish() -> None:
    with _lock:
        _states.pop(_key(), None)


def _view(s: dict) -> dict:
    s = dict(s)
    now = time.time()
    s["elapsed"] = round(now - s.get("started", now), 1)
    pct = None
    if s.get("step") == 0 and s.get("expected_kb"):
        pct = min(0.99, s.get("kb", 0) / s["expected_kb"])
    elif s.get("items_total"):
        pct = min(0.99, s.get("items_done", 0) / s["items_total"])
    s["step_pct"] = pct
    w = s.get("weights") or [1]
    i = s.get("step", 0)
    done = sum(w[:i]) + (w[i] * (pct or 0) if i < len(w) else 0)
    s["indeterminate"] = bool(s.get("generic")) or (pct is None and i == 0)
    s["pct"] = None if s["indeterminate"] else round(min(0.99, done), 3)
    s["eta"] = round(s["elapsed"] / done - s["elapsed"]) if (done > 0.03 and not s["indeterminate"]) else None
    return s


def get() -> dict:
    """The main running job (heavy lane first), plus brief info on other running jobs in `others`."""
    with _lock:
        states = [dict(s) for s in _states.values() if s.get("active")]
    if not states:
        return {"active": False, "others": []}
    states.sort(key=lambda s: (s.get("lane") != "heavy", s.get("started", 0)))
    main = _view(states[0])
    main["others"] = [{k: v[k] for k in ("job_id", "title", "pct", "current", "elapsed") if k in v}
                      for v in map(_view, states[1:])]
    return main


def current() -> dict:
    """Progress state of the job bound to this thread (for code that reads its own counters)."""
    with _lock:
        return dict(_states.get(_key(), {}))
