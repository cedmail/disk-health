"""One sandbox per test module, so the suite does not depend on import order.

diskhealth.config resolves HOME and DISK_HEALTH_DATA into module-level paths, and
diskhealth.db keeps a single connection. Before this helper existed, whichever test module
imported diskhealth first owned both for the whole process: running the modules together
gave 25 errors ("attempt to write a readonly database" once test_engine deleted its own
sandbox) while each module passed on its own.

use("<name>") gives the calling module its own home/ and data/ under one process-wide root
and re-points diskhealth at them. The root lives until the process exits, so a module can
never hand the next one a path inside the real $HOME.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SANDBOX = Path(tempfile.mkdtemp(prefix="dh-tests-")).resolve()
atexit.register(shutil.rmtree, SANDBOX, ignore_errors=True)


def use(name: str, root: Path | str | None = None) -> Path:
    """Point $HOME and $DISK_HEALTH_DATA at this module's own sandbox. Returns its root."""
    base = Path(root).resolve() if root else SANDBOX / name
    home, data = base / "home", base / "data"
    home.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    os.environ["HOME"] = str(home)
    os.environ["DISK_HEALTH_DATA"] = str(data)
    if "diskhealth.config" in sys.modules:   # already imported: re-point it at the new sandbox
        from diskhealth import config, db
        db.close()
        config.reload_paths()
    return base
