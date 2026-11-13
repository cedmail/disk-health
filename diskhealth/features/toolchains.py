"""Old toolchain versions: JDKs, Node, Python, Ruby, Go and Rust versions installed by version managers (incl. mise).

Each version gets a size, whether it is the default/current one, when its executables were last used (atime of
bin/*, approximate on macOS) and which projects pin it (.nvmrc, .python-version, .tool-versions ...).
A version that is not the default, not pinned and not used for `toolchain_unused_days` days is "unused".
"""
from __future__ import annotations

import os
import plistlib
import re
import shutil
import time
from pathlib import Path

from .. import config, db, jobs, progress, trash
from ..util import DeleteTracker, du_kb, have, human, move_to_trash, run_out

NAME = "toolchains"
KEY = "toolchains"
SCAN_LABEL = "Toolchains"

# Overridable (tests point these at temp dirs / fake binaries).
JAVA_HOME_BIN = os.environ.get("DH_JAVA_HOME_BIN", "/usr/libexec/java_home")
SYSTEM_JVM_DIR = "/Library/Java/JavaVirtualMachines"

LANGS = ["Java", "Node", "Python", "Ruby", "Go", "Rust"]
MISE_FILES = {"mise.toml", ".mise.toml", "mise.local.toml", ".mise.local.toml"}
PIN_FILES = {".nvmrc", ".node-version", ".python-version", ".java-version", ".sdkmanrc", ".tool-versions",
             ".ruby-version", ".go-version", "rust-toolchain", "rust-toolchain.toml"} | MISE_FILES
SKIP_DIRS = {"node_modules", ".git", "target", "build", "dist", "vendor", ".venv", "venv", "__pycache__", ".gradle",
             ".idea", "Pods", ".tox", ".cache", "Library"}
# .tool-versions (asdf / mise) plugin names -> our tool ids
ASDF = {"nodejs": "node", "node": "node", "python": "python", "java": "java", "ruby": "ruby", "golang": "go",
        "go": "go", "rust": "rust", "maven": "maven", "gradle": "gradle"}


def _home() -> Path:
    return Path.home()


def _p(env: str, default: str) -> Path:
    return Path(os.environ.get(env) or (_home() / default))


# ------------------------------------------------------------------ version helpers
def _num(v: str) -> str:
    """Numeric part of a version string: 'v18.17.0' -> '18.17.0', 'temurin-17.0.2+8' -> '17.0.2', '1.8.0_292' -> '8.0.292'."""
    m = re.search(r"\d+(?:[._]\d+)*", v or "")
    if not m:
        return ""
    n = m.group(0).replace("_", ".")
    if n.startswith("1.") and len(n.split(".")) > 1 and n.split(".")[1] in ("5", "6", "7", "8"):  # Java 1.8 == 8
        n = n[2:]
    return n


def _prefix_match(pin: str, ver: str) -> bool:
    """'18' matches '18.17.0'; '3.11' matches '3.11.4'; never '3.1' vs '3.11'."""
    a, b = pin.split("."), ver.split(".")
    return bool(pin) and len(a) <= len(b) and a == b[:len(a)]


def _rust_channel(name: str) -> str:
    return re.sub(r"-(aarch64|x86_64|arm64|i686|armv7)-.*$", "", name)


def pin_matches(pin: str, item: dict) -> bool:
    pin = (pin or "").strip()
    if not pin or pin in ("system", "node", "stable-system"):
        return False
    names = {item["version"], item["name"], *item.get("aliases", [])}
    if pin in names or pin.lstrip("v") in {n.lstrip("v") for n in names}:
        return True
    if item["tool"] == "rust":
        ch = _rust_channel(item["name"])
        if pin == ch or pin.split("-")[0] == ch:
            return True
        if not re.match(r"\d", pin):
            return False  # "stable"/"nightly" only match their own channel
    if item["tool"] == "node" and pin.startswith("lts/"):
        return False  # resolved earlier when possible
    pn = _num(pin)
    return bool(pn) and any(_prefix_match(pn, _num(n)) for n in names if _num(n))


def _first_token(path: Path) -> list[str]:
    try:
        text = path.read_text(errors="replace")[:4000]
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line.split()[0])
    return out


# ------------------------------------------------------------------ pins in projects
def project_roots() -> list[str]:
    roots: list[str] = []
    try:
        from .. import gitrepos
        roots = list(gitrepos.roots())
    except Exception:
        roots = []
    if not roots:
        from .. import caches
        roots = [config.expand(r) for r in caches.PROJECT_ROOTS if os.path.isdir(config.expand(r))]
    seen, out = set(), []
    for r in roots:
        rp = os.path.realpath(r)
        if rp not in seen:
            seen.add(rp)
            out.append(r)
    return out


def _nvm_lts(name: str) -> str:
    """Resolve 'lts/iron' or 'lts/*' via ~/.nvm/alias/lts."""
    nvm = _p("NVM_DIR", ".nvm")
    cur = name
    for _ in range(5):
        if not cur.startswith("lts/"):
            return cur
        f = nvm / "alias" / cur
        toks = _first_token(f)
        if not toks:
            return name
        cur = toks[0]
    return cur


def _toml_str_list(v: str) -> list[str]:
    v = v.strip()
    if v.startswith("{"):                              # node = { version = "20", ... }
        m = re.search(r'\bversion\s*=\s*["\']([^"\']+)', v)
        return [m.group(1)] if m else []
    return re.findall(r'["\']([^"\']+)["\']', v)     # "20" or ["20", "22"]


def parse_mise_toml(text: str) -> list[tuple[str, str]]:
    """[(tool, version)] from the [tools] table of a mise config (no TOML library on Python 3.9)."""
    out = []
    sec = re.search(r"^\[tools\]\s*$(.*?)(?=^\s*\[|\Z)", text, re.M | re.S)
    if not sec:
        return out
    for line in sec.group(1).splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.match(r'^["\']?([^"\'=]+?)["\']?\s*=\s*(.+)$', line)
        if not m:
            continue
        key = m.group(1).strip()
        key = key.split(":", 1)[1] if key.startswith("core:") else key
        tool = ASDF.get(key, key)
        out += [(tool, v) for v in _toml_str_list(m.group(2))]
    return out


def parse_pin_file(path: Path) -> list[tuple[str, str]]:
    """[(tool, version)] pinned by one file."""
    n = path.name
    if n in MISE_FILES or (n == "config.toml" and path.parent.name in ("mise", ".mise")):
        try:
            return parse_mise_toml(path.read_text(errors="replace")[:20000])
        except OSError:
            return []
    if n in (".nvmrc", ".node-version"):
        return [("node", _nvm_lts(t)) for t in _first_token(path)[:1]]
    if n == ".python-version":
        return [("python", t) for t in _first_token(path)]
    if n == ".java-version":
        return [("java", t) for t in _first_token(path)[:1]]
    if n == ".ruby-version":
        return [("ruby", t.replace("ruby-", "", 1)) for t in _first_token(path)[:1]]
    if n == ".go-version":
        return [("go", t) for t in _first_token(path)[:1]]
    try:
        text = path.read_text(errors="replace")[:8000]
    except OSError:
        return []
    out = []
    if n == ".sdkmanrc":
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip()
            if "=" in line:
                k, v = (s.strip() for s in line.split("=", 1))
                out.append((k, v))
    elif n == ".tool-versions":
        for line in text.splitlines():
            parts = line.split("#", 1)[0].split()
            if len(parts) >= 2 and parts[0] in ASDF:
                out += [(ASDF[parts[0]], v) for v in parts[1:]]
    elif n == "rust-toolchain.toml":
        m = re.search(r'^\s*channel\s*=\s*"([^"]+)"', text, re.M)
        if m:
            out.append(("rust", m.group(1)))
    elif n == "rust-toolchain":
        toks = _first_token(path)
        if toks and "=" not in toks[0] and not toks[0].startswith("["):
            out.append(("rust", toks[0]))
        else:
            m = re.search(r'channel\s*=\s*"([^"]+)"', text)
            if m:
                out.append(("rust", m.group(1)))
    return out


def find_pins(roots: list[str] | None = None, max_depth: int = 4) -> list[dict]:
    """Pin files under the project roots (max depth 4, node_modules & co skipped)."""
    pins: list[dict] = []
    for root in roots if roots is not None else project_roots():
        root = os.path.realpath(root)
        base = root.rstrip("/").count("/")
        for top, dirs, files in os.walk(root, onerror=lambda e: None):
            jobs.checkpoint()
            depth = top.rstrip("/").count("/") - base
            if depth >= max_depth:
                dirs[:] = []
            else:
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for f in files:
                if f in PIN_FILES:
                    for tool, ver in parse_pin_file(Path(top, f)):
                        pins.append({"tool": tool, "version": ver, "project": top, "file": f})
    # Config files mise has loaded (any folder, incl. proj/.config/mise.toml that the walk skips).
    seen = {(p["project"], p["file"]) for p in pins}
    gdir = os.path.realpath(_mise_global_dir())
    for cfg_path in _mise_tracked_configs():
        proj, f = os.path.dirname(cfg_path), os.path.basename(cfg_path)
        while os.path.basename(proj) in (".config", ".mise", "mise") and proj != "/":
            proj = os.path.dirname(proj)            # proj/.config/mise.toml -> proj
        if cfg_path.startswith(gdir + "/") or (proj, f) in seen:
            continue
        for tool, ver in parse_pin_file(Path(cfg_path)):
            pins.append({"tool": tool, "version": ver, "project": proj, "file": f})
    # Rustup directory overrides count as pins too.
    for path, ch in _rustup_settings().get("overrides", {}).items():
        pins.append({"tool": "rust", "version": ch, "project": path, "file": "rustup override"})
    return pins


# ------------------------------------------------------------------ detection
def _bin_atime(bindirs: list[Path]) -> int:
    best = 0
    for d in bindirs:
        try:
            for e in os.scandir(d):
                try:
                    st = e.stat()  # follow symlinks (npm -> lib/node_modules/...)
                except OSError:
                    continue
                best = max(best, int(st.st_atime))
        except OSError:
            continue
    return best


def _ctime(p: Path) -> int:
    try:
        return int(os.lstat(p).st_mtime)
    except OSError:
        return 0


def _item(lang, tool, manager, name, version, path, bins, vendor="", default=False, removable=True, reason="",
          **kw) -> dict:
    p = Path(path)
    d = {"lang": lang, "tool": tool, "manager": manager, "name": name, "version": version, "vendor": vendor,
         "path": str(p), "default": bool(default), "removable": removable, "reason": reason,
         "last_used": _bin_atime([p / b for b in bins]), "installed": _ctime(p), "kb": 0, "aliases": [],
         "pinned_by": [], "admin": False, "symlink": p.is_symlink()}
    d.update(kw)
    return d


def _which_real(cmd: str) -> str:
    w = shutil.which(cmd)
    return os.path.realpath(w) if w else ""


def _under(path: str, parent: str) -> bool:
    if not path or not parent:
        return False
    parent = os.path.realpath(parent)
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _subdirs(d: Path) -> list[Path]:
    try:
        return sorted((p for p in d.iterdir() if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.name)
    except OSError:
        return []


def _java_home_default() -> str:
    if not os.path.exists(JAVA_HOME_BIN):
        return ""
    code, out, _e = run_out([JAVA_HOME_BIN], timeout=20)
    return out.strip() if code == 0 else ""


def _java_home_list() -> list[str]:
    """JDK homes from `java_home -V` (it prints the list on stderr)."""
    if not os.path.exists(JAVA_HOME_BIN):
        return []
    _c, out, err = run_out([JAVA_HOME_BIN, "-V"], timeout=20)
    homes = []
    for line in (err + "\n" + out).splitlines():
        m = re.search(r"\s(/\S.*?/Contents/Home)\s*$", line)
        if m:
            homes.append(m.group(1))
    return homes


def _jdk_info(bundle: Path) -> tuple[str, str]:
    try:
        with open(bundle / "Contents" / "Info.plist", "rb") as f:
            pl = plistlib.load(f)
    except Exception:
        return "", ""
    jvm = pl.get("JavaVM") or {}
    ver = str(jvm.get("JVMPlatformVersion") or jvm.get("JVMVersion") or pl.get("CFBundleShortVersionString") or "")
    if jvm.get("JVMVersion"):
        ver = str(jvm["JVMVersion"])
    vendor = str(jvm.get("JVMVendor") or pl.get("CFBundleGetInfoString") or "").strip()
    if not vendor:
        vendor = str(pl.get("CFBundleName") or "")
    return ver, vendor


def detect_java() -> list[dict]:
    items = []
    default_home = _java_home_default()
    jenv_root = _p("JENV_ROOT", ".jenv")
    jenv_global = (_first_token(jenv_root / "version") or [""])[0]
    jenv_links: dict[str, list[str]] = {}
    for v in _subdirs(jenv_root / "versions") if (jenv_root / "versions").is_dir() else []:
        if v.is_symlink():
            jenv_links.setdefault(os.path.realpath(v), []).append(v.name)
    jenv_default_real = os.path.realpath(jenv_root / "versions" / jenv_global) if jenv_global and jenv_global != "system" else ""
    bp = config.brew_prefix()
    bundles: list[tuple[Path, str, bool]] = []
    for base, system in ((Path(SYSTEM_JVM_DIR), True), (_home() / "Library/Java/JavaVirtualMachines", False)):
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        bundles += [(b, "System JDK" if system else "User JDK", system) for b in entries if b.name.endswith(".jdk")]
    known = {os.path.realpath(b) for b, _m, _s in bundles}
    for home in _java_home_list():
        b = Path(home).parent.parent
        if b.name.endswith(".jdk") and os.path.realpath(b) not in known and not _under(os.path.realpath(b), bp or "/nonexistent"):
            known.add(os.path.realpath(b))
            bundles.append((b, "JDK", False))
    for b, manager, system in bundles:
        ver, vendor = _jdk_info(b)
        home = b / "Contents" / "Home"
        real = os.path.realpath(b)
        is_default = bool(default_home) and _under(os.path.realpath(default_home), real)
        if jenv_default_real and _under(jenv_default_real, real):
            is_default = True
        aliases = [a for r, names in jenv_links.items() if _under(r, real) for a in names]
        removable, reason = True, ""
        if b.is_symlink():
            removable, reason = False, (f"Symlink to {os.path.realpath(b)}" + (" (Homebrew): uninstall it from the Homebrew panel"
                                         if bp and _under(real, bp) else ""))
        it = _item("Java", "java", manager, b.name, ver or _num(b.name), b, [home / "bin"], vendor=vendor,
                   default=is_default, removable=removable, reason=reason, aliases=aliases)
        it["admin"] = system and not os.access(str(b.parent), os.W_OK)
        items.append(it)
    # SDKMAN (java + other JVM candidates)
    sdk = _p("SDKMAN_DIR", ".sdkman") / "candidates"
    for cand in _subdirs(sdk):
        cur = os.path.realpath(cand / "current") if (cand / "current").exists() else ""
        for v in _subdirs(cand):
            if v.name == "current" or v.is_symlink():
                continue
            lang = "Java"
            items.append(_item(lang, cand.name, "SDKMAN", f"{cand.name} {v.name}" if cand.name != "java" else v.name,
                               v.name, v, ["bin"], vendor=_sdk_vendor(v.name) if cand.name == "java" else cand.name,
                               default=cur == os.path.realpath(v)))
    # jenv versions that are real folders (not symlinks to a JDK listed above)
    for v in _subdirs(jenv_root / "versions"):
        if not v.is_symlink():
            items.append(_item("Java", "java", "jenv", v.name, _num(v.name), v, ["bin"], default=v.name == jenv_global))
    # Homebrew openjdk kegs: listed only, removal goes through Homebrew.
    if bp:
        for keg in sorted(Path(bp, "Cellar").glob("openjdk*")):
            for v in _subdirs(keg):
                real = os.path.realpath(v)
                items.append(_item("Java", "java", "Homebrew", keg.name, v.name, v,
                                   ["bin", "libexec/openjdk.jdk/Contents/Home/bin"], vendor="Homebrew OpenJDK",
                                   default=bool(default_home) and _under(os.path.realpath(default_home), real),
                                   removable=False, reason=f"Managed by Homebrew: use the Homebrew panel or `brew uninstall {keg.name}`"))
    return items


def _sdk_vendor(v: str) -> str:
    suffix = v.rsplit("-", 1)[-1] if "-" in v else ""
    return {"tem": "Temurin", "zulu": "Zulu", "amzn": "Corretto", "graal": "GraalVM", "graalce": "GraalVM CE",
            "librca": "Liberica", "ms": "Microsoft", "oracle": "Oracle", "open": "OpenJDK", "sapmchn": "SapMachine",
            "sem": "Semeru", "kona": "Kona", "zulufx": "Zulu FX", "nik": "Liberica NIK", "jbr": "JetBrains"}.get(suffix, suffix)


def _nvm_default(nvm: Path, versions: list[str]) -> str:
    """Installed version the nvm 'default' alias resolves to (highest match, like nvm)."""
    cur = (_first_token(nvm / "alias" / "default") or [""])[0]
    for _ in range(5):
        if cur.startswith("lts/"):
            cur = _nvm_lts(cur)
        elif cur and (nvm / "alias" / cur).is_file() and not re.match(r"v?\d", cur):
            cur = (_first_token(nvm / "alias" / cur) or [""])[0]
        else:
            break
    if not cur:
        return ""
    def key(v):
        return [int(x) for x in _num(v).split(".") if x.isdigit()]
    if cur in ("node", "stable"):
        return max(versions, key=key, default="")
    want = _num(cur)
    matches = [v for v in versions if v.lstrip("v") == cur.lstrip("v") or (want and _prefix_match(want, _num(v)))]
    return max(matches, key=key, default="")


def detect_node() -> list[dict]:
    items = []
    node_real = _which_real("node")
    nvm = _p("NVM_DIR", ".nvm")
    vdirs = _subdirs(nvm / "versions" / "node")
    default = _nvm_default(nvm, [v.name for v in vdirs])
    for v in vdirs:
        items.append(_item("Node", "node", "nvm", v.name, v.name.lstrip("v"), v, ["bin"],
                           default=v.name == default or _under(node_real, str(v))))
    for fnm in (Path(os.environ["FNM_DIR"]) if os.environ.get("FNM_DIR") else None,
                _home() / ".local/share/fnm", _home() / "Library/Application Support/fnm", _home() / ".fnm"):
        if not fnm or not (fnm / "node-versions").is_dir():
            continue
        dflt = os.path.realpath(fnm / "aliases" / "default") if (fnm / "aliases" / "default").exists() else ""
        for v in _subdirs(fnm / "node-versions"):
            items.append(_item("Node", "node", "fnm", v.name, v.name.lstrip("v"), v, ["installation/bin"],
                               default=_under(dflt, str(v)) or _under(node_real, str(v))))
        break
    volta = _p("VOLTA_HOME", ".volta")
    vdflt = ""
    try:
        import json
        vdflt = (json.loads((volta / "tools/user/platform.json").read_text()).get("node") or {}).get("runtime") or ""
    except Exception:
        pass
    for v in _subdirs(volta / "tools/image/node"):
        items.append(_item("Node", "node", "Volta", v.name, v.name, v, ["bin"], default=v.name == vdflt))
    return items


def detect_python() -> list[dict]:
    items = []
    pyenv = _p("PYENV_ROOT", ".pyenv")
    glob_ = set(_first_token(pyenv / "version"))
    vers = pyenv / "versions"
    # pyenv-virtualenv: versions/<venv> -> versions/<base>/envs/<venv>; a global venv makes its base the default.
    venv_names: dict[str, list[str]] = {}
    for v in _subdirs(vers):
        if v.is_symlink():
            base = Path(os.path.realpath(v)).parent.parent.name
            venv_names.setdefault(base, []).append(v.name)
    for v in _subdirs(vers):
        if v.is_symlink():
            continue
        envs = len(_subdirs(v / "envs"))
        items.append(_item("Python", "python", "pyenv", v.name, v.name, v, ["bin"],
                           default=v.name in glob_ or bool(glob_ & set(venv_names.get(v.name, []))),
                           venvs=envs, aliases=venv_names.get(v.name, [])))
    uvdir = Path(os.environ.get("UV_PYTHON_INSTALL_DIR") or (_home() / ".local/share/uv/python"))
    uv_pin = (_first_token(_home() / ".config/uv/.python-version") or [""])[0]
    for v in _subdirs(uvdir):
        if v.is_symlink():
            continue
        m = re.match(r"([a-z]+)-(\d+\.\d+\.\d+[a-z0-9+]*)", v.name)
        ver = m.group(2) if m else _num(v.name)
        impl = m.group(1) if m else ""
        items.append(_item("Python", "python", "uv", v.name, ver, v, ["bin", "install/bin"],
                           vendor=impl if impl != "cpython" else "",
                           default=bool(uv_pin) and _prefix_match(_num(uv_pin), _num(ver))))
    return items


def detect_ruby() -> list[dict]:
    items = []
    rbenv = _p("RBENV_ROOT", ".rbenv")
    g = set(_first_token(rbenv / "version"))
    for v in _subdirs(rbenv / "versions"):
        if not v.is_symlink():
            items.append(_item("Ruby", "ruby", "rbenv", v.name, v.name, v, ["bin"], default=v.name in g))
    rvm = _p("rvm_path", ".rvm")
    dflt = os.path.realpath(rvm / "rubies/default") if (rvm / "rubies/default").exists() else ""
    for v in _subdirs(rvm / "rubies"):
        if v.is_symlink() or v.name == "default":
            continue
        items.append(_item("Ruby", "ruby", "RVM", v.name, v.name.replace("ruby-", "", 1), v, ["bin"],
                           default=dflt == os.path.realpath(v)))
    return items


def detect_go() -> list[dict]:
    items = []
    go_real = _which_real("go")
    for v in _subdirs(_home() / "sdk"):
        if v.name.startswith("go") and re.match(r"go\d", v.name):
            items.append(_item("Go", "go", "go SDK", v.name, v.name[2:], v, ["bin"], default=_under(go_real, str(v))))
    gvm = _p("GVM_ROOT", ".gvm")
    groot = ""
    try:
        m = re.search(r'GOROOT="?([^"\n]+)', (gvm / "environments/default").read_text())
        groot = os.path.realpath(m.group(1)) if m else ""
    except OSError:
        pass
    for v in _subdirs(gvm / "gos"):
        items.append(_item("Go", "go", "gvm", v.name, v.name.lstrip("go"), v, ["bin"],
                           default=(groot == os.path.realpath(v)) or _under(go_real, str(v))))
    return items


def _rustup_home() -> Path:
    return _p("RUSTUP_HOME", ".rustup")


def _rustup_settings() -> dict:
    try:
        text = (_rustup_home() / "settings.toml").read_text()
    except OSError:
        return {}
    res: dict = {"overrides": {}}
    m = re.search(r'^\s*default_toolchain\s*=\s*"([^"]+)"', text, re.M)
    if m:
        res["default"] = m.group(1)
    sec = re.search(r"^\[overrides\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    if sec:
        for k, v in re.findall(r'^\s*"?([^"=\n]+?)"?\s*=\s*"([^"]+)"', sec.group(1), re.M):
            res["overrides"][k] = v
    return res


def detect_rust() -> list[dict]:
    tc = _rustup_home() / "toolchains"
    if not tc.is_dir():
        return []
    dflt = _rustup_settings().get("default", "")
    if not dflt and have("rustup"):
        _c, out, _e = run_out(["rustup", "default"], timeout=20)
        dflt = out.split()[0] if out.split() else ""
    items = []
    for v in _subdirs(tc):
        ch = _rust_channel(v.name)
        is_def = bool(dflt) and (v.name == dflt or ch == dflt or v.name.startswith(dflt + "-"))
        items.append(_item("Rust", "rust", "rustup", v.name, ch, v, ["bin"], default=is_def))
    return items


# ------------------------------------------------------------------ mise
MISE_LANG = {"java": "Java", "node": "Node", "python": "Python", "ruby": "Ruby", "go": "Go", "rust": "Rust"}


def _mise_dir(env: str, xdg: str, default: str) -> Path:
    if os.environ.get(env):
        return Path(os.environ[env])
    if os.environ.get(xdg):
        return Path(os.environ[xdg]) / "mise"
    return _home() / default


def _mise_global_dir() -> str:
    return os.environ.get("MISE_CONFIG_DIR") or str(_home() / ".config/mise")


def _mise_tracked_configs() -> list[str]:
    d = _mise_dir("MISE_STATE_DIR", "XDG_STATE_HOME", ".local/state/mise") / "tracked-configs"
    out = []
    try:
        for e in os.scandir(d):
            t = os.path.realpath(e.path)
            if os.path.isfile(t):
                out.append(t)
    except OSError:
        pass
    return sorted(set(out))


def _mise_global_pins() -> list[tuple[str, str]]:
    g = Path(_mise_global_dir())
    pins = []
    for f in (g / "config.toml", g / "config.local.toml", _home() / ".mise.toml"):
        try:
            pins += parse_mise_toml(f.read_text(errors="replace")[:20000])
        except OSError:
            pass
    try:
        pins += parse_pin_file(_home() / ".tool-versions")
    except OSError:
        pass
    return pins


def detect_mise() -> list[dict]:
    """Language runtimes installed by mise (installs/<tool>/<version>; mise's alias symlinks are skipped).
    Other mise tools (maven, terraform, npm:…) are listed in the Homebrew & mise panel."""
    root = Path(os.environ.get("MISE_INSTALLS_DIR") or
                _mise_dir("MISE_DATA_DIR", "XDG_DATA_HOME", ".local/share/mise") / "installs")
    if not root.is_dir():
        return []
    gpins = _mise_global_pins()
    items = []
    for t in _subdirs(root):
        tool = ASDF.get(t.name, t.name)
        lang = MISE_LANG.get(tool)
        if not lang:
            continue
        for v in _subdirs(t):
            if v.is_symlink():
                continue
            vendor = ""
            if tool == "java" and "-" in v.name and not v.name[0].isdigit():
                vendor = v.name.split("-", 1)[0].capitalize()
            it = _item(lang, tool, "mise", v.name, _num(v.name) or v.name, v, ["bin", "Contents/Home/bin"],
                       vendor=vendor)
            it["default"] = any(pt == tool and pin_matches(pv, it) for pt, pv in gpins)
            items.append(it)
    # A global "latest"/"lts" keeps the newest installed version of that tool.
    for pt, pv in gpins:
        if pv in ("latest", "lts", "lts/*"):
            cand = [i for i in items if i["tool"] == pt]
            if cand:
                max(cand, key=lambda i: [int(n) if n.isdigit() else 0 for n in _num(i["version"]).split(".")])["default"] = True
    return items


DETECTORS = [detect_java, detect_node, detect_python, detect_ruby, detect_go, detect_rust, detect_mise]


def detect(measure: bool = True, cfg: dict | None = None, log=None) -> list[dict]:
    cfg = cfg or config.load()
    items: list[dict] = []
    for d in DETECTORS:
        try:
            items += d()
        except jobs.Cancelled:
            raise
        except Exception as ex:
            if log:
                log(f"  ! {d.__name__}: {ex}")
    pins = find_pins()
    for it in items:
        it["pinned_by"] = sorted({p["project"] for p in pins if p["tool"] == it["tool"] and pin_matches(p["version"], it)})
    for p in pins:   # mise.toml `node = "latest"`: the newest installed version is the one in use
        if p["version"] in ("latest", "lts") and p["file"] in MISE_FILES | {"config.toml"}:
            cand = [i for i in items if i["tool"] == p["tool"] and i["manager"] == "mise"]
            if cand:
                top = max(cand, key=lambda i: [int(n) if n.isdigit() else 0 for n in _num(i["version"]).split(".")])
                if p["project"] not in top["pinned_by"]:
                    top["pinned_by"] = sorted(top["pinned_by"] + [p["project"]])
    days = int(cfg.get("toolchain_unused_days", 120))
    cutoff = time.time() - days * 86400
    if measure:
        progress.update(items_total=len(items), counter_label="versions measured")
    for i, it in enumerate(items, 1):
        jobs.checkpoint()
        if measure and not it["symlink"]:
            progress.update(current=it["path"], items_done=i - 1)
            it["kb"] = du_kb(it["path"])
        if it["removable"]:
            if it["default"]:
                it["removable"], it["reason"] = False, "Default / current version"
            elif it["pinned_by"]:
                it["removable"], it["reason"] = False, f"Pinned by {len(it['pinned_by'])} project(s)"
        recent = max(it["last_used"], it["installed"]) >= cutoff
        it["unused"] = (not it["default"] and not it["pinned_by"] and not it.get("venvs")
                        and not recent and it["manager"] != "Homebrew" and not it["symlink"])
    items.sort(key=lambda x: (LANGS.index(x["lang"]) if x["lang"] in LANGS else 99, x["manager"], x["tool"],
                              [int(n) if n.isdigit() else 0 for n in _num(x["version"]).split(".")]))
    return items


def scan(log=print) -> dict:
    cfg = config.load()
    items = detect(measure=True, cfg=cfg, log=log)
    unused = [i for i in items if i["unused"]]
    res = {"items": items, "count": len(items), "total_kb": sum(i["kb"] for i in items),
           "unused_count": len(unused), "unused_kb": sum(i["kb"] for i in unused),
           "unused_days": int(cfg.get("toolchain_unused_days", 120)), "roots": project_roots(),
           "ts": int(time.time())}
    for i in items:
        log(f"  {i['lang']:<6} {i['manager']:<10} {i['name']:<34} {human(i['kb']):>9}"
            f"{'  default' if i['default'] else ''}{'  pinned' if i['pinned_by'] else ''}{'  unused' if i['unused'] else ''}")
    log(f"{len(items)} toolchain versions, {human(res['total_kb'])}; {len(unused)} unused ({human(res['unused_kb'])})")
    db.kv_set(KEY, res)
    return res


# ------------------------------------------------------------------ removal
def _running_under(path: str) -> list[str]:
    """Executables of running processes located under `path` (refuse to remove a toolchain in use)."""
    code, out, _e = run_out(["ps", "-axo", "comm="], timeout=20)
    if code != 0:
        return []
    real = os.path.realpath(path)
    hits = []
    for line in out.splitlines():
        exe = line.strip()
        if exe.startswith("/") and (_under(exe, path) or _under(os.path.realpath(exe), real)):
            hits.append(exe)
    return hits


_JDK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\.jdk$")


def _admin_remove_jdk(path: str, log) -> bool:
    """Remove a JDK bundle in /Library/Java/JavaVirtualMachines with the macOS admin prompt (osascript)."""
    name = os.path.basename(path)
    if (os.path.dirname(path) != SYSTEM_JVM_DIR or not _JDK_NAME.match(name) or os.path.islink(path)
            or os.path.realpath(path) != path):
        raise ValueError(f"Refusing: {path} is not a JDK bundle directly in {SYSTEM_JVM_DIR}")
    shell = f"/bin/rm -rf '{SYSTEM_JVM_DIR}/{name}'"
    script = f'do shell script "{shell}" with administrator privileges'
    log(f"{path} belongs to the system (admin rights needed): asking macOS for your password to run `{shell}` …")
    code, out, err = run_out(["osascript", "-e", script], timeout=600)
    if code != 0:
        msg = (err or out).strip()
        if "-128" in msg or "canceled" in msg.lower() or "cancelled" in msg.lower():
            log("You cancelled the admin prompt: nothing was removed.")
        else:
            log(f"! osascript failed: {msg}")
        return False
    return not os.path.lexists(path)


def _manager_cmd(it: dict) -> list[str] | None:
    m, name = it["manager"], it["name"]
    if m == "pyenv" and have("pyenv"):
        return ["pyenv", "uninstall", "-f", name]
    if m == "rustup" and have("rustup"):
        return ["rustup", "toolchain", "uninstall", name]
    if m == "rbenv" and have("rbenv"):
        return ["rbenv", "uninstall", "-f", name]
    if m == "fnm" and have("fnm"):
        return ["fnm", "uninstall", name]
    if m == "uv" and have("uv"):
        return ["uv", "python", "uninstall", name]
    if m == "mise" and have("mise"):
        return ["mise", "uninstall", f"{os.path.basename(os.path.dirname(it['path']))}@{name}"]
    return None


def remove(target: str, log=print) -> int:
    if not isinstance(target, str) or not target.startswith("/") or "\0" in target:
        raise ValueError("Bad target")
    path = os.path.normpath(target)
    items = detect(measure=False)
    it = next((i for i in items if i["path"] == path), None)
    if not it:
        raise ValueError(f"{path} is not a detected toolchain version (refresh the Toolchains panel)")
    if it["default"]:
        raise ValueError(f"{it['name']} is the default/current {it['lang']} version: switch the default first")
    if it["pinned_by"]:
        raise ValueError(f"{it['name']} is pinned by: {', '.join(it['pinned_by'][:5])}")
    if not it["removable"]:
        raise ValueError(f"{it['name']} can't be removed here: {it['reason']}")
    busy = _running_under(path)
    if busy:
        raise ValueError(f"{it['name']} is in use by running processes: {', '.join(busy[:3])}")
    prev = {i["path"]: i["kb"] for i in (db.kv_get(KEY) or {}).get("items", [])}
    kb = prev.get(path) or du_kb(path)
    log(f"Removing {it['lang']} {it['name']} ({it['manager']}, {human(kb)}) at {path}")
    ok, how = False, ""
    if it["manager"] == "System JDK" and it["admin"]:
        ok, how = _admin_remove_jdk(path, log), "removed with admin rights"
    else:
        cmd = _manager_cmd(it)
        if cmd:
            log("$ " + " ".join(cmd))
            env = {}
            if it["manager"] == "pyenv":
                env["PYENV_ROOT"] = str(_p("PYENV_ROOT", ".pyenv"))
            code, out, err = run_out(cmd, timeout=600, env=env)
            for line in (out + err).strip().splitlines()[-20:]:
                log("  " + line)
            ok, how = code == 0 and not os.path.lexists(path), f"`{' '.join(cmd[:3])}`"
            if not ok and os.path.lexists(path):
                log(f"! {cmd[0]} didn't remove it (exit {code}); moving the folder to the Trash instead")
        if not ok and os.path.lexists(path):
            if it["manager"] in ("nvm", "SDKMAN", "mise"):
                # nvm/sdk are shell functions: their own uninstall just deletes this folder.
                t = DeleteTracker(lambda tr, cur: progress.update(kb=tr.freed_kb, counter=tr.files, current=cur))
                t.remove(path)
                for e in t.errors[:5]:
                    log("  ! " + e)
                ok, how = not os.path.lexists(path), ("deleted (what `mise uninstall` does)" if it["manager"] == "mise"
                                                       else "deleted (what `nvm uninstall` / `sdk uninstall` do)")
            elif it["manager"] == "System JDK":
                t = DeleteTracker()
                t.remove(path)
                ok, how = not os.path.lexists(path), "deleted"
            else:
                dest = move_to_trash(path)
                trash.remember(dest, path)
                ok, how = True, f"moved to the Trash ({dest}); it can be put back from the Trash panel"
                trash.listing()
    log(("Done: " if ok else "Failed: ") + f"{it['name']} {how}")
    db.log_action("toolchain-remove", path, ok, kb if ok else 0, f"{it['lang']} {it['manager']} {it['name']}: {how}")
    log("Refreshing toolchains …")
    scan(log=lambda *_: None)
    if not ok:
        raise RuntimeError(f"Couldn't remove {path}")
    return kb


ACTIONS = {
    "toolchain-remove": {"label": lambda t: f"Remove toolchain {os.path.basename(str(t))}", "fn": remove,
                         "target": True, "lane": "heavy"},
}


def alerts(cfg) -> list[dict]:
    r = db.kv_get(KEY) or {}
    n = r.get("unused_count") or 0
    if not n:
        return []
    return [{"id": f"toolchains:{int(time.time() // (7 * 86400))}:{n}", "level": "info", "notify": False,
             "title": f"{n} old toolchain version{'s' if n != 1 else ''} unused for "
                      f"{r.get('unused_days', 120)}+ days ({human(r.get('unused_kb', 0))})", "section": NAME}]
