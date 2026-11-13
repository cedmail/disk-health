"""Update-check tests (real git repos in a temp folder, a local HTTP server for update_url).

    python3 -m unittest tests.test_updates -v
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest

from tests import _env
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from diskhealth import config, db  # noqa: E402
from diskhealth.features import updates as up  # noqa: E402

_saved = {}


def setUpModule():
    # Own HOME + data folder for this module. Must happen here, not at import: unittest
    # discover imports every test module before running any, so the last import would win.
    _env.use("updates")
    with db._lock:
        _saved.update(conn=db._conn, path=config.DB_PATH)
        db._conn = None
        config.DB_PATH = Path(tempfile.mkdtemp(prefix="dh-updates-db-")).resolve() / "db.sqlite3"


def tearDownModule():
    with db._lock:
        if db._conn is not None:
            db._conn.close()
        db._conn, config.DB_PATH = _saved["conn"], _saved["path"]


def git(cwd, *args):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=env)


class GitModeTests(unittest.TestCase):
    def setUp(self):
        t = Path(tempfile.mkdtemp(prefix="dh-up-git-")).resolve()
        self.origin, self.clone = t / "origin", t / "clone"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        (self.origin / "VERSION").write_text("1.0.0\n")
        git(self.origin, "add", ".")
        git(self.origin, "commit", "-qm", "v1")
        git(t, "clone", "-q", str(self.origin), str(self.clone))
        self.p = [mock.patch.object(up, "PROJECT_DIR", self.clone), mock.patch.object(up, "__version__", "1.0.0"),
                  mock.patch.object(up, "_git_usable", lambda: True)]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def test_up_to_date(self):
        rec = up.check(log=lambda *a: None)
        self.assertEqual(rec["behind"], 0)
        self.assertFalse(rec["available"])
        self.assertEqual(up.alerts({}), [])

    def test_new_commits_upstream(self):
        (self.origin / "VERSION").write_text("1.1.0\n")
        git(self.origin, "commit", "-qam", "v1.1")
        rec = up.check(log=lambda *a: None)
        self.assertTrue(rec["available"])
        self.assertEqual(rec["behind"], 1)
        self.assertEqual(rec["latest"], "1.1.0")
        a = up.alerts({})
        self.assertEqual(len(a), 1)
        self.assertIn("1.1.0", a[0]["title"])
        self.assertIn("git pull && ./install.sh", a[0]["cmd"])

    def test_fetch_failure_is_quiet(self):
        git(self.clone, "remote", "set-url", "origin", str(self.origin) + "-missing")
        rec = up.check(log=lambda *a: None)
        self.assertIn("error", rec)
        self.assertEqual(up.alerts({}), [])


class _H(http.server.BaseHTTPRequestHandler):
    body = b"1.2.0\n"

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *a):
        pass


class UrlModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}/VERSION"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.noproj = Path(tempfile.mkdtemp(prefix="dh-up-zip-")).resolve()
        self.p = [mock.patch.object(up, "PROJECT_DIR", self.noproj), mock.patch.object(up, "__version__", "1.0.0")]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()
        _H.body = b"1.2.0\n"

    def _check(self, **cfg):
        with mock.patch.object(config, "load", lambda: dict(config.DEFAULTS, **cfg)):
            return up.check(log=lambda *a: None)

    def test_plain_text_version(self):
        rec = self._check(update_url=self.url, update_page="https://example.com/releases")
        self.assertTrue(rec["available"])
        self.assertEqual(rec["latest"], "1.2.0")
        self.assertEqual(up.alerts({})[0]["url"], "https://example.com/releases")

    def test_github_json(self):
        _H.body = json.dumps({"tag_name": "v1.0.0", "html_url": "https://github.com/x/y/releases/v1.0.0"}).encode()
        rec = self._check(update_url=self.url)
        self.assertFalse(rec["available"])

    def test_no_url_configured(self):
        rec = self._check(update_url="")
        self.assertEqual(rec["mode"], "none")
        self.assertEqual(up.alerts({}), [])

    def test_version_compare(self):
        self.assertGreater(up._vtuple("1.10.0"), up._vtuple("1.9.3"))
        self.assertEqual(up._vtuple("v2.0.0-rc1"), (2, 0, 0))


class BrewModeTests(unittest.TestCase):
    def setUp(self):
        t = Path(tempfile.mkdtemp(prefix="dh-up-brew-")).resolve()
        self.cellar = t / "Cellar/disk-health/1.0.0/libexec"
        self.cellar.mkdir(parents=True)
        (self.cellar / "VERSION").write_text("1.0.0\n")
        (t / "opt/disk-health").mkdir(parents=True)
        self.opt = t / "opt/disk-health/libexec"
        self.opt.symlink_to(self.cellar)
        bindir = t / "bin"
        bindir.mkdir()
        brew = bindir / "brew"
        brew.write_text('#!/bin/sh\necho \'{"formulae":[{"full_name":"jahia/disk-health/disk-health",'
                        '"tap":"jahia/disk-health","versions":{"stable":"1.2.0"}}]}\'\n')
        brew.chmod(0o755)
        import diskhealth
        self.p = [mock.patch.object(diskhealth, "PROJECT_DIR", self.cellar), mock.patch.object(up, "PROJECT_DIR", self.cellar),
                  mock.patch.object(up, "__version__", "1.0.0"),
                  mock.patch.dict(os.environ, {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"})]
        for p in self.p:
            p.start()

    def tearDown(self):
        for p in self.p:
            p.stop()

    def test_brew_opt_dir(self):
        import diskhealth
        self.assertEqual(diskhealth.brew_opt_dir(), self.opt)

    def test_newer_formula(self):
        rec = up.check(log=lambda *a: None)
        self.assertEqual(rec["mode"], "brew")
        self.assertTrue(rec["available"])
        a = up.alerts({})[0]
        self.assertIn("1.2.0", a["title"])
        self.assertEqual(a["cmd"], "brew upgrade disk-health")

    def test_version_on_disk_follows_opt(self):
        import diskhealth
        self.assertEqual(diskhealth.version_on_disk(), "1.0.0")
        new = self.cellar.parent.parent / "1.2.0/libexec"      # brew upgrade: new keg, opt relinked
        new.mkdir(parents=True)
        (new / "VERSION").write_text("1.2.0\n")
        self.opt.unlink()
        self.opt.symlink_to(new)
        self.assertEqual(diskhealth.version_on_disk(), "1.2.0")


class RestartOnUpgradeTests(unittest.TestCase):
    def test_scheduler_exits_when_version_changes(self):
        from diskhealth import server
        with mock.patch.object(server, "version_on_disk", lambda: "9.9.9"), \
                mock.patch.object(server, "python_replaced", lambda: False), \
                mock.patch.object(server.os, "_exit", side_effect=SystemExit(75)) as ex:
            with self.assertRaises(SystemExit):
                server.scheduler_tick(config.load())
            ex.assert_called_once_with(75)

    def test_no_exit_when_same_version(self):
        from diskhealth import server, __version__
        calls = []
        with mock.patch.object(server, "version_on_disk", lambda: __version__), \
                mock.patch.object(server, "python_replaced", lambda: False), \
                mock.patch.object(server.os, "_exit", side_effect=lambda c: calls.append(c)), \
                mock.patch.object(server.jobs, "submit", lambda *a, **k: None), \
                mock.patch.object(server.scanner, "sample_free", lambda *a, **k: None), \
                mock.patch.object(server.trash, "listing", lambda *a, **k: None), \
                mock.patch.object(server.features, "modules", lambda: []):
            server.scheduler_tick(config.load())
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
