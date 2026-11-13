"""Seed demo data and serve the dashboard on http://127.0.0.1:7777 (no scheduler, no real actions).

    python3 tests/demo/run_demo.py [--port 7777] [--data DIR]

Everything that would touch the real machine is stubbed:
  * scanner.sample_free returns the demo's latest free-space sample (1 TB disk, ~128 GB free),
  * config.HOME is /Users/cedric so paths render as ~/...,
  * every ACTIONS entry is a fake job that streams a few log lines,
  * apps.leftovers returns canned paths.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7777)
    ap.add_argument("--data", default=None)
    ap.add_argument("--empty", action="store_true", help="fresh install: no samples, scans or checks yet")
    args = ap.parse_args()

    data_dir = args.data or tempfile.mkdtemp(prefix="dh-demo-")
    os.environ["DISK_HEALTH_DATA"] = data_dir
    import seed_demo  # noqa: E402
    if not args.empty:
        seed_demo.seed(data_dir)

    from http.server import ThreadingHTTPServer
    from diskhealth import apps, config, db, jobs, scanner, server

    config.HOME = Path(seed_demo.HOME)
    rows = db.query("SELECT ts, total, used, free FROM free_samples ORDER BY ts DESC LIMIT 1")
    free = int(seed_demo.FINAL_FREE_GB * seed_demo.GIB)
    latest = rows[0] if rows else {"total": seed_demo.TOTAL, "free": free, "used": seed_demo.TOTAL - free}

    def fake_sample_free(record: bool = True) -> dict:
        return {"ts": int(time.time()), "total": latest["total"], "used": latest["used"], "free": latest["free"]}
    scanner.sample_free = fake_sample_free

    apps.leftovers = lambda p: [
        {"path": f"{seed_demo.HOME}/Library/Application Support/{Path(p).stem}", "kb": 412_000},
        {"path": f"{seed_demo.HOME}/Library/Caches/{Path(p).stem}", "kb": 96_000},
        {"path": f"{seed_demo.HOME}/Library/Preferences/com.example.{Path(p).stem.replace(' ', '')}.plist", "kb": 8},
    ]

    def fake(lines, delay=0.35):
        def fn(target=None, log=print):
            for ln in lines:
                log(ln.format(t=target))
                time.sleep(delay)
            return 0
        return fn

    def job(kind, label, lines, delay=0.35):
        return lambda t: jobs.submit(kind, label.format(t=t), fake(lines, delay), t)

    scan_lines = ["== Disk scan ==", "scanning /Users/cedric (depth 5) ...", "  1,284 folders >= 50 MB recorded",
                  "scanning /Applications (depth 5) ...", "  212 folders >= 50 MB recorded",
                  "scanning /Library (depth 5) ...", "  341 folders >= 50 MB recorded",
                  "scanning /opt/homebrew (depth 5) ...", "  96 folders >= 50 MB recorded",
                  "snapshot #21 done in 184s", "== Caches ==", "  Docker (reclaimable): 21.6 GB",
                  "  Maven local repository: 15.1 GB", "== Applications ==", "  14 apps, 6 unused", "done (demo: nothing changed)"]
    server.ACTIONS = {
        "scan": job("scan", "Full scan", scan_lines, 0.4),
        "refresh": job("refresh", "Refresh caches & apps", ["measuring caches ...", "  Docker (reclaimable): 21.6 GB",
                                                            "scanning applications ...", "done (demo)"]),
        "brew-check": job("brew-check", "Homebrew check", ["== Homebrew check ==", "  brew update", "  brew doctor",
                                                           "  brew cleanup --dry-run", "  leaves + shell history", "done (demo)"]),
        "brewed": job("brewed", "brewed (update, upgrade, doctor, autoremove, cleanup, omz)",
                      ["$ brew update", "Already up-to-date.", "$ brew upgrade -q", "==> Upgrading 5 outdated packages:",
                       "node 24.8.0 -> 24.10.0", "gh 2.80.0 -> 2.81.0", "imagemagick 7.1.2-3 -> 7.1.2-5",
                       "$ brew doctor", "Your system is ready to brew.", "$ brew autoremove", "$ brew cleanup -s --prune=30",
                       "Removing: /Users/cedric/Library/Caches/Homebrew/node--24.8.0... (21.4MB)", "$ omz update",
                       "Oh My Zsh is already at the latest version.", "done (demo: nothing changed)"], 0.5),
        "clean-cache": job("clean", "Clean {t}", ["cleaning {t} ...", "removing contents ...", "freed 0 B (demo)",
                                                  "refreshing measurements ...", "done"]),
        "uninstall-app": job("uninstall", "Uninstall {t}", ["(demo) would move {t} to the Trash", "done"]),
        "reinstall-app": job("reinstall", "Clean reinstall {t}", ["(demo) would reinstall {t}", "done"]),
        "brew-uninstall": job("brew-uninstall", "brew uninstall {t}", ["$ brew uninstall {t}", "(demo) skipped", "done"]),
        "brew-reinstall": job("brew-reinstall", "brew reinstall {t}", ["$ brew reinstall {t}", "(demo) skipped", "done"]),
    }
    # One finished job so "Recent jobs" isn't empty.
    if not args.empty:
        jobs.submit("scan", "Scheduled full scan", fake(["== Disk scan ==", "done"], 0))
        jobs.submit("brew-check", "Scheduled Homebrew check", fake(["done"], 0))

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), server.Handler)
    print(f"demo dashboard on http://127.0.0.1:{args.port}  (data: {data_dir})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
