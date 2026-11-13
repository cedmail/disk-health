"""Homebrew + oh-my-zsh: daily checks and the `brewed` sequence.

Your alias:
  brew update && brew upgrade -q; brew doctor && brew autoremove && omz update && omz reload
is run here as independent steps so a `brew doctor` warning never blocks autoremove/omz,
`omz reload` (which only makes sense in an interactive terminal) is skipped, and
`brew cleanup` is added.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import config, db
from .util import du_kb, have, human, run, run_out

# HOMEBREW_NO_COLOR (not HOMEBREW_COLOR=0: Homebrew treats HOMEBREW_COLOR as "set => force colour").
ENV = {"HOMEBREW_NO_ENV_HINTS": "1", "HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_COLOR": "1", "NONINTERACTIVE": "1"}
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def available() -> bool:
    return have("brew")


def _stream(cmd: list[str], log, env: dict | None = None, timeout: int = 3600) -> int:
    e = dict(os.environ)
    e.update(ENV)
    e.update(env or {})
    log("$ " + " ".join(cmd))
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", env=e, bufsize=1,
                             start_new_session=True)
    except (FileNotFoundError, PermissionError):
        log(f"command not found: {cmd[0]}")
        return 127
    timed_out = threading.Event()

    def _kill():  # watchdog: fires even if the command hangs without printing anything
        timed_out.set()
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except Exception:
            p.kill()

    from . import jobs
    jobs.track_process(p)
    timer = threading.Timer(timeout, _kill)
    timer.daemon = True
    timer.start()
    try:
        for line in p.stdout:  # type: ignore[union-attr]
            log(_ANSI.sub("", line.rstrip()))
    finally:
        timer.cancel()
        jobs.untrack_process(p)
    jobs.checkpoint()
    code = p.wait()
    if timed_out.is_set():
        log("timed out")
        return 124
    return code


# ------------------------------------------------------------------ history
def _history_usage() -> dict[str, tuple[int, int]]:
    """command -> (last timestamp, count) from zsh extended history."""
    path = Path(config.expand(config.load()["zsh_history"]))
    usage: dict[str, list[int]] = {}
    if not path.exists():
        return {}
    for ts, cmd in _history_entries(path.read_bytes()):
        for seg in re.split(r"\|\||&&|[|;\n]|\$\(|`|\(|\)|\{|\}", cmd):
            for w in seg.split():
                if "=" in w and not w.startswith("="):
                    continue  # VAR=value prefix
                if w in _SKIP_WORDS or w.startswith("-"):
                    continue
                w = os.path.basename(w.strip("()'\"\\"))
                if not w or w in _SKIP_WORDS:
                    continue
                u = usage.setdefault(w, [0, 0])
                u[0] = max(u[0], ts)
                u[1] += 1
                break
    return {k: (v[0], v[1]) for k, v in usage.items()}


# Prefix commands and shell keywords: the command that matters is the next word.
_SKIP_WORDS = {"sudo", "time", "noglob", "nohup", "exec", "command", "builtin", "env", "nice", "watch", "xargs",
               "if", "then", "else", "elif", "fi", "for", "while", "until", "do", "done", "case", "esac", "select",
               "function", "!", "[[", "]]", "{", "}", "in", "coproc", "repeat"}


def _unmetafy(data: bytes) -> bytes:
    """zsh writes bytes 0x83-0xA2 of non-ASCII text as 0x83 (Meta) followed by byte ^ 0x20."""
    if b"\x83" not in data:
        return data
    out, i, n = bytearray(), 0, len(data)
    while i < n:
        b = data[i]
        if b == 0x83 and i + 1 < n:
            out.append(data[i + 1] ^ 0x20)
            i += 2
        else:
            out.append(b)
            i += 1
    return bytes(out)


def _history_entries(raw: bytes) -> list[tuple[int, str]]:
    """(timestamp, command) pairs from a zsh history file (extended ": <ts>:<dur>;cmd" or plain lines).

    A multi-line command is stored as lines ending with a backslash; they're joined back into one entry.
    """
    text = _unmetafy(raw).decode("utf-8", errors="ignore")
    entries: list[tuple[int, str]] = []
    cur: list[str] | None = None
    ts = 0
    for line in text.split("\n"):
        if cur is None:
            m = re.match(r"^: *(\d+):\d+;(.*)$", line, re.S)
            ts, line = (int(m.group(1)), m.group(2)) if m else (0, line)
            cur = []
        if line.endswith("\\"):
            cur.append(line[:-1])
            continue
        cur.append(line)
        entries.append((ts, "\n".join(cur)))
        cur = None
    if cur:
        entries.append((ts, "\n".join(cur)))
    return [(t, c) for t, c in entries if c.strip()]


def _leaves(prefix: str, cfg: dict) -> list[dict]:
    code, out, _err = run_out(["brew", "info", "--json=v2", "--installed"], timeout=300, env=ENV)
    if code != 0:
        return []
    try:
        formulae = json.loads(out).get("formulae", [])
    except Exception:
        return []
    lcode, leaves_out, _err = run_out(["brew", "leaves", "--installed-on-request"], timeout=120, env=ENV)
    if lcode != 0:
        return []
    # Tap formulae are listed by full name ("user/tap/foo").
    leaves = set(leaves_out.split())
    hist = _history_usage()
    now = time.time()
    items = []
    for f in formulae:
        name = f.get("name")
        if not name or (name not in leaves and f.get("full_name") not in leaves):
            continue
        inst = (f.get("installed") or [{}])[-1]
        try:
            installed_ts = int(inst.get("time") or 0)
        except (TypeError, ValueError):
            installed_ts = 0
        bins = []
        for sub in ("bin", "sbin"):
            d = Path(prefix, "opt", name, sub)
            if d.is_dir():
                try:
                    bins += [p.name for p in d.iterdir()]
                except OSError:
                    pass
        last, count = 0, 0
        for b in bins:
            if b in hist:
                last = max(last, hist[b][0])
                count += hist[b][1]
        ref = last or installed_ts
        idle = int((now - ref) / 86400) if ref else None
        items.append({
            "name": name, "desc": f.get("desc", ""), "kb": du_kb(str(Path(prefix, "Cellar", name))),
            "bins": bins[:8], "bin_count": len(bins), "last_seen": last or None, "uses": count,
            "installed": installed_ts or None, "idle_days": idle,
            "unused": bool(bins) and idle is not None and idle >= cfg["unused_tool_days"],
        })
    items.sort(key=lambda i: (not i["unused"], -i["kb"]))
    return items


# ------------------------------------------------------------------ checks
def check(progress=print) -> dict:
    cfg = config.load()
    if not available():
        res = {"available": False}
        db.kv_set("brew", res)
        return res
    progress("  brew update")
    code, out = run(["brew", "update", "--quiet"], timeout=600, env=ENV)
    if code != 0:
        progress(f"  ! brew update failed (exit {code}); checking against the current index")
    _c, prefix, _e = run_out(["brew", "--prefix"], env=ENV)
    prefix = (prefix.strip().splitlines() or [""])[-1].strip() or config.brew_prefix()

    progress("  brew outdated")
    # JSON is parsed from stdout only (warnings go to stderr). `brew outdated` may exit 1 when things are outdated.
    _c, out, _e = run_out(["brew", "outdated", "--json=v2"], timeout=300, env=ENV)
    od = _json_obj(out)
    outdated = [{"name": f.get("name"), "kind": "formula", "from": _versions(f.get("installed_versions")),
                 "to": f.get("current_version"), "pinned": bool(f.get("pinned", False))}
                for f in od.get("formulae") or [] if isinstance(f, dict)]
    outdated += [{"name": c.get("name"), "kind": "cask", "from": _versions(c.get("installed_versions")),
                  "to": c.get("current_version"), "pinned": False}
                 for c in od.get("casks") or [] if isinstance(c, dict)]

    progress("  brew autoremove --dry-run")
    _c, out, _e = run_out(["brew", "autoremove", "--dry-run"], timeout=300, env=ENV)
    orphans = parse_autoremove(out)

    progress("  brew doctor")
    code, out = run(["brew", "doctor"], timeout=600, env=ENV)
    warnings = parse_doctor(out)

    progress("  brew cleanup --dry-run")
    _c, out = run(["brew", "cleanup", "-n", f"--prune={cfg['brew_cleanup_prune_days']}"], timeout=600, env=ENV)
    m = re.search(r"free approximately ([\d.]+\s*[KMGT]?B)", _ANSI.sub("", out))
    cleanup = m.group(1) if m else "0B"

    progress("  leaves + shell history")
    leaves = _leaves(prefix, cfg)

    progress("  reading your brewed alias")
    alias = read_alias(cfg.get("brewed_alias_name", "brewed"))

    res = {"available": True, "prefix": prefix, "outdated": outdated, "orphans": orphans, "alias": alias,
           "alias_name": cfg.get("brewed_alias_name", "brewed"),
           "doctor_ok": code == 0 and not warnings, "doctor": warnings, "cleanup_reclaimable": cleanup,
           "leaves": leaves, "unused_leaves": sum(1 for l in leaves if l["unused"]),
           "omz": omz_status(), "ts": int(time.time())}
    db.kv_set("brew", res)
    return res


def _json_obj(out: str) -> dict:
    """Parse brew's JSON output; tolerate stray text before it."""
    out = out.strip()
    for candidate in (out, out[out.find("{"):] if "{" in out else ""):
        try:
            v = json.loads(candidate)
            return v if isinstance(v, dict) else {}
        except Exception:
            continue
    return {}


def _versions(v) -> str:
    # formulae: list of strings; casks: a list, or a plain string in some brew versions.
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return ""


def parse_autoremove(out: str) -> list[str]:
    """Names after "==> Would autoremove N unneeded formulae:" (one per line, or space-separated)."""
    names, inside = [], False
    for line in _ANSI.sub("", out).splitlines():
        if line.startswith("==>"):
            inside = "autoremove" in line.lower()
            continue
        if inside:
            if line.startswith(("Warning:", "Error:")) or not line.strip():
                if line.strip():
                    inside = False
                continue
            names += line.split()
    return list(dict.fromkeys(names))


def parse_doctor(out: str) -> list[dict]:
    """`brew doctor` warning blocks: "Warning: <title>\n<body>" separated by blank lines.

    A clean system prints "Your system is ready to brew." and exits 0.
    """
    warnings = []
    for block in re.split(r"\n(?=Warning: )", "\n" + _ANSI.sub("", out)):
        block = block.strip()
        if block.startswith("Warning:"):
            title, _, body = block.partition("\n")
            warnings.append({"title": title[len("Warning:"):].strip(), "body": body.strip()[:1500]})
    return warnings


def read_alias(name: str = "brewed") -> str | None:
    """The user's alias text from their interactive zsh config (None if not defined)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name or ""):
        return None
    code, out, _err = run_out(["zsh", "-ic", f"alias {name}"], timeout=20)
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith(name + "="):
            v = line[len(name) + 1:]
            if len(v) >= 2 and v[0] == v[-1] == "'":
                v = v[1:-1].replace("'\\''", "'")
            return v
    return None


def omz_status() -> dict:
    d = Path(config.expand(config.load()["omz_dir"]))
    if not d.is_dir():
        return {"installed": False}
    last = None
    f = d / "cache/.zsh-update"
    if f.exists():
        m = re.search(r"LAST_EPOCH=(\d+)", f.read_text())
        if m:
            last = int(m.group(1)) * 86400
    return {"installed": True, "dir": str(d), "last_update": last}


# ------------------------------------------------------------------ actions
def brewed(log=print) -> int:
    """Your `brewed` alias, step by step, with brew cleanup added."""
    cfg = config.load()
    steps = [
        ("brew update", ["brew", "update"], True),
        ("brew upgrade", ["brew", "upgrade", "-q"], False),
        ("brew doctor", ["brew", "doctor"], False),
        ("brew autoremove", ["brew", "autoremove"], False),
        ("brew cleanup", ["brew", "cleanup", "-s", f"--prune={cfg['brew_cleanup_prune_days']}"], False),
    ]
    failures = 0
    cache_before = du_kb(config.expand("~/Library/Caches/Homebrew"))
    for label, cmd, critical in steps:
        log(f"\n=== {label} ===")
        code = _stream(cmd, log)
        if code != 0:
            failures += 1
            log(f"({label} exited with {code}{' — continuing' if not critical else ''})")
            if critical:
                break
    omz = Path(config.expand(cfg["omz_dir"]))
    if (omz / "tools/upgrade.sh").exists():
        log("\n=== omz update ===")
        code = _stream(["zsh", "-f", str(omz / "tools/upgrade.sh")], log, env={"ZSH": str(omz)})
        if code == 0:
            (omz / "cache").mkdir(exist_ok=True)
            (omz / "cache/.zsh-update").write_text(f"LAST_EPOCH={int(time.time() // 86400)}\n")
            log("(omz reload skipped: it only works in an interactive terminal. Run `omz reload` there, "
                "or use 'Copy for Terminal' on the dashboard to run your own brewed alias.)")
        else:
            failures += 1
    freed = max(0, cache_before - du_kb(config.expand("~/Library/Caches/Homebrew")))
    log(f"\nDone. {failures} step(s) reported problems. Homebrew cache freed: {human(freed)}")
    db.log_action("brewed", "", failures == 0, freed)
    check(progress=lambda *_: None)
    return failures


def uninstall_formula(name: str, log=print) -> None:
    data = db.kv_get("brew", {}) or {}
    if name not in {l["name"] for l in data.get("leaves", [])}:
        raise ValueError("Only leaf formulae (nothing depends on them) can be removed from here")
    code = _stream(["brew", "uninstall", name], log)
    if code == 0:
        _stream(["brew", "autoremove"], log)
    db.log_action("brew-uninstall", name, code == 0)
    check(progress=lambda *_: None)
    if code != 0:
        raise RuntimeError(f"brew uninstall {name} exited with {code}")


def reinstall_formula(name: str, log=print) -> None:
    data = db.kv_get("brew", {}) or {}
    if name not in {l["name"] for l in data.get("leaves", [])}:
        raise ValueError("Unknown formula")
    code = _stream(["brew", "uninstall", "--ignore-dependencies", name], log)
    if code == 0:
        code = _stream(["brew", "install", name], log)
    db.log_action("brew-reinstall", name, code == 0)
    if code != 0:
        raise RuntimeError(f"brew reinstall {name} failed (exit {code})")
