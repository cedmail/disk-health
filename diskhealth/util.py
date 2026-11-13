"""Small helpers shared by the checks."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

IS_MAC = sys.platform == "darwin"

# A LaunchAgent starts with a minimal PATH; make sure Homebrew's bin dirs are reachable.
# ~/.local/bin: where mise's own installer puts it.
for _d in ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", os.path.expanduser("~/.local/bin")):
    if os.path.isdir(_d) and _d not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + _d


def _run(cmd, timeout: int, env: dict | None, shell: bool, merge: bool, cwd: str | None = None) -> tuple[int, str, str]:
    e = dict(os.environ)
    if env:
        e.update(env)
    try:
        # New session so a timeout can kill the whole process group (brew spawns ruby, git, curl ...);
        # stdin closed so nothing ever waits for a password prompt.
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT if merge else subprocess.PIPE,
                             env=e, shell=shell, start_new_session=True, cwd=cwd,
                             text=True, encoding="utf-8", errors="replace")
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return 127, "", f"command not found: {cmd if isinstance(cmd, str) else cmd[0]}"
    except Exception as ex:  # pragma: no cover
        return 1, "", str(ex)
    from . import jobs
    jobs.track_process(p)
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out or "", err or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except Exception:
            p.kill()
        try:
            out, err = p.communicate(timeout=10)
        except Exception:
            out, err = "", ""
        # Keep partial output (e.g. what du printed before the timeout).
        return 124, out or "", (err or "") + "\ntimed out"
    finally:
        jobs.untrack_process(p)


def run(cmd, timeout: int = 600, env: dict | None = None, shell: bool = False) -> tuple[int, str]:
    """Run a command, return (exit code, combined stdout+stderr in emission order). Never raises."""
    code, out, err = _run(cmd, timeout, env, shell, merge=True)
    return code, out + err


def run_out(cmd, timeout: int = 600, env: dict | None = None, cwd: str | None = None) -> tuple[int, str, str]:
    """Run a command, return (exit code, stdout, stderr) separately — use when stdout is machine-parsed (JSON...)."""
    return _run(cmd, timeout, env, False, merge=False, cwd=cwd)


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def parse_du(out: str) -> dict[str, int]:
    """Parse `du -k` output ("<kb>\\t<path>" per line) into {path: kb}; ignores error lines."""
    res: dict[str, int] = {}
    for line in out.splitlines():
        kb, sep, path = line.partition("\t")
        kb = kb.strip()
        if sep and kb.isdigit() and path:
            res[path] = int(kb)
    return res


def du_kb(path: str, timeout: int = 900) -> int:
    """Total size of a path in KB (0 if missing). Symlinks are not followed."""
    if not os.path.lexists(path):
        return 0
    _code, out, _err = run_out(["du", "-sk", path], timeout=timeout)
    return sum(parse_du(out).values())


def dir_children(path: str) -> list[Path]:
    try:
        return list(Path(path).iterdir())
    except Exception:
        return []


def remove_path(p: Path, errors: list[str]) -> None:
    """Delete a file, symlink (never its target) or folder tree; keeps going past undeletable files."""
    try:
        if p.is_dir() and not p.is_symlink():
            if sys.version_info >= (3, 12):
                shutil.rmtree(p, onexc=lambda _f, path, exc: errors.append(f"{path}: {exc}"))
            else:
                shutil.rmtree(p, onerror=lambda _f, path, exc: errors.append(f"{path}: {exc[1]}"))
        else:
            p.unlink()
    except Exception as ex:
        errors.append(f"{p}: {ex}")


def remove_contents(path: str) -> tuple[int, list[str]]:
    """Delete everything inside `path` (not the folder itself). Returns (KB freed, errors)."""
    before = du_kb(path)
    errors: list[str] = []
    for child in dir_children(path):
        remove_path(child, errors)
    return max(0, before - du_kb(path)), errors


def move_to_trash(path: str) -> str:
    """Move a file/folder to ~/.Trash (recoverable). Returns destination."""
    trash = Path.home() / ".Trash"
    trash.mkdir(exist_ok=True)
    src = Path(path)
    dest = trash / src.name
    i = 1
    while os.path.lexists(dest):
        dest = trash / f"{src.stem} {i}{src.suffix}"
        i += 1
    shutil.move(str(src), str(dest))
    return str(dest)


def human(kb: float) -> str:
    n = float(kb) * 1024
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class DeleteTracker:
    """Deletes trees file by file, counting what is freed, and reports progress every ~0.3 s."""

    def __init__(self, report=None):
        self.freed_kb = 0
        self.files = 0
        self.errors: list[str] = []
        self.report = report or (lambda t, current: None)
        self._last = 0.0

    def _tick(self, current: str) -> None:
        import time
        from . import jobs
        jobs.checkpoint()
        now = time.time()
        if now - self._last > 0.3:
            self._last = now
            self.report(self, current)

    def _unlink(self, path: str) -> None:
        try:
            st = os.lstat(path)
            os.unlink(path)
            self.freed_kb += getattr(st, "st_blocks", 0) // 2 or st.st_size // 1024
            self.files += 1
        except FileNotFoundError:
            pass
        except Exception as ex:
            self.errors.append(f"{path}: {ex}")
        self._tick(path)

    def remove(self, path: str, keep_root: bool = False) -> None:
        """Delete `path` (a file, a symlink — never its target — or a folder tree). keep_root keeps the folder."""
        if not os.path.lexists(path):
            return
        if os.path.islink(path) or not os.path.isdir(path):
            if not keep_root:
                self._unlink(path)
            return
        for top, dirs, files in os.walk(path, topdown=False, followlinks=False,
                                        onerror=lambda e: self.errors.append(str(e))):
            for f in files:
                self._unlink(os.path.join(top, f))
            for d in dirs:
                full = os.path.join(top, d)
                if os.path.islink(full):
                    self._unlink(full)
                else:
                    try:
                        os.rmdir(full)
                    except FileNotFoundError:
                        pass
                    except Exception as ex:
                        self.errors.append(f"{full}: {ex}")
        if not keep_root:
            try:
                os.rmdir(path)
            except Exception as ex:
                self.errors.append(f"{path}: {ex}")
        self.report(self, path)


def stream(cmd: list[str], on_line, timeout: int = 1800, env: dict | None = None, cwd: str | None = None) -> int:
    """Run a command, calling on_line(text) for each output line as it arrives. Returns the exit code."""
    import threading
    e = dict(os.environ)
    if env:
        e.update(env)
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace", env=e, start_new_session=True, bufsize=1,
                             cwd=cwd)
    except (FileNotFoundError, NotADirectoryError):
        on_line(f"command not found: {cmd[0]}" if cwd is None or os.path.isdir(cwd) else f"folder not found: {cwd}")
        return 127
    from . import jobs
    jobs.track_process(p)
    timer = threading.Timer(timeout, lambda: os.killpg(p.pid, signal.SIGKILL))
    timer.daemon = True
    timer.start()
    try:
        for line in p.stdout:  # type: ignore[union-attr]
            on_line(line.rstrip("\n"))
        code = p.wait()
        jobs.checkpoint()
        return code
    finally:
        timer.cancel()
        jobs.untrack_process(p)
