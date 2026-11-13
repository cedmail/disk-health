"""Turn the latest results into alerts. Stable ids let the Chrome extension notify only once per alert."""
from __future__ import annotations

import hashlib
import time

from . import config, db, scanner
from .util import human


def _h(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:10]


def compute(growth7: dict | None = None) -> list[dict]:
    cfg = config.load()
    out: list[dict] = []
    week = int(time.time() // (7 * 86400))

    s = scanner.sample_free(record=False)
    pct = 100 * s["free"] / s["total"] if s["total"] else 100
    if pct < cfg["free_critical_percent"]:
        out.append({"id": f"free:critical:{week}", "level": "critical", "notify": True,
                    "title": f"Disk almost full: {human(s['free'] / 1024)} free ({pct:.0f}%)", "section": "overview"})
    elif pct < cfg["free_warn_percent"]:
        out.append({"id": f"free:warning:{week}", "level": "warning", "notify": True,
                    "title": f"Low disk space: {human(s['free'] / 1024)} free ({pct:.0f}%)", "section": "overview"})

    g = growth7 if growth7 is not None else scanner.growth(7)
    for item in g.get("items", []):
        if item.get("abnormal"):
            out.append({"id": f"growth:{_h(item['path'], g['to_ts'])}", "level": "warning", "notify": True,
                        "title": f"{item['path'].replace(str(config.HOME), '~')} grew {human(item['delta_kb'])} "
                                 f"in {g['window_days']:.0f} days", "section": "growth"})

    c = db.kv_get("caches") or {}
    if c.get("total_kb", 0) >= cfg["cache_alert_gb"] * 1024 ** 2:
        out.append({"id": f"caches:{week}", "level": "notice", "notify": True,
                    "title": f"{human(c['total_kb'])} of caches can be cleaned", "section": "caches"})

    t = db.kv_get("trash") or {}
    if t.get("total_kb", 0) >= cfg.get("trash_alert_gb", 2) * 1024 ** 2:
        out.append({"id": f"trash:{week}", "level": "notice", "notify": True,
                    "title": f"{human(t['total_kb'])} in the Trash can be freed", "section": "trash"})

    b = db.kv_get("brew") or {}
    if b.get("outdated"):
        names = sorted(str(o.get("name")) for o in b["outdated"])
        out.append({"id": f"brew:outdated:{_h(*names)}", "level": "notice", "notify": True,
                    "title": f"brew: {len(names)} outdated" + (f", {len(b.get('doctor', []))} doctor warning(s)"
                                                               if b.get("doctor") else ""), "section": "brew"})
    elif b.get("doctor"):
        out.append({"id": f"brew:doctor:{_h(*[w['title'] for w in b['doctor']])}", "level": "notice", "notify": True,
                    "title": f"brew doctor: {len(b['doctor'])} warning(s)", "section": "brew"})
    if b.get("orphans"):
        out.append({"id": f"brew:orphans:{_h(*b['orphans'])}", "level": "notice", "notify": False,
                    "title": f"brew: {len(b['orphans'])} unneeded dependencies", "section": "brew"})

    a = db.kv_get("apps") or {}
    if a.get("unused_count"):
        out.append({"id": f"apps:{week}:{a['unused_count']}", "level": "info", "notify": False,
                    "title": f"{a['unused_count']} apps unused for {cfg['unused_app_days']}+ days "
                             f"({human(a['unused_kb'])})", "section": "apps"})
    if b.get("unused_leaves"):
        out.append({"id": f"tools:{week}:{b['unused_leaves']}", "level": "info", "notify": False,
                    "title": f"{b['unused_leaves']} brew tools not used for {cfg['unused_tool_days']}+ days",
                    "section": "brew"})
    return out
