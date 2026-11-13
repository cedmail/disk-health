"""Hidden space & big folders tests (fake tmutil/diskutil/xcrun, temp HOME with MobileSync backups etc.).

    python3 -m unittest tests.test_storage -v
"""
from __future__ import annotations

import json
import os
import plistlib
import sys
import tempfile
import time
import unittest

from tests import _env
from datetime import datetime
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from diskhealth import config, db, trash  # noqa: E402
from diskhealth.features import storage as st  # noqa: E402

OLD = time.time() - 300 * 86400
OLD_SNAP = time.strftime("%Y-%m-%d-%H%M%S", time.localtime(time.time() - 12 * 86400))
NEW_SNAP = time.strftime("%Y-%m-%d-%H%M%S", time.localtime(time.time() - 3600))
RT_UUID = "3A5C0E2B-1D2F-4C7B-9E8A-0123456789AB"
RT_FIXED = "AAAAAAAA-1D2F-4C7B-9E8A-0123456789AB"
DEV_GONE = "11111111-2222-3333-4444-555555555555"
DEV_OK = "66666666-7777-8888-9999-000000000000"


def _exe(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    p.chmod(0o755)


def _blob(p: Path, size: int, ts: float | None = None) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(os.urandom(size))
    if ts:
        os.utime(p, (ts, ts))


def build_fakes(home: Path, binp: Path) -> None:
    """Fake tmutil / diskutil / xcrun (state in $HOME/.fake-*) and the folders they describe."""
    (home / ".fake-snaps").write_text(f"{OLD_SNAP}\n{NEW_SNAP}\n")
    _exe(binp / "tmutil", r"""#!/bin/sh
S="$HOME/.fake-snaps"
case "$1" in
  listlocalsnapshotdates) echo "Snapshot dates for all disks:"; cat "$S" ;;
  listlocalsnapshots) echo "Snapshots for disk /:"; sed 's/.*/com.apple.TimeMachine.&.local/' "$S" ;;
  thinlocalsnapshots) echo "Thinned local snapshots:"; head -1 "$S"; tail -n +2 "$S" > "$S.n"; mv "$S.n" "$S" ;;
  deletelocalsnapshots)
    if [ -f "$HOME/.fake-tm-denied" ]; then echo "Failed to delete local snapshot: Operation not permitted" >&2; exit 1; fi
    grep -v "^$2$" "$S" > "$S.n"; mv "$S.n" "$S"; echo "Deleted local snapshot '$2'" ;;
  *) exit 2 ;;
esac
""")
    info = plistlib.dumps({"APFSContainerReference": "disk3", "APFSContainerSize": 994662584320,
                           "APFSContainerFree": 412000000000, "VolumeName": "Macintosh HD"}).decode()
    apfs = plistlib.dumps({"Containers": [{"ContainerReference": "disk3", "CapacityCeiling": 994662584320,
                                           "CapacityFree": 412000000000, "Volumes": [
        {"Name": "Data", "Roles": ["Data"], "CapacityInUse": 540000000000, "DeviceIdentifier": "disk3s5"},
        {"Name": "Macintosh HD", "Roles": ["System"], "CapacityInUse": 11000000000, "DeviceIdentifier": "disk3s1"},
        {"Name": "VM", "Roles": ["VM"], "CapacityInUse": 4300000000, "DeviceIdentifier": "disk3s6"},
        {"Name": "Preboot", "Roles": ["Preboot"], "CapacityInUse": 7000000000, "DeviceIdentifier": "disk3s2"}]}]}).decode()
    (home / ".fake-diskutil-info").write_text(info)
    (home / ".fake-diskutil-apfs").write_text(apfs)
    _exe(binp / "diskutil", r"""#!/bin/sh
case "$1 $2" in
  "info -plist") cat "$HOME/.fake-diskutil-info" ;;
  "apfs list") cat "$HOME/.fake-diskutil-apfs" ;;
  *) exit 1 ;;
esac
""")
    devs = home / "Library/Developer/CoreSimulator/Devices"
    for u in (DEV_GONE, DEV_OK):
        _blob(devs / u / "data" / "blob", 40000)
    (home / ".fake-devices.json").write_text(json.dumps({"devices": {
        "com.apple.CoreSimulator.SimRuntime.iOS-16-4": [
            {"udid": DEV_GONE, "name": "iPhone 14", "isAvailable": False, "state": "Shutdown",
             "availabilityError": "runtime profile not found"}],
        "com.apple.CoreSimulator.SimRuntime.iOS-17-2": [
            {"udid": DEV_OK, "name": "iPhone 15 Pro", "isAvailable": True, "state": "Shutdown"}]}}))
    (home / ".fake-runtimes.json").write_text(json.dumps({
        RT_UUID: {"identifier": RT_UUID, "version": "17.2", "build": "21C62", "sizeBytes": 7300000000,
                  "state": "Ready", "deletable": True, "platformIdentifier": "com.apple.platform.iphonesimulator",
                  "runtimeIdentifier": "com.apple.CoreSimulator.SimRuntime.iOS-17-2", "lastUsedAt": "2026-05-01T10:00:00Z"},
        RT_FIXED: {"identifier": RT_FIXED, "version": "18.0", "build": "22A3351", "sizeBytes": 8100000000,
                   "state": "Ready", "deletable": False, "platformIdentifier": "com.apple.platform.iphonesimulator",
                   "runtimeIdentifier": "com.apple.CoreSimulator.SimRuntime.iOS-18-0"}}))
    _exe(binp / "xcrun", r"""#!/bin/sh
[ "$1" = simctl ] || exit 1
case "$2 $3" in
  "list devices") cat "$HOME/.fake-devices.json" ;;
  "runtime list") cat "$HOME/.fake-runtimes.json" ;;
  "delete unavailable") rm -rf "$HOME/Library/Developer/CoreSimulator/Devices/11111111-2222-3333-4444-555555555555"; echo deleted ;;
  "runtime delete") echo "$4" >> "$HOME/.fake-rt-deleted" ;;
  *) exit 1 ;;
esac
""")
    # iPhone backups
    bk = home / "Library/Application Support/MobileSync/Backup"
    for udid, name, prod, ver, when in (("00008110-000A", "Cedric's iPhone", "iPhone15,2", "17.1", datetime(2026, 9, 1, 8, 0)),
                                        ("00008027-000B", "Old iPad", "iPad8,1", "16.7", datetime(2024, 2, 3, 9, 30))):
        d = bk / udid
        _blob(d / "ab" / "abcdef", 50000, OLD)
        with open(d / "Info.plist", "wb") as f:
            plistlib.dump({"Device Name": name, "Product Type": prod, "Product Version": ver,
                           "Last Backup Date": when}, f)
        os.utime(d / "Info.plist", (OLD, OLD))
        os.utime(d / "ab", (OLD, OLD))
        os.utime(d, (OLD, OLD))
    # Xcode archives
    ar = home / "Library/Developer/Xcode/Archives/2025-03-04/MyApp 04-03-2025, 10.12.xcarchive"
    _blob(ar / "Products/Applications/MyApp.app/MyApp", 30000)
    with open(ar / "Info.plist", "wb") as f:
        plistlib.dump({"Name": "MyApp", "CreationDate": datetime(2025, 3, 4, 10, 12),
                       "ApplicationProperties": {"CFBundleIdentifier": "com.example.myapp",
                                                 "CFBundleShortVersionString": "1.4"}}, f)
    # Mail
    _blob(home / "Library/Mail/V10/MailData/Attachments/1/a.pdf", 20000)
    _blob(home / "Library/Mail/V10/acct/INBOX.mbox/1.emlx", 10000)
    md = home / "Library/Containers/com.apple.mail/Data/Library/Mail Downloads"
    _blob(md / "8F2C/report.pdf", 25000)
    _blob(md / "invoice.zip", 5000)
    # Messages attachments, two years
    ma = home / "Library/Messages/Attachments"
    _blob(ma / "0a/10/GUID-1/IMG_1.heic", 30000, time.mktime((2024, 6, 1, 12, 0, 0, 0, 0, -1)))
    _blob(ma / "0b/11/GUID-2/IMG_2.heic", 20000, time.mktime((2025, 6, 1, 12, 0, 0, 0, 0, -1)))
    _blob(ma / "0b/12/GUID-3/video.mov", 60000, time.mktime((2025, 7, 1, 12, 0, 0, 0, 0, -1)))
    # Photos + iCloud Drive
    _blob(home / "Pictures/Photos Library.photoslibrary/originals/0/a.jpg", 45000)
    _blob(home / "Library/Mobile Documents/com~apple~CloudDocs/doc.pages", 15000)


_saved = {}


def setUpModule():
    # Own HOME + data folder for this module. Must happen here, not at import: unittest
    # discover imports every test module before running any, so the last import would win.
    _env.use("storage")
    # Inside `unittest discover` another module may delete its data dir: use a private database file.
    with db._lock:
        _saved.update(conn=db._conn, path=config.DB_PATH)
        db._conn = None
        config.DB_PATH = Path(tempfile.mkdtemp(prefix="dh-storage-db-")).resolve() / "db.sqlite3"


def tearDownModule():
    with db._lock:
        if db._conn is not None:
            db._conn.close()
        db._conn, config.DB_PATH = _saved["conn"], _saved["path"]


class StorageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_env = {k: os.environ.get(k) for k in ("HOME", "PATH")}
        cls.tmp = Path(tempfile.mkdtemp(prefix="dh-st-")).resolve()
        cls.home, cls.bin = cls.tmp / "home", cls.tmp / "bin"
        cls.home.mkdir()
        cls.bin.mkdir()
        os.environ["HOME"] = str(cls.home)
        os.environ["PATH"] = f"{cls.bin}{os.pathsep}{os.environ.get('PATH', '')}"

    def setUp(self):
        # Fresh tree per test (actions mutate it).
        import shutil
        for p in self.home.iterdir():
            shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
        build_fakes(self.home, self.bin)

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.old_env.items():
            os.environ[k] = v

    def folder(self, r, fid):
        return next((f for f in r["folders"] if f["id"] == fid), None)

    def test_hidden_space(self):
        h = st.hidden_space()
        self.assertEqual(h["container_size"], 994662584320)
        self.assertEqual(h["container_free"], 412000000000)
        self.assertEqual(h["volumes"][0]["name"], "Data")
        self.assertEqual([s["date"] for s in h["snapshots"]], [OLD_SNAP, NEW_SNAP])
        self.assertEqual(h["snapshots"][0]["name"], f"com.apple.TimeMachine.{OLD_SNAP}.local")
        self.assertLess(h["oldest"], h["newest"])

    def test_scan_folders(self):
        r = st.scan(log=lambda *_: None)
        b = self.folder(r, "ios-backups")
        self.assertEqual(b["count"], 2)
        names = {c["name"]: c for c in b["children"]}
        self.assertIn("Cedric's iPhone", names)
        self.assertIn("iOS 17.1", names["Cedric's iPhone"]["detail"])
        self.assertGreater(b["kb"], 0)
        sims = self.folder(r, "sim-devices")
        self.assertEqual(sims["unavailable"], 1)
        self.assertEqual(sims["actions"][0]["action"], "storage-simctl-delete-unavailable")
        rts = self.folder(r, "sim-runtimes")
        self.assertEqual(rts["kb"], (7300000000 + 8100000000) // 1024)
        fixed = next(c for c in rts["children"] if c["id"] == RT_FIXED)
        self.assertEqual(fixed["action"], "")
        arch = self.folder(r, "xcode-archives")
        self.assertEqual(arch["children"][0]["name"], "MyApp")
        self.assertEqual(self.folder(r, "mail-downloads")["count"], 2)
        msg = self.folder(r, "messages")
        self.assertEqual(msg["count"], 3)
        self.assertEqual([c["name"] for c in msg["children"]], ["2025", "2024"])
        self.assertTrue(self.folder(r, "icloud-drive")["info_only"])
        self.assertTrue(any(f["id"].startswith("photos:") for f in r["folders"]))

    def test_needs_full_disk_access(self):
        if os.geteuid() == 0:
            # root ignores permissions: simulate macOS TCC refusing the listing instead
            orig = os.listdir
            target = str(st.messages_dir())
            def deny(p="."):
                if str(p) == target:
                    raise PermissionError(1, "Operation not permitted")
                return orig(p)
            os.listdir = deny
            try:
                f = st.scan_messages()
            finally:
                os.listdir = orig
        else:
            os.chmod(st.messages_dir(), 0)
            try:
                f = st.scan_messages()
            finally:
                os.chmod(st.messages_dir(), 0o755)
        self.assertEqual(f["state"], "fda")
        self.assertEqual(f["kb"], 0)

    def test_thin_snapshots(self):
        st.scan(log=lambda *_: None)
        st.thin_snapshots(log=lambda *_: None)
        h = db.kv_get("storage")["hidden"]
        self.assertEqual([s["date"] for s in h["snapshots"]], [NEW_SNAP])

    def test_delete_snapshot(self):
        st.scan(log=lambda *_: None)
        with self.assertRaises(ValueError):
            st.delete_snapshot("2026-10-13; rm -rf /", log=lambda *_: None)
        with self.assertRaises(ValueError):
            st.delete_snapshot("2001-01-01-000000", log=lambda *_: None)  # not listed
        st.delete_snapshot(OLD_SNAP, log=lambda *_: None)
        self.assertEqual([s["date"] for s in db.kv_get("storage")["hidden"]["snapshots"]], [NEW_SNAP])
        self.assertIsNone(db.kv_get("storage")["last_error"])

    def test_delete_snapshot_needs_admin(self):
        st.scan(log=lambda *_: None)
        (self.home / ".fake-tm-denied").write_text("1")
        logs = []
        with self.assertRaisesRegex(RuntimeError, "admin"):
            st.delete_snapshot(NEW_SNAP, log=logs.append)
        err = db.kv_get("storage")["last_error"]
        self.assertEqual(err["cmd"], f"sudo tmutil deletelocalsnapshots {NEW_SNAP}")

    def test_trash_backup(self):
        bk = st.backup_dir()
        with self.assertRaises(ValueError):
            st.trash_backup(str(bk), log=lambda *_: None)                 # the Backup folder itself
        with self.assertRaises(ValueError):
            st.trash_backup(str(bk / "00008110-000A/ab"), log=lambda *_: None)  # nested
        with self.assertRaises(ValueError):
            st.trash_backup(str(bk / "00008110-000A/../00008110-000A/ab"), log=lambda *_: None)
        os.symlink(self.home / "Library", bk / "sneaky")
        with self.assertRaises(ValueError):
            st.trash_backup(str(bk / "sneaky"), log=lambda *_: None)
        st.scan(log=lambda *_: None)
        p = str(bk / "00008027-000B")
        st.trash_backup(p, log=lambda *_: None)
        self.assertFalse(os.path.exists(p))
        origins = db.kv_get(trash.ORIGINS_KEY) or {}
        self.assertIn(p, origins.values())
        self.assertEqual(self.folder(db.kv_get("storage"), "ios-backups")["count"], 1)

    def test_trash_backup_refuses_running_backup(self):
        p = st.backup_dir() / "00008110-000A"
        (p / "Status.plist").write_text("x")  # modified just now
        with self.assertRaisesRegex(ValueError, "backup may be running"):
            st.trash_backup(str(p), log=lambda *_: None)

    def test_trash_archive(self):
        a = st.archives_dir()
        with self.assertRaises(ValueError):
            st.trash_archive(str(a / "2025-03-04"), log=lambda *_: None)
        with self.assertRaises(ValueError):
            st.trash_archive(str(self.home / "Other/x.xcarchive"), log=lambda *_: None)
        p = str(a / "2025-03-04/MyApp 04-03-2025, 10.12.xcarchive")
        st.trash_archive(p, log=lambda *_: None)
        self.assertFalse(os.path.exists(p))
        self.assertFalse(os.path.exists(a / "2025-03-04"))  # empty date folder removed

    def test_mail_downloads(self):
        st.trash_mail_downloads(log=lambda *_: None)
        self.assertEqual(os.listdir(st.mail_downloads_dir()), [])
        self.assertTrue(st.mail_downloads_dir().is_dir())

    def test_simctl_actions(self):
        st.scan(log=lambda *_: None)
        st.simctl_delete_unavailable(log=lambda *_: None)
        self.assertFalse((st.sim_devices_dir() / DEV_GONE).exists())
        with self.assertRaises(ValueError):
            st.simctl_runtime_delete("iOS 17.2; reboot", log=lambda *_: None)
        with self.assertRaisesRegex(ValueError, "not deletable"):
            st.simctl_runtime_delete(RT_FIXED, log=lambda *_: None)
        st.simctl_runtime_delete(RT_UUID, log=lambda *_: None)
        self.assertEqual((self.home / ".fake-rt-deleted").read_text().strip(), RT_UUID)

    def test_alerts(self):
        st.scan(log=lambda *_: None)
        a = st.alerts({"storage_backup_alert_gb": 0.00001, "storage_snapshot_alert_days": 7})
        ids = [x["id"].split(":")[1] for x in a]
        self.assertEqual(ids, ["backups", "snapshots"])
        self.assertIn("1 Time Machine local snapshot older", a[1]["title"])
        self.assertEqual(st.alerts({"storage_backup_alert_gb": 20, "storage_snapshot_alert_days": 30}), [])


if __name__ == "__main__":
    unittest.main()
