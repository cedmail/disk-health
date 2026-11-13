"""Spotlight feature tests (fake mdfind / mdutil / osascript on PATH).

    python3 -m unittest tests.test_spotlight -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

from tests import _env
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from diskhealth import config, db  # noqa: E402
from diskhealth.features import spotlight as sp  # noqa: E402

FAKES = {
    "mdutil": '#!/bin/sh\necho "/:"\necho "\tIndexing enabled."\n',
    # big numbers for .m2, tiny for .npm
    "mdfind": '#!/bin/sh\ncase "$2" in *".npm"*) echo 12;; *) echo 48210;; esac\n',
    "osascript": '#!/bin/sh\nprintf "4194304\\t/System/Volumes/Data/.Spotlight-V100\\n---DH---\\n"\n'
                 'echo "{\\"Exclusions\\": [\\"$DH_HOME/.m2\\"]}"\n',
}


_saved = {}


def setUpModule():
    # Own HOME + data folder for this module. Must happen here, not at import: unittest
    # discover imports every test module before running any, so the last import would win.
    _env.use("spotlight")
    # Inside `unittest discover` another module may delete its data dir: use a private database file.
    with db._lock:
        _saved.update(conn=db._conn, path=config.DB_PATH)
        db._conn = None
        config.DB_PATH = Path(tempfile.mkdtemp(prefix="dh-spotlight-db-")).resolve() / "db.sqlite3"


def tearDownModule():
    with db._lock:
        if db._conn is not None:
            db._conn.close()
        db._conn, config.DB_PATH = _saved["conn"], _saved["path"]


class SpotlightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-sp-t-")).resolve()
        self.home = self.tmp / "home"
        for d in (".m2/repository", ".npm/_cacache", ".gradle/caches"):
            (self.home / d).mkdir(parents=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name, body in FAKES.items():
            f = self.bin / name
            f.write_text(body)
            f.chmod(0o755)
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home), "DH_HOME": str(self.home),
                                                "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"})
        self.env.start()
        self.mac = mock.patch.object(sp, "IS_MAC", True)
        self.mac.start()
        db.kv_set(sp.ADMIN_KEY, {})
        db.kv_set("git", {})

    def tearDown(self):
        self.mac.stop()
        self.env.stop()

    def test_scan_suggests_big_indexed_folders(self):
        r = sp.scan(log=lambda *a: None)
        self.assertTrue(r["enabled"])
        paths = {os.path.basename(s["path"]): s for s in r["suggestions"]}
        self.assertIn(".m2", paths)
        self.assertIn(".gradle", paths)
        self.assertNotIn(".npm", paths)            # only 12 indexed items: below the threshold
        self.assertEqual(paths[".m2"]["indexed"], 48210)
        self.assertFalse(paths[".m2"]["excluded"])

    def test_zero_indexed_counts_as_excluded(self):
        (self.bin / "mdfind").write_text('#!/bin/sh\ncase "$2" in *".gradle"*) echo 0;; *) echo 48210;; esac\n')
        r = sp.scan(log=lambda *a: None)
        g = next(s for s in r["suggestions"] if s["path"].endswith(".gradle"))
        self.assertTrue(g["excluded"])
        m2 = next(s for s in r["suggestions"] if s["path"].endswith(".m2"))
        self.assertFalse(m2["excluded"])

    def test_inspect_reads_size_and_exclusions(self):
        rec = sp.inspect(log=lambda *a: None)
        self.assertEqual(rec["index_kb"], 4194304)
        self.assertEqual(rec["exclusions"], [str(self.home / ".m2")])
        r = db.kv_get(sp.KEY)
        m2 = next(s for s in r["suggestions"] if s["path"].endswith(".m2"))
        self.assertTrue(m2["excluded"])
        self.assertEqual(r["index_kb"], 4194304)

    def test_inspect_cancelled_keeps_previous(self):
        db.kv_set(sp.ADMIN_KEY, {"index_kb": 100, "exclusions": [], "ts": 1})
        (self.bin / "osascript").write_text('#!/bin/sh\necho "execution error: User canceled. (-128)" >&2\nexit 1\n')
        rec = sp.inspect(log=lambda *a: None)
        self.assertEqual(rec["index_kb"], 100)
        self.assertEqual(rec["error"], "cancelled")

    def test_admin_refuses_quotes(self):
        with self.assertRaises(ValueError):
            sp._admin('echo "x"')

    def test_alert_on_big_index(self):
        db.kv_set(sp.ADMIN_KEY, {"index_kb": 12 * 1024 ** 2})
        self.assertEqual(len(sp.alerts({})), 1)
        db.kv_set(sp.ADMIN_KEY, {"index_kb": 2 * 1024 ** 2})
        self.assertEqual(sp.alerts({}), [])


if __name__ == "__main__":
    unittest.main()
