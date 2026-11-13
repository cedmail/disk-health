"""Maven / Gradle selective cleanup tests (caches.py, clean="selection").

    python3 -m unittest tests.test_cache_selection -v

Runs alone or under `unittest discover`: `_env.use()` gives this module its own HOME and data folder.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest

from tests import _env
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
caches = db = HOME = None
DAY = 86400


def setUpModule():
    global caches, db, HOME
    _env.use("cache_selection")
    from diskhealth import caches as _c, db as _db
    caches, db, HOME = _c, _db, Path(os.environ["HOME"])


def _version(root: Path, rel: str, age_days: float, size: int = 40 * 1024, kind: str = "jar") -> Path:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    art, ver = d.parent.name, d.name
    t = time.time() - age_days * DAY
    for name, n in ((f"{art}-{ver}.pom", 2048), (f"{art}-{ver}.{kind}", size)):
        (d / name).write_bytes(os.urandom(n))
        os.utime(d / name, (t, t))
    return d


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.m2 = HOME / ".m2" / "repository"
        self.gr = HOME / ".gradle"
        m = self.m2
        self.snap = _version(m, "org/jahia/modules/news/3.2.0-SNAPSHOT", 1)
        self.snap_old = _version(m, "org/jahia/modules/news/3.1.0-SNAPSHOT", 400)
        self.news_rel = _version(m, "org/jahia/modules/news/3.0.0", 400)
        self.old1 = _version(m, "com/acme/lib/1.9", 200)
        self.old2 = _version(m, "com/acme/lib/1.10", 150)     # highest version: kept although unused
        self.recent = _version(m, "com/acme/lib/1.2", 5)       # used recently: kept
        self.lonely = _version(m, "org/only/one/0.1", 900)     # only version: kept
        self.snap_only = _version(m, "org/snaponly/thing/1.0-SNAPSHOT", 3)
        (self.snap_only.parent / "maven-metadata-local.xml").write_text("<metadata/>")
        f = self.gr / "caches" / "modules-2" / "files-2.1"
        self.g_old = _version(f, "com.google/guava/31.0", 300)
        self.g_new = _version(f, "com.google/guava/33.0", 200)
        self.g_recent = _version(f, "com.google/guava/32.0", 2)
        for v in ("7.6", "8.1.1", "8.10"):
            _version(self.gr / "caches", f"{v}/kotlin-dsl", 1)
        for v in ("gradle-7.6-bin", "gradle-7.6.4-bin", "gradle-8.5-all", "gradle-8.10.2-bin", "gradle-8.10.2-all"):
            (self.gr / "wrapper" / "dists" / v / "abc").mkdir(parents=True)
            (self.gr / "wrapper" / "dists" / v / "abc" / "gradle.zip").write_bytes(os.urandom(30 * 1024))

    def tearDown(self):
        shutil.rmtree(HOME / ".m2", ignore_errors=True)
        shutil.rmtree(HOME / ".gradle", ignore_errors=True)

    def _items(self):
        res = caches.measure(progress=lambda *_: None)
        return res, {i["id"]: i for i in res["items"]}

    def test_measure_selections(self):
        res, items = self._items()
        self.assertIn("maven", items)
        snaps = {d["path"] for d in db.kv_get("cache_selection:maven-snapshots")["dirs"]}
        self.assertEqual(snaps, {str(self.snap), str(self.snap_old), str(self.snap_only)})
        old = {d["path"] for d in db.kv_get("cache_selection:maven-old")["dirs"]}
        # 3.0.0 is news' only release (kept), 1.10 is lib's highest (kept), 1.2 was used recently
        self.assertEqual(old, {str(self.old1)})
        self.assertIn("2 artifacts", items["maven-snapshots"]["detail"])
        self.assertIn("90+ days", items["maven-old"]["name"])
        g = {d["path"] for d in db.kv_get("cache_selection:gradle-old")["dirs"]}
        dists = self.gr / "wrapper" / "dists"
        self.assertEqual(g, {str(self.g_old), str(self.gr / "caches" / "7.6"), str(self.gr / "caches" / "8.1.1"),
                             str(dists / "gradle-7.6-bin"), str(dists / "gradle-8.5-all")})
        # nested in ~/.m2/repository / ~/.gradle/caches: not counted twice in the total
        du = caches.du_kb
        sel_kb = items["maven-snapshots"]["kb"] + items["maven-old"]["kb"]
        wrapper_kb = sum(d["kb"] for d in db.kv_get("cache_selection:gradle-old")["dirs"]
                         if d["path"].startswith(str(dists)))
        others = sum(i["kb"] for i in res["items"] if i["id"] not in ("maven-snapshots", "maven-old", "gradle-old"))
        self.assertEqual(res["total_kb"], others + wrapper_kb)
        self.assertLessEqual(sel_kb, du(str(self.m2)))

    def test_clean_snapshots(self):
        self._items()
        log = []
        freed = caches.clean("maven-snapshots", log=log.append)
        self.assertGreater(freed, 0)
        for p in (self.snap, self.snap_old, self.snap_only):
            self.assertFalse(p.exists(), p)
        self.assertFalse(self.snap_only.parent.exists(), "artifact folder left with only metadata is removed")
        self.assertFalse((self.m2 / "org" / "snaponly").exists(), "empty parents removed")
        self.assertTrue(self.news_rel.exists())
        self.assertTrue(any("org/jahia/modules/news: 2 versions" in l for l in log), log)
        # the parent "Maven local repository" entry was re-measured too
        items = {i["id"]: i for i in db.kv_get("caches")["items"]}
        self.assertNotIn("maven-snapshots", items)
        self.assertEqual(items["maven"]["kb"], caches.du_kb(str(self.m2)))

    def test_clean_old_rechecks_usage(self):
        self._items()
        # used by a build after the measurement: must be kept
        now = time.time()
        for f in self.old1.iterdir():
            os.utime(f, (now, now))
        log = []
        caches.clean("maven-old", log=log.append)
        self.assertTrue(self.old1.exists())
        self.assertTrue(self.old2.exists() and self.recent.exists() and self.lonely.exists())
        self.assertTrue(any("used since" in l for l in log), log)

    def test_clean_refuses_paths_outside_root(self):
        self._items()
        outside = HOME / "precious" / "1.0-SNAPSHOT"
        outside.mkdir(parents=True)
        (outside / "x.pom").write_text("keep")
        sel = db.kv_get("cache_selection:maven-snapshots")
        sel["dirs"].append({"path": str(outside), "kb": 4, "group": "evil"})
        link = self.m2 / "org" / "link-SNAPSHOT"
        link.symlink_to(outside)
        sel["dirs"].append({"path": str(link), "kb": 4, "group": "evil"})
        sel.pop("_ts", None)
        db.kv_set("cache_selection:maven-snapshots", sel)
        caches.clean("maven-snapshots", log=lambda *_: None)
        self.assertTrue((outside / "x.pom").exists())
        shutil.rmtree(HOME / "precious")

    def test_clean_gradle(self):
        self._items()
        caches.clean("gradle-old", log=lambda *_: None)
        self.assertFalse(self.g_old.exists())
        self.assertTrue(self.g_new.exists() and self.g_recent.exists())
        self.assertTrue((self.gr / "caches" / "8.10").exists())
        self.assertFalse((self.gr / "caches" / "7.6").exists())
        dists = self.gr / "wrapper" / "dists"
        self.assertEqual(sorted(p.name for p in dists.iterdir()),
                         ["gradle-7.6.4-bin", "gradle-8.10.2-all", "gradle-8.10.2-bin"])

    def test_version_key(self):
        k = caches._version_key
        self.assertGreater(k("1.10"), k("1.9"))
        self.assertGreater(k("2.0"), k("2.0-RC1"))
        self.assertGreater(k("8.10.2"), k("8.5"))


if __name__ == "__main__":
    unittest.main()
