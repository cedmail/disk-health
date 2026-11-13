"""Registry of known caches: measure them and clean them safely.

Each entry:
  id, name, group, paths (globs, ~ allowed), clean ("contents" | "cmd" | "docker" | "stale_builds" | "selection"),
  cmd (list, for clean="cmd"), needs (binary that must exist), risk, note
  clean="selection": `selector` names a function of SELECTORS that picks folders (old Maven/Gradle versions ...);
  `parent` is the entry whose folder contains them (so the total doesn't count them twice).
Risk levels: "safe" (pure cache), "rebuild" (rebuilt on next build/launch), "redownload" (re-fetched from network).
Add your own entries in config.json under "extra_caches".
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
from pathlib import Path

from . import config, db, progress
from .util import (DeleteTracker, du_kb, have, human, is_under, parse_du, remove_contents, remove_path, run,
                   run_out, same_dir, stream)

REGISTRY: list[dict] = [
    # --- package managers
    {"id": "brew", "name": "Homebrew downloads", "group": "Package managers", "paths": ["~/Library/Caches/Homebrew"],
     "clean": "cmd", "cmd": ["brew", "cleanup", "-s", "--prune=all"], "needs": "brew", "risk": "safe",
     "note": "Old bottles and downloads. Installed packages are untouched."},
    {"id": "npm", "name": "npm cache", "group": "Package managers", "paths": ["~/.npm/_cacache"],
     "clean": "cmd", "cmd": ["npm", "cache", "clean", "--force"], "needs": "npm", "risk": "redownload"},
    {"id": "yarn", "name": "Yarn cache", "group": "Package managers", "paths": ["~/Library/Caches/Yarn"],
     "clean": "contents", "risk": "redownload"},
    {"id": "pnpm", "name": "pnpm store", "group": "Package managers", "paths": ["~/Library/pnpm/store"],
     "clean": "cmd", "cmd": ["pnpm", "store", "prune"], "needs": "pnpm", "risk": "safe",
     "note": "Prune only removes packages no project references."},
    {"id": "pip", "name": "pip cache", "group": "Package managers", "paths": ["~/Library/Caches/pip"],
     "clean": "contents", "risk": "redownload"},
    {"id": "uv", "name": "uv cache", "group": "Package managers", "paths": ["~/.cache/uv"],
     "clean": "cmd", "cmd": ["uv", "cache", "clean"], "needs": "uv", "risk": "redownload"},
    {"id": "mise", "name": "mise cache", "group": "Package managers", "paths": ["~/Library/Caches/mise"],
     "clean": "cmd", "cmd": ["mise", "cache", "clear"], "needs": "mise", "risk": "redownload",
     "note": "Version lists and downloads; installed tools are not touched (old versions: see Homebrew & mise)."},
    {"id": "maven", "name": "Maven local repository", "group": "Package managers", "paths": ["~/.m2/repository"],
     "clean": "contents", "risk": "redownload",
     "note": "Everything is re-downloaded on the next build (slow first Jahia module build). Unpublished local installs are lost."},
    {"id": "maven-snapshots", "name": "Maven SNAPSHOTs", "group": "Package managers", "paths": [],
     "clean": "selection", "selector": "maven_snapshots", "parent": "maven", "risk": "rebuild",
     "note": "Every *-SNAPSHOT version in ~/.m2/repository: rebuilt by mvn install or re-downloaded from the "
             "snapshot repository. Part of the Maven local repository above."},
    {"id": "maven-old", "name": "Maven: versions unused for {maven_unused_days}+ days", "group": "Package managers",
     "paths": [], "clean": "selection", "selector": "maven_old", "parent": "maven", "risk": "redownload",
     "note": "Released versions no build has read for {maven_unused_days}+ days; the newest version of each artifact "
             "is always kept. Re-downloaded if a build needs them again. Part of the Maven local repository above."},
    {"id": "gradle", "name": "Gradle caches", "group": "Package managers", "paths": ["~/.gradle/caches"],
     "clean": "contents", "risk": "redownload"},
    {"id": "gradle-old", "name": "Gradle: old cache versions", "group": "Package managers", "paths": [],
     "clean": "selection", "selector": "gradle_old", "parent": "gradle", "risk": "redownload",
     "note": "Dependencies unused for {maven_unused_days}+ days (newest version of each kept), caches of older Gradle "
             "versions, and old wrapper distributions (newest per major version kept)."},
    {"id": "go", "name": "Go build cache", "group": "Package managers", "paths": ["~/Library/Caches/go-build"],
     "clean": "cmd", "cmd": ["go", "clean", "-cache"], "needs": "go", "risk": "rebuild"},
    {"id": "cargo", "name": "Cargo registry cache", "group": "Package managers", "paths": ["~/.cargo/registry/cache"],
     "clean": "contents", "risk": "redownload"},
    {"id": "cocoapods", "name": "CocoaPods cache", "group": "Package managers", "paths": ["~/Library/Caches/CocoaPods"],
     "clean": "contents", "risk": "redownload"},
    # --- dev tools
    {"id": "docker", "name": "Docker (reclaimable)", "group": "Developer tools",
     "paths": ["~/Library/Containers/com.docker.docker/Data/vms/0/data"], "clean": "docker", "needs": "docker",
     "risk": "rebuild",
     "note": "docker system prune: stopped containers, dangling images, unused networks, build cache. Volumes are kept."},
    {"id": "xcode-derived", "name": "Xcode DerivedData", "group": "Developer tools",
     "paths": ["~/Library/Developer/Xcode/DerivedData"], "clean": "contents", "risk": "rebuild"},
    {"id": "xcode-devicesupport", "name": "Xcode device support", "group": "Developer tools",
     "paths": ["~/Library/Developer/Xcode/iOS DeviceSupport", "~/Library/Developer/Xcode/watchOS DeviceSupport"],
     "clean": "contents", "risk": "redownload", "note": "Recreated when a device is plugged in."},
    {"id": "simulators", "name": "Simulator caches", "group": "Developer tools",
     "paths": ["~/Library/Developer/CoreSimulator/Caches"], "clean": "contents", "risk": "rebuild"},
    {"id": "jetbrains", "name": "JetBrains IDE caches & logs", "group": "Developer tools",
     "paths": ["~/Library/Caches/JetBrains", "~/Library/Logs/JetBrains"], "clean": "contents", "risk": "rebuild",
     "note": "Close IntelliJ first. Indexes are rebuilt on next open."},
    {"id": "vscode", "name": "VS Code caches", "group": "Developer tools",
     "paths": ["~/Library/Application Support/Code/Cache", "~/Library/Application Support/Code/CachedData",
               "~/Library/Application Support/Code/CachedExtensionVSIXs",
               "~/Library/Application Support/Code/Code Cache"], "clean": "contents", "risk": "safe"},
    {"id": "stale-builds", "name": "Stale build folders (node_modules, target)", "group": "Developer tools",
     "paths": [], "clean": "stale_builds", "risk": "rebuild",
     "note": "In projects untouched for 60+ days. Run npm install / mvn package to recreate."},
    # --- apps
    {"id": "chrome", "name": "Chrome cache", "group": "Apps", "paths": ["~/Library/Caches/Google/Chrome"],
     "clean": "contents", "risk": "safe", "note": "Pages may load slower for a while."},
    {"id": "firefox", "name": "Firefox cache", "group": "Apps", "paths": ["~/Library/Caches/Firefox"],
     "clean": "contents", "risk": "safe"},
    {"id": "slack", "name": "Slack cache", "group": "Apps",
     "paths": ["~/Library/Application Support/Slack/Cache", "~/Library/Application Support/Slack/Service Worker/CacheStorage",
               "~/Library/Containers/com.tinyspeck.slackmacgap/Data/Library/Application Support/Slack/Cache",
               "~/Library/Containers/com.tinyspeck.slackmacgap/Data/Library/Application Support/Slack/Service Worker/CacheStorage"],
     "clean": "contents", "risk": "safe", "note": "Quit Slack first."},
    {"id": "teams", "name": "Microsoft Teams cache", "group": "Apps",
     "paths": ["~/Library/Containers/com.microsoft.teams2/Data/Library/Caches"], "clean": "contents", "risk": "safe",
     "note": "Quit Teams first."},
    {"id": "spotify", "name": "Spotify cache", "group": "Apps", "paths": ["~/Library/Caches/com.spotify.client"],
     "clean": "contents", "risk": "safe"},
    # --- system
    {"id": "logs", "name": "User logs", "group": "System", "paths": ["~/Library/Logs"], "clean": "contents",
     "risk": "safe"},
    {"id": "other-caches", "name": "Other app caches (~/Library/Caches)", "group": "System",
     "paths": ["~/Library/Caches"], "clean": "contents", "risk": "safe",
     "note": "Everything in ~/Library/Caches not listed above. Quit apps first; they rebuild their cache."},
]

PROJECT_ROOTS = ["~/projects", "~/Projects", "~/dev", "~/git", "~/IdeaProjects", "~/src", "~/work"]


def registry() -> list[dict]:
    cfg = config.load()
    days = str(_unused_days(cfg))
    out = []
    for e in REGISTRY + list(cfg.get("extra_caches", [])):
        if "{maven_unused_days}" in e.get("name", "") + (e.get("note") or ""):
            e = dict(e, name=e["name"].replace("{maven_unused_days}", days),
                     note=(e.get("note") or "").replace("{maven_unused_days}", days))
        out.append(e)
    return out


def _unused_days(cfg: dict | None = None) -> int:
    try:
        return max(1, int((cfg or config.load()).get("maven_unused_days", 90)))
    except (TypeError, ValueError):
        return 90


def _paths(entry: dict) -> list[str]:
    out = []
    for p in entry.get("paths", []):
        out.extend(glob.glob(config.expand(p)) or [])
    return [p for p in out if os.path.exists(p)]


def _registered_under(parent: str) -> list[str]:
    parent = parent.rstrip("/") + "/"
    return [p for e in registry() if e["id"] != "other-caches" for p in _paths(e) if p.startswith(parent)]


def _other_caches_children(root: str) -> tuple[list[Path], list[Path]]:
    """Split ~/Library/Caches children into (deletable, kept).

    Kept = registered caches (measured/cleaned by their own entry) AND top-level folders that *contain* a registered
    cache (e.g. "Google" because of Google/Chrome). Those parent folders are skipped wholesale — their other content
    is neither counted nor cleaned by "other-caches" — so measure() and clean() always agree.
    """
    keep = set(_registered_under(root))
    deletable, kept = [], []
    for child in sorted(Path(root).iterdir()) if os.path.isdir(root) else []:
        c = str(child)
        (kept if c in keep or any(k.startswith(c + "/") for k in keep) else deletable).append(child)
    return deletable, kept


# Paths that must never be emptied, whatever a (user-supplied) registry entry says.
_PROTECTED = ["~", "~/Library", "~/Library/Application Support", "~/Library/Containers", "~/Library/Group Containers",
              "~/Library/Mobile Documents", "~/Library/CloudStorage", "~/Library/Preferences", "~/Documents",
              "~/Desktop", "~/Downloads", "~/Pictures", "~/Movies", "~/Music", "~/Applications", "~/Library/Developer"]


def _safe_target(p: str) -> str | None:
    """Return None if `p` may be emptied, else the reason it may not.

    Both checks compare inodes, not path strings. macOS volumes are case-insensitive by
    default, so an `extra_caches` entry spelled "~/documents" names exactly the same folder
    as "~/Documents" while comparing unequal as text: a string-based protected-folder check
    fails *open* and would empty the user's Documents.
    """
    if os.path.islink(p):
        return "is a symlink (not followed)"
    home = str(config.HOME)
    if same_dir(p, home) or not is_under(p, home):
        return "is outside your home folder"
    if any(same_dir(p, config.expand(x)) for x in _PROTECTED):
        return "is a protected folder"
    return None


class _DuMemo:
    """du results reused within one measure() run (paths overlap: ~/Library/Caches, ~/Library/Logs ...)."""

    def __init__(self):
        self.sizes: dict[str, int] = {}

    def preload(self, root: str, depth: int) -> None:
        if os.path.isdir(root) and not os.path.islink(root):
            _c, out, _e = run_out(["du", "-k", "-d", str(depth), root], timeout=900)
            for path, kb in parse_du(out).items():
                self.sizes[path.rstrip("/") if path != "/" else path] = kb

    def __call__(self, path: str) -> int:
        key = path.rstrip("/") or "/"
        if key not in self.sizes:
            self.sizes[key] = du_kb(path)
        return self.sizes[key]


# --- docker -----------------------------------------------------------------
# Docker prints sizes with go-units HumanSize: decimal (1 kB = 1000 B), e.g. "1.2GB (45%)", "512.5MB", "0B".
_UNITS = {"B": 1, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4, "PB": 1000 ** 5}


def _parse_size(s: str) -> int:
    """Docker size string -> KB (1024 bytes)."""
    m = re.match(r"\s*([\d.]+)\s*([kKMGTP]?B)", s or "")
    if not m:
        return 0
    return int(float(m.group(1)) * _UNITS.get(m.group(2).upper(), 1) / 1024)


def docker_reclaimable() -> tuple[int, str]:
    code, out, _err = run_out(["docker", "system", "df", "--format", "{{json .}}"], timeout=60)
    if code != 0:
        return 0, "Docker isn't running"
    total, parts = 0, []
    for line in out.splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        if not isinstance(d, dict) or d.get("Type") == "Local Volumes":
            continue  # never touched
        r = _parse_size(d.get("Reclaimable", ""))
        total += r
        parts.append(f"{d.get('Type')}: {human(r)}")
    return total, ", ".join(parts)


# --- stale build folders -------------------------------------------------------
def stale_builds(days: int = 60) -> list[dict]:
    cutoff = time.time() - days * 86400
    found = []
    seen_roots = set()
    for root in PROJECT_ROOTS:
        root = os.path.realpath(config.expand(root))
        try:
            st = os.stat(root)
        except OSError:
            continue
        # ~/projects and ~/Projects are the same folder on a case-insensitive APFS volume: scan it once.
        if not os.path.isdir(root) or (st.st_dev, st.st_ino) in seen_roots:
            continue
        seen_roots.add((st.st_dev, st.st_ino))
        _c, out, _e = run_out(["find", root, "-maxdepth", "6", "-type", "d", "(", "-name", "node_modules", "-o",
                               "-name", "target", ")", "-prune"], timeout=300)
        for d in out.splitlines():
            proj = os.path.dirname(d)
            marker = "package.json" if d.endswith("node_modules") else "pom.xml"
            if not os.path.exists(os.path.join(proj, marker)):
                continue
            if "/node_modules/" in d:
                continue
            try:
                newest = max((e.stat().st_mtime for e in os.scandir(proj)
                              if e.name not in ("node_modules", "target", ".DS_Store")), default=0)
            except Exception:
                continue
            if newest < cutoff:
                found.append({"path": d, "project": proj, "last_touched": int(newest)})
    return found


# --- selections: old Maven / Gradle versions -------------------------------------
# A "version folder" is the folder of one version of one artifact (~/.m2/repository/org/foo/bar/1.2). Selections are
# computed with one os.scandir walk per root (no `du` per folder: ~/.m2 can hold 50k versions), stored in kv
# ("cache_selection:<id>") at measure time, and clean() deletes exactly that list after re-validating every path.
_VERSION_FILES = (".pom", ".jar")
_SKIP_NAMES = {".DS_Store"}


def _tree_stats(path: str) -> tuple[int, float]:
    """(KB on disk, newest max(atime, mtime) of the files) of a folder tree; symlinks are not followed."""
    kb, newest = 0, 0.0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                kb += getattr(st, "st_blocks", 0) // 2 if hasattr(st, "st_blocks") else st.st_size // 1024
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif e.name not in _SKIP_NAMES:
                    newest = max(newest, st.st_atime, st.st_mtime)
    return kb, newest


def _version_key(name: str) -> tuple:
    """Natural sort key for version strings: 1.10 > 1.9, 2.0 > 2.0-RC1 (qualifiers sort before the release)."""
    parts = re.split(r"[.\-_+]", name)
    key = []
    for p in parts:
        if p.isdigit():
            key.append((2, int(p), ""))
        elif p.lower() in ("final", "ga", "release"):
            key.append((1, 0, ""))
        else:
            key.append((0, 0, p.lower()))
    return tuple(key) + ((1, 0, ""),)  # a release (no qualifier left) beats "1.0-rc"


def _walk_maven(root: str) -> list[dict]:
    """All version folders under a Maven repository: [{path, artifact, version, kb, used}]."""
    found = []
    stack = [root]
    from . import jobs
    n = 0
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        n += 1
        if n % 500 == 0:
            jobs.checkpoint()
        if any(e.name.endswith(_VERSION_FILES) and e.is_file(follow_symlinks=False) for e in entries):
            kb, used = _tree_stats(d)
            found.append({"path": d, "artifact": os.path.dirname(d), "version": os.path.basename(d),
                          "kb": kb, "used": int(used)})
            continue
        stack.extend(e.path for e in entries if e.is_dir(follow_symlinks=False))
    return found


def _rel(p: str, root: str) -> str:
    return os.path.relpath(p, root) if p.startswith(root.rstrip("/") + "/") else p


def _old_versions(versions: list[dict], cutoff: float) -> list[dict]:
    """Released versions unused since `cutoff`, never the highest version of an artifact."""
    by_artifact: dict[str, list[dict]] = {}
    for v in versions:
        by_artifact.setdefault(v["artifact"], []).append(v)
    out = []
    for vs in by_artifact.values():
        newest = max(vs, key=lambda v: _version_key(v["version"]))
        out.extend(v for v in vs if v is not newest and v["used"] < cutoff)
    return out


def _maven_versions(walks: dict, root: str) -> list[dict]:
    if root not in walks:  # one walk shared by the SNAPSHOT and "unused" entries within a measure() run
        walks[root] = _walk_maven(root)
    return walks[root]


def select_maven_snapshots(walks: dict, cfg: dict) -> dict:
    root = config.expand("~/.m2/repository")
    vs = _maven_versions(walks, root)
    dirs = [dict(v, group=_rel(v["artifact"], root)) for v in vs if v["version"].endswith("-SNAPSHOT")]
    return {"roots": [root], "dirs": dirs, "recheck_days": None}


def select_maven_old(walks: dict, cfg: dict) -> dict:
    root = config.expand("~/.m2/repository")
    vs = _maven_versions(walks, root)
    days = _unused_days(cfg)
    released = [v for v in vs if not v["version"].endswith("-SNAPSHOT")]
    dirs = [dict(v, group=_rel(v["artifact"], root)) for v in _old_versions(released, time.time() - days * 86400)]
    return {"roots": [root], "dirs": dirs, "recheck_days": days}


_GRADLE_VER = re.compile(r"^\d+(\.\d+)+(-[\w.-]+)?$")
_WRAPPER_DIST = re.compile(r"^gradle-(\d+)(\.[\d.]+)?(-[\w.-]+?)?-(bin|all)$")


def _subdirs(p: str) -> list[os.DirEntry]:
    try:
        with os.scandir(p) as it:
            return [e for e in it if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def select_gradle_old(walks: dict, cfg: dict) -> dict:
    from . import jobs
    caches_root = config.expand("~/.gradle/caches")
    files_root = os.path.join(caches_root, "modules-2", "files-2.1")
    dists_root = config.expand("~/.gradle/wrapper/dists")
    days = _unused_days(cfg)
    cutoff = time.time() - days * 86400
    dirs = []
    # 1. Dependencies: files-2.1/<group>/<artifact>/<version>/<sha1>/<file>
    versions = []
    for g in _subdirs(files_root):
        jobs.checkpoint()
        for a in _subdirs(g.path):
            for v in _subdirs(a.path):
                kb, used = _tree_stats(v.path)
                versions.append({"path": v.path, "artifact": a.path, "version": v.name, "kb": kb, "used": int(used)})
    dirs += [dict(v, group=_rel(v["artifact"], files_root), recheck=True) for v in _old_versions(versions, cutoff)]
    # 2. Caches of older Gradle versions: ~/.gradle/caches/7.6, 8.1.1 ... (all but the newest).
    gv = [e for e in _subdirs(caches_root) if _GRADLE_VER.match(e.name)]
    if len(gv) > 1:
        newest = max(gv, key=lambda e: _version_key(e.name))
        for e in gv:
            if e is not newest:
                kb, used = _tree_stats(e.path)
                dirs.append({"path": e.path, "artifact": caches_root, "version": e.name, "kb": kb, "used": int(used),
                             "group": f"Gradle {e.name} caches"})
    # 3. Wrapper distributions: keep the newest per major version (both -bin and -all of it).
    by_major: dict[str, list] = {}
    for e in _subdirs(dists_root):
        m = _WRAPPER_DIST.match(e.name)
        if m:
            ver = e.name[len("gradle-"):].rsplit("-", 1)[0]
            by_major.setdefault(m.group(1), []).append((ver, e))
    for items in by_major.values():
        top = max(_version_key(v) for v, _e in items)
        for v, e in items:
            if _version_key(v) != top:
                kb, used = _tree_stats(e.path)
                dirs.append({"path": e.path, "artifact": dists_root, "version": v, "kb": kb, "used": int(used),
                             "group": f"wrapper {e.name}"})
    return {"roots": [caches_root, dists_root], "dirs": dirs, "recheck_days": days}


SELECTORS = {"maven_snapshots": select_maven_snapshots, "maven_old": select_maven_old,
             "gradle_old": select_gradle_old}


def _selection_key(cache_id: str) -> str:
    return f"cache_selection:{cache_id}"


def _measure_selection(e: dict, du) -> tuple[int, str, dict]:
    """Compute and store an entry's selection. Returns (kb, detail, {root: kb inside that root})."""
    walks = getattr(du, "walks", None)
    if walks is None:
        walks = du.walks = {}
    sel = SELECTORS[e["selector"]](walks, config.load())
    dirs = sel["dirs"]
    db.kv_set(_selection_key(e["id"]), {"roots": sel["roots"], "recheck_days": sel["recheck_days"],
                                        "dirs": [{k: d.get(k) for k in ("path", "kb", "group", "used", "recheck")}
                                                 for d in dirs]})
    covered: dict[str, int] = {}
    for d in dirs:
        root = next((r for r in sel["roots"] if d["path"].startswith(r.rstrip("/") + "/")), "")
        covered[root] = covered.get(root, 0) + d["kb"]
    groups = len({d["group"] for d in dirs})
    detail = f"{len(dirs):,} version{'' if len(dirs) == 1 else 's'} in {groups:,} artifact{'' if groups == 1 else 's'}"
    return sum(d["kb"] for d in dirs), detail, covered


def _selection_targets(e: dict, log=print) -> list[dict]:
    """The stored selection, re-validated: still under its root, a real folder, and (for "unused") still unused."""
    sel = db.kv_get(_selection_key(e["id"])) or {}
    roots = [os.path.realpath(r) for r in sel.get("roots") or []]
    days = sel.get("recheck_days")
    cutoff = time.time() - days * 86400 if days else None
    out, skipped = [], 0
    for d in sel.get("dirs") or []:
        p = d.get("path") or ""
        real = os.path.realpath(p)
        ok = (p and os.path.isdir(p) and not os.path.islink(p)
              and any(real.startswith(r + "/") for r in roots) and _safe_target(p) is None)
        if ok and e["selector"] == "maven_snapshots":
            ok = os.path.basename(p).endswith("-SNAPSHOT")
        if ok and cutoff and (e["selector"] == "maven_old" or d.get("recheck")):
            ok = _tree_stats(p)[1] < cutoff  # used by a build since it was measured: keep it
        if ok:
            out.append(d)
        else:
            skipped += 1
    if skipped:
        log(f"skipping {skipped} folder{'s' if skipped > 1 else ''} gone, moved or used since the last measurement")
    return out


_METADATA = re.compile(r"^(maven-metadata.*\.xml(\.sha1|\.md5)?|resolver-status\.properties|_remote\.repositories|\.DS_Store)$")


def _prune_empty_parents(path: str, roots: list[str]) -> None:
    """Remove folders left empty (or holding only Maven metadata) between `path` and its selection root."""
    roots = [os.path.realpath(r) for r in roots]
    d = os.path.dirname(os.path.realpath(path))
    while d and any(d.startswith(r + "/") for r in roots):
        try:
            names = os.listdir(d)
        except OSError:
            return
        if any(not _METADATA.match(n) or os.path.isdir(os.path.join(d, n)) for n in names):
            return
        for n in names:
            try:
                os.unlink(os.path.join(d, n))
            except OSError:
                return
        try:
            os.rmdir(d)
        except OSError:
            return
        d = os.path.dirname(d)


# --- measure / clean -----------------------------------------------------------
def _measure_entry(e: dict, du, caches_root: str):
    """Size one registry entry. Returns an item dict, or None if absent/empty."""
    paths = _paths(e)
    detail = ""
    if e["clean"] == "docker":
        if not have("docker"):
            return None
        kb, detail = docker_reclaimable()
    elif e["clean"] == "stale_builds":
        sb = stale_builds()
        for s in sb:
            s["kb"] = du(s["path"])
        kb = sum(s["kb"] for s in sb)
        detail = f"{len(sb)} folder{'' if len(sb) == 1 else 's'}"
        db.kv_set("stale_builds", sb)
    elif e["clean"] == "selection":
        kb, detail, covered = _measure_selection(e, du)
    elif e["id"] == "other-caches":
        # Exactly what clean() would delete: everything minus the kept (registered / containing) folders.
        if not os.path.isdir(caches_root):
            return None
        _deletable, kept = _other_caches_children(caches_root)
        kb = du(caches_root) - sum(du(str(k)) for k in kept)
    else:
        if not paths:
            return None
        kb = sum(du(p) for p in paths)
    if kb <= 0 and e["clean"] != "docker":
        return None
    item = {k: e.get(k) for k in ("id", "name", "group", "risk", "note")}
    item.update({"kb": max(0, kb), "paths": paths, "detail": detail})
    if e["clean"] == "selection":
        item.update(parent=e.get("parent"), covered=covered)
    return item


def _save(items: list[dict], du) -> dict:
    items.sort(key=lambda i: i["kb"], reverse=True)
    # Don't count twice a registered path nested in another entry's path (e.g. ~/Library/Logs/JetBrains inside
    # ~/Library/Logs). other-caches already excludes its registered children.
    total = sum(i["kb"] for i in items)
    flat = [(p, i["id"]) for i in items if i["id"] != "other-caches" for p in i["paths"]]
    for p, iid in flat:
        if any(q != p and oid != iid and p.startswith(q.rstrip("/") + "/") for q, oid in flat):
            total -= du(p)
    # Selections (old Maven versions ...) live inside another entry's folder: count them once.
    for i in items:
        for root, kb in (i.get("covered") or {}).items():
            if root and any(i2 is not i and not i2.get("covered") and
                            any(root == q or root.startswith(q.rstrip("/") + "/") for q in i2["paths"])
                            for i2 in items):
                total -= kb
    result = {"items": items, "total_kb": max(0, total), "ts": int(time.time())}
    db.kv_set("caches", result)
    return result


def measure(progress=print) -> dict:
    items = []
    du = _DuMemo()
    caches_root = config.expand("~/Library/Caches")
    # One du pass gives ~/Library/Caches and its first two levels (Homebrew, pip, Google/Chrome ...).
    progress("  sizing ~/Library/Caches ...")
    du.preload(caches_root, 2)
    for e in registry():
        progress(f"  {e['name']} ...")
        item = _measure_entry(e, du, caches_root)
        if item:
            progress(f"  {e['name']}: {human(item['kb'])}")
            items.append(item)
    return _save(items, du)


def measure_one(cache_id: str, progress=print) -> dict:
    """Re-measure a single cache (after cleaning it) and update the stored results; fast."""
    e = next((x for x in registry() if x["id"] == cache_id), None)
    data = db.kv_get("caches") or {}
    items = [i for i in data.get("items", []) if i["id"] != cache_id]
    if e:
        item = _measure_entry(e, _DuMemo(), config.expand("~/Library/Caches"))
        if item:
            progress(f"  {e['name']} is now {human(item['kb'])}")
            items.append(item)
        else:
            progress(f"  {e['name']} is now empty")
    return _save(items, _DuMemo())


def clean(cache_id: str, log=print) -> int:
    """Clean one cache with live progress (files deleted, space freed, current file), then re-measure it."""
    e = next((x for x in registry() if x["id"] == cache_id), None)
    if not e:
        raise ValueError(f"unknown cache {cache_id}")
    stored = next((i for i in (db.kv_get("caches") or {}).get("items", []) if i["id"] == cache_id), {})
    expected = int(stored.get("kb") or 0)
    progress.start(["Cleaning", "Updating size"], title=f"Cleaning {e['name']}", weights=[0.95, 0.05],
                   expected_kb=expected, expected_from="last measurement", counter_label="files deleted",
                   kb_label="freed")
    progress.step(0, e.get("note") or "")

    def report(t: DeleteTracker, current: str) -> None:
        progress.update(kb=freed_before + t.freed_kb, counter=files_before + t.files, current=current)

    def deleted(t: DeleteTracker, what: str) -> None:
        log(f"  {what}: {human(t.freed_kb)} in {t.files:,} files")
        for er in t.errors[:10]:
            log(f"  ! {er}")
        if len(t.errors) > 10:
            log(f"  ! ... {len(t.errors) - 10} more errors (files in use or permission denied)")

    freed_before = files_before = 0
    freed = 0
    if e["clean"] == "cmd" and not have(e["cmd"][0]):
        log(f"{e['cmd'][0]} not found: emptying the folder instead")
        e = dict(e, clean="contents")

    if e["clean"] in ("cmd", "docker"):
        cmds = [e["cmd"]] if e["clean"] == "cmd" else [["docker", "system", "prune", "-f"],
                                                       ["docker", "builder", "prune", "-f"]]
        before = sum(du_kb(p) for p in _paths(e)) if e["clean"] == "cmd" else docker_reclaimable()[0]
        lines = 0
        for cmd in cmds:
            log("$ " + " ".join(cmd))

            def on_line(line: str) -> None:
                nonlocal lines
                lines += 1
                log(line)
                progress.update(current=line, counter=lines)
            code = stream(cmd, on_line, timeout=1800)
            if code:
                log(f"(exit {code})")
        after = sum(du_kb(p) for p in _paths(e)) if e["clean"] == "cmd" else docker_reclaimable()[0]
        freed = max(0, before - after)
        progress.update(kb=freed)
    elif e["clean"] == "selection":
        targets = _selection_targets(e, log=log)
        roots = (db.kv_get(_selection_key(cache_id)) or {}).get("roots") or []
        progress.update(items_total=len(targets), expected_kb=sum(int(t.get("kb") or 0) for t in targets))
        log(f"removing {len(targets):,} version folder{'' if len(targets) == 1 else 's'}")
        group, g_kb, g_n = None, 0, 0

        def flush() -> None:
            if group is not None and g_n:
                log(f"  {group}: {g_n} version{'' if g_n == 1 else 's'}, {human(g_kb)}")

        for i, d in enumerate(sorted(targets, key=lambda t: (t.get("group") or "", t["path"])), 1):
            if d.get("group") != group:
                flush()
                group, g_kb, g_n = d.get("group"), 0, 0
            t = DeleteTracker(report)
            t.remove(d["path"])
            for er in t.errors[:3]:
                log(f"  ! {er}")
            if not t.errors:
                _prune_empty_parents(d["path"], roots)
            freed_before += t.freed_kb
            files_before += t.files
            g_kb += t.freed_kb
            g_n += 1
            progress.update(items_done=i)
        flush()
        freed = freed_before
    else:
        if e["clean"] == "stale_builds":
            targets = [(s.get("path", ""), False) for s in (db.kv_get("stale_builds", []) or [])]
            targets = [(p, k) for p, k in targets
                       if os.path.isdir(p) and not os.path.islink(p)
                       and os.path.basename(p) in ("node_modules", "target") and _safe_target(p) is None]
        elif e["id"] == "other-caches":
            root = config.expand("~/Library/Caches")
            why = _safe_target(root)
            if why:
                log(f"skipping {root}: {why}")
                targets = []
            else:
                deletable, kept = _other_caches_children(root)
                if kept:
                    log("keeping (cleaned by their own entry): " + ", ".join(k.name for k in kept))
                targets = [(str(c), False) for c in deletable]
        else:
            targets = []
            for p in _paths(e):
                why = _safe_target(p)
                if why:
                    log(f"skipping {p}: {why}")
                else:
                    targets.append((p, True))  # empty the folder, keep it
        progress.update(items_total=len(targets))
        for i, (p, keep_root) in enumerate(targets, 1):
            log(f"{'emptying' if keep_root else 'removing'} {p}  ({i}/{len(targets)})")
            # Emptying a folder: go child by child so the log shows what is being removed (org/, com/ ...).
            parts = sorted(Path(p).iterdir(), key=lambda c: c.name) if keep_root and os.path.isdir(p) else [Path(p)]
            for child in parts:
                is_dir = child.is_dir() and not child.is_symlink()
                t = DeleteTracker(report)
                t.remove(str(child))
                if t.files or t.errors:
                    deleted(t, child.name + ("/" if is_dir else ""))
                freed_before += t.freed_kb
                files_before += t.files
            progress.update(items_done=i)
        freed = freed_before

    log(f"freed {human(freed)}")
    db.log_action("clean", cache_id, True, freed)
    progress.step(1, "Re-measuring")
    log("updating the size of this cache ...")
    measure_one(cache_id, progress=log)
    # Entries that overlap this one (Maven repository <-> its SNAPSHOT / unused selections) changed too.
    for r in registry():
        if r["id"] != cache_id and (r.get("parent") == cache_id or r["id"] == e.get("parent")):
            measure_one(r["id"], progress=log)
    return freed
