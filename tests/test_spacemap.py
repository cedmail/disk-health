"""Space map feature: tree built from the latest folder-size snapshot.

    python3 -m unittest tests.test_spacemap -v
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="dh-spacemap-"))
if "diskhealth" not in sys.modules:  # alone: private HOME / data dir (diskhealth reads them once, at import)
    (TMP / "home").mkdir()
    (TMP / "data").mkdir()
    os.environ["HOME"] = str(TMP / "home")
    os.environ["DISK_HEALTH_DATA"] = str(TMP / "data")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from diskhealth import config, db  # noqa: E402
from diskhealth.features import spacemap  # noqa: E402

_saved = {}


def setUpModule():
    # Inside `unittest discover` other modules share the package: point the db at a private file so these
    # snapshots never leak into (or depend on) theirs.
    with db._lock:
        _saved.update(conn=db._conn, path=config.DB_PATH)
        db._conn = None
        config.DB_PATH = TMP / "spacemap.sqlite3"


def tearDownModule():
    with db._lock:
        if db._conn is not None:
            db._conn.close()
        db._conn, config.DB_PATH = _saved["conn"], _saved["path"]


GB = 1024 * 1024  # kb
H = "/Users/t"
NOW = int(time.time())


def snap(ts, roots, sizes):
    sid = db.execute("INSERT INTO snapshots (ts, duration_s, roots) VALUES (?,?,?)", (ts, 10, ",".join(roots))).lastrowid
    db.executemany("INSERT INTO sizes VALUES (?,?,?,?)", [(sid, p, p.count("/"), kb) for p, kb in sizes.items()])
    return sid


def walk(n):
    yield n
    for c in n.get("children") or []:
        yield from walk(c)


class SpaceMapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        old = {H: 100 * GB, f"{H}/Library": 60 * GB, f"{H}/Library/Caches": 20 * GB,
               f"{H}/Library/Caches/A": 10 * GB, f"{H}/Downloads": 30 * GB, "/Applications": 40 * GB}
        snap(NOW - 8 * 86400, [H, "/Applications"], old)
        cur = {H: 130 * GB, f"{H}/Library": 80 * GB, f"{H}/Library/Caches": 30 * GB,
               f"{H}/Library/Caches/A": 25 * GB, f"{H}/Library/Caches/A/deep": 24 * GB,
               f"{H}/Library/Caches/A/deep/er": 23 * GB, f"{H}/Downloads": 25 * GB,
               f"{H}/NewThing": 5 * GB, "/Applications": 40 * GB, "/Applications/Xcode.app": 15 * GB}
        # 50 subfolders of Many: only the top 40 are kept
        cur[f"{H}/Many"] = 15 * GB
        for i in range(50):
            cur[f"{H}/Many/d{i:02d}"] = (i + 1) * 10 * 1024
        # an unfinished snapshot (duration 0) must be ignored
        db.execute("INSERT INTO snapshots (ts, duration_s, roots) VALUES (?,?,?)", (NOW + 10, 0, H))
        snap(NOW, [H, "/Applications"], cur)
        cls.cur = cur

    def check_adds_up(self, tree):
        for n in walk(tree):
            ch = n.get("children")
            if ch and not n.get("virtual"):
                self.assertEqual(sum(c["kb"] for c in ch), n["kb"], n["path"])
                sizes = [c["kb"] for c in ch if not c.get("other")]
                self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_virtual_root(self):
        r = spacemap.spacemap({})
        self.assertTrue(r["ready"] and r["found"])
        t = r["tree"]
        self.assertTrue(t["virtual"])
        self.assertEqual([c["path"] for c in t["children"]], [H, "/Applications"])
        self.assertEqual(t["kb"], 170 * GB)
        self.assertIsNone(r["compare"])
        self.check_adds_up(t)
        # default depth 3: roots are level 1, so nothing deeper than root + 2
        self.assertEqual(max(n["path"].count("/") for n in walk(t) if n["path"].startswith(H + "/") and not n.get("other")),
                         H.count("/") + 2)

    def test_subtree_shape_and_remainder(self):
        r = spacemap.spacemap({"path": H + "/", "depth": "2"})
        t = r["tree"]
        self.assertEqual(r["path"], H)
        self.assertEqual(r["root"], H)
        names = [c["name"] for c in t["children"]]
        self.assertEqual(names, ["Library", "Downloads", "Many", "NewThing", spacemap.OTHER_NAME])
        other = t["children"][-1]
        self.assertTrue(other["other"])
        self.assertEqual(other["kb"], 130 * GB - (80 + 25 + 15 + 5) * GB)
        lib = t["children"][0]
        self.assertEqual([c["name"] for c in lib["children"]], ["Caches", spacemap.OTHER_NAME])
        self.assertEqual(lib["children"][1]["kb"], 50 * GB)
        self.assertNotIn("children", lib["children"][0])  # depth limit
        self.assertEqual(t["children"][3]["children"], [])  # no stored subfolder: empty, no "(other)" child
        self.check_adds_up(t)

    def test_top_n_grouping(self):
        t = spacemap.spacemap({"path": H + "/Many", "depth": "1"})["tree"]
        ch = t["children"]
        self.assertEqual(len(ch), spacemap.MAX_CHILDREN + 1)
        self.assertEqual(ch[0]["name"], "d49")
        self.assertEqual(ch[spacemap.MAX_CHILDREN - 1]["name"], "d10")
        self.assertEqual(ch[-1]["count"], 10)
        self.assertEqual(ch[-1]["kb"], 15 * GB - sum((i + 1) * 10 * 1024 for i in range(10, 50)))
        self.check_adds_up(t)

    def test_depth_clamped(self):
        r = spacemap.spacemap({"path": H, "depth": "99"})
        self.assertEqual(r["depth"], spacemap.MAX_DEPTH)
        paths = {n["path"] for n in walk(r["tree"])}
        self.assertIn(f"{H}/Library/Caches/A/deep", paths)
        self.assertNotIn(f"{H}/Library/Caches/A/deep/er", paths)
        self.assertEqual(spacemap.spacemap({"path": H, "depth": "x"})["depth"], 3)

    def test_deltas(self):
        r = spacemap.spacemap({"path": H, "depth": "3", "compare_days": "7"})
        self.assertIsNotNone(r["compare"]["from_ts"])
        by = {n["path"]: n for n in walk(r["tree"])}
        self.assertEqual(by[H]["delta_kb"], 30 * GB)
        self.assertEqual(by[f"{H}/Library"]["delta_kb"], 20 * GB)
        self.assertEqual(by[f"{H}/Downloads"]["delta_kb"], -5 * GB)
        self.assertEqual(by[f"{H}/NewThing"]["delta_kb"], 5 * GB)  # new: was under the threshold
        self.assertTrue(by[f"{H}/NewThing"]["new"])
        lib_other = [c for c in by[f"{H}/Library"]["children"] if c.get("other")][0]
        self.assertEqual(lib_other["delta_kb"], 20 * GB - 10 * GB)
        # virtual root: sum of roots
        v = spacemap.spacemap({"compare_days": "7", "depth": "1"})["tree"]
        self.assertEqual(v["delta_kb"], 30 * GB)
        # deep path (root outside the prefix query) still knows its root was measured
        a = spacemap.spacemap({"path": f"{H}/Library/Caches/A", "compare_days": "7"})["tree"]
        self.assertEqual(a["delta_kb"], 15 * GB)
        self.assertEqual(a["children"][0]["delta_kb"], 24 * GB)

    def test_unknown_root_delta_is_null(self):
        r = spacemap.spacemap({"path": "/Applications", "compare_days": "30", "depth": "1"})
        by = {n["path"]: n for n in walk(r["tree"])}
        self.assertEqual(by["/Applications"]["delta_kb"], 0)
        self.assertEqual(by["/Applications/Xcode.app"]["delta_kb"], 15 * GB)

    def test_unknown_path(self):
        r = spacemap.spacemap({"path": "/nope/x"})
        self.assertTrue(r["ready"])
        self.assertFalse(r["found"])
        self.assertIsNone(r["tree"])
        self.assertFalse(spacemap.spacemap({"path": "relative"})["found"])

    def test_route_registered(self):
        self.assertIs(spacemap.ROUTES["/api/spacemap"], spacemap.spacemap)


class SpaceMapEmptyAndPerfTest(unittest.TestCase):
    def test_empty_state(self):
        # Runs before SpaceMapTest seeds data (unittest orders classes by name).
        from diskhealth import scanner
        if not scanner.snapshots():
            r = spacemap.spacemap({})
            self.assertFalse(r["ready"])
            self.assertIsNone(r["tree"])

    def test_perf_20k(self):
        # A second, large snapshot (newer than the others) – then removed so other tests are unaffected.
        sizes = {"/R": 0}
        total = 0
        for a in range(20):
            for b in range(20):
                for c in range(50):
                    sizes[f"/R/a{a}/b{b}/c{c}"] = 60 * 1024
                sizes[f"/R/a{a}/b{b}"] = 50 * 60 * 1024
            sizes[f"/R/a{a}"] = 20 * 50 * 60 * 1024
            total += sizes[f"/R/a{a}"]
        sizes["/R"] = total
        sid = snap(NOW + 100, ["/R"], sizes)
        try:
            self.assertGreater(len(sizes), 20000)
            t0 = time.time()
            r = spacemap.spacemap({"depth": "3"})
            dt = time.time() - t0
            self.assertTrue(r["found"])
            self.assertLess(dt, 1.5, f"spacemap took {dt:.2f}s")
            t0 = time.time()
            r = spacemap.spacemap({"path": "/R/a3", "depth": "2", "compare_days": "7"})
            self.assertLess(time.time() - t0, 1.0)
            self.assertEqual(len(r["tree"]["children"]), 20)
            self.assertIsNone(r["tree"].get("delta_kb"))  # /R wasn't in any baseline
        finally:
            db.execute("DELETE FROM sizes WHERE snapshot_id=?", (sid,))
            db.execute("DELETE FROM snapshots WHERE id=?", (sid,))


if __name__ == "__main__":
    unittest.main()
