"""Seed a realistic demo database for the dashboard (a macOS developer laptop, 1 TB disk).

Usage:
    python3 tests/demo/seed_demo.py [DATA_DIR]

DISK_HEALTH_DATA must point at a throwaway directory *before* diskhealth is imported,
so this module sets it from argv / a temp dir and only then imports the package.
"""
from __future__ import annotations

import math
import os
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

HOME = "/Users/cedric"
TOTAL = 994_662_584_320  # what a "1 TB" Apple SSD reports
GIB = 1024 ** 3
KB_PER_GB = 1024 ** 2
FINAL_FREE_GB = 128.4  # ~13.8 % free -> below the 15 % warning threshold

# Leaf folders: path -> (size in GB on the latest scan, growth per day in GB, first day it exists or None)
# "Days" count backwards: 0 is the latest scan, 19 the oldest.
LEAVES = {
    # Docker VM disk: the abnormal one (accelerating over the last week)
    f"{HOME}/Library/Containers/com.docker.docker/Data/vms/0/data": ("docker", 64.0),
    f"{HOME}/.m2/repository/org/jahia": (9.6, 0.12),
    f"{HOME}/.m2/repository/org/apache": (3.1, 0.02),
    f"{HOME}/.m2/repository/com": (2.4, 0.01),
    f"{HOME}/Library/Caches/JetBrains/IntelliJIdea2026.2": (7.8, 0.18),
    f"{HOME}/Library/Caches/Google/Chrome": (2.9, 0.03),
    f"{HOME}/Library/Caches/Homebrew": (4.2, 0.06),
    f"{HOME}/Library/Caches/Yarn": (1.6, 0.0),
    f"{HOME}/Library/Application Support/Slack/Cache": (1.9, 0.05),
    f"{HOME}/Library/Application Support/Slack/IndexedDB": (0.9, 0.01),
    f"{HOME}/Library/Application Support/Code/User": (1.2, 0.01),
    f"{HOME}/Library/Developer/Xcode/DerivedData": (11.4, 0.0),
    f"{HOME}/Library/Developer/CoreSimulator/Devices": (18.7, 0.0),
    f"{HOME}/Library/Group Containers/UBF8T346G9.Office": (3.4, 0.0),
    f"{HOME}/projects/jahia-modules/jcontent/node_modules": (1.8, 0.0),
    f"{HOME}/projects/jahia-modules/jcontent/target": (0.9, 0.04),
    f"{HOME}/projects/jahia-modules/graphql-dxm-provider/target": (0.6, 0.02),
    f"{HOME}/projects/jahia-modules/abeilles-site/node_modules": (1.1, 0.0),
    f"{HOME}/projects/jahia-modules/abeilles-site/target": (0.7, 0.05),
    f"{HOME}/projects/jahia-docker/volumes/jahia-data": (12.3, 0.22),
    f"{HOME}/projects/jahia-docker/volumes/mariadb": (4.6, 0.08),
    f"{HOME}/Downloads/jahia-8.2.1.0-export": ("new", 6.2),
    f"{HOME}/Downloads/installers": (8.8, 0.0),
    f"{HOME}/Pictures/Photos Library.photoslibrary": (38.5, 0.04),
    f"{HOME}/Movies/recordings": (14.2, 0.0),
    f"{HOME}/Documents/customers": (9.4, 0.02),
    f"{HOME}/.npm/_cacache": (3.3, 0.04),
    f"{HOME}/.gradle/caches": (2.1, 0.0),
    f"{HOME}/.Trash": (1.4, 0.0),
    "/Applications/Xcode.app/Contents/Developer": (11.9, 0.0),
    "/Applications/Xcode.app/Contents/Applications": (0.6, 0.0),
    "/Applications/IntelliJ IDEA.app/Contents": (3.6, 0.0),
    "/Applications/Docker.app/Contents": (2.1, 0.0),
    "/Applications/Microsoft Word.app/Contents": (2.3, 0.0),
    "/Applications/Microsoft Excel.app/Contents": (2.0, 0.0),
    "/Applications/Slack.app/Contents": (0.5, 0.0),
    "/Applications/GarageBand.app/Contents": (1.9, 0.0),
    "/Applications/Final Cut Pro Trial.app/Contents": (5.4, 0.0),
    "/Library/Developer/CommandLineTools": (4.1, 0.0),
    "/Library/Application Support/Apple": (2.6, 0.0),
    "/Library/Audio/Apple Loops": (8.9, 0.0),
    "/opt/homebrew/Cellar/openjdk@17": (0.36, 0.0),
    "/opt/homebrew/Cellar/node": (0.21, 0.0),
    "/opt/homebrew/Cellar/maven": (0.12, 0.0),
    "/opt/homebrew/Cellar/imagemagick": (0.31, 0.0),
    "/opt/homebrew/Cellar/ffmpeg": (0.26, 0.0),
    "/opt/homebrew/Cellar/llvm": (1.7, 0.0),
    "/opt/homebrew/Caskroom": (0.9, 0.0),
    "/opt/homebrew/Library": (0.6, 0.0),
}
# Space directly inside a folder that isn't in a recorded child (small files, sub-threshold dirs), in GB.
OWN = {
    HOME: 4.0, f"{HOME}/Library": 6.5, f"{HOME}/Library/Caches": 3.2, f"{HOME}/projects": 0.8,
    f"{HOME}/Documents": 2.1, f"{HOME}/Downloads": 1.3, "/Applications": 1.4, "/Library": 3.3,
    "/opt/homebrew": 0.4, "/opt/homebrew/Cellar": 0.8,
}
ROOTS = [HOME, "/Applications", "/Library", "/opt/homebrew"]
N_SNAPS = 20


def leaf_gb(path: str, spec, day_back: int) -> float | None:
    a, b = spec
    if a == "docker":  # slow growth, then +24 GB in the last 7 days
        if day_back >= 7:
            return 30.0 + (19 - day_back) * 0.8 + random.uniform(-0.1, 0.1)
        return 40.0 + (7 - day_back) / 7 * (b - 40.0)
    if a == "new":
        return b if day_back <= 4 else None
    return max(0.05, a - day_back * b + random.uniform(-0.02, 0.02))


def root_of(path: str) -> str:
    return next(r for r in sorted(ROOTS, key=len, reverse=True) if path == r or path.startswith(r + "/"))


def snapshot_sizes(day_back: int) -> dict[str, int]:
    gb: dict[str, float] = {}
    for p, spec in LEAVES.items():
        v = leaf_gb(p, spec, day_back)
        if v is None:
            continue
        gb[p] = gb.get(p, 0) + v
        root = root_of(p)
        q = p
        while q != root:  # add to every ancestor up to the scan root
            q = q.rsplit("/", 1)[0]
            gb[q] = gb.get(q, 0) + v
    for p, extra in OWN.items():
        if p in gb or p in ROOTS:
            root = root_of(p)
            gb[p] = gb.get(p, 0) + extra
            q = p
            while q != root:
                q = q.rsplit("/", 1)[0]
                gb[q] = gb.get(q, 0) + extra
    return {p: int(v * KB_PER_GB) for p, v in gb.items() if v >= 0.05}


def seed(data_dir: str | None = None) -> str:
    data_dir = data_dir or os.environ.get("DISK_HEALTH_DATA") or tempfile.mkdtemp(prefix="dh-demo-")
    os.environ["DISK_HEALTH_DATA"] = data_dir
    from diskhealth import config, db  # noqa: E402  (after env is set)
    assert str(config.DATA_DIR) == data_dir, (config.DATA_DIR, data_dir)
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    db._conn = None
    random.seed(7)
    now = int(time.time())
    day = 86400

    # ---- free space: 90 days hourly, declining, with two clean-up jumps
    rows = []
    start_free = 236.0
    hours = 90 * 24
    for h in range(hours + 1):
        ts = now - (hours - h) * 3600
        frac = h / hours
        free = start_free - frac * 160.0                   # steady decline (~1.8 GB/day)
        free += 0 if frac < 0.33 else 31.0                 # brew cleanup + DerivedData purge
        free += 0 if frac < 0.72 else 22.0                 # Trash emptied / Docker prune
        free -= max(0.0, (frac - 0.92) / 0.08) * 22.0      # Docker growth spurt, last week
        free += 2.2 * math.sin(h / 24 * 2 * math.pi) + random.uniform(-0.6, 0.6)
        rows.append((ts, TOTAL, 0, 0, free))
    # pin the last sample at FINAL_FREE_GB and keep everything consistent
    shift = FINAL_FREE_GB - rows[-1][4]
    out = []
    for i, (ts, total, _u, _f, free) in enumerate(rows):
        f = int((free + shift * i / len(rows)) * GIB)
        out.append((ts, total, total - f, f))
    db.executemany("INSERT INTO free_samples VALUES (?,?,?,?)", out)

    # ---- daily snapshots
    roots = ",".join(ROOTS)
    for d in range(N_SNAPS - 1, -1, -1):
        ts = now - d * day - 3 * 3600 - random.randint(0, 900)
        sid = db.execute("INSERT INTO snapshots (ts, duration_s, roots) VALUES (?,?,?)",
                         (ts, round(random.uniform(140, 260), 1), roots)).lastrowid
        sizes = snapshot_sizes(d)
        db.executemany("INSERT INTO sizes VALUES (?,?,?,?)",
                       [(sid, p, p.count("/") - root_of(p).rstrip("/").count("/"), kb) for p, kb in sizes.items()])

    # ---- caches
    def c(id_, name, group, risk, gb, paths, note=None, detail=None):
        return {"id": id_, "name": name, "group": group, "risk": risk, "note": note,
                "kb": int(gb * KB_PER_GB), "paths": paths, "detail": detail}
    caches = [
        c("docker", "Docker (reclaimable)", "Developer tools", "rebuild", 21.6,
          [f"{HOME}/Library/Containers/com.docker.docker/Data/vms/0/data"],
          "docker system prune: stopped containers, dangling images, unused networks, build cache. Volumes are kept.",
          "14 dangling images, 6 stopped containers, 9.8 GB build cache"),
        c("xcode-derived", "Xcode DerivedData", "Developer tools", "rebuild", 11.4,
          [f"{HOME}/Library/Developer/Xcode/DerivedData"]),
        c("jetbrains", "JetBrains IDE caches & logs", "Developer tools", "rebuild", 8.3,
          [f"{HOME}/Library/Caches/JetBrains", f"{HOME}/Library/Logs/JetBrains"],
          "Close IntelliJ first. Indexes are rebuilt on next open."),
        c("stale-builds", "Stale build folders (node_modules, target)", "Developer tools", "rebuild", 2.9, [],
          "In projects untouched for 60+ days. Run npm install / mvn package to recreate.",
          "5 folders in 3 projects"),
        c("maven", "Maven local repository", "Package managers", "redownload", 15.1, [f"{HOME}/.m2/repository"],
          "Everything is re-downloaded on the next build (slow first Jahia module build). Unpublished local installs are lost."),
        c("brew", "Homebrew downloads", "Package managers", "safe", 4.2, [f"{HOME}/Library/Caches/Homebrew"],
          "Old bottles and downloads. Installed packages are untouched."),
        c("npm", "npm cache", "Package managers", "redownload", 3.3, [f"{HOME}/.npm/_cacache"]),
        c("gradle", "Gradle caches", "Package managers", "redownload", 2.1, [f"{HOME}/.gradle/caches"]),
        c("yarn", "Yarn cache", "Package managers", "redownload", 1.6, [f"{HOME}/Library/Caches/Yarn"]),
        c("chrome", "Chrome cache", "Apps", "safe", 2.9, [f"{HOME}/Library/Caches/Google/Chrome"],
          "Pages may load slower for a while."),
        c("slack", "Slack cache", "Apps", "safe", 1.9,
          [f"{HOME}/Library/Application Support/Slack/Cache",
           f"{HOME}/Library/Application Support/Slack/Service Worker/CacheStorage"], "Quit Slack first."),
        c("teams", "Microsoft Teams cache", "Apps", "safe", 0.7,
          [f"{HOME}/Library/Containers/com.microsoft.teams2/Data/Library/Caches"], "Quit Teams first."),
        c("logs", "User logs", "System", "safe", 0.4, [f"{HOME}/Library/Logs"]),
        c("other-caches", "Other app caches (~/Library/Caches)", "System", "safe", 3.2, [f"{HOME}/Library/Caches"],
          "Everything in ~/Library/Caches not listed above. Quit apps first; they rebuild their cache."),
        c("trash", "Trash", "System", "safe", 1.4, [f"{HOME}/.Trash"], "Permanent."),
    ]
    caches.sort(key=lambda i: i["kb"], reverse=True)
    db.kv_set("caches", {"items": caches, "total_kb": sum(i["kb"] for i in caches), "ts": now - 3 * 3600})

    # ---- apps
    def app(name, bid, ver, gb, last_days, inst_days, source, cask=None):
        last = now - last_days * day if last_days is not None else None
        inst = now - inst_days * day
        idle = last_days if last_days is not None else inst_days
        return {"path": f"/Applications/{name}.app", "name": name, "bundle_id": bid, "version": ver,
                "kb": int(gb * KB_PER_GB), "last_used": last, "installed": inst, "idle_days": idle,
                "source": source, "cask": cask, "unused": idle >= 90}
    apps = [
        app("Xcode", "com.apple.dt.Xcode", "26.0.1", 12.5, 142, 400, "appstore"),
        app("Final Cut Pro Trial", "com.apple.FinalCutTrial", "11.1", 5.4, None, 260, "manual"),
        app("GarageBand", "com.apple.garageband10", "10.4.12", 1.9, 410, 700, "appstore"),
        app("Microsoft Excel", "com.microsoft.Excel", "16.101", 2.0, 121, 500, "manual"),
        app("Postman", "com.postmanlabs.mac", "11.62.0", 0.62, 188, 320, "brew", "postman"),
        app("Zoom", "us.zoom.xos", "6.6.2", 0.41, 97, 600, "brew", "zoom"),
        app("IntelliJ IDEA", "com.jetbrains.intellij", "2026.2.3", 3.6, 0, 300, "brew", "intellij-idea"),
        app("Docker", "com.docker.docker", "4.47.0", 2.1, 0, 420, "brew", "docker-desktop"),
        app("Microsoft Word", "com.microsoft.Word", "16.101", 2.3, 6, 500, "manual"),
        app("Slack", "com.tinyspeck.slackmacgap", "4.46.99", 0.5, 0, 650, "appstore"),
        app("Google Chrome", "com.google.Chrome", "141.0.7390.66", 1.4, 0, 900, "brew", "google-chrome"),
        app("Visual Studio Code", "com.microsoft.VSCode", "1.105.0", 0.68, 2, 500, "brew", "visual-studio-code"),
        app("iTerm", "com.googlecode.iterm2", "3.6.4", 0.11, 0, 800, "brew", "iterm2"),
        app("Obsidian", "md.obsidian", "1.9.14", 0.36, 1, 300, "brew", "obsidian"),
    ]
    apps.sort(key=lambda i: (not i["unused"], -i["kb"]))
    db.kv_set("apps", {"items": apps, "ts": now - 3 * 3600,
                       "unused_kb": sum(i["kb"] for i in apps if i["unused"]),
                       "unused_count": sum(1 for i in apps if i["unused"])})

    # ---- brew
    def leaf(name, desc, mb, bins, last_days, uses, inst_days):
        last = now - last_days * day if last_days is not None else None
        ref = last or (now - inst_days * day)
        idle = int((now - ref) / day)
        return {"name": name, "desc": desc, "kb": int(mb * 1024), "bins": bins[:8], "bin_count": len(bins),
                "last_seen": last, "uses": uses, "installed": now - inst_days * day, "idle_days": idle,
                "unused": bool(bins) and idle >= 120}
    leaves = [
        leaf("llvm", "Next-gen compiler infrastructure", 1740, ["clang", "clang++", "lld", "llvm-ar"], None, 0, 380),
        leaf("ffmpeg", "Play, record, convert, and stream audio and video", 266, ["ffmpeg", "ffprobe"], 233, 4, 500),
        leaf("hugo", "Configurable static site generator", 92, ["hugo"], 301, 12, 640),
        leaf("openjdk@17", "Development kit for the Java programming language", 368, ["java", "javac", "jar"], 0, 812, 700),
        leaf("imagemagick", "Tools and libraries to manipulate images in many formats", 318, ["magick", "convert"], 12, 37, 420),
        leaf("node", "Platform built on V8 to build network applications", 214, ["node", "npm", "npx"], 0, 2210, 900),
        leaf("maven", "Java-based project management", 12, ["mvn"], 0, 3402, 900),
        leaf("jq", "Lightweight and flexible command-line JSON processor", 1.2, ["jq"], 1, 588, 900),
        leaf("gh", "GitHub command-line tool", 46, ["gh"], 3, 140, 700),
        leaf("libpq", "Postgres C API library", 9.8, [], None, 0, 400),
    ]
    leaves.sort(key=lambda i: (not i["unused"], -i["kb"]))
    db.kv_set("brew", {
        "available": True, "prefix": "/opt/homebrew",
        "outdated": [
            {"name": "node", "kind": "formula", "from": "24.8.0", "to": "24.10.0", "pinned": False},
            {"name": "gh", "kind": "formula", "from": "2.80.0", "to": "2.81.0", "pinned": False},
            {"name": "imagemagick", "kind": "formula", "from": "7.1.2-3", "to": "7.1.2-5", "pinned": False},
            {"name": "openjdk@17", "kind": "formula", "from": "17.0.16", "to": "17.0.17", "pinned": True},
            {"name": "google-chrome", "kind": "cask", "from": "141.0.7390.54", "to": "141.0.7390.66", "pinned": False},
            {"name": "docker-desktop", "kind": "cask", "from": "4.46.0", "to": "4.47.0", "pinned": False},
        ],
        "orphans": ["python@3.12", "libheif", "little-cms2"],
        "doctor_ok": False,
        "doctor": [
            {"title": "Some installed formulae are deprecated or disabled.",
             "body": "You should find replacements for the following formulae:\n  hugo@0.111"},
            {"title": "You have unlinked kegs in your Cellar.",
             "body": "Leaving kegs unlinked can lead to build-trouble and cause formulae that depend on\n"
                     "those kegs to fail to run properly once built. Run `brew link` on these:\n  openjdk@21"},
        ],
        "cleanup_reclaimable": "1.4GB", "leaves": leaves,
        "unused_leaves": sum(1 for l in leaves if l["unused"]),
        "omz": {"installed": True, "dir": f"{HOME}/.oh-my-zsh", "last_update": now - 16 * day},
        "ts": now - 5 * 3600,
    })

    mi = f"{HOME}/.local/share/mise/installs"
    db.kv_set("mise", {
        "available": True, "version": "2026.11.0", "bin": "/opt/homebrew/bin/mise", "install_method": "Homebrew",
        "self_update": False, "data_dir": f"{HOME}/.local/share/mise", "installs_kb": int(6.8 * KB_PER_GB), "versions": 11,
        "tools": [{"tool": "java", "versions": ["temurin-17.0.12+7", "temurin-21.0.5+11.0.LTS", "temurin-11.0.24+8"], "kb": int(3.9 * KB_PER_GB)},
                  {"tool": "node", "versions": ["18.20.4", "20.18.0", "22.11.0"], "kb": int(1.6 * KB_PER_GB)},
                  {"tool": "python", "versions": ["3.11.10", "3.12.7"], "kb": int(0.9 * KB_PER_GB)},
                  {"tool": "maven", "versions": ["3.9.9", "3.8.8"], "kb": int(0.03 * KB_PER_GB)},
                  {"tool": "npm-prettier", "versions": ["3.3.3"], "kb": 18000}],
        "cache_dir": f"{HOME}/Library/Caches/mise", "cache_kb": int(0.7 * KB_PER_GB),
        "projects": [f"{HOME}/projects/jahia-core", f"{HOME}/projects/site-builder", f"{HOME}/projects/infra"],
        "outdated": [{"name": "node", "requested": "20", "current": "20.18.0", "latest": "20.19.5", "sources": [f"{HOME}/projects/site-builder/mise.toml"], "dirs": [f"{HOME}/projects/site-builder"]},
                     {"name": "java", "requested": "temurin-21", "current": "temurin-21.0.5+11.0.LTS", "latest": "temurin-21.0.9+10.0.LTS", "sources": [f"{HOME}/.config/mise/config.toml"], "dirs": [HOME]}],
        "prunable": [{"tool": "java", "version": "temurin-11.0.24+8", "path": f"{mi}/java/temurin-11.0.24+8", "kb": int(1.2 * KB_PER_GB)},
                     {"tool": "node", "version": "18.20.4", "path": f"{mi}/node/18.20.4", "kb": int(0.5 * KB_PER_GB)},
                     {"tool": "maven", "version": "3.8.8", "path": f"{mi}/maven/3.8.8", "kb": 10000}],
        "prunable_kb": int(1.71 * KB_PER_GB), "doctor": {"ok": True, "problems": [], "exit": 0}, "ts": now - 3 * 3600,
    })

    db.kv_set("last_full_scan", {"ts": now - 3 * 3600})
    acts = [
        (now - 2 * day, "clean-cache", "chrome", 1, int(2.4 * KB_PER_GB), ""),
        (now - 9 * day, "brew-uninstall", "terraform", 1, int(0.31 * KB_PER_GB), ""),
        (now - 12 * day, "uninstall-app", "/Applications/Figma.app", 1, int(0.52 * KB_PER_GB), ""),
        (now - 18 * day, "clean-cache", "docker", 1, int(19.7 * KB_PER_GB), ""),
        (now - 18 * day - 600, "clean-cache", "xcode-derived", 0, 0, "Permission denied"),
        (now - 40 * day, "clean-cache", "trash", 1, int(6.1 * KB_PER_GB), ""),
    ]
    db.executemany("INSERT INTO actions VALUES (?,?,?,?,?,?)", acts)
    return data_dir


if __name__ == "__main__":
    d = seed(sys.argv[1] if len(sys.argv) > 1 else None)
    print(d)
