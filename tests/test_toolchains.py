"""Toolchains feature tests (fake nvm/pyenv/SDKMAN/rustup/JDK layout, fake java_home/osascript/pyenv/rustup/ps).

    python3 -m unittest tests.test_toolchains -v
"""
from __future__ import annotations

import os
import plistlib
import sys
import tempfile
import time
import unittest
from pathlib import Path

if "diskhealth" not in sys.modules:  # standalone run: isolate HOME and data before importing diskhealth
    _T = Path(tempfile.mkdtemp(prefix="dh-tc-"))
    (_T / "home").mkdir()
    (_T / "data").mkdir()
    os.environ["HOME"] = str(_T / "home")
    os.environ["DISK_HEALTH_DATA"] = str(_T / "data")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from diskhealth import config, db, trash  # noqa: E402
from diskhealth.features import toolchains as tc  # noqa: E402

OLD = time.time() - 400 * 86400
RECENT = time.time() - 3 * 86400


def _exe(p: Path, body: str = "#!/bin/sh\nexit 0\n", ts: float | None = None) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    p.chmod(0o755)
    if ts:
        os.utime(p, (ts, ts))


def _ver(path: Path, bins=("bin",), used: float = OLD, size: int = 20000) -> Path:
    for b in bins:
        _exe(path / b / "tool", ts=used)
    (path / "lib").mkdir(parents=True, exist_ok=True)
    (path / "lib" / "blob").write_bytes(b"x" * size)
    os.utime(path, (used, used))
    return path


def _jdk(base: Path, name: str, version: str, vendor: str, used: float) -> Path:
    b = base / name
    _ver(b / "Contents" / "Home", used=used)
    with open(b / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump({"CFBundleName": name, "JavaVM": {"JVMVersion": version, "JVMVendor": vendor}}, f)
    os.utime(b, (used, used))
    return b


def build_home(home: Path, binp: Path, jvm: Path) -> None:
    """Fake toolchain layout used by the tests (and by the screenshot demo)."""
    # nvm
    nvm = home / ".nvm"
    _ver(nvm / "versions/node/v16.20.0", size=60000)
    _ver(nvm / "versions/node/v18.17.0")
    _ver(nvm / "versions/node/v18.12.0")
    _ver(nvm / "versions/node/v20.5.0")
    (nvm / "alias").mkdir(parents=True, exist_ok=True)
    (nvm / "alias/default").write_text("18\n")
    # pyenv (+ a virtualenv)
    py = home / ".pyenv/versions"
    _ver(py / "3.9.18", size=80000)
    _ver(py / "3.11.4")
    (py / "3.11.4/envs/myenv").mkdir(parents=True)
    os.symlink(py / "3.11.4/envs/myenv", py / "myenv")
    _ver(py / "3.12.0", used=RECENT)
    (home / ".pyenv/version").write_text("3.12.0\n")
    # uv
    _ver(home / ".local/share/uv/python/cpython-3.10.13-macos-aarch64-none")
    # SDKMAN
    sdk = home / ".sdkman/candidates"
    _ver(sdk / "java/17.0.9-tem")
    _ver(sdk / "java/11.0.21-tem")
    os.symlink(sdk / "java/17.0.9-tem", sdk / "java/current")
    _ver(sdk / "maven/3.9.5")
    _ver(sdk / "maven/3.6.3")
    os.symlink(sdk / "maven/3.9.5", sdk / "maven/current")
    # rustup
    ru = home / ".rustup"
    for n in ("stable-x86_64-apple-darwin", "1.70.0-x86_64-apple-darwin", "nightly-x86_64-apple-darwin"):
        _ver(ru / "toolchains" / n)
    (ru / "settings.toml").write_text('version = "12"\ndefault_toolchain = "stable-x86_64-apple-darwin"\n\n[overrides]\n')
    # rbenv, go
    _ver(home / ".rbenv/versions/3.2.2")
    (home / ".rbenv/version").write_text("3.2.2\n")
    _ver(home / "sdk/go1.20.5")
    # system JDKs + java_home
    jvm.mkdir(parents=True, exist_ok=True)
    _jdk(jvm, "jdk-21.jdk", "21.0.1", "Oracle Corporation", RECENT)
    _jdk(jvm, "zulu-8.jdk", "1.8.0_292", "Azul Systems, Inc.", OLD)
    _jdk(jvm, "temurin-19.jdk", "19.0.2", "Eclipse Adoptium", OLD)
    _exe(binp / "java_home", f"""#!/bin/sh
if [ "$1" = "-V" ]; then
  echo "Matching Java Virtual Machines (2):" >&2
  echo "    21.0.1 (arm64) \\"Oracle Corporation\\" - \\"Java SE 21.0.1\\" {jvm}/jdk-21.jdk/Contents/Home" >&2
  echo "    1.8.0_292 (arm64) \\"Azul Systems, Inc.\\" - \\"Zulu 8\\" {jvm}/zulu-8.jdk/Contents/Home" >&2
  echo "{jvm}/jdk-21.jdk/Contents/Home"
  exit 0
fi
echo "{jvm}/jdk-21.jdk/Contents/Home"
""")
    # projects pinning versions
    proj = home / "projects"
    (proj / "web").mkdir(parents=True, exist_ok=True)
    (proj / "web/.nvmrc").write_text("v20\n")
    (proj / "svc").mkdir(exist_ok=True)
    (proj / "svc/.sdkmanrc").write_text("# sdk env\njava=11.0.21-tem\n")
    (proj / "legacy/sub").mkdir(parents=True, exist_ok=True)
    (proj / "legacy/sub/.java-version").write_text("1.8\n")
    (proj / "crate").mkdir(exist_ok=True)
    (proj / "crate/.tool-versions").write_text("rust 1.70.0\nnodejs 18.12.0\n")
    (proj / "web/node_modules/dep").mkdir(parents=True, exist_ok=True)
    (proj / "web/node_modules/dep/.python-version").write_text("3.9.18\n")  # skipped: inside node_modules
    (proj / "a/b/c/d/e").mkdir(parents=True, exist_ok=True)
    (proj / "a/b/c/d/e/.ruby-version").write_text("3.2.2\n")  # too deep (depth > 4)


_saved = {}


def setUpModule():
    # Inside `unittest discover` another module may delete its data dir: use a private database file.
    with db._lock:
        _saved.update(conn=db._conn, path=config.DB_PATH)
        db._conn = None
        config.DB_PATH = Path(tempfile.mkdtemp(prefix="dh-toolchains-db-")) / "db.sqlite3"


def tearDownModule():
    with db._lock:
        if db._conn is not None:
            db._conn.close()
        db._conn, config.DB_PATH = _saved["conn"], _saved["path"]


class ToolchainsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_env = {k: os.environ.get(k) for k in ("HOME", "PATH")}
        cls.tmp = Path(tempfile.mkdtemp(prefix="dh-tc-"))
        cls.home, cls.bin, cls.jvm = cls.tmp / "home", cls.tmp / "bin", cls.tmp / "JavaVirtualMachines"
        cls.home.mkdir()
        cls.bin.mkdir()
        os.environ["HOME"] = str(cls.home)
        os.environ["PATH"] = f"{cls.bin}{os.pathsep}{os.environ.get('PATH', '')}"
        for k in ("NVM_DIR", "PYENV_ROOT", "SDKMAN_DIR", "RUSTUP_HOME", "RBENV_ROOT", "JENV_ROOT", "VOLTA_HOME"):
            os.environ.pop(k, None)
        build_home(cls.home, cls.bin, cls.jvm)
        cls.saved = (tc.JAVA_HOME_BIN, tc.SYSTEM_JVM_DIR)
        tc.JAVA_HOME_BIN, tc.SYSTEM_JVM_DIR = str(cls.bin / "java_home"), str(cls.jvm)
        _exe(cls.bin / "ps", '#!/bin/sh\ncat "$HOME/.fake_ps" 2>/dev/null\nexit 0\n')
        _exe(cls.bin / "pyenv", '#!/bin/sh\n[ "$1" = uninstall ] && rm -rf "$PYENV_ROOT/versions/$3"\n')
        _exe(cls.bin / "rustup", '#!/bin/sh\n[ "$1 $2" = "toolchain uninstall" ] && rm -rf "$HOME/.rustup/toolchains/$3"\n')
        cls.items = {(i["manager"], i["name"]): i for i in tc.detect(measure=True)}

    @classmethod
    def tearDownClass(cls):
        tc.JAVA_HOME_BIN, tc.SYSTEM_JVM_DIR = cls.saved
        for k, v in cls.old_env.items():
            os.environ[k] = v

    def it(self, manager, name):
        return self.items[(manager, name)]

    # -------------------------------------------------- detection
    def test_detects_all_managers(self):
        managers = {m for m, _n in self.items}
        for m in ("nvm", "pyenv", "uv", "SDKMAN", "rustup", "rbenv", "go SDK", "System JDK"):
            self.assertIn(m, managers)
        self.assertNotIn(("pyenv", "myenv"), self.items)  # virtualenv symlink isn't a version
        self.assertGreater(self.it("nvm", "v16.20.0")["kb"], 0)

    def test_defaults(self):
        self.assertTrue(self.it("nvm", "v18.17.0")["default"])      # alias "18" -> highest 18.x
        self.assertFalse(self.it("nvm", "v18.12.0")["default"])
        self.assertTrue(self.it("pyenv", "3.12.0")["default"])
        self.assertTrue(self.it("SDKMAN", "17.0.9-tem")["default"])
        self.assertTrue(self.it("SDKMAN", "maven 3.9.5")["default"])
        self.assertTrue(self.it("rustup", "stable-x86_64-apple-darwin")["default"])
        self.assertTrue(self.it("System JDK", "jdk-21.jdk")["default"])
        self.assertTrue(self.it("rbenv", "3.2.2")["default"])

    def test_jdk_plist(self):
        j = self.it("System JDK", "zulu-8.jdk")
        self.assertEqual(j["version"], "1.8.0_292")
        self.assertIn("Azul", j["vendor"])

    def test_pins(self):
        self.assertEqual(len(self.it("nvm", "v20.5.0")["pinned_by"]), 1)
        self.assertTrue(self.it("nvm", "v18.12.0")["pinned_by"])            # .tool-versions nodejs 18.12.0
        self.assertTrue(self.it("SDKMAN", "11.0.21-tem")["pinned_by"])
        self.assertTrue(self.it("System JDK", "zulu-8.jdk")["pinned_by"])  # .java-version 1.8 == 8
        self.assertTrue(self.it("rustup", "1.70.0-x86_64-apple-darwin")["pinned_by"])
        self.assertFalse(self.it("pyenv", "3.9.18")["pinned_by"])          # only inside node_modules
        self.assertFalse(self.it("SDKMAN", "maven 3.6.3")["pinned_by"])    # java pin doesn't pin maven

    def test_unused(self):
        unused = {k for k, i in self.items.items() if i["unused"]}
        for k in [("nvm", "v16.20.0"), ("pyenv", "3.9.18"), ("rustup", "nightly-x86_64-apple-darwin"),
                  ("System JDK", "temurin-19.jdk"), ("SDKMAN", "maven 3.6.3"), ("go SDK", "go1.20.5")]:
            self.assertIn(k, unused)
        for k in [("nvm", "v20.5.0"), ("nvm", "v18.17.0"), ("pyenv", "3.11.4"), ("pyenv", "3.12.0"),
                  ("System JDK", "zulu-8.jdk")]:
            self.assertNotIn(k, unused)

    def test_pin_matching(self):
        it = {"tool": "python", "name": "3.11.4", "version": "3.11.4"}
        self.assertTrue(tc.pin_matches("3.11", it))
        self.assertTrue(tc.pin_matches("3", it))
        self.assertFalse(tc.pin_matches("3.1", it))
        self.assertFalse(tc.pin_matches("system", it))
        r = {"tool": "rust", "name": "stable-aarch64-apple-darwin", "version": "stable"}
        self.assertTrue(tc.pin_matches("stable", r))
        self.assertFalse(tc.pin_matches("nightly", r))
        self.assertEqual(tc._num("temurin-17.0.2+8"), "17.0.2")
        self.assertEqual(tc._num("1.8.0_292"), "8.0.292")

    def test_pin_file_parsing(self):
        d = Path(tempfile.mkdtemp())
        (d / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.72.0"\n')
        self.assertEqual(tc.parse_pin_file(d / "rust-toolchain.toml"), [("rust", "1.72.0")])
        (d / ".tool-versions").write_text("python 3.11.4 3.10.1\ngolang 1.21 # comment\nunknown 1\n")
        self.assertEqual(tc.parse_pin_file(d / ".tool-versions"),
                         [("python", "3.11.4"), ("python", "3.10.1"), ("go", "1.21")])

    # -------------------------------------------------- removal
    def test_remove_refusals(self):
        with self.assertRaisesRegex(ValueError, "default"):
            tc.remove(self.it("nvm", "v18.17.0")["path"], log=lambda *_: None)
        with self.assertRaisesRegex(ValueError, "pinned"):
            tc.remove(self.it("nvm", "v20.5.0")["path"], log=lambda *_: None)
        with self.assertRaisesRegex(ValueError, "not a detected"):
            tc.remove(str(self.home / ".nvm/versions/node"), log=lambda *_: None)
        with self.assertRaisesRegex(ValueError, "not a detected"):
            tc.remove(self.it("nvm", "v16.20.0")["path"] + "/../v18.17.0/bin", log=lambda *_: None)
        with self.assertRaises(ValueError):
            tc.remove("relative/path", log=lambda *_: None)

    def test_remove_refuses_in_use(self):
        p = self.it("SDKMAN", "maven 3.6.3")["path"]
        (self.home / ".fake_ps").write_text(f"/sbin/launchd\n{p}/bin/mvn\n")
        try:
            with self.assertRaisesRegex(ValueError, "in use"):
                tc.remove(p, log=lambda *_: None)
            self.assertTrue(os.path.isdir(p))
        finally:
            (self.home / ".fake_ps").unlink()

    def test_remove_nvm_deletes(self):
        p = self.it("nvm", "v16.20.0")["path"]
        logs = []
        kb = tc.remove(p, log=logs.append)
        self.assertFalse(os.path.exists(p))
        self.assertGreater(kb, 0)
        self.assertTrue(any("nvm uninstall" in l for l in logs))
        r = db.kv_get("toolchains")
        self.assertNotIn(p, [i["path"] for i in r["items"]])

    def test_remove_pyenv_uses_manager(self):
        p = self.it("pyenv", "3.9.18")["path"]
        logs = []
        tc.remove(p, log=logs.append)
        self.assertFalse(os.path.exists(p))
        self.assertTrue(any("pyenv uninstall -f 3.9.18" in l for l in logs))

    def test_remove_rustup_uses_manager(self):
        p = self.it("rustup", "nightly-x86_64-apple-darwin")["path"]
        logs = []
        tc.remove(p, log=logs.append)
        self.assertFalse(os.path.exists(p))
        self.assertTrue(any("rustup toolchain uninstall" in l for l in logs))

    def test_remove_go_moves_to_trash(self):
        p = self.it("go SDK", "go1.20.5")["path"]
        tc.remove(p, log=lambda *_: None)
        self.assertFalse(os.path.exists(p))
        origins = db.kv_get(trash.ORIGINS_KEY) or {}
        dest = next(k for k, v in origins.items() if v == p)
        self.assertTrue(os.path.isdir(dest))
        self.assertTrue(dest.startswith(str(self.home / ".Trash")))

    def test_admin_jdk_osascript(self):
        rec = self.tmp / "osascript.txt"
        _exe(self.bin / "osascript", f'#!/bin/sh\nprintf "%s" "$2" > "{rec}"\nexit 0\n')
        b = str(self.jvm / "temurin-19.jdk")
        ok = tc._admin_remove_jdk(b, log=lambda *_: None)
        self.assertFalse(ok)  # fake didn't delete it -> reported as not removed
        self.assertEqual(rec.read_text(),
                         f"do shell script \"/bin/rm -rf '{self.jvm}/temurin-19.jdk'\" with administrator privileges")
        # cancel
        _exe(self.bin / "osascript", '#!/bin/sh\necho "execution error: User canceled. (-128)" >&2\nexit 1\n')
        logs = []
        self.assertFalse(tc._admin_remove_jdk(b, log=logs.append))
        self.assertTrue(any("cancelled" in l for l in logs))
        # refusals: not directly in the JVM dir, odd names, symlinks
        for bad in (str(self.jvm / "temurin-19.jdk/Contents"), str(self.home / "x.jdk"), str(self.jvm / "a'b.jdk")):
            with self.assertRaises(ValueError):
                tc._admin_remove_jdk(bad, log=lambda *_: None)
        os.symlink(self.jvm / "temurin-19.jdk", self.jvm / "link.jdk")
        try:
            with self.assertRaises(ValueError):
                tc._admin_remove_jdk(str(self.jvm / "link.jdk"), log=lambda *_: None)
        finally:
            os.unlink(self.jvm / "link.jdk")

    def test_scan_and_alert(self):
        r = tc.scan(log=lambda *_: None)
        self.assertEqual(r["count"], len(r["items"]))
        self.assertGreater(r["unused_count"], 0)
        a = tc.alerts({})
        self.assertEqual(a[0]["section"], "toolchains")


if __name__ == "__main__":
    unittest.main()
