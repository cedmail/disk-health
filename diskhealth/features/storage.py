"""Hidden space (APFS container, Time Machine local snapshots) and big "other" folders that the cache and app
scans don't cover: iPhone/iPad backups, Xcode simulators and archives, Mail and Messages attachments, Photos
library and the iCloud Drive local copy.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import config, db, jobs, progress, trash
from ..util import have, human, move_to_trash, parse_du, run_out, stream

NAME = "storage"
KEY = "storage"
SCAN_LABEL = "Hidden space & big folders"

SNAP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{6}$")
UUID_RE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")
FDA_NOTE = "Needs Full Disk Access: System Settings ▸ Privacy & Security ▸ Full Disk Access ▸ enable DiskHealth.app"
VOLUME = "/"


def _home() -> Path:
    return Path.home()


def backup_dir() -> Path:
    return _home() / "Library/Application Support/MobileSync/Backup"


def archives_dir() -> Path:
    return _home() / "Library/Developer/Xcode/Archives"


def sim_devices_dir() -> Path:
    return _home() / "Library/Developer/CoreSimulator/Devices"


def mail_downloads_dir() -> Path:
    return _home() / "Library/Containers/com.apple.mail/Data/Library/Mail Downloads"


def messages_dir() -> Path:
    return _home() / "Library/Messages/Attachments"


# ------------------------------------------------------------------ helpers
def _du(path) -> tuple[int, bool]:
    """(KB, complete). complete is False when du hit permission errors (partial size)."""
    path = str(path)
    if not os.path.lexists(path):
        return 0, True
    _c, out, err = run_out(["du", "-sk", path], timeout=1800)
    denied = "Operation not permitted" in err or "Permission denied" in err
    return sum(parse_du(out).values()), not denied


def _du_children(path: Path) -> dict[str, int]:
    """Sizes of direct children with one du call."""
    try:
        kids = [str(p) for p in path.iterdir() if p.name != ".DS_Store"]
    except OSError:
        return {}
    if not kids:
        return {}
    _c, out, _e = run_out(["du", "-sk", *kids], timeout=1800)
    return parse_du(out)


def _readable(path: Path) -> str:
    """'' if readable, 'fda' if macOS refuses (TCC), 'missing' if absent."""
    if not path.exists():
        return "missing"
    try:
        os.listdir(path)
        return ""
    except PermissionError:
        return "fda"
    except OSError:
        return "missing"


def _plist(path) -> dict:
    try:
        with open(path, "rb") as f:
            v = plistlib.load(f)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _ts(v) -> int:
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return int(v.timestamp())
    return 0


def _mtime(p) -> int:
    try:
        return int(os.lstat(p).st_mtime)
    except OSError:
        return 0


def _statvfs() -> tuple[int, int]:
    try:
        st = os.statvfs(VOLUME)
        return st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
    except OSError:
        return 0, 0


def snap_ts(date: str) -> int:
    try:
        return int(time.mktime(time.strptime(date, "%Y-%m-%d-%H%M%S")))
    except ValueError:
        return 0


# ------------------------------------------------------------------ hidden space
def local_snapshots() -> list[dict]:
    if not have("tmutil"):
        return []
    _c, out, _e = run_out(["tmutil", "listlocalsnapshotdates", VOLUME], timeout=60)
    dates = sorted({l.strip() for l in out.splitlines() if SNAP_RE.match(l.strip())})
    _c, out2, _e = run_out(["tmutil", "listlocalsnapshots", VOLUME], timeout=60)
    names = [l.strip() for l in out2.splitlines() if l.strip().startswith("com.apple")]
    for n in names:  # names carry the date too: com.apple.TimeMachine.2026-10-13-101500.local
        m = re.search(r"(\d{4}-\d{2}-\d{2}-\d{6})", n)
        if m and m.group(1) not in dates:
            dates.append(m.group(1))
    res = []
    for d in sorted(dates):
        name = next((n for n in names if d in n), f"com.apple.TimeMachine.{d}.local")
        res.append({"date": d, "name": name, "ts": snap_ts(d)})
    return res


def hidden_space() -> dict:
    total, free = _statvfs()
    h: dict = {"volume_total": total, "volume_free": free, "container_size": 0, "container_free": 0,
               "volumes": [], "diskutil": have("diskutil"), "tmutil": have("tmutil")}
    if have("diskutil"):
        code, out, _e = run_out(["diskutil", "info", "-plist", VOLUME], timeout=60)
        info = {}
        if code == 0:
            try:
                info = plistlib.loads(out.encode())
            except Exception:
                info = {}
        h["container_size"] = int(info.get("APFSContainerSize") or info.get("TotalSize") or 0)
        h["container_free"] = int(info.get("APFSContainerFree") or info.get("FreeSpace") or 0)
        h["volume_name"] = info.get("VolumeName", "")
        cref = info.get("APFSContainerReference", "")
        h["container"] = cref
        code, out, _e = run_out(["diskutil", "apfs", "list", "-plist"], timeout=60)
        if code == 0 and cref:
            try:
                apfs = plistlib.loads(out.encode())
            except Exception:
                apfs = {}
            for c in apfs.get("Containers", []):
                if c.get("ContainerReference") != cref:
                    continue
                h["container_size"] = int(c.get("CapacityCeiling") or h["container_size"])
                h["container_free"] = int(c.get("CapacityFree") or h["container_free"])
                for v in c.get("Volumes", []):
                    h["volumes"].append({"name": v.get("Name", ""), "roles": v.get("Roles", []),
                                         "used": int(v.get("CapacityInUse") or 0), "device": v.get("DeviceIdentifier", "")})
                h["volumes"].sort(key=lambda v: v["used"], reverse=True)
    # Space neither in container-free nor in volumes we can see: what macOS calls purgeable/"System Data" overhead.
    if h["container_free"] and free:
        h["purgeable_hint"] = max(0, free - h["container_free"])
    snaps = local_snapshots()
    h["snapshots"] = snaps
    h["snapshot_count"] = len(snaps)
    h["oldest"] = snaps[0]["ts"] if snaps else None
    h["newest"] = snaps[-1]["ts"] if snaps else None
    return h


# ------------------------------------------------------------------ big folders
def _folder(fid, title, path, **kw) -> dict:
    d = {"id": fid, "title": title, "path": str(path), "kb": 0, "count": 0, "newest": None, "oldest": None,
         "state": "", "complete": True, "children": [], "note": "", "info_only": False, "actions": []}
    d.update(kw)
    return d


def scan_backups() -> dict:
    b = backup_dir()
    f = _folder("ios-backups", "iPhone & iPad backups", b,
                note="Made by Finder when you back up a device to this Mac. Deleting old ones is safe once "
                     "the device is backed up elsewhere (iCloud or a newer backup).")
    f["state"] = _readable(b)
    if f["state"]:
        return f
    sizes = _du_children(b)
    for d in sorted(b.iterdir()):
        if not d.is_dir() or d.is_symlink():
            continue
        info = _plist(d / "Info.plist")
        last = _ts(info.get("Last Backup Date")) or _mtime(d)
        f["children"].append({
            "name": info.get("Device Name") or info.get("Display Name") or d.name, "path": str(d),
            "kb": sizes.get(str(d), 0), "ts": last,
            "detail": " · ".join(x for x in (str(info.get("Product Type") or ""),
                                             f"iOS {info['Product Version']}" if info.get("Product Version") else "",
                                             d.name[:12] + "…") if x),
            "action": "storage-trash-backup"})
    _summarize(f)
    return f


def scan_simulators() -> list[dict]:
    out_items = []
    dev = sim_devices_dir()
    devs = _folder("sim-devices", "Simulator devices", dev,
                   note="Each simulator keeps its own data. “Unavailable” ones belong to runtimes you no longer have.")
    devs["state"] = _readable(dev)
    devices = []
    if have("xcrun"):
        code, out, _e = run_out(["xcrun", "simctl", "list", "devices", "-j"], timeout=60)
        try:
            for rt, lst in (json.loads(out).get("devices") or {}).items() if code == 0 else []:
                for d in lst:
                    devices.append({"udid": d.get("udid", ""), "name": d.get("name", ""), "runtime": rt,
                                    "available": d.get("isAvailable", True), "state": d.get("state", ""),
                                    "error": d.get("availabilityError", "")})
        except ValueError:
            pass
    if not devs["state"]:
        sizes = _du_children(dev)
        by_udid = {d["udid"]: d for d in devices}
        for p, kb in sizes.items():
            u = os.path.basename(p)
            d = by_udid.get(u, {})
            if not os.path.isdir(p):
                continue
            rt = d.get("runtime", "").replace("com.apple.CoreSimulator.SimRuntime.", "").replace("-", " ", 1).replace("-", ".")
            devs["children"].append({"name": d.get("name") or u, "path": p, "kb": kb, "ts": _mtime(p),
                                     "detail": (rt or "unknown runtime"),
                                     "tags": ([] if d.get("available", True) else ["unavailable"])})
        _summarize(devs)
    devs["unavailable"] = sum(1 for d in devices if not d["available"])
    devs["unavailable_kb"] = sum(c["kb"] for c in devs["children"] if "unavailable" in c.get("tags", []))
    if have("xcrun") and devs["unavailable"]:
        devs["actions"].append({"action": "storage-simctl-delete-unavailable",
                                "label": f"Delete {devs['unavailable']} unavailable"})
    if devs["state"] != "missing" or devices:
        out_items.append(devs)
    # runtimes
    if have("xcrun"):
        code, out, _e = run_out(["xcrun", "simctl", "runtime", "list", "-j"], timeout=60)
        rts = {}
        if code == 0:
            try:
                rts = json.loads(out) or {}
            except ValueError:
                rts = {}
        if isinstance(rts, dict) and rts:
            f = _folder("sim-runtimes", "Simulator runtimes", "/Library/Developer/CoreSimulator/Images",
                        note="Downloaded iOS/watchOS/tvOS/visionOS simulator runtimes (disk images managed by Xcode).")
            for key, r in rts.items():
                rid = r.get("identifier") or key
                last = r.get("lastUsedAt") or ""
                ts = 0
                if last:
                    try:
                        ts = int(datetime.fromisoformat(last.replace("Z", "+00:00")).timestamp())
                    except ValueError:
                        ts = 0
                plat = {"iphonesimulator": "iOS", "appletvsimulator": "tvOS", "watchsimulator": "watchOS",
                        "xrsimulator": "visionOS"}.get((r.get("platformIdentifier") or "").replace("com.apple.platform.", ""), "")
                f["children"].append({"name": f"{plat or 'Runtime'} {r.get('version', '')}".strip(), "path": r.get("path") or "",
                                      "id": rid, "kb": int(r.get("sizeBytes") or 0) // 1024, "ts": ts,
                                      "detail": f"build {r.get('build', '?')} · {r.get('state', '')}".strip(" ·"),
                                      "action": "storage-simctl-runtime-delete" if r.get("deletable", True) else "",
                                      "tags": [] if r.get("deletable", True) else ["not deletable"]})
            _summarize(f, ts_key="ts")
            out_items.append(f)
    return out_items


def scan_archives() -> dict:
    a = archives_dir()
    f = _folder("xcode-archives", "Xcode archives", a,
                note="Builds you archived for distribution. Keep the ones whose dSYMs you still need for crash reports.")
    f["state"] = _readable(a)
    if f["state"]:
        return f
    for day in sorted(a.iterdir()):
        if not day.is_dir() or day.is_symlink():
            continue
        sizes = _du_children(day)
        for x in sorted(day.glob("*.xcarchive")):
            info = _plist(x / "Info.plist")
            props = info.get("ApplicationProperties") or {}
            f["children"].append({"name": info.get("Name") or x.stem, "path": str(x), "kb": sizes.get(str(x), 0),
                                  "ts": _ts(info.get("CreationDate")) or _mtime(x),
                                  "detail": " · ".join(v for v in (props.get("CFBundleIdentifier", ""),
                                                                     props.get("CFBundleShortVersionString", "")) if v),
                                  "action": "storage-trash-archive"})
    _summarize(f)
    return f


def scan_mail() -> list[dict]:
    res = []
    mail = _home() / "Library/Mail"
    f = _folder("mail", "Mail store", mail, info_only=True,
                note="Mail keeps local copies of messages and attachments. To shrink it: Mail ▸ Settings ▸ Accounts "
                     "(remove unused accounts) or select messages ▸ Message ▸ Remove Attachments.")
    f["state"] = _readable(mail)
    if not f["state"]:
        f["kb"], f["complete"] = _du(mail)
        f["newest"] = _mtime(mail)
        att = 0
        for d in mail.glob("V*/MailData/Attachments"):
            att += _du(d)[0]
        if att:
            f["children"].append({"name": "Attachments (MailData)", "path": str(next(mail.glob("V*/MailData/Attachments"))),
                                  "kb": att, "ts": None, "detail": ""})
        for v in sorted(mail.glob("V[0-9]*")):
            if v.is_dir():
                f["children"].append({"name": f"{v.name} (mail store)", "path": str(v), "kb": _du(v)[0], "ts": _mtime(v), "detail": ""})
    if f["state"] != "missing":
        res.append(f)
    md = mail_downloads_dir()
    g = _folder("mail-downloads", "Mail Downloads", md,
                note="Attachments you opened from Mail (copies; the originals stay in the mail store).")
    g["state"] = _readable(md)
    if not g["state"]:
        sizes = _du_children(md)
        for p, kb in sorted(sizes.items(), key=lambda x: -x[1]):
            g["children"].append({"name": os.path.basename(p), "path": p, "kb": kb, "ts": _mtime(p), "detail": ""})
        _summarize(g)
        if g["children"]:
            g["actions"].append({"action": "storage-trash-mail-downloads", "label": "Move all to Trash"})
    if g["state"] != "missing":
        res.append(g)
    return res


def scan_messages() -> dict:
    m = messages_dir()
    f = _folder("messages", "Messages attachments", m, info_only=True,
                note="Photos, videos and files received in Messages. Review them in System Settings ▸ General ▸ "
                     "Storage ▸ Messages (“Review large attachments”), or set Messages ▸ Settings ▸ General ▸ "
                     "Keep messages: 1 Year.")
    f["state"] = _readable(m)
    if f["state"]:
        return f
    years: dict[int, list[int]] = {}
    total = files = 0
    newest, oldest = 0, 0
    for top, _dirs, fl in os.walk(m, onerror=lambda e: None):
        for name in fl:
            try:
                st = os.lstat(os.path.join(top, name))
            except OSError:
                continue
            kb = getattr(st, "st_blocks", 0) // 2 or st.st_size // 1024
            y = time.localtime(st.st_mtime).tm_year
            e = years.setdefault(y, [0, 0])
            e[0] += kb
            e[1] += 1
            total += kb
            files += 1
            newest = max(newest, int(st.st_mtime))
            oldest = int(st.st_mtime) if not oldest else min(oldest, int(st.st_mtime))
            if files % 2000 == 0:
                jobs.checkpoint()
                progress.update(current=top)
    f.update(kb=total, count=files, newest=newest or None, oldest=oldest or None)
    f["children"] = [{"name": str(y), "path": "", "kb": v[0], "ts": None, "detail": f"{v[1]:,} file{'s' if v[1] != 1 else ''}"}
                     for y, v in sorted(years.items(), reverse=True)]
    return f


def scan_info_only() -> list[dict]:
    res = []
    for lib in sorted((_home() / "Pictures").glob("*.photoslibrary")):
        f = _folder("photos:" + lib.name, "Photos library", lib, info_only=True,
                    note="Managed by Photos. With iCloud Photos, Photos ▸ Settings ▸ iCloud ▸ “Optimize Mac Storage” "
                         "keeps only small versions locally.")
        f["state"] = _readable(lib)
        if not f["state"]:
            f["kb"], f["complete"] = _du(lib)
            f["newest"] = _mtime(lib)
        res.append(f)
    icloud = _home() / "Library/Mobile Documents"
    f = _folder("icloud-drive", "iCloud Drive (local copy)", icloud, info_only=True,
                note="Files downloaded from iCloud Drive. Turn on System Settings ▸ Apple Account ▸ iCloud ▸ Drive ▸ "
                     "“Optimize Mac Storage” so macOS evicts files you don't use (they stay in iCloud).")
    f["state"] = _readable(icloud)
    if not f["state"]:
        f["kb"], f["complete"] = _du(icloud)
        f["newest"] = _mtime(icloud)
    if f["state"] != "missing":
        res.append(f)
    return res


def _summarize(f: dict, ts_key: str = "ts") -> None:
    ch = f["children"]
    ch.sort(key=lambda c: c["kb"], reverse=True)
    if not f["kb"]:
        f["kb"] = sum(c["kb"] for c in ch)
    f["count"] = len(ch)
    tss = [c[ts_key] for c in ch if c.get(ts_key)]
    f["newest"] = max(tss) if tss else None
    f["oldest"] = min(tss) if tss else None


SCANNERS = [("iPhone backups", lambda: [scan_backups()]), ("Xcode simulators", scan_simulators),
            ("Xcode archives", lambda: [scan_archives()]), ("Mail", scan_mail), ("Messages", lambda: [scan_messages()]),
            ("Photos & iCloud Drive", scan_info_only)]


def scan(log=print) -> dict:
    log("hidden space (APFS, Time Machine local snapshots) …")
    hidden = hidden_space()
    log(f"  container {human(hidden['container_size'] / 1024)}, free {human(hidden['volume_free'] / 1024)}, "
        f"{hidden['snapshot_count']} local snapshot(s)")
    folders: list[dict] = []
    progress.update(items_total=len(SCANNERS), counter_label="")
    for i, (label, fn) in enumerate(SCANNERS):
        jobs.checkpoint()
        progress.update(current=label, items_done=i)
        try:
            for f in fn():
                if f["state"] == "missing":
                    continue
                folders.append(f)
                log(f"  {f['title']}: " + ("needs Full Disk Access" if f["state"] == "fda" else human(f["kb"])))
        except jobs.Cancelled:
            raise
        except Exception as ex:
            log(f"  ! {label}: {ex}")
    folders.sort(key=lambda f: (f["state"] == "fda", -f["kb"]))
    prev = db.kv_get(KEY) or {}
    res = {"hidden": hidden, "folders": folders, "total_kb": sum(f["kb"] for f in folders),
           "fda_needed": [f["title"] for f in folders if f["state"] == "fda"], "fda_note": FDA_NOTE,
           "last_error": prev.get("last_error"), "ts": int(time.time())}
    db.kv_set(KEY, res)
    return res


def _refresh_hidden(log=print, error: dict | None = None) -> None:
    r = db.kv_get(KEY) or {}
    r.pop("_ts", None)
    r["hidden"] = hidden_space()
    r["last_error"] = error
    r.setdefault("folders", [])
    db.kv_set(KEY, r)


def _refresh_folder(fid_prefix: str) -> None:
    """Re-measure the folder(s) an action touched without redoing the whole scan."""
    r = db.kv_get(KEY) or {}
    r.pop("_ts", None)
    fresh = []
    for _label, fn in SCANNERS:
        try:
            got = fn()
        except Exception:
            continue
        if any(f["id"].startswith(fid_prefix) for f in got):
            fresh = [f for f in got if f["id"].startswith(fid_prefix)]
            break
    folders = [f for f in r.get("folders", []) if not f["id"].startswith(fid_prefix)]
    folders += [f for f in fresh if f["state"] != "missing"]
    folders.sort(key=lambda f: (f["state"] == "fda", -f["kb"]))
    r["folders"] = folders
    r["total_kb"] = sum(f["kb"] for f in folders)
    db.kv_set(KEY, r)


# ------------------------------------------------------------------ actions
def thin_snapshots(log=print) -> int:
    if not have("tmutil"):
        raise RuntimeError("tmutil not found (macOS only)")
    _t, before = _statvfs()
    log(f"Free before: {human(before / 1024)}")
    log("$ tmutil thinlocalsnapshots / 999999999999 4")
    code = stream(["tmutil", "thinlocalsnapshots", VOLUME, "999999999999", "4"], lambda l: log("  " + l), timeout=1800)
    _t, after = _statvfs()
    freed = max(0, after - before) // 1024
    log(f"Free after: {human(after / 1024)} ({'+' + human(freed) if freed else 'no change'})")
    db.log_action("thin-snapshots", "/", code == 0, freed)
    _refresh_hidden(log)
    if code != 0:
        raise RuntimeError(f"tmutil exited with {code}")
    return freed


def delete_snapshot(target: str, log=print) -> int:
    if not isinstance(target, str) or not SNAP_RE.match(target):
        raise ValueError("Bad snapshot date (expected YYYY-MM-DD-HHMMSS)")
    if target not in {s["date"] for s in local_snapshots()}:
        raise ValueError(f"No local snapshot dated {target}")
    _t, before = _statvfs()
    log(f"$ tmutil deletelocalsnapshots {target}")
    code, out, err = run_out(["tmutil", "deletelocalsnapshots", target], timeout=600)
    text = (out + err).strip()
    for line in text.splitlines():
        log("  " + line)
    still = target in {s["date"] for s in local_snapshots()}
    if code != 0 or still:
        perm = re.search(r"permi|not permitted|root|privilege|authoriz", text, re.I)
        cmd = f"sudo tmutil deletelocalsnapshots {target}"
        msg = ("macOS needs admin rights to delete this snapshot. Disk Health never uses sudo itself: run this in "
               f"Terminal:  {cmd}") if perm or code == 0 else f"tmutil failed (exit {code})."
        log(msg)
        _refresh_hidden(log, {"date": target, "cmd": cmd, "message": msg, "ts": int(time.time())})
        db.log_action("delete-snapshot", target, False, 0, text[:500])
        raise RuntimeError(msg)
    _t, after = _statvfs()
    freed = max(0, after - before) // 1024
    log(f"Deleted snapshot {target}" + (f", {human(freed)} freed" if freed else ""))
    db.log_action("delete-snapshot", target, True, freed)
    _refresh_hidden(log)
    return freed


def _recently_modified(path: str, minutes: int = 10) -> bool:
    cutoff = time.time() - minutes * 60
    n = 0
    for top, dirs, files in os.walk(path, onerror=lambda e: None):
        for x in files + dirs:
            n += 1
            if _mtime(os.path.join(top, x)) > cutoff:
                return True
            if n > 200000:
                return False
    return False


def _trash(path: str, kind: str, log) -> int:
    from ..util import du_kb
    kb = du_kb(path)
    dest = move_to_trash(path)
    trash.remember(dest, path)
    log(f"Moved {path} to the Trash ({human(kb)}). Put it back from the Trash panel if needed; "
        f"the space is freed when you empty the Trash.")
    db.log_action(kind, path, True, kb, f"moved to Trash: {dest}")
    return kb


def _direct_child(target: str, parent: Path) -> str:
    if not isinstance(target, str) or not target.startswith("/") or "\0" in target:
        raise ValueError("Bad target")
    p = os.path.normpath(target)
    if os.path.islink(p) or not os.path.isdir(p):
        raise ValueError(f"{p} is not a folder")
    if os.path.dirname(os.path.realpath(p)) != os.path.realpath(parent) or os.path.dirname(p) != str(parent):
        raise ValueError(f"Refusing: {p} is not directly inside {parent}")
    return p


def trash_backup(target: str, log=print) -> int:
    p = _direct_child(target, backup_dir())
    if _recently_modified(p):
        raise ValueError("This backup changed in the last 10 minutes: a backup may be running. Try again later.")
    name = _plist(Path(p) / "Info.plist").get("Device Name") or os.path.basename(p)
    log(f"Moving the backup of {name} to the Trash …")
    kb = _trash(p, "trash-ios-backup", log)
    trash.listing()
    _refresh_folder("ios-backups")
    return kb


def trash_archive(target: str, log=print) -> int:
    if not isinstance(target, str) or not target.endswith(".xcarchive"):
        raise ValueError("Not an .xcarchive")
    p = os.path.normpath(target)
    day = os.path.dirname(p)
    if os.path.dirname(day) != str(archives_dir()) or os.path.realpath(os.path.dirname(day)) != os.path.realpath(archives_dir()):
        raise ValueError(f"Refusing: {p} is not an archive in {archives_dir()}")
    _direct_child(p, Path(day))
    kb = _trash(p, "trash-xcode-archive", log)
    try:
        if not [x for x in os.listdir(day) if x != ".DS_Store"]:
            for x in os.listdir(day):
                os.unlink(os.path.join(day, x))
            os.rmdir(day)
    except OSError:
        pass
    trash.listing()
    _refresh_folder("xcode-archives")
    return kb


def trash_mail_downloads(log=print) -> int:
    md = mail_downloads_dir()
    if _readable(md):
        raise ValueError(f"Can't read {md}: " + FDA_NOTE)
    total = 0
    for p in sorted(md.iterdir()):
        if p.name == ".DS_Store":
            continue
        jobs.checkpoint()
        total += _trash(str(p), "trash-mail-download", log)
    trash.listing()
    _refresh_folder("mail")
    log(f"Moved {human(total)} to the Trash")
    return total


def simctl_delete_unavailable(log=print) -> int:
    if not have("xcrun"):
        raise RuntimeError("xcrun not found (needs Xcode)")
    before, _ = _du(sim_devices_dir())
    log("$ xcrun simctl delete unavailable")
    code = stream(["xcrun", "simctl", "delete", "unavailable"], lambda l: log("  " + l), timeout=1800)
    after, _ = _du(sim_devices_dir())
    freed = max(0, before - after)
    log(f"freed {human(freed)}")
    db.log_action("simctl-delete-unavailable", str(sim_devices_dir()), code == 0, freed)
    _refresh_folder("sim-")
    if code != 0:
        raise RuntimeError(f"simctl exited with {code}")
    return freed


def simctl_runtime_delete(target: str, log=print) -> int:
    if not isinstance(target, str) or not UUID_RE.match(target):
        raise ValueError("Bad runtime identifier")
    if not have("xcrun"):
        raise RuntimeError("xcrun not found (needs Xcode)")
    code, out, _e = run_out(["xcrun", "simctl", "runtime", "list", "-j"], timeout=60)
    try:
        rts = json.loads(out) if code == 0 else {}
    except ValueError:
        rts = {}
    r = next((v for k, v in rts.items() if (v.get("identifier") or k) == target), None)
    if not r:
        raise ValueError(f"No simulator runtime {target}")
    if r.get("deletable") is False:
        raise ValueError("Xcode marks this runtime as not deletable (bundled with Xcode)")
    kb = int(r.get("sizeBytes") or 0) // 1024
    log(f"$ xcrun simctl runtime delete {target}   ({r.get('runtimeIdentifier', '')} {r.get('version', '')}, {human(kb)})")
    code = stream(["xcrun", "simctl", "runtime", "delete", target], lambda l: log("  " + l), timeout=1800)
    db.log_action("simctl-runtime-delete", f"{r.get('runtimeIdentifier', '')} {r.get('version', '')}", code == 0,
                  kb if code == 0 else 0)
    _refresh_folder("sim-")
    if code != 0:
        raise RuntimeError(f"simctl exited with {code}")
    return kb


ACTIONS = {
    "storage-thin-snapshots": {"label": lambda t: "Thin Time Machine local snapshots", "fn": thin_snapshots,
                               "target": False, "lane": "heavy"},
    "storage-delete-snapshot": {"label": lambda t: f"Delete local snapshot {t}", "fn": delete_snapshot,
                                "target": True, "lane": "heavy"},
    "storage-trash-backup": {"label": lambda t: f"Move device backup {os.path.basename(str(t))[:12]}… to Trash",
                             "fn": trash_backup, "target": True, "lane": "heavy"},
    "storage-trash-archive": {"label": lambda t: f"Move {os.path.basename(str(t))} to Trash", "fn": trash_archive,
                              "target": True, "lane": "heavy"},
    "storage-trash-mail-downloads": {"label": lambda t: "Move Mail Downloads to Trash", "fn": trash_mail_downloads,
                                     "target": False, "lane": "heavy"},
    "storage-simctl-delete-unavailable": {"label": lambda t: "Delete unavailable simulators",
                                          "fn": simctl_delete_unavailable, "target": False, "lane": "heavy"},
    "storage-simctl-runtime-delete": {"label": lambda t: f"Delete simulator runtime {t}", "fn": simctl_runtime_delete,
                                      "target": True, "lane": "heavy"},
}


def alerts(cfg) -> list[dict]:
    r = db.kv_get(KEY) or {}
    out = []
    week = int(time.time() // (7 * 86400))
    gb = float(cfg.get("storage_backup_alert_gb", 20))
    b = next((f for f in r.get("folders", []) if f["id"] == "ios-backups"), None)
    if b and b["kb"] >= gb * 1024 ** 2:
        out.append({"id": f"storage:backups:{week}", "level": "notice", "notify": True,
                    "title": f"{human(b['kb'])} of iPhone/iPad backups ({b['count']})", "section": NAME})
    days = float(cfg.get("storage_snapshot_alert_days", 7))
    snaps = (r.get("hidden") or {}).get("snapshots") or []
    old = [s for s in snaps if s.get("ts") and s["ts"] < time.time() - days * 86400]
    if old:
        out.append({"id": f"storage:snapshots:{old[0]['date']}", "level": "notice", "notify": False,
                    "title": f"{len(old)} Time Machine local snapshot{'s' if len(old) != 1 else ''} older than "
                             f"{days:g} days (purgeable space)", "section": NAME})
    return out
