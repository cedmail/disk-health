"""disk-health: keep a Mac disk healthy (growth tracking, caches, unused apps, Homebrew)."""
from pathlib import Path as _Path

PROJECT_DIR = _Path(__file__).resolve().parent.parent
try:
    __version__ = (PROJECT_DIR / "VERSION").read_text().strip() or "0.0.0"
except OSError:
    __version__ = "0.0.0"


def brew_opt_dir():
    """$(brew --prefix)/opt/disk-health/libexec when installed with Homebrew (stable across upgrades), else None."""
    s = str(PROJECT_DIR)
    if "/Cellar/disk-health/" not in s:
        return None
    return _Path(s.split("/Cellar/", 1)[0]) / "opt" / "disk-health" / "libexec"


def version_on_disk():
    """VERSION of the code currently installed (it changes under a running service after
    `brew upgrade` or `git pull`); None if it can't be read."""
    opt = brew_opt_dir()
    try:
        return ((opt or PROJECT_DIR) / "VERSION").read_text().strip() or None
    except OSError:
        return None
