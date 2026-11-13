"""mise tests: a fake `mise` on PATH, a fake ~/.local/share/mise and tracked configs.

    python3 -m unittest tests.test_mise -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if "diskhealth" not in sys.modules:
    _T = Path(tempfile.mkdtemp(prefix="dh-mise-"))
    (_T / "home").mkdir()
    (_T / "data").mkdir()
    os.environ["HOME"] = str(_T / "home")
    os.environ["DISK_HEALTH_DATA"] = str(_T / "data")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from diskhealth import config, db  # noqa: E402
from diskhealth.features import mise, toolchains  # noqa: E402

FAKE = r'''#!/bin/sh
echo "$PWD $*" >> "$DH_CALLS"
case "$1" in
  --version) echo "2026.11.0 macos-arm64 (2026-11-01)";;
  outdated)
    case "$PWD" in
      *proj-a) printf '{"node":{"name":"node","requested":"20","current":"20.10.0","bump":null,"latest":"20.19.5","source":{"type":"mise.toml","path":"%s/mise.toml"}}}\n' "$PWD";;
      *) echo '{}';;
    esac;;
  prune)
    if [ "$3" = "--dry-run" ]; then
      echo "mise node@18.0.0 is prunable: not used by any tracked config"
      echo "mise node@18.0.0 [dryrun]  remove ~/.local/share/mise/installs/node/18.0.0, ~/.cache/mise/node/18.0.0"
    else
      rm -rf "$HOME/.local/share/mise/installs/node/18.0.0"; echo "mise node@18.0.0 remove"
    fi;;
  doctor)
    printf 'version: 2026.11.0\nactivated: no\n\n2 problems found:\n\n1. mise is not activated, run `mise help activate`\n2. plugin java is outdated\n   run mise plugins update\n'
    exit 1;;
  upgrade) echo "mise node@20.19.5 installed";;
  *) exit 0;;
esac
'''

_saved = {}


def setUpModule():
    with db._lock:
        _saved.update(conn=db._conn, path=config.DB_PATH)
        db._conn = None
        config.DB_PATH = Path(tempfile.mkdtemp(prefix="dh-mise-db-")) / "db.sqlite3"


def tearDownModule():
    with db._lock:
        if db._conn is not None:
            db._conn.close()
        db._conn, config.DB_PATH = _saved["conn"], _saved["path"]


def mkver(p: Path, kb: int = 8):
    (p / "bin").mkdir(parents=True)
    (p / "bin" / "tool").write_bytes(b"x" * kb * 1024)


class MiseTests(unittest.TestCase):
    def setUp(self):
        t = Path(tempfile.mkdtemp(prefix="dh-mise-t-"))
        self.home = t / "home"
        inst = self.home / ".local/share/mise/installs"
        for v in ("node/20.10.0", "node/18.0.0", "java/temurin-21.0.5+11.0.LTS", "maven/3.9.9"):
            mkver(inst / v, 64 if "18" in v else 8)
        (inst / "node" / "20").symlink_to(inst / "node" / "20.10.0")     # mise's own alias link
        self.pa, self.pb = self.home / "projects/proj-a", self.home / "projects/proj-b"
        self.pa.mkdir(parents=True)
        (self.pb / ".config").mkdir(parents=True)
        (self.pa / "mise.toml").write_text('[tools]\nnode = "20"\n')
        (self.pb / ".config/mise.toml").write_text('[tools]\n"core:python" = ["3.12"]\n')
        g = self.home / ".config/mise"
        g.mkdir(parents=True)
        (g / "config.toml").write_text('[tools]\njava = { version = "temurin-21" }\n')
        tc = self.home / ".local/state/mise/tracked-configs"
        tc.mkdir(parents=True)
        (tc / "aaa").symlink_to(self.pa / "mise.toml")
        (tc / "bbb").symlink_to(self.pb / ".config/mise.toml")
        (tc / "ccc").symlink_to(g / "config.toml")
        (tc / "gone").symlink_to(self.home / "deleted/mise.toml")
        self.bin = t / "bin"
        self.bin.mkdir()
        f = self.bin / "mise"
        f.write_text(FAKE)
        f.chmod(0o755)
        self.calls = t / "calls.log"
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home), "DH_CALLS": str(self.calls),
                                                "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}"})
        self.env.start()
        for k in ("MISE_DATA_DIR", "MISE_STATE_DIR", "MISE_CACHE_DIR", "MISE_CONFIG_DIR", "MISE_INSTALLS_DIR",
                  "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.env.stop()

    def test_tracked_projects(self):
        self.assertEqual(sorted(mise.tracked_projects()), sorted([str(self.pa), str(self.pb)]))

    def test_installed_skips_alias_symlinks(self):
        names = {(i["tool"], i["version"]) for i in mise.installed()}
        self.assertEqual(names, {("node", "20.10.0"), ("node", "18.0.0"), ("java", "temurin-21.0.5+11.0.LTS"),
                                 ("maven", "3.9.9")})

    def test_scan(self):
        r = mise.scan(log=lambda *a: None)
        self.assertTrue(r["available"])
        self.assertEqual(r["version"], "2026.11.0")
        self.assertEqual(r["install_method"], "standalone")
        self.assertEqual(r["versions"], 4)
        self.assertEqual(len(r["outdated"]), 1)
        od = r["outdated"][0]
        self.assertEqual((od["name"], od["current"], od["latest"]), ("node", "20.10.0", "20.19.5"))
        self.assertEqual(od["dirs"], [str(self.pa)])
        self.assertEqual([(p["tool"], p["version"]) for p in r["prunable"]], [("node", "18.0.0")])
        self.assertGreater(r["prunable_kb"], 60)
        # "not activated" is an artefact of running from a service: filtered out
        self.assertEqual(len(r["doctor"]["problems"]), 1)
        self.assertIn("plugin java is outdated", r["doctor"]["problems"][0])
        self.assertEqual(r["tools"][0]["tool"], "node")

    def test_outdated_runs_in_each_project_without_mise_yes(self):
        with mock.patch.dict(os.environ, {"MISE_YES": ""}):
            mise.outdated([str(self.pa), str(self.pb)], log=lambda *a: None)
        cwds = [l.split(" ")[0] for l in self.calls.read_text().splitlines()]
        self.assertEqual(cwds, [str(self.home), str(self.pa), str(self.pb)])
        self.assertNotIn("MISE_YES", mise.ENV)

    def test_upgrade_in_home_and_outdated_projects(self):
        mise.scan(log=lambda *a: None)
        self.calls.write_text("")
        mise.upgrade(log=lambda *a: None)
        ups = [l.split(" ")[0] for l in self.calls.read_text().splitlines() if " upgrade" in l]
        self.assertEqual(ups, [str(self.home), str(self.pa)])
        self.assertNotIn("--bump", self.calls.read_text())

    def test_prune(self):
        mise.scan(log=lambda *a: None)
        freed = mise.prune(log=lambda *a: None)
        self.assertGreater(freed, 60)
        self.assertFalse((self.home / ".local/share/mise/installs/node/18.0.0").exists())
        self.assertEqual(db.kv_get(mise.KEY)["prunable"], [])

    def test_alert_threshold(self):
        mise.scan(log=lambda *a: None)
        kinds = [a["id"].split(":")[0] for a in mise.alerts({"mise_prune_alert_gb": 0.00001})]
        self.assertIn("mise-prune", kinds)
        self.assertNotIn("mise-prune", [a["id"].split(":")[0] for a in mise.alerts({"mise_prune_alert_gb": 1})])

    def test_not_installed(self):
        with mock.patch.object(mise, "mise_bin", lambda: None):
            self.assertEqual(mise.scan(log=lambda *a: None), {"available": False})
        self.assertEqual(mise.alerts({}), [])

    # ---------------------------------------------------------------- toolchains integration
    def test_toolchains_detect_mise(self):
        items = {(i["tool"], i["name"]): i for i in toolchains.detect_mise()}
        self.assertNotIn(("maven", "3.9.9"), items)            # not a language runtime: shown in the mise panel
        java = items[("java", "temurin-21.0.5+11.0.LTS")]
        self.assertTrue(java["default"])                        # global config java = temurin-21
        self.assertEqual(java["vendor"], "Temurin")
        self.assertFalse(items[("node", "18.0.0")]["default"])

    def test_toolchains_pins_from_tracked_configs(self):
        with mock.patch.object(toolchains, "project_roots", lambda: []):
            pins = toolchains.find_pins()
        got = {(p["tool"], p["version"], p["project"]) for p in pins}
        self.assertIn(("node", "20", str(self.pa)), got)
        self.assertIn(("python", "3.12", str(self.pb)), got)
        with mock.patch.object(toolchains, "project_roots", lambda: []):
            items = {i["name"]: i for i in toolchains.detect(measure=False) if i["manager"] == "mise"}
        self.assertEqual(items["20.10.0"]["pinned_by"], [str(self.pa)])
        self.assertFalse(items["20.10.0"]["removable"])
        self.assertEqual(items["18.0.0"]["pinned_by"], [])

    def test_parse_mise_toml(self):
        got = toolchains.parse_mise_toml('[env]\nX = "1"\n[tools]\nnode = "20" # lts\n"core:python" = ["3.11", "3.12"]\n'
                                         'java = { version = "temurin-21", os = ["macos"] }\n"npm:prettier" = "3"\n'
                                         '[settings]\nexperimental = true\n')
        self.assertEqual(got, [("node", "20"), ("python", "3.11"), ("python", "3.12"), ("java", "temurin-21"),
                               ("npm:prettier", "3")])


if __name__ == "__main__":
    unittest.main()
