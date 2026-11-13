"""Optional feature modules (one panel each). The server discovers them here.

A feature module may define (all optional except NAME):

    NAME = "docker"                  # id; also the dashboard section id and the overview key
    KEY = "docker"                   # kv key whose value is sent to the dashboard as overview["features"][NAME]
    SCAN_LABEL = "Docker"            # if scan() exists: label of its step in the daily full scan
    def scan(log=print) -> dict      # measure + db.kv_set(KEY, result); runs inside the full scan (and on Refresh)
    ACTIONS = {                      # POST /api/run {"action": name, "target": ...}
        "docker-prune-images": {"label": lambda t: "Remove unused images", "fn": prune_images,
                                "target": False, "lane": "heavy"},
    }                                # fn(log=...) when target is False, fn(target, log=...) when True
    ROUTES = {"/api/docker/details": lambda q: {...}}   # extra GET endpoints (q = query dict)
    def alerts(cfg) -> list[dict]    # same shape as alerts.compute() items
    def tick(now, cfg) -> None       # called every minute by the scheduler; must be quick

Long loops should call jobs.checkpoint() (cancel support) and use progress.start/update/step for live numbers.
Run commands with util.run / util.run_out / util.stream so Cancel can kill them.
The dashboard side lives in web/static/panel-<NAME>.js (see web/index.html: DH.registerPanel).
"""
from __future__ import annotations

import importlib
import pkgutil

_loaded: list | None = None


def modules() -> list:
    global _loaded
    if _loaded is None:
        _loaded = []
        for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
            if info.name.startswith("_"):
                continue
            try:
                m = importlib.import_module(f"{__name__}.{info.name}")
            except Exception as ex:  # a broken feature must never take the service down
                print(f"[features] {info.name} disabled: {ex}", flush=True)
                continue
            if getattr(m, "NAME", None):
                _loaded.append(m)
    return _loaded
