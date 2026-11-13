"""Localhost HTTP server (dashboard + API) and the background scheduler."""
from __future__ import annotations

import hmac
import json
import secrets
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import re

from . import __version__, version_on_disk, alerts, apps, brew, caches, config, db, features, gitrepos, jobs, progress, scanner, trash
from .util import IS_MAC, human, is_under, run


# ------------------------------------------------------------------ tasks
def full_scan(log=print) -> None:
    feats = [m for m in features.modules() if hasattr(m, "scan")]
    steps = ["Disk scan", "Caches", "Applications", "Git repositories"] + [getattr(m, "SCAN_LABEL", m.NAME) for m in feats]
    rest = 0.25 / (len(steps) - 1)
    progress.start(steps, title="Scanning", weights=[0.75] + [rest] * (len(steps) - 1),
                   counter_label="folders read", kb_label="counted")
    log("== Disk scan ==")
    progress.step(0, "Measuring every folder (du)")
    scanner.sample_free()
    scanner.take_snapshot(progress=log)
    scanner.prune_old()
    log("== Caches ==")
    progress.step(1, "Measuring known caches")
    _soft(log, "caches", lambda: caches.measure(progress=_counting(log)))
    log("== Applications ==")
    progress.step(2, "Reading app sizes and last-used dates")
    _soft(log, "applications", lambda: apps.scan(progress=_counting(log)))
    log("== Git repositories ==")
    progress.step(3, "Reading local clones (no network)")
    _soft(log, "git", lambda: gitrepos.scan(progress_log=log))
    for i, m in enumerate(feats, start=4):
        log(f"== {steps[i]} ==")
        progress.step(i, steps[i])
        _soft(log, m.NAME, lambda m=m: m.scan(log=log))
    db.kv_set("last_full_scan", {"ts": int(time.time())})


def _soft(log, what, fn):
    """Run one scan step; a failure is logged but doesn't throw away the other steps' results."""
    try:
        fn()
    except jobs.Cancelled:
        raise
    except Exception as ex:
        import traceback
        log(f"  ! {what} step failed: {ex}")
        log(traceback.format_exc())


def _counting(log):
    """Wrap a progress logger so each logged line also advances the live item counter."""
    def f(msg=""):
        log(msg)
        s = progress.get()
        progress.update(current=str(msg).strip(), items_done=s.get("items_done", 0) + 1)
    return f


def brew_check(log=print) -> None:
    log("== Homebrew check ==")
    try:
        brew.check(progress=log)
    except Exception:
        # Keep the previous results visible, just drop the "pending" flag set by the scheduler.
        prev = db.kv_get("brew") or {}
        if prev.get("pending"):
            prev = {k: v for k, v in prev.items() if k != "pending"}
            db.kv_set("brew", prev)
        raise


def refresh_quick(log=print) -> None:
    caches.measure(progress=log)
    apps.scan(progress=log)


def overview() -> dict:
    cfg = config.load()
    s = scanner.sample_free(record=False)
    snaps = scanner.snapshots()
    g7 = scanner.growth(7)
    return {
        "disk": s, "free_history": scanner.free_history(120),
        "growth7": g7, "growth30": scanner.growth(30),
        "caches": db.kv_get("caches") or {}, "trash": db.kv_get("trash") or {}, "git": db.kv_get("git") or {}, "apps": db.kv_get("apps") or {}, "brew": db.kv_get("brew") or {},
        "alerts": _alerts(g7), "snapshots": len(snaps), "last_scan": snaps[-1]["ts"] if snaps else None,
        "local_snapshots": db.kv_get("tm_snapshots") or [],
        "actions": db.query("SELECT * FROM actions ORDER BY ts DESC LIMIT 30"),
        "freed_30d_kb": (db.query("SELECT COALESCE(SUM(freed_kb),0) s FROM actions WHERE ts>?",
                                  (int(time.time()) - 30 * 86400,))[0]["s"]),
        "jobs": jobs.recent()[:10], "busy": jobs.busy(), "progress": progress.get(),
        "config": {k: cfg[k] for k in ("unused_app_days", "unused_tool_days", "free_warn_percent",
                                       "free_critical_percent", "scan_every_hours")},
        "home": str(config.HOME), "version": __version__,
        "features": {m.NAME: db.kv_get(getattr(m, "KEY", m.NAME)) or {} for m in features.modules()},
    }


def status() -> dict:
    s = scanner.sample_free(record=False)
    a = _alerts()
    worst = next((l for l in ("critical", "warning", "notice") if any(x["level"] == l for x in a)), "ok")
    return {"free": s["free"], "total": s["total"], "free_h": human(s["free"] / 1024),
            "free_pct": round(100 * s["free"] / s["total"], 1) if s["total"] else 0.0, "level": worst, "alerts": a,
            "busy": jobs.busy(), "progress": _progress_brief(), "version": __version__, "ts": int(time.time())}


def _alerts(g7=None) -> list[dict]:
    out = alerts.compute(growth7=g7)
    cfg = config.load()
    for m in features.modules():
        if hasattr(m, "alerts"):
            try:
                out += m.alerts(cfg) or []
            except Exception as ex:
                print(f"[alerts] {m.NAME}: {ex}", flush=True)
    return out


def _tail(path, n: int = 300) -> list[str]:
    n = max(1, min(n, 2000))
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []


def _progress_brief() -> dict:
    p = progress.get()
    if not p.get("active"):
        return {"active": False}
    return {"active": True, "title": p.get("title", ""), "step": p["steps"][p["step"]], "step_index": p["step"], "steps": len(p["steps"]),
            "pct": p["pct"], "eta": p["eta"], "elapsed": p["elapsed"], "current": p.get("current", "")}


# Actions whose target must be a non-empty string.
NEEDS_TARGET = {"clean-cache", "uninstall-app", "uninstall-app-permanent", "reinstall-app", "brew-uninstall",
                "brew-reinstall", "trash-delete", "trash-put-back", "git-gc", "git-fetch", "git-remove"}

ACTIONS = {
    "scan": lambda t: jobs.submit("scan", "Full scan", full_scan),
    "refresh": lambda t: jobs.submit("refresh", "Refresh caches & apps", refresh_quick, lane="light"),
    "brew-check": lambda t: jobs.submit("brew-check", "Homebrew check", brew_check),
    "brewed": lambda t: jobs.submit("brewed", "brewed (update, upgrade, doctor, autoremove, cleanup, omz)", brew.brewed),
    "clean-cache": lambda t: jobs.submit("clean", f"Clean {t}", _then_refresh(caches.clean), t),
    "uninstall-app": lambda t: jobs.submit("uninstall", f"Uninstall {os.path.basename(t)}",
                                           _then_refresh(apps.uninstall), t),
    "reinstall-app": lambda t: jobs.submit("reinstall", f"Clean reinstall {os.path.basename(t)}",
                                           _then_refresh(apps.reinstall), t),
    "uninstall-app-permanent": lambda t: jobs.submit("uninstall", f"Uninstall {os.path.basename(t)} (permanently)",
                                                     _then_refresh(_permanent_uninstall), t),
    "empty-trash": lambda t: jobs.submit("trash", "Empty the Trash", trash.empty),
    "trash-delete": lambda t: jobs.submit("trash", f"Delete {os.path.basename(t)} from the Trash",
                                          lambda target, log=print: trash.empty([target], log=log), t),
    "trash-put-back": lambda t: jobs.submit("trash", f"Put back {os.path.basename(t)}", trash.put_back, t, lane="light"),
    "git-scan": lambda t: jobs.submit("git", "Refresh Git repositories", _git_scan, lane="light"),
    "git-gc": lambda t: jobs.submit("git", f"git gc {os.path.basename(t)}", gitrepos.gc, t),
    "git-fetch": lambda t: jobs.submit("git", f"git fetch {os.path.basename(t)}", gitrepos.fetch, t, lane="light"),
    "git-remove": lambda t: jobs.submit("git", f"Remove clone {os.path.basename(t)}", gitrepos.remove, t),
    "brew-uninstall": lambda t: jobs.submit("brew-uninstall", f"brew uninstall {t}", brew.uninstall_formula, t),
    "brew-reinstall": lambda t: jobs.submit("brew-reinstall", f"brew reinstall {t}", brew.reinstall_formula, t),
}


def _register_features() -> None:
    for m in features.modules():
        for name, spec in (getattr(m, "ACTIONS", None) or {}).items():
            def submit(t, spec=spec, name=name, m=m):
                label = spec["label"](t) if callable(spec.get("label")) else (spec.get("label") or name)
                args = (t,) if spec.get("target") else ()
                return jobs.submit(m.NAME, label, spec["fn"], *args, lane=spec.get("lane", "heavy"))
            ACTIONS[name] = submit
            if spec.get("target"):
                NEEDS_TARGET.add(name)
        if hasattr(m, "scan") and f"{m.NAME}-scan" not in ACTIONS:
            def refresh(t, m=m):
                def fn(log=print):
                    progress.start([getattr(m, "SCAN_LABEL", m.NAME)], title=f"Refreshing {getattr(m, 'SCAN_LABEL', m.NAME)}")
                    return m.scan(log=log)
                return jobs.submit(m.NAME, f"Refresh {getattr(m, 'SCAN_LABEL', m.NAME)}", fn, lane="light")
            ACTIONS[f"{m.NAME}-scan"] = refresh


_register_features()


def _git_scan(log=print):
    progress.start(["Git repositories"], title="Refreshing Git repositories", counter_label="", kb_label="")
    gitrepos.scan(progress_log=log)


def _permanent_uninstall(target, log=print):
    return apps.uninstall(target, log=log, permanent=True)


def _then_refresh(fn):
    def wrapped(target, log=print):
        r = fn(target, log=log)
        if fn is caches.clean:
            pass  # clean() re-measures its own cache
        elif fn is apps.reinstall:
            log("updating app list ...")
            apps.scan(progress=log)
        else:
            apps.forget(target)
            trash.listing()
        log("done")
        return r
    return wrapped


# ------------------------------------------------------------------ scheduler
def python_replaced() -> bool:
    return not os.path.exists(os.path.realpath(sys.executable)) or not os.path.exists(os.__file__)


def scheduler_tick(cfg: dict, now: float | None = None) -> None:
    """One scheduler pass. Every submission first stamps its kv key, so a failing or slow job is retried only
    after its full interval (never every minute), and the last good results stay visible meanwhile."""
    now = time.time() if now is None else now
    if python_replaced() and not jobs.busy():
        # `brew upgrade python` removed the interpreter we run from; lazy stdlib imports would now fail.
        # Exit and let launchd (KeepAlive) restart us on the new Python.
        print("[scheduler] Python was upgraded underneath the service; restarting", flush=True)
        os._exit(75)
    on_disk = version_on_disk()
    if on_disk and on_disk != __version__ and not jobs.busy():
        # brew upgrade / git pull replaced the code: restart on it (launchd KeepAlive starts us again).
        print(f"[scheduler] Disk Health {on_disk} installed (running {__version__}); restarting", flush=True)
        os._exit(75)
    last_free = (db.query("SELECT MAX(ts) t FROM free_samples")[0]["t"] or 0)
    if now - last_free >= cfg["free_sample_every_minutes"] * 60:
        scanner.sample_free()
    last_scan = (db.kv_get("last_full_scan") or {}).get("ts", 0)
    if now - last_scan >= cfg["scan_every_hours"] * 3600 and not jobs.busy():
        db.kv_set("last_full_scan", {"ts": int(now)})  # avoid re-queue while running / after a failure
        jobs.submit("scan", "Scheduled full scan", full_scan)
    if brew.available() and now - db.kv_ts("brew") >= cfg["brew_check_every_hours"] * 3600 and not jobs.busy():
        prev = db.kv_get("brew") or {}
        prev.pop("_ts", None)
        db.kv_set("brew", dict(prev, pending=True))  # refreshes kv ts => throttles retries
        jobs.submit("brew-check", "Scheduled Homebrew check", brew_check)
    if IS_MAC and now - db.kv_ts("tm_snapshots") >= 6 * 3600:
        db.kv_set("tm_snapshots", scanner.local_snapshots())
    if now - db.kv_ts("trash") >= 3600 and not jobs.busy():
        trash.listing()
    for m in features.modules():
        if hasattr(m, "tick"):
            try:
                m.tick(now, cfg)
            except Exception as ex:
                print(f"[scheduler] {m.NAME}: {ex}", flush=True)


def scheduler(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            scheduler_tick(config.load())  # re-read so config.json edits apply without a restart
        except Exception as ex:  # keep the loop alive
            print(f"[scheduler] {ex}", flush=True)
        stop.wait(60)


# ------------------------------------------------------------------ HTTP
MAX_BODY = 64 * 1024
FEATURE_ROUTES = {path: fn for m in features.modules() for path, fn in (getattr(m, "ROUTES", None) or {}).items()}
REVEAL_ROOTS = ("/Applications", "/Library", "/opt/homebrew", "/usr/local")


def _reveal_allowed(p: str) -> bool:
    """May `open -R <p>` be run? Only for something that exists inside one of the known roots.

    Containment is tested by inode (util.is_under), not by string prefix: a prefix test is fooled
    by case differences on a case-insensitive volume and by a symlinked parent.
    """
    if not isinstance(p, str) or not p.startswith("/") or "\0" in p:
        return False
    if not os.path.lexists(p):
        return False
    roots = [r for r in (str(config.HOME), config.brew_prefix()) + REVEAL_ROOTS if r and r != "/"]
    return any(is_under(p, r) for r in roots)


# Sent on every response. The dashboard is same-origin only and embeds nothing, so these
# can all be set to their strictest value.
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",            # no MIME sniffing of /static or API JSON
    "Referrer-Policy": "no-referrer",               # never leak local paths in a Referer
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",  # another origin cannot read our responses
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "disk-health"
    # HTTP/1.1 keep-alive: the dashboard polls /api/progress once a second and the extension
    # polls /api/status, so reusing the connection removes a TCP handshake per poll. Every
    # response therefore MUST carry an accurate Content-Length (see _json) and every early
    # return MUST close the connection (see _err), or the next request desynchronises.
    protocol_version = "HTTP/1.1"
    timeout = 60  # drop idle/slow connections

    def log_message(self, fmt, *args):  # quiet
        pass

    def end_headers(self):
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        super().end_headers()

    # -- helpers
    def _host_ok(self) -> bool:
        # Blocks DNS rebinding: a page on evil.example resolving to 127.0.0.1 still sends Host: evil.example.
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: int, msg: str):
        # Close the connection on every rejection. Most rejections happen before the request body
        # has been read, and on a keep-alive connection those unread bytes would be parsed as the
        # next request line. Closing is also the right signal: the client got nothing useful.
        self.close_connection = True
        self._json({"error": msg}, code)

    # -- GET
    def do_GET(self):
        if not self._host_ok():
            return self._err(403, "bad host")
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                # Per-response nonce: the dashboard's one inline <script> carries it, so script-src
                # no longer needs 'unsafe-inline' and an injected <script> cannot run. Styles keep
                # 'unsafe-inline' because a nonce does not cover inline style attributes.
                nonce = secrets.token_urlsafe(16)
                html = (config.WEB_DIR / "index.html").read_text()
                html = html.replace("__DH_TOKEN__", config.token()).replace("__DH_NONCE__", nonce)
                body = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                 f"script-src 'self' 'nonce-{nonce}'; img-src 'self' data:; "
                                 "connect-src 'self'; base-uri 'none'; form-action 'none'; "
                                 "object-src 'none'; frame-ancestors 'none'")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path.startswith("/static/"):
                name = u.path[len("/static/"):]
                f = config.WEB_DIR / "static" / name
                if not re.fullmatch(r"[\w.-]+\.(js|css)", name) or not f.is_file():
                    return self._err(404, "not found")
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8" if name.endswith(".js")
                                 else "text/css; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/panels":
                self._json(sorted(p.name for p in (config.WEB_DIR / "static").glob("panel-*.js")))
            elif u.path in FEATURE_ROUTES:
                self._json(FEATURE_ROUTES[u.path](q))
            elif u.path == "/api/status":
                self._json(status())
            elif u.path == "/api/progress":
                p = progress.get()
                p["queued"] = [{"id": j["id"], "label": j["label"]} for j in reversed(jobs.recent())
                               if j["status"] == "queued"]
                self._json(p)
            elif u.path == "/api/trash":
                cached = db.kv_get("trash") or {}
                fresh = q.get("refresh") or time.time() - (cached.get("ts") or 0) > 60
                self._json(trash.listing() if fresh and not jobs.busy() else cached)
            elif u.path == "/api/agent-log":
                self._json({"lines": _tail(config.LOG_DIR / "agent.log", int(q.get("lines", 300)))})
            elif u.path == "/api/overview":
                self._json(overview())
            elif u.path == "/api/tree":
                self._json(scanner.children(q.get("path") or None))
            elif u.path == "/api/leftovers":
                if not q.get("path"):
                    return self._err(400, "path required")
                self._json({"items": apps.leftovers(q["path"])})
            elif u.path == "/api/jobs":
                self._json(jobs.recent())
            elif u.path.startswith("/api/jobs/"):
                try:
                    jid, since = int(u.path.rsplit("/", 1)[1]), max(0, int(q.get("since", 0)))
                except ValueError:
                    return self._err(400, "bad job id")
                j = jobs.get(jid, since)
                self._json(j) if j else self._err(404, "no such job")
            else:
                self._err(404, "not found")
        except Exception as ex:
            self._err(500, str(ex))

    # -- POST (state-changing: token + same-origin required)
    def do_POST(self):
        if not self._host_ok():
            return self._err(403, "bad host")
        port = self.server.server_address[1]
        origin = self.headers.get("Origin")
        if origin is not None and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return self._err(403, "bad origin")
        # Browsers that send Sec-Fetch-Site must say the request is same-origin. A browser always
        # sends Origin on POST, so "neither header" means a non-browser client (curl, a script):
        # those are allowed, because the X-DH-Token check below is what actually protects them and
        # a web page cannot attach that header cross-origin (it forces a preflight we never answer).
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site != "same-origin":
            return self._err(403, "cross-site request")
        if not hmac.compare_digest(self.headers.get("X-DH-Token", "").encode(), config.token().encode()):
            return self._err(403, "bad token")
        try:
            try:
                n = int(self.headers.get("Content-Length", 0))
            except ValueError:
                return self._err(400, "bad length")
            if n < 0 or n > MAX_BODY:
                return self._err(413, "body too large")
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._err(400, "bad json")
            if not isinstance(data, dict):
                return self._err(400, "expected a JSON object")
            u = urlparse(self.path)
            if u.path == "/api/run":
                action, target = data.get("action"), data.get("target")
                if action not in ACTIONS:
                    return self._err(400, "unknown action")
                if action in NEEDS_TARGET and (not isinstance(target, str) or not target):
                    return self._err(400, "target required")
                self._json(ACTIONS[action](target))
            elif u.path == "/api/cancel":
                try:
                    jid = int(data.get("id"))
                except (TypeError, ValueError):
                    return self._err(400, "bad job id")
                j = jobs.cancel(jid)
                self._json(j) if j else self._err(404, "no such job")
            elif u.path == "/api/reveal":
                p = data.get("path", "")
                if not _reveal_allowed(p):
                    return self._err(400, "path not allowed")
                if IS_MAC:
                    run(["open", "-R", os.path.normpath(p)], timeout=10)
                self._json({"ok": True})
            else:
                self._err(404, "not found")
        except Exception as ex:
            self._err(500, str(ex))


class Server(ThreadingHTTPServer):
    daemon_threads = True       # request threads never block shutdown
    allow_reuse_address = True  # restart right away after a crash (TIME_WAIT)


def make_server(port: int | None = None) -> Server:
    """Bind 127.0.0.1:<port> (config "port" by default; 0 = any free port)."""
    if port is None:
        port = int(config.load()["port"])
    return Server(("127.0.0.1", int(port)), Handler)


def serve(port: int | None = None, with_scheduler: bool = True, httpd: Server | None = None) -> None:
    config.ensure_dirs()
    config.token()
    db.query("SELECT 1")  # open/create the database
    httpd = httpd or make_server(port)
    stop = threading.Event()
    if with_scheduler:
        threading.Thread(target=scheduler, args=(stop,), daemon=True, name="scheduler").start()
    print(f"disk-health on http://127.0.0.1:{httpd.server_address[1]}  (data: {config.DATA_DIR})", flush=True)
    try:
        httpd.serve_forever()
    finally:
        stop.set()
        httpd.server_close()
