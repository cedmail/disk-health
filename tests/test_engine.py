"""Engine tests that run on Linux with fake macOS/Homebrew/Docker binaries.

    python3 -m unittest tests.test_engine -v      (from the repo root; works on Python 3.9+)
"""
import http.client
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

from tests import _env

# ---------------------------------------------------------------- environment (before importing diskhealth)
TMP = Path(tempfile.mkdtemp(prefix="dh-test-")).resolve()
HOME = TMP / "home"
DATA = TMP / "data"
BIN = TMP / "bin"
PREFIX = TMP / "brew-prefix"
OUTSIDE = TMP / "outside"
for d in (BIN, PREFIX, OUTSIDE):
    d.mkdir(parents=True)
for d in (HOME, DATA):
    d.mkdir(parents=True, exist_ok=True)
os.environ["FAKE_PREFIX"] = str(PREFIX)


def setUpModule():
    # Own HOME + data folder for this module. Must happen here, not at import: unittest
    # discover imports every test module before running any, so the last import would win.
    _env.use("engine", root=TMP)
    os.environ["PATH"] = f"{BIN}{os.pathsep}{os.environ.get('PATH', '')}"

OLD = int(time.time()) - 400 * 86400  # install time of an unused formula
RECENT = int(time.time()) - 2 * 86400

FAKE_BREW = r"""#!/bin/bash
case "$1" in
  update) exit 0 ;;
  --prefix) echo "$FAKE_PREFIX"; exit 0 ;;
  outdated)
    echo "Warning: some tap is deprecated" >&2
    cat <<'EOF'
{
  "formulae": [
    {"name": "git", "installed_versions": ["2.44.0"], "current_version": "2.45.1", "pinned": false, "pinned_version": null},
    {"name": "node", "installed_versions": ["21.7.1", "21.7.2"], "current_version": "22.2.0", "pinned": true, "pinned_version": "21.7.2"}
  ],
  "casks": [
    {"name": "firefox", "installed_versions": ["125.0.3"], "current_version": "126.0"},
    {"name": "iterm2", "installed_versions": "3.4.23", "current_version": "3.5.0"}
  ]
}
EOF
    exit 1 ;;
  autoremove)
    cat <<'EOF'
==> Would autoremove 3 unneeded formulae:
libfoo
libbar python@3.11
EOF
    echo "Warning: Not autoremoving pinned formulae" >&2
    exit 0 ;;
  doctor)
    if [ -n "$FAKE_DOCTOR_OK" ]; then echo "Your system is ready to brew."; exit 0; fi
    echo "Please note that these warnings are just used to help the Homebrew maintainers"
    echo "with debugging if you file an issue. If everything you use Homebrew for"
    echo "is working fine: please don't worry or file an issue; just ignore this. Thanks!"
    echo ""
    echo "Warning: Some installed formulae are deprecated or disabled."
    echo "You should find replacements for the following formulae:"
    echo "  python@3.8"
    echo ""
    echo "Warning: Unbrewed dylibs were found in /usr/local/lib."
    echo "If you didn't put them there on purpose they could cause problems."
    echo ""
    echo "Unexpected dylibs:"
    echo "  /usr/local/lib/libfoo.dylib"
    exit 1 ;;
  cleanup)
    if [ "$2" = "-n" ]; then
      echo "Would remove: /Users/x/Library/Caches/Homebrew/git--2.44.0.bottle.tar.gz (8.1MB)"
      echo "==> This operation would free approximately 1.2GB of disk space."
    else
      rm -rf "$HOME/Library/Caches/Homebrew/"*
      echo "Removing: old bottles"
    fi
    exit 0 ;;
  leaves) printf 'ripgrep\nuser/tap/footool\n'; exit 0 ;;
  info)
    if [ "$2" = "--cask" ]; then
      cat <<'EOF'
{"formulae": [], "casks": [
  {"token": "visual-studio-code", "artifacts": [{"uninstall": [{"quit": "com.microsoft.VSCode"}]}, {"app": ["Visual Studio Code.app"]}, {"binary": ["code"]}, {"zap": [{"trash": ["~/Library/Caches/com.microsoft.VSCode"]}]}]},
  {"token": "renamed", "artifacts": [{"app": ["Source.app", {"target": "Target Name.app"}]}]},
  {"token": "legacy", "artifacts": [["Legacy.app"], {"zap": []}]}
]}
EOF
    else
      cat <<EOF
{"formulae": [
  {"name": "ripgrep", "full_name": "ripgrep", "desc": "Search tool", "installed": [{"version": "14.1.0", "time": $RECENT_TS, "installed_on_request": true}]},
  {"name": "footool", "full_name": "user/tap/footool", "desc": "Tap tool", "installed": [{"version": "1.0", "time": $OLD_TS, "installed_on_request": true}]},
  {"name": "libfoo", "full_name": "libfoo", "desc": "dep", "installed": [{"version": "1.0", "time": $OLD_TS, "installed_on_request": false}]}
], "casks": []}
EOF
    fi
    exit 0 ;;
  uninstall) [ -n "$FAKE_BREW_UNINSTALL_FAIL" ] && { echo "Error: boom" >&2; exit 1; }; echo "Uninstalling $*"; exit 0 ;;
esac
echo "fake brew: unhandled $*" >&2
exit 1
"""

FAKE_DOCKER = r"""#!/bin/bash
if [ "$1 $2" = "system df" ]; then
  echo '{"Active":"2","Reclaimable":"1.5GB (45%)","Size":"3.2GB","TotalCount":"7","Type":"Images"}'
  echo '{"Active":"1","Reclaimable":"0B (0%)","Size":"12kB","TotalCount":"2","Type":"Containers"}'
  echo '{"Active":"1","Reclaimable":"9GB (90%)","Size":"10GB","TotalCount":"3","Type":"Local Volumes"}'
  echo '{"Active":"0","Reclaimable":"512MB","Size":"512MB","TotalCount":"40","Type":"Build Cache"}'
  exit 0
fi
exit 0
"""

# mdls -raw: NUL-separated values, no trailing NUL; missing files -> stderr message and no value.
FAKE_MDLS = r"""#!/bin/bash
shift 5  # -raw -nullMarker NONE -name kMDItemLastUsedDate
first=1
for f in "$@"; do
  case "$f" in
    *missing*) echo "$f: could not find $f." >&2; continue ;;
    *Used*) v="2024-01-15 10:23:45 +0000" ;;
    *) v="NONE" ;;
  esac
  [ $first = 1 ] || printf '\0'
  printf '%s' "$v"
  first=0
done
"""


def _write_bin(name, body, **subst):
    p = BIN / name
    for k, v in subst.items():
        body = body.replace(k, str(v))
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


_write_bin("brew", FAKE_BREW, **{"$RECENT_TS": RECENT, "$OLD_TS": OLD})
_write_bin("docker", FAKE_DOCKER)
_write_bin("mdls", FAKE_MDLS)

(DATA / "config.json").write_text(json.dumps({
    "scan_roots": [str(HOME / "scan")], "scan_min_mb": 0, "scan_depth": 3, "scan_exclude": [],
    "free_warn_percent": 0, "free_critical_percent": 0,
    "extra_caches": [{"id": "bad-docs", "name": "Bad", "group": "x", "paths": ["~/Documents"], "clean": "contents",
                      "risk": "safe"}],
}))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from diskhealth import alerts, apps, brew, caches, config, db, gitrepos, jobs, scanner, server, util  # noqa: E402


def _mkfile(p: Path, size: int = 64 * 1024) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(os.urandom(size))
    return p


def _wait_job(jid, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = jobs.get(jid)
        if j["status"] in ("done", "failed"):
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


# ---------------------------------------------------------------- brew
class DirIdentityTest(unittest.TestCase):
    """util's inode-based directory helpers.

    A path string does not identify a directory. On macOS's default case-insensitive volume
    ~/projects and ~/Projects are one folder, and os.path.realpath() does not say so: it
    resolves symlinks, never letter case. De-duplicating roots by realpath therefore kept both
    spellings, and the Git panel listed (and du-ed) every clone twice.
    """

    @classmethod
    def setUpClass(cls):
        cls.base = OUTSIDE / "ident"
        (cls.base / "projects" / "repo").mkdir(parents=True, exist_ok=True)
        (cls.base / "other").mkdir(exist_ok=True)
        cls.ci = os.path.isdir(cls.base / "PROJECTS")   # case-insensitive volume?

    def test_same_dir_sees_through_spelling(self):
        p = str(self.base / "projects")
        self.assertTrue(util.same_dir(p, p + "/"))
        self.assertTrue(util.same_dir(p, str(self.base / "." / "projects")))
        self.assertFalse(util.same_dir(p, str(self.base / "other")))
        self.assertFalse(util.same_dir(p, str(self.base / "missing")))

    def test_is_under_uses_inodes_not_prefixes(self):
        base, proj = str(self.base), str(self.base / "projects")
        self.assertTrue(util.is_under(proj, base))
        self.assertTrue(util.is_under(str(self.base / "projects" / "repo"), base))
        self.assertTrue(util.is_under(base, base))                       # a root contains itself
        self.assertFalse(util.is_under(base, proj))
        self.assertFalse(util.is_under(str(OUTSIDE), proj))
        # a sibling whose *name* starts with the root's name is not inside it
        (self.base.parent / (self.base.name + "-sibling")).mkdir(exist_ok=True)
        self.assertFalse(util.is_under(str(self.base.parent / (self.base.name + "-sibling")), base))

    def test_is_under_follows_a_symlinked_path(self):
        link = self.base / "link-to-projects"
        if not link.exists():
            link.symlink_to(self.base / "projects")
        self.assertTrue(util.is_under(str(link / "repo"), str(self.base / "projects")))

    def test_dedupe_dirs_collapses_case_variants(self):
        if not self.ci:
            self.skipTest("case-sensitive filesystem")
        both = [str(self.base / "projects"), str(self.base / "Projects")]
        self.assertTrue(all(os.path.isdir(p) for p in both), "both spellings resolve on this volume")
        self.assertNotEqual(os.path.realpath(both[0]), os.path.realpath(both[1]),
                            "realpath does not canonicalise case -- this is why realpath dedup failed")
        self.assertEqual(util.dedupe_dirs(both), both[:1])

    def test_dedupe_dirs_keeps_distinct_dirs_and_drops_missing(self):
        d = util.dedupe_dirs([str(self.base / "projects"), str(self.base / "other"),
                              str(self.base / "nope"), ""])
        self.assertEqual(d, [str(self.base / "projects"), str(self.base / "other")])

    def test_scan_roots_drops_nested_roots(self):
        sibling = OUTSIDE.parent / "ident-sibling"
        sibling.mkdir(exist_ok=True)
        roots = util.scan_roots([str(self.base), str(self.base / "projects"), str(sibling)])
        self.assertIn(str(self.base), roots)
        self.assertNotIn(str(self.base / "projects"), roots)   # nested in base
        self.assertIn(str(sibling), roots)
        # the outer root wins however the list is ordered
        self.assertEqual(util.scan_roots([str(self.base / "projects"), str(self.base)]), [str(self.base)])

    def test_git_and_toolchain_roots_are_listed_once(self):
        """The regression itself: both default roots exist on a case-insensitive volume."""
        if not self.ci:
            self.skipTest("case-sensitive filesystem")
        (HOME / "projects").mkdir(exist_ok=True)
        self.assertTrue(os.path.isdir(HOME / "Projects"), "both spellings must resolve")
        self.assertEqual(len(gitrepos.roots()), len(set(util.dir_key(r) for r in gitrepos.roots())))
        self.assertEqual(len(gitrepos.roots()), 1)


class BrewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for name, bins in (("ripgrep", ["rg"]), ("footool", ["footool"])):
            for b in bins:
                _mkfile(PREFIX / "opt" / name / "bin" / b, 10)
            _mkfile(PREFIX / "Cellar" / name / "1.0" / "bin" / bins[0], 4096)
        # zsh extended history: metafied non-ASCII, a multi-line command, keyword-led commands.
        hist = (
            f": {RECENT}:0;rg TODO src\n"
            f": {RECENT - 10}:0;for f in *; do\\\n  rg -l foo \"$f\"\\\ndone\n"
            f": {RECENT - 20}:0;sudo git status && FOO=1 node app.js | less\n"
        ).encode() + f": {RECENT - 30}:0;echo a".encode() + b"\xe2\x80\x83\xb4" + b"b\n"  # "—" (E2 80 94) metafied
        (HOME / ".zsh_history").write_bytes(hist)

    def test_history_entries(self):
        raw = (HOME / ".zsh_history").read_bytes()
        entries = brew._history_entries(raw)
        self.assertEqual(len(entries), 4)  # the 3-line `for` loop is one entry
        self.assertIn("rg -l foo", entries[1][1])
        self.assertEqual(entries[3][1], "echo a—b")  # unmetafied
        usage = brew._history_usage()
        self.assertEqual(usage["rg"][1], 2)
        self.assertEqual(usage["rg"][0], RECENT)
        for w in ("git", "node", "less", "echo"):
            self.assertIn(w, usage)
        for w in ("do", "done", "for", "sudo", "FOO=1", "-l"):
            self.assertNotIn(w, usage)

    def test_check(self):
        res = brew.check(progress=lambda *_: None)
        self.assertTrue(res["available"])
        self.assertEqual(res["prefix"], str(PREFIX))
        by = {o["name"]: o for o in res["outdated"]}
        self.assertEqual(set(by), {"git", "node", "firefox", "iterm2"})
        self.assertEqual(by["node"]["from"], "21.7.1, 21.7.2")
        self.assertTrue(by["node"]["pinned"])
        self.assertEqual(by["iterm2"]["from"], "3.4.23")  # string, not "3, ., 4, ..."
        self.assertEqual(by["iterm2"]["kind"], "cask")
        self.assertEqual(res["orphans"], ["libfoo", "libbar", "python@3.11"])
        self.assertFalse(res["doctor_ok"])
        self.assertEqual([w["title"] for w in res["doctor"]],
                         ["Some installed formulae are deprecated or disabled.",
                          "Unbrewed dylibs were found in /usr/local/lib."])
        self.assertIn("libfoo.dylib", res["doctor"][1]["body"])
        self.assertEqual(res["cleanup_reclaimable"], "1.2GB")
        leaves = {l["name"]: l for l in res["leaves"]}
        self.assertEqual(set(leaves), {"ripgrep", "footool"})  # tap leaf matched by full_name
        self.assertFalse(leaves["ripgrep"]["unused"])
        self.assertEqual(leaves["ripgrep"]["uses"], 2)
        self.assertTrue(leaves["footool"]["unused"])
        self.assertEqual(res["unused_leaves"], 1)
        self.assertEqual(db.kv_get("brew")["cleanup_reclaimable"], "1.2GB")

    def test_doctor_ok(self):
        os.environ["FAKE_DOCTOR_OK"] = "1"
        try:
            res = brew.check(progress=lambda *_: None)
        finally:
            del os.environ["FAKE_DOCTOR_OK"]
        self.assertTrue(res["doctor_ok"])
        self.assertEqual(res["doctor"], [])

    def test_parsers_tolerate_variants(self):
        self.assertEqual(brew.parse_autoremove("==> Would autoremove 2 unneeded formulae:\na b\n"), ["a", "b"])
        self.assertEqual(brew.parse_autoremove(""), [])
        self.assertEqual(brew._versions("1.0"), "1.0")
        self.assertEqual(brew._versions(None), "")
        self.assertEqual(brew._json_obj("garbage"), {})


# ---------------------------------------------------------------- apps
class AppsTest(unittest.TestCase):
    def test_cask_map(self):
        m = apps._cask_map()
        self.assertEqual(m["Visual Studio Code.app"], "visual-studio-code")
        self.assertEqual(m["Target Name.app"], "renamed")
        self.assertNotIn("Source.app", m)
        self.assertEqual(m["Legacy.app"], "legacy")

    def test_mdls(self):
        old = apps.IS_MAC
        apps.IS_MAC = True
        try:
            got = apps._last_used(["/A/Used.app", "/A/Never.app", "/A/Used2.app"])
            self.assertEqual(got["/A/Used.app"], 1705314225)
            self.assertIsNone(got["/A/Never.app"])
            self.assertEqual(got["/A/Used2.app"], 1705314225)
            # A missing file shifts mdls' values: we must fall back to per-file queries, not misattribute.
            got = apps._last_used(["/A/missing.app", "/A/Never.app", "/A/Used.app"])
            self.assertEqual(got, {"/A/missing.app": None, "/A/Never.app": None, "/A/Used.app": 1705314225})
        finally:
            apps.IS_MAC = old

    def test_leftovers_name_matching(self):
        app = HOME / "Applications" / "Code.app"
        _mkfile(app / "Contents" / "MacOS" / "x", 10)
        import plistlib
        (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.example.code"}))
        lib = HOME / "Library"
        _mkfile(lib / "Application Support" / "Code" / "x", 10)          # too short a name: not matched
        _mkfile(lib / "Preferences" / "com.example.code.plist", 10)       # bundle id: matched
        _mkfile(lib / "Containers" / "com.example.code" / "x", 10)        # bundle id: matched
        found = {Path(i["path"]).relative_to(lib).as_posix() for i in apps.leftovers(str(app))}
        self.assertEqual(found, {"Preferences/com.example.code.plist", "Containers/com.example.code"})

        app2 = HOME / "Applications" / "Fancy Tool.app"
        _mkfile(app2 / "Contents" / "MacOS" / "x", 10)
        _mkfile(lib / "Application Support" / "Fancy Tool" / "x", 10)
        _mkfile(lib / "Preferences" / "Fancy Tool", 10)  # name match outside the name dirs: ignored
        found = {Path(i["path"]).relative_to(lib).as_posix() for i in apps.leftovers(str(app2))}
        self.assertEqual(found, {"Application Support/Fancy Tool"})

    def test_uninstall_falls_back_to_trash_when_brew_fails(self):
        app = HOME / "Applications" / "Broken.app"
        _mkfile(app / "Contents" / "MacOS" / "x", 1024)
        db.kv_set("apps", {"items": [{"path": str(app), "kb": 4, "cask": "broken"}]})
        os.environ["FAKE_BREW_UNINSTALL_FAIL"] = "1"
        try:
            lines = []
            apps.uninstall(str(app), log=lines.append)
        finally:
            del os.environ["FAKE_BREW_UNINSTALL_FAIL"]
        self.assertFalse(app.exists())
        self.assertTrue((HOME / ".Trash" / "Broken.app").exists())
        self.assertTrue(any("Trash instead" in l for l in lines))


# ---------------------------------------------------------------- caches
class CachesTest(unittest.TestCase):
    def setUp(self):
        self.lib = HOME / "Library"
        c = self.lib / "Caches"
        shutil.rmtree(c, ignore_errors=True)
        _mkfile(c / "Homebrew" / "bottle.tar.gz", 200 * 1024)
        _mkfile(c / "pip" / "http" / "a", 100 * 1024)
        _mkfile(c / "Google" / "Chrome" / "Default" / "Cache" / "data", 300 * 1024)
        _mkfile(c / "Google" / "Other" / "x", 50 * 1024)
        _mkfile(c / "RandomApp" / "cache.db", 120 * 1024)
        _mkfile(c / "loose-file.bin", 40 * 1024)
        self.ext = OUTSIDE / "precious"
        _mkfile(self.ext / "keep.txt", 30 * 1024)
        (c / "link-to-outside").symlink_to(self.ext)
        yarn_target = OUTSIDE / "yarn-store"
        _mkfile(yarn_target / "pkg.tgz", 30 * 1024)
        (c / "Yarn").symlink_to(yarn_target)       # registered cache that is a symlink
        _mkfile(self.lib / "Logs" / "JetBrains" / "idea.log", 80 * 1024)
        _mkfile(self.lib / "Logs" / "system.log", 20 * 1024)
        _mkfile(HOME / "Documents" / "thesis.tex", 10 * 1024)

    def test_measure_and_clean(self):
        res = caches.measure(progress=lambda *_: None)
        items = {i["id"]: i for i in res["items"]}
        du = caches.du_kb
        c = self.lib / "Caches"
        expected_other = du(str(c)) - du(str(c / "Homebrew")) - du(str(c / "pip")) - du(str(c / "Google"))
        self.assertEqual(items["other-caches"]["kb"], expected_other)
        self.assertEqual(items["chrome"]["kb"], du(str(c / "Google" / "Chrome")))
        self.assertIn("docker", items)
        self.assertEqual(items["docker"]["kb"], int((1.5e9 + 512e6) / 1024))  # decimal units, volumes excluded
        # ~/Library/Logs/JetBrains is in both "logs" and "jetbrains": counted once in the total.
        total = sum(i["kb"] for i in res["items"])
        self.assertEqual(res["total_kb"], total - du(str(self.lib / "Logs" / "JetBrains")))

        log = []
        freed = caches.clean("other-caches", log=log.append)
        self.assertGreater(freed, 0)
        self.assertFalse((c / "RandomApp").exists())
        self.assertFalse((c / "loose-file.bin").exists())
        self.assertFalse((c / "link-to-outside").exists())
        self.assertTrue((self.ext / "keep.txt").exists(), "symlink target must survive")
        self.assertTrue((c / "Google" / "Other" / "x").exists(), "folders containing a registered cache are kept")
        self.assertTrue((c / "Google" / "Chrome" / "Default" / "Cache" / "data").exists())
        self.assertTrue((c / "Homebrew" / "bottle.tar.gz").exists())
        self.assertTrue((c / "pip" / "http" / "a").exists())
        # after cleaning, other-caches measures ~0 (only folder entries remain)
        res = caches.measure(progress=lambda *_: None)
        other = next((i for i in res["items"] if i["id"] == "other-caches"), {"kb": 0})
        self.assertLess(other["kb"], 16)

    def test_clean_contents_and_safety(self):
        c = self.lib / "Caches"
        caches.clean("pip", log=lambda *_: None)
        self.assertTrue((c / "pip").is_dir())
        self.assertEqual(list((c / "pip").iterdir()), [])
        caches.clean("chrome", log=lambda *_: None)
        self.assertEqual(list((c / "Google" / "Chrome").iterdir()), [])
        self.assertTrue((c / "Google" / "Other" / "x").exists())
        log = []
        caches.clean("yarn", log=log.append)
        self.assertTrue((OUTSIDE / "yarn-store" / "pkg.tgz").exists())
        self.assertTrue(any("symlink" in l for l in log))
        log = []
        caches.clean("bad-docs", log=log.append)  # user config pointing at ~/Documents
        self.assertTrue((HOME / "Documents" / "thesis.tex").exists())
        self.assertTrue(any("protected" in l for l in log))
        caches.clean("brew", log=lambda *_: None)  # clean="cmd" via the fake brew
        self.assertEqual(list((c / "Homebrew").iterdir()), [])

    def test_protected_folders_are_matched_by_inode_not_by_name(self):
        """A protected folder must stay protected however its path is spelled.

        The check used to compare realpath() strings. On a case-insensitive volume
        "~/documents" and "~/Documents" are the same folder but different strings, so a
        user-supplied extra_caches entry in the wrong case would have been emptied.
        """
        docs = HOME / "Documents"
        docs.mkdir(exist_ok=True)
        self.assertEqual(caches._safe_target(str(docs)), "is a protected folder")
        self.assertEqual(caches._safe_target(str(docs) + "/"), "is a protected folder")
        self.assertEqual(caches._safe_target(str(HOME / "." / "Documents")), "is a protected folder")
        if os.path.isdir(HOME / "documents"):      # case-insensitive volume
            self.assertEqual(caches._safe_target(str(HOME / "documents")), "is a protected folder")
            self.assertEqual(caches._safe_target(str(HOME / "DOCUMENTS")), "is a protected folder")
        # the home folder itself, and anything outside it, are refused too
        self.assertEqual(caches._safe_target(str(HOME)), "is outside your home folder")
        self.assertEqual(caches._safe_target(str(OUTSIDE)), "is outside your home folder")
        # a real cache folder underneath a protected one is still allowed
        ok = HOME / "Library" / "Caches" / "some-tool"
        ok.mkdir(parents=True, exist_ok=True)
        self.assertIsNone(caches._safe_target(str(ok)))

    def test_parse_size(self):
        self.assertEqual(caches._parse_size("1.2GB (45%)"), int(1.2e9 / 1024))
        self.assertEqual(caches._parse_size("12kB"), int(12000 / 1024))
        self.assertEqual(caches._parse_size("0B (0%)"), 0)
        self.assertEqual(caches._parse_size(""), 0)


# ---------------------------------------------------------------- scanner + alerts
class ScannerTest(unittest.TestCase):
    def setUp(self):
        for t in ("snapshots", "sizes"):
            db.execute(f"DELETE FROM {t}")
        db.execute("DELETE FROM kv WHERE key IN ('caches', 'brew', 'apps')")

    def test_take_snapshot(self):
        root = HOME / "scan"
        _mkfile(root / "a" / "b" / "f", 100 * 1024)
        _mkfile(root / "a_b%" / "f", 10 * 1024)
        _mkfile(root / "c" / "f", 10 * 1024)
        sid = scanner.take_snapshot(progress=lambda *_: None)
        snaps = scanner.snapshots()
        self.assertEqual([s["id"] for s in snaps], [sid])  # complete (duration_s > 0) even for a fast scan
        tree = scanner.children(None)
        self.assertEqual([i["path"] for i in tree["items"]], [str(root)])
        tree = scanner.children(str(root))
        self.assertEqual({Path(i["path"]).name for i in tree["items"]}, {"a", "a_b%", "c"})
        # "_" and "%" are literal (no LIKE wildcards): a_b% has no children; a has exactly b
        self.assertEqual(scanner.children(str(root / "a_b%"))["items"], [])
        self.assertEqual([Path(i["path"]).name for i in scanner.children(str(root / "a") + "/")["items"]], ["b"])

    def _snap(self, ts, roots, sizes):
        sid = db.execute("INSERT INTO snapshots (ts, duration_s, roots) VALUES (?,?,?)", (ts, 1.0, ",".join(roots))).lastrowid
        db.executemany("INSERT INTO sizes VALUES (?,?,?,?)", [(sid, p, p.count("/"), kb) for p, kb in sizes.items()])
        return sid

    def test_growth_and_alerts(self):
        GB = 1024 ** 2
        now = int(time.time())
        roots = ["/r", "/s"]
        self._snap(now - 30 * 86400, ["/r"], {"/r": 50 * GB, "/r/a": 2 * GB, "/r/a/x": 1 * GB})
        base7 = self._snap(now - 8 * 86400, ["/r"], {"/r": 50 * GB, "/r/a": 2 * GB, "/r/a/x": 1 * GB,
                                                     "/r/b": 4 * GB, "/r/b/y": 1 * GB})
        self._snap(now - 2 * 86400, roots, {"/r": 52 * GB, "/r/a": 3 * GB, "/r/a/x": 1 * GB})
        self._snap(now, roots, {
            "/r": 60 * GB,
            "/r/a": 7 * GB, "/r/a/x": 5.8 * GB,      # child explains >=80% -> parent dropped
            "/r/b": 7 * GB, "/r/b/y": 2 * GB,         # child explains 1/3 -> both kept
            "/r/new": 1 * GB,                         # new folder under a measured root
            "/s": 100 * GB, "/s/big": 90 * GB,        # root missing from the 7-day baseline: not "growth"
        })
        g = scanner.growth(7)
        self.assertTrue(g["ready"])
        self.assertEqual(g["from_ts"], now - 8 * 86400)  # closest snapshot at least ~7 days old
        self.assertFalse(g["partial"])
        paths = [i["path"] for i in g["items"]]
        self.assertNotIn("/r/a", paths)
        self.assertIn("/r/a/x", paths)
        self.assertIn("/r/b", paths)
        self.assertIn("/r/b/y", paths)
        self.assertIn("/r/new", paths)
        self.assertNotIn("/s", paths)
        self.assertNotIn("/s/big", paths)
        self.assertEqual(paths[0], "/r", "sorted by delta")  # +10 GB; children explain < 80%
        item = {i["path"]: i for i in g["items"]}
        self.assertTrue(item["/r/a/x"]["abnormal"])
        self.assertTrue(item["/r/new"]["new"])
        self.assertEqual(len(item["/r/a/x"]["series"]), 4)

        g30 = scanner.growth(30)
        self.assertEqual(g30["from_ts"], now - 30 * 86400)

        db.kv_set("caches", {"items": [], "total_kb": 25 * GB})
        db.kv_set("brew", {"outdated": [{"name": "git"}], "doctor": [{"title": "w"}], "orphans": ["libfoo"],
                           "unused_leaves": 2})
        db.kv_set("apps", {"unused_count": 3, "unused_kb": 5 * GB})
        a = alerts.compute()
        ids = [x["id"] for x in a]
        self.assertTrue(any(i.startswith("growth:") for i in ids))
        self.assertTrue(any(i.startswith("caches:") for i in ids))
        self.assertTrue(any(i.startswith("brew:outdated:") for i in ids))
        self.assertTrue(any(i.startswith("brew:orphans:") for i in ids))
        self.assertTrue(any(i.startswith("apps:") for i in ids))
        self.assertTrue(any(i.startswith("tools:") for i in ids))
        self.assertFalse(any(i.startswith("free:") for i in ids))
        self.assertEqual(ids, [x["id"] for x in alerts.compute()], "ids must be stable")

        orig = scanner.sample_free
        scanner.sample_free = lambda record=True: {"ts": now, "total": 1000, "used": 950, "free": 50}
        try:
            cfg = json.loads((DATA / "config.json").read_text())
            (DATA / "config.json").write_text(json.dumps(dict(cfg, free_critical_percent=8, free_warn_percent=15)))
            a = alerts.compute()
            self.assertEqual(a[0]["level"], "critical")
        finally:
            scanner.sample_free = orig
            (DATA / "config.json").write_text(json.dumps(cfg))

    def test_growth_not_ready(self):
        self.assertFalse(scanner.growth(7)["ready"])
        self._snap(int(time.time()), ["/r"], {"/r": 1})
        self.assertFalse(scanner.growth(7)["ready"])


# ---------------------------------------------------------------- scheduler
class SchedulerTest(unittest.TestCase):
    def test_brew_throttle_and_failure(self):
        cfg = dict(config.load(), scan_every_hours=10 ** 6)
        db.kv_set("last_full_scan", {"ts": int(time.time())})
        db.kv_set("brew", {"available": True, "outdated": [{"name": "git"}], "ts": 1})
        db.execute("UPDATE kv SET ts=? WHERE key='brew'", (int(time.time()) - 2 * 86400,))
        orig = brew.check

        release = threading.Event()

        def boom(progress=print):
            release.wait(10)
            raise RuntimeError("brew exploded")

        brew.check = boom
        try:
            server.scheduler_tick(cfg)
            self.assertTrue(db.kv_get("brew").get("pending"))
            n_jobs = len(jobs.recent())
            server.scheduler_tick(cfg)  # ts was refreshed: no second submission
            j = next(j for j in jobs.recent() if j["kind"] == "brew-check")
            release.set()
            _wait_job(j["id"])
            server.scheduler_tick(cfg)
            self.assertEqual(len(jobs.recent()), n_jobs)
            self.assertEqual(jobs.get(j["id"])["status"], "failed")
            b = db.kv_get("brew")
            self.assertNotIn("pending", b)
            self.assertEqual(b["outdated"], [{"name": "git"}], "old results stay visible")
        finally:
            brew.check = orig


# ---------------------------------------------------------------- jobs
class JobsTest(unittest.TestCase):
    def test_log_offsets_survive_trimming(self):
        def noisy(log):
            for i in range(6200):
                log(f"line {i}")
        j = jobs.submit("test", "noisy", noisy)
        j = _wait_job(j["id"])
        self.assertEqual(j["log_len"], 6200)
        tail = jobs.get(j["id"], 6190)["log"]
        self.assertEqual(tail, [f"line {i}" for i in range(6190, 6200)])
        self.assertEqual(jobs.get(j["id"], 6200)["log"], [])


# ---------------------------------------------------------------- HTTP
class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        web = config.WEB_DIR / "index.html"
        cls.httpd = server.make_server(0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=server.serve, kwargs={"httpd": cls.httpd, "with_scheduler": False},
                                      daemon=True)
        cls.thread.start()
        cls.has_web = web.exists()

    @property
    def token(self):
        # Read it each time: another test module may wipe the shared data dir (and so the token) meanwhile.
        return config.token()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def raw_req(self, method, path, body=None, host=None, headers=None):
        """(status, headers, body bytes)."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        status, hdrs = r.status, dict(r.getheaders())
        c.close()
        return status, hdrs, raw

    def req(self, method, path, body=None, host=None, headers=None):
        status, _h, raw = self.raw_req(method, path, body, host, headers)
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw

    def test_daemon_threads(self):
        self.assertTrue(self.httpd.daemon_threads)

    def test_get(self):
        code, s = self.req("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertIsInstance(s["free"], int)
        self.assertIn(s["level"], ("ok", "notice", "warning", "critical"))
        code, o = self.req("GET", "/api/overview", host=f"localhost:{self.port}")
        self.assertEqual(code, 200)
        for k in ("disk", "free_history", "growth7", "growth30", "caches", "apps", "brew", "alerts", "snapshots",
                  "last_scan", "local_snapshots", "actions", "freed_30d_kb", "jobs", "busy", "config", "home"):
            self.assertIn(k, o)
        self.assertEqual(self.req("GET", "/api/tree")[0], 200)
        self.assertEqual(self.req("GET", "/api/jobs")[0], 200)
        self.assertEqual(self.req("GET", "/api/jobs/abc")[0], 400)
        self.assertEqual(self.req("GET", "/api/jobs/999999")[0], 404)
        self.assertEqual(self.req("GET", "/api/leftovers")[0], 400)
        if self.has_web:
            code, html = self.req("GET", "/")
            self.assertEqual(code, 200)

    def test_bad_host(self):
        self.assertEqual(self.req("GET", "/api/status", host="evil.example")[0], 403)
        self.assertEqual(self.req("GET", "/api/status", host=f"evil.example:{self.port}")[0], 403)
        self.assertEqual(self.req("POST", "/api/run", {"action": "brew-check"}, host="evil.example",
                                  headers={"X-DH-Token": self.token})[0], 403)

    def test_post_auth(self):
        run = {"action": "brew-check"}
        self.assertEqual(self.req("POST", "/api/run", run)[0], 403)
        self.assertEqual(self.req("POST", "/api/run", run, headers={"X-DH-Token": "nope"})[0], 403)
        self.assertEqual(self.req("POST", "/api/run", run, headers={"X-DH-Token": self.token,
                                                                    "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req("POST", "/api/run", run, headers={"X-DH-Token": self.token, "Origin": "null"})[0], 403)
        code, j = self.req("POST", "/api/run", run, headers={"X-DH-Token": self.token,
                                                             "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(code, 200)
        self.assertEqual(_wait_job(j["id"])["status"], "done")
        auth = {"X-DH-Token": self.token}
        self.assertEqual(self.req("POST", "/api/run", {"action": "rm-rf"}, headers=auth)[0], 400)
        self.assertEqual(self.req("POST", "/api/run", {"action": "clean-cache"}, headers=auth)[0], 400)
        self.assertEqual(self.req("POST", "/api/run", [1, 2], headers=auth)[0], 400)

    def test_reveal(self):
        auth = {"X-DH-Token": self.token}
        self.assertEqual(self.req("POST", "/api/reveal", {"path": "/etc/passwd"}, headers=auth)[0], 400)
        self.assertEqual(self.req("POST", "/api/reveal", {"path": str(HOME) + "/../../etc"}, headers=auth)[0], 400)
        self.assertEqual(self.req("POST", "/api/reveal", {"path": 5}, headers=auth)[0], 400)
        self.assertEqual(self.req("POST", "/api/reveal", {"path": str(HOME)}, headers=auth)[0], 200)

    # ---------------------------------------------------------------- security
    def test_security_headers_on_every_response(self):
        for path in ("/api/status", "/nope"):
            _code, h, _b = self.raw_req("GET", path)
            self.assertEqual(h.get("X-Content-Type-Options"), "nosniff", path)
            self.assertEqual(h.get("Referrer-Policy"), "no-referrer", path)
            self.assertEqual(h.get("Cross-Origin-Resource-Policy"), "same-origin", path)

    def test_csp_nonce_matches_the_inline_script(self):
        if not self.has_web:
            self.skipTest("web/index.html not present")
        code, h, body = self.raw_req("GET", "/")
        self.assertEqual(code, 200)
        csp = h["Content-Security-Policy"]
        self.assertNotIn("'unsafe-inline'", csp.split("script-src")[1].split(";")[0])
        m = re.search(r"script-src [^;]*'nonce-([\w-]+)'", csp)
        self.assertIsNotNone(m, csp)
        html = body.decode()
        self.assertIn(f'<script nonce="{m.group(1)}">', html)
        self.assertNotIn("__DH_NONCE__", html)
        self.assertNotIn("__DH_TOKEN__", html)

    def test_nonce_differs_per_response(self):
        if not self.has_web:
            self.skipTest("web/index.html not present")
        seen = {self.raw_req("GET", "/")[1]["Content-Security-Policy"] for _ in range(3)}
        self.assertEqual(len(seen), 3)

    def test_post_rejects_cross_site_fetch_metadata(self):
        auth = {"X-DH-Token": self.token}
        for site in ("cross-site", "same-site"):
            code, _ = self.req("POST", "/api/run", {"action": "scan"},
                               headers=dict(auth, **{"Sec-Fetch-Site": site}))
            self.assertEqual(code, 403, site)
        # same-origin, and a non-browser client sending neither header, are both allowed
        code, _ = self.req("POST", "/api/reveal", {"path": str(HOME)},
                           headers=dict(auth, **{"Sec-Fetch-Site": "same-origin"}))
        self.assertEqual(code, 200)
        self.assertEqual(self.req("POST", "/api/reveal", {"path": str(HOME)}, headers=auth)[0], 200)


    def test_keepalive_survives_a_rejected_request(self):
        """A rejection must not leave an unread body to be parsed as the next request."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        host = f"127.0.0.1:{self.port}"
        body = json.dumps({"action": "scan"}).encode()
        c.request("POST", "/api/run", body=body,
                  headers={"Host": host, "Content-Type": "application/json", "X-DH-Token": "wrong"})
        r = c.getresponse()
        self.assertEqual(r.status, 403)
        r.read()
        c.close()
        # a fresh connection still speaks a clean protocol
        self.assertEqual(self.req("GET", "/api/status")[0], 200)

    def test_keepalive_reuses_one_connection(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        for _ in range(3):
            c.request("GET", "/api/status", headers={"Host": f"127.0.0.1:{self.port}"})
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            r.read()
            self.assertFalse(r.will_close, "connection should stay open for the next poll")
        c.close()


def tearDownModule():
    # Close the shared connection BEFORE the file disappears. The connection is a module-level
    # singleton in diskhealth.db, so leaving it open on a deleted file made every later test
    # module in the same process fail with "attempt to write a readonly database".
    from diskhealth import db
    db.close()
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
