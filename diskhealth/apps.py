"""Installed applications: size, last-used date, origin, unused detection and clean uninstall."""
from __future__ import annotations

import calendar
import glob
import json
import os
import plistlib
import re
import time
from datetime import datetime
from pathlib import Path

from . import config, db
from .util import IS_MAC, DeleteTracker, du_kb, human, move_to_trash, run, run_out

BREW_ENV = {"HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_COLOR": "1", "HOMEBREW_NO_ENV_HINTS": "1"}

# Only these folders are searched by app *name* (bundle-id matches are searched everywhere): a name like "Code" or
# "Docker" is ambiguous, and in Preferences/Containers/LaunchAgents etc. entries are named by bundle id anyway.
NAME_MATCH_DIRS = ["~/Library/Application Support", "~/Library/Caches", "~/Library/Logs"]
NAME_MIN_LEN = 5  # "Code", "Zoom", "Mail" ... are too ambiguous to match by name
GENERIC_NAMES = {"apple", "google", "microsoft", "adobe", "jetbrains", "mozilla", "oracle", "java", "cache", "caches",
                 "logs", "crashreporter", "com.apple", "default", "data", "temp", "tmp", "help", "update", "updater"}

LEFTOVER_DIRS = [
    "~/Library/Application Support", "~/Library/Caches", "~/Library/Containers", "~/Library/Group Containers",
    "~/Library/Preferences", "~/Library/Saved Application State", "~/Library/Logs", "~/Library/HTTPStorages",
    "~/Library/WebKit", "~/Library/LaunchAgents", "~/Library/Application Scripts", "~/Library/Cookies",
]


def _info(app: str) -> dict:
    try:
        with open(os.path.join(app, "Contents/Info.plist"), "rb") as f:
            return plistlib.load(f)
    except Exception:
        return {}


def _parse_mdls_date(v: str) -> int | None:
    v = v.strip()
    if not v or v in ("NONE", "(null)"):
        return None
    # mdls prints dates as "2024-01-15 10:23:45 +0000". Parsed by hand: datetime.strptime lazily imports
    # _strptime, which breaks if Homebrew upgraded the running Python underneath the service.
    m = re.match(r"(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d) ([+-])(\d\d)(\d\d)$", v)
    if not m:
        return None
    y, mo, d, h, mi, se = (int(x) for x in m.groups()[:6])
    off = (int(m.group(8)) * 60 + int(m.group(9))) * 60 * (1 if m.group(7) == "+" else -1)
    try:
        return calendar.timegm((y, mo, d, h, mi, se, 0, 0, 0)) - off
    except (ValueError, OverflowError):
        return None


def _mdls_last_used(files: list[str]) -> list[str] | None:
    """Raw kMDItemLastUsedDate values, one per file, or None if the output can't be aligned with `files`."""
    _c, raw, _err = run_out(["mdls", "-raw", "-nullMarker", "NONE", "-name", "kMDItemLastUsedDate", *files], timeout=120)
    # With -raw, values for several files are separated by NUL (no trailing NUL). Errors go to stderr, kept apart.
    vals = raw.split("\0")
    if vals and vals[-1] == "" and len(vals) == len(files) + 1:
        vals.pop()
    return vals if len(vals) == len(files) else None


def _last_used(apps: list[str]) -> dict[str, int | None]:
    out: dict[str, int | None] = {a: None for a in apps}
    if not IS_MAC:
        return out
    for i in range(0, len(apps), 50):
        chunk = apps[i:i + 50]
        vals = _mdls_last_used(chunk)
        if vals is None:  # a file mdls couldn't read shifted the values: ask one by one
            vals = [(_mdls_last_used([a]) or [""])[0] for a in chunk]
        for app, v in zip(chunk, vals):
            out[app] = _parse_mdls_date(v)
    return out


def _cask_map() -> dict[str, str]:
    """App bundle name -> cask token."""
    code, out, _err = run_out(["brew", "info", "--cask", "--json=v2", "--installed"], timeout=300, env=BREW_ENV)
    if code != 0:
        return {}
    m = {}
    try:
        casks = json.loads(out).get("casks", [])
    except Exception:
        return {}
    for c in casks:
        try:
            for name in _cask_app_names(c.get("artifacts") or []):
                m[name] = c["token"]
        except Exception:
            continue
    return m


def _cask_app_names(artifacts) -> list[str]:
    """App bundle names a cask installs.

    Current brew: [{"app": ["Foo.app"]}, {"zap": [...]}, ...]; with a target: {"app": ["Foo.app", {"target": "Bar.app"}]}
    (installed as Bar.app). Older brew emitted bare lists: [["Foo.app"], {"zap": ...}].
    """
    names = []
    for art in artifacts:
        if isinstance(art, dict):
            if "app" not in art:
                continue
            spec = art["app"]
        elif isinstance(art, list):
            spec = art
        else:
            continue
        spec = spec if isinstance(spec, list) else [spec]
        target = next((x.get("target") for x in spec if isinstance(x, dict) and x.get("target")), None)
        if target:
            names.append(os.path.basename(str(target).rstrip("/")))
        else:
            names += [os.path.basename(x.rstrip("/")) for x in spec if isinstance(x, str) and x.endswith(".app")]
    return names


def list_apps() -> list[str]:
    pats = ["/Applications/*.app", "/Applications/*/*.app", "~/Applications/*.app"]
    apps = []
    for p in pats:
        apps += glob.glob(os.path.expanduser(p))
    return sorted(set(apps))


def scan(progress=print) -> dict:
    cfg = config.load()
    apps = list_apps()
    progress(f"  {len(apps)} applications")
    used = _last_used(apps)
    casks = _cask_map()
    now = time.time()
    items = []
    for a in apps:
        info = _info(a)
        bid = info.get("CFBundleIdentifier", "")
        if bid.startswith("com.diskhealth."):
            continue  # our own launcher
        if bid.startswith("com.apple.") and not os.path.exists(os.path.join(a, "Contents/_MASReceipt")):
            continue
        try:
            st = os.stat(a)
        except OSError:
            continue  # removed while scanning
        installed = int(getattr(st, "st_birthtime", st.st_mtime))
        name = os.path.basename(a)
        source = "brew" if name in casks else ("appstore" if os.path.exists(os.path.join(a, "Contents/_MASReceipt"))
                                               else "manual")
        last = used.get(a)
        ref = last or installed
        idle_days = int((now - ref) / 86400)
        items.append({
            "path": a, "name": name[:-4], "bundle_id": bid, "version": info.get("CFBundleShortVersionString", ""),
            "kb": du_kb(a), "last_used": last, "installed": installed, "idle_days": idle_days,
            "source": source, "cask": casks.get(name),
            "unused": idle_days >= cfg["unused_app_days"],
        })
    items.sort(key=lambda i: (not i["unused"], -i["kb"]))
    result = {"items": items, "ts": int(now),
              "unused_kb": sum(i["kb"] for i in items if i["unused"]),
              "unused_count": sum(1 for i in items if i["unused"])}
    db.kv_set("apps", result)
    return result


def leftovers(app_path: str) -> list[dict]:
    info = _info(app_path)
    bid = info.get("CFBundleIdentifier")
    if not isinstance(bid, str) or bid.count(".") < 1 or bid.startswith("com.apple."):
        bid = None  # missing/odd id: a pattern like "*.foo" would match unrelated things
    name = Path(app_path).stem
    name_ok = len(name) >= NAME_MIN_LEN and name.lower() not in GENERIC_NAMES
    name_dirs = {config.expand(d) for d in NAME_MATCH_DIRS}
    found = set()
    for d in LEFTOVER_DIRS:
        base = config.expand(d)
        if bid:
            b = glob.escape(bid)
            for pat in (b, b + ".*", "*." + b, "*." + b + ".*"):  # exact id, id.plist/.savedState, group containers
                found.update(glob.glob(os.path.join(base, pat)))
        if name_ok and base in name_dirs:
            found.update(glob.glob(os.path.join(base, glob.escape(name))))
    out = [{"path": p, "kb": du_kb(p)} for p in sorted(found)]
    return out


def _known(app_path: str) -> dict:
    data = db.kv_get("apps", {}) or {}
    app = next((i for i in data.get("items", []) if i["path"] == app_path), None)
    if not app:
        raise ValueError("Unknown application (rescan apps first)")
    return app


def uninstall(app_path: str, log=print, permanent: bool = False) -> int:
    app = _known(app_path)
    total = app["kb"]
    ok = False
    if app["cask"]:
        cmd = ["brew", "uninstall", "--cask", "--zap", app["cask"]]
        log("$ " + " ".join(cmd))
        code, out = run(cmd, timeout=1800, env=BREW_ENV)
        log(out.strip())
        ok = code == 0
        if not ok and os.path.lexists(app_path):
            log(f"brew failed (exit {code}): moving the app and its leftovers to the Trash instead")
    if not ok and os.path.lexists(app_path):
        left = leftovers(app_path)
        ok = True
        for p in [app_path] + [l["path"] for l in left]:
            try:
                if permanent:
                    log(f"✕ deleting {p}")
                    t = DeleteTracker()
                    t.remove(p)
                    if t.errors:
                        raise PermissionError(t.errors[0])
                else:
                    log(f"→ Trash: {p}")
                    from . import trash
                    trash.remember(move_to_trash(p), p)
                total += next((l["kb"] for l in left if l["path"] == p), 0)
            except PermissionError:
                # Let Finder do it: it asks for an admin password when needed.
                esc = p.replace("\\", "\\\\").replace('"', '\\"')
                code, out = run(["osascript", "-e", f'tell application "Finder" to delete POSIX file "{esc}"'], timeout=300)
                log("  (via Finder) " + (out.strip() or "ok"))
                ok = ok and code == 0
            except Exception as ex:
                log(f"  ! {ex}")
                ok = False
        if not permanent:
            log("Items are in the Trash: empty it from the Trash section to free the space (or Put Back to undo).")
    db.log_action("uninstall", app_path, ok, total if ok else 0)
    log(("done, " if ok else "finished with errors, ") + f"{human(total)}")
    return total if ok else 0


def forget(app_path: str) -> None:
    """Drop an uninstalled app from the stored list (instead of rescanning every app)."""
    data = db.kv_get("apps") or {}
    items = [i for i in data.get("items", []) if i["path"] != app_path or os.path.lexists(app_path)]
    data.update(items=items, unused_count=sum(1 for i in items if i["unused"]),
                unused_kb=sum(i["kb"] for i in items if i["unused"]))
    data.pop("_ts", None)
    db.kv_set("apps", data)


def reinstall(app_path: str, log=print) -> None:
    app = _known(app_path)
    if not app["cask"]:
        raise ValueError("Clean reinstall is only automated for Homebrew casks")
    for cmd in (["brew", "uninstall", "--cask", "--zap", app["cask"]], ["brew", "install", "--cask", app["cask"]]):
        log("$ " + " ".join(cmd))
        code, out = run(cmd, timeout=3600, env=BREW_ENV)
        log(out.strip())
        if code != 0:
            log(f"stopped (exit {code})")
            return
    db.log_action("reinstall", app_path, True)
