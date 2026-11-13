"""Duplicate-file finder tests (temp folders; no macOS needed).

    python3 -m unittest tests.test_duplicates -v

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
MB = 1024 * 1024
dup = db = W = None


def setUpModule():
    global dup, db, W
    _env.use("duplicates")
    from diskhealth import db as _db
    from diskhealth.features import duplicates as _dup
    dup, db = _dup, _db
    W = Path(tempfile.mkdtemp(prefix="dup-work-", dir=os.environ["HOME"])).resolve()


def tearDownModule():
    shutil.rmtree(W, ignore_errors=True)


def _write(p: Path, data: bytes, mtime: float | None = None) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


class DuplicatesTest(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(W, ignore_errors=True)
        W.mkdir()
        self.a, self.b = W / "Downloads", W / "Documents"
        now = time.time()
        self.big = os.urandom(3 * MB)
        _write(self.a / "movie.mp4", self.big, now - 3000)
        _write(self.b / "copy of movie.mp4", self.big, now - 1000)        # newest
        _write(self.b / "deep" / "movie (1).mp4", self.big, now - 2000)
        os.link(self.a / "movie.mp4", self.a / "hardlink.mp4")             # same inode: not a duplicate
        (self.a / "symlink.mp4").symlink_to(self.a / "movie.mp4")          # symlink: skipped
        # same size, same first 64 KB, different end -> not duplicates
        head = os.urandom(64 * 1024)
        _write(self.a / "v1.bin", head + b"A" * MB)
        _write(self.b / "v2.bin", head + b"B" * MB)
        # a second, smaller group
        small = os.urandom(2 * MB)
        _write(self.a / "doc.pdf", small)
        _write(self.b / "doc.pdf", small)
        # different sizes, below the minimum, skipped folders
        _write(self.a / "other.bin", os.urandom(2 * MB + 1))
        _write(self.a / "tiny1.txt", b"x" * 1000)
        _write(self.b / "tiny2.txt", b"x" * 1000)
        _write(self.a / "proj" / "node_modules" / "m.mp4", self.big)
        _write(self.a / "proj" / ".git" / "m.mp4", self.big)
        _write(self.a / "Thing.app" / "Contents" / "m.mp4", self.big)
        _write(self.a / "movie.mp4.icloud", self.big)
        dup._cfg = lambda: ([str(self.a), str(self.b), str(W / "missing")], MB)

    def test_find(self):
        res = dup.find(log=lambda *_: None)
        self.assertEqual(res["group_count"], 2)
        g0, g1 = res["groups"]
        self.assertEqual(g0["size"], 3 * MB)
        self.assertEqual(len(g0["files"]), 3, g0["files"])  # hard link, symlink, skipped dirs excluded
        names = {os.path.basename(f["path"]) for f in g0["files"]}
        self.assertTrue({"copy of movie.mp4", "movie (1).mp4"} < names)
        self.assertEqual(len(names & {"movie.mp4", "hardlink.mp4"}), 1)  # one name per inode
        self.assertEqual([f.get("links") for f in g0["files"] if f["path"].startswith(str(self.a))], [2])
        self.assertEqual(g0["wasted"], 6 * MB)
        self.assertEqual(os.path.basename(g0["files"][0]["path"]), "copy of movie.mp4")  # newest first
        self.assertEqual(g1["size"], 2 * MB)
        self.assertEqual(res["total_wasted"], 8 * MB)
        self.assertEqual(db.kv_get("duplicates")["group_count"], 2)

    def test_trash_and_last_copy(self):
        res = dup.find(log=lambda *_: None)
        g = res["groups"][1]  # doc.pdf x2
        p1, p2 = g["files"][0]["path"], g["files"][1]["path"]
        dup.trash_file(p1, log=lambda *_: None)
        self.assertFalse(os.path.exists(p1))
        self.assertTrue(os.path.exists(p2))
        trashed = [p for p in (Path(os.environ["HOME"]) / ".Trash").iterdir() if p.name.startswith("doc")]
        self.assertTrue(trashed)
        d = db.kv_get("duplicates")
        self.assertEqual(d["group_count"], 1)
        self.assertEqual(d["total_wasted"], 6 * MB)
        # the remaining copy is no longer listed -> refused
        with self.assertRaises(ValueError):
            dup.trash_file(p2, log=lambda *_: None)
        self.assertTrue(os.path.exists(p2))

    def test_refuses_when_other_copy_changed(self):
        res = dup.find(log=lambda *_: None)
        g = res["groups"][1]
        p1, p2 = g["files"][0]["path"], g["files"][1]["path"]
        Path(p2).write_bytes(os.urandom(2 * MB))  # edited since the search
        with self.assertRaises(ValueError):
            dup.trash_file(p1, log=lambda *_: None)
        self.assertTrue(os.path.exists(p1))

    def test_keep_newest(self):
        res = dup.find(log=lambda *_: None)
        g = res["groups"][0]
        dup.keep_newest(g["id"], log=lambda *_: None)
        self.assertTrue((self.b / "copy of movie.mp4").exists())
        self.assertFalse((self.b / "deep" / "movie (1).mp4").exists())
        # exactly one of the two hard-linked names was trashed
        self.assertEqual((self.a / "movie.mp4").exists() + (self.a / "hardlink.mp4").exists(), 1)
        d = db.kv_get("duplicates")
        self.assertEqual([x["size"] for x in d["groups"]], [2 * MB])
        with self.assertRaises(ValueError):
            dup.keep_newest(g["id"], log=lambda *_: None)

    def test_actions_registered(self):
        self.assertEqual(dup.NAME, "duplicates")
        self.assertFalse(hasattr(dup, "scan"))
        self.assertTrue(dup.ACTIONS["duplicates-trash"]["target"])
        self.assertFalse(dup.ACTIONS["duplicates-find"]["target"])


if __name__ == "__main__":
    unittest.main()
