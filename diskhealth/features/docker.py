"""Docker detail panel: images, containers, volumes, build cache and the VM disk image they live in.

Everything is read with Docker's machine-readable output (`--format '{{json .}}'`). Sizes are kept in bytes.
Docker Desktop, OrbStack and Colima all run the engine in a Linux VM whose disk is a single sparse file on
macOS; we report that file's real allocated size (st_blocks * 512) next to its apparent size.
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
from datetime import datetime

from .. import db, jobs, progress
from ..util import human, have, run_out, stream

NAME = "docker"
KEY = "docker"
SCAN_LABEL = "Docker"

PROJECT_LABEL = "com.docker.compose.project"
MAX_BUILD_ROWS = 150

# ------------------------------------------------------------------ parsing helpers
# Docker prints sizes with go-units HumanSize: decimal units (1 kB = 1000 B), e.g. "1.23GB", "12.5kB", "0B",
# "1.2GB (45%)", "12.3MB (virtual 1.2GB)". Binary units (KiB, MiB ...) are accepted too.
_DEC = {"": 1, "B": 1, "K": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4, "P": 1000 ** 5}
_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([kKMGTP]?)(i?)[bB]?\b")


def parse_size(s) -> int:
    """Docker size string -> bytes (0 if empty/unknown/"N/A")."""
    if isinstance(s, (int, float)):
        return int(s)
    m = _SIZE_RE.match(str(s or ""))
    if not m:
        return 0
    try:
        n = float(m.group(1))
    except ValueError:
        return 0
    unit = m.group(2).upper()
    mult = (1024 ** "BKMGTP".index(unit)) if (m.group(3) and unit) else _DEC.get(unit, 1)
    return int(n * mult)


def parse_labels(s) -> dict:
    """Docker's "k=v,k2=v2" Labels field (or a dict) -> dict. A piece without "=" belongs to the previous value."""
    if isinstance(s, dict):
        return {str(k): str(v) for k, v in s.items()}
    out: dict = {}
    last = None
    for piece in str(s or "").split(","):
        k, sep, v = piece.partition("=")
        if sep and k.strip():
            last = k.strip()
            out[last] = v
        elif last is not None:
            out[last] += "," + piece
    return out


def parse_time(s) -> int:
    """'2024-05-01 10:00:00 +0200 CEST' (or RFC 3339) -> epoch seconds; 0 if unparseable."""
    s = str(s or "").strip()
    if not s:
        return 0
    m = re.match(r"(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d:\d\d)(?:\.\d+)?\s*(Z|[+-]\d\d:?\d\d)?", s)
    if not m:
        return 0
    tz = (m.group(3) or "+0000").replace("Z", "+0000").replace(":", "")
    try:
        return int(datetime.strptime(f"{m.group(1)} {m.group(2)} {tz}", "%Y-%m-%d %H:%M:%S %z").timestamp())
    except ValueError:
        return 0


def _bool(v) -> bool:
    return v is True or str(v).strip().lower() in ("true", "1", "yes")


def _int(v) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def short_id(i) -> str:
    i = str(i or "")
    if i.startswith("sha256:"):
        i = i[7:]
    return i[:12]


def json_lines(out: str) -> list:
    """Parse `--format '{{json .}}'` output: one JSON object per line (or a single JSON array/object)."""
    out = (out or "").strip()
    if not out:
        return []
    if out[0] == "[":
        try:
            v = json.loads(out)
            return [x for x in v if isinstance(x, dict)]
        except ValueError:
            pass
    res = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            res.append(d)
    return res


def reclaimed_bytes(text: str) -> int:
    """Sum of every "Total reclaimed space: X" in a prune's output."""
    return sum(parse_size(m) for m in re.findall(r"Total reclaimed space:\s*([\d.]+\s*[A-Za-z]*)", text or ""))


_DOCKER_ENV: dict = {"DOCKER_CLI_HINTS": "false"}


def _sockets() -> list:
    """Known Docker sockets on macOS (Docker Desktop, OrbStack, Colima, legacy)."""
    home = os.path.expanduser("~")
    cands = [os.path.join(home, ".docker/run/docker.sock"), os.path.join(home, ".orbstack/run/docker.sock"),
             os.path.join(os.environ.get("COLIMA_HOME") or os.path.join(home, ".colima"), "default/docker.sock"),
             "/var/run/docker.sock"]
    return [c for c in cands if os.path.exists(c)]


def _docker(args: list, timeout: int = 60):
    return run_out(["docker"] + args, timeout=timeout, env=_DOCKER_ENV)


def _ping(log=print) -> tuple:
    """Is the daemon reachable? Uses `docker version` (fast: unlike `docker info` it doesn't run every CLI
    plugin). If the CLI's default context doesn't answer from the background service, retry each known socket
    with DOCKER_HOST and keep the one that works for the rest of the session. Returns (info dict | None, error)."""
    attempts = [None] + [f"unix://{s}" for s in _sockets()]
    if _DOCKER_ENV.get("DOCKER_HOST"):
        attempts.insert(0, _DOCKER_ENV["DOCKER_HOST"])
    errors = []
    for host in dict.fromkeys(attempts):
        if host:
            _DOCKER_ENV["DOCKER_HOST"] = host
        else:
            _DOCKER_ENV.pop("DOCKER_HOST", None)
        t0 = time.time()
        code, out, err = _docker(["version", "--format", "{{json .Server}}"], timeout=30)
        srv = (json_lines(out) or [None])[0]
        if code == 0 and isinstance(srv, dict) and srv:
            if host:
                log(f"using DOCKER_HOST={host}")
            plat = (srv.get("Platform") or {}).get("Name", "") if isinstance(srv.get("Platform"), dict) else ""
            return {"ServerVersion": srv.get("Version", ""), "OperatingSystem": plat or srv.get("Os", ""),
                    "KernelVersion": srv.get("KernelVersion", ""), "Name": plat}, ""
        why = "timed out after 30 s" if code == 124 else ((err or out or "").strip().splitlines() or [f"exit {code}"])[-1]
        errors.append(f"{host or 'default context'}: {why} ({time.time() - t0:.1f}s)")
    _DOCKER_ENV.pop("DOCKER_HOST", None)
    return None, "; ".join(errors)


# ------------------------------------------------------------------ VM disk image
def _vm_candidates() -> list:
    """(engine, path) for each VM disk file that exists. Computed at call time (HOME may change in tests)."""
    home = os.path.expanduser("~")
    colima_home = os.environ.get("COLIMA_HOME") or os.path.join(home, ".colima")
    pats = [
        ("Docker Desktop", os.path.join(home, "Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw")),
        ("Docker Desktop", os.path.join(home, "Library/Containers/com.docker.docker/Data/vms/0/data/Docker.qcow2")),
        ("Docker Desktop", os.path.join(home, "Library/Containers/com.docker.docker/Data/vms/0/Docker.qcow2")),
        ("OrbStack", os.path.join(home, "Library/Group Containers/*.dev.orbstack/data/data.img")),
        ("OrbStack", os.path.join(home, ".orbstack/data/data.img")),
        ("Colima", os.path.join(colima_home, "_lima/_disks/*/datadisk")),
        ("Colima", os.path.join(colima_home, "_lima/*/diffdisk")),
    ]
    found, seen = [], set()
    for engine, pat in pats:
        for p in sorted(glob.glob(pat)):
            rp = os.path.realpath(p)
            if rp in seen or not os.path.isfile(rp):
                continue
            seen.add(rp)
            found.append((engine, p))
    return found


def vm_disks() -> list:
    res = []
    for engine, p in _vm_candidates():
        try:
            st = os.stat(p)
        except OSError:
            continue
        res.append({"engine": engine, "path": p, "allocated": getattr(st, "st_blocks", 0) * 512,
                    "apparent": st.st_size, "mtime": int(st.st_mtime)})
    return res


def _engine(info: dict, disks: list) -> str:
    blob = " ".join(str(info.get(k, "")) for k in ("OperatingSystem", "Name", "KernelVersion")).lower()
    ctx = ""
    code, out, _e = _docker(["context", "show"], timeout=10)
    if code == 0:
        ctx = out.strip().lower()
    if "orbstack" in blob or "orbstack" in ctx:
        return "OrbStack"
    if "colima" in blob or "colima" in ctx:
        return "Colima"
    if "docker desktop" in blob or ctx.startswith("desktop-"):
        return "Docker Desktop"
    if disks:
        return max(disks, key=lambda d: d["allocated"])["engine"]
    return "Docker"


# ------------------------------------------------------------------ collection
def _system_df_verbose() -> dict | None:
    code, out, _e = _docker(["system", "df", "-v", "--format", "{{json .}}"], timeout=120)
    if code != 0:
        return None
    for d in json_lines(out):
        if any(k in d for k in ("Images", "Volumes", "BuildCache", "Containers")):
            return d
    return None


def _system_df_summary() -> dict:
    code, out, _e = _docker(["system", "df", "--format", "{{json .}}"], timeout=60)
    res = {}
    if code != 0:
        return res
    key = {"images": "images", "containers": "containers", "local volumes": "volumes", "build cache": "build_cache"}
    for d in json_lines(out):
        k = key.get(str(d.get("Type", "")).strip().lower())
        if k:
            res[k] = {"size": parse_size(d.get("Size")), "reclaimable": parse_size(d.get("Reclaimable")),
                      "count": _int(d.get("TotalCount")), "active": _int(d.get("Active"))}
    return res


def _containers(with_size: bool = True) -> tuple:
    """Container dicts, and whether sizes were measured."""
    sized = with_size
    out = ""
    if with_size:
        code, out, _e = _docker(["ps", "-a", "--no-trunc", "--size", "--format", "{{json .}}"], timeout=60)
        if code != 0:
            sized = False
    if not sized:
        code, out, _e = _docker(["ps", "-a", "--no-trunc", "--format", "{{json .}}"], timeout=30)
        if code != 0:
            return [], False
    res = []
    for d in json_lines(out):
        labels = parse_labels(d.get("Labels"))
        size_s = str(d.get("Size") or "")
        virt = re.search(r"virtual\s+([\d.]+\s*[A-Za-z]+)", size_s)
        state = str(d.get("State") or "").lower()
        if not state:
            st = str(d.get("Status") or "").lower()
            state = "running" if st.startswith("up") else "exited" if st.startswith("exited") else "created"
        mounts = [m.strip() for m in str(d.get("Mounts") or "").split(",") if m.strip()]
        res.append({
            "id": str(d.get("ID") or ""), "short_id": short_id(d.get("ID")),
            "name": str(d.get("Names") or "").split(",")[0],
            "image": str(d.get("Image") or ""), "image_id": "",
            "state": state, "status": str(d.get("Status") or ""),
            "created": parse_time(d.get("CreatedAt")), "running_for": str(d.get("RunningFor") or ""),
            "size": parse_size(size_s) if sized else None,
            "virtual": parse_size(virt.group(1)) if (sized and virt) else None,
            "project": labels.get(PROJECT_LABEL, ""), "service": labels.get("com.docker.compose.service", ""),
            "mounts": mounts,
        })
    _fill_image_ids(res)
    return res, sized


def _fill_image_ids(conts: list) -> None:
    """Resolve each container's image ID (a tag can since point to another image) with batched inspects."""
    ids = [c["id"] for c in conts if c["id"]]
    found = {}
    for i in range(0, len(ids), 100):
        code, out, _e = _docker(["container", "inspect", "--format", "{{.Id}} {{.Image}}"] + ids[i:i + 100],
                                timeout=30)
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2:
                found[parts[0]] = parts[1]
    for c in conts:
        iid = found.get(c["id"]) or next((v for k, v in found.items() if k.startswith(c["id"]) and c["id"]), "")
        c["image_id"] = iid


def _image_ref_matches(ref: str, img: dict) -> bool:
    ref = (ref or "").strip()
    if not ref:
        return False
    sid = img["short_id"]
    bare = ref[7:] if ref.startswith("sha256:") else ref
    if re.fullmatch(r"[0-9a-f]{12,64}", bare) and sid and (bare.startswith(sid) or sid.startswith(bare)):
        return True
    if img["repository"] in ("", "<none>"):
        return False
    if "@" in ref:
        ref = ref.split("@", 1)[0]
    last = ref.rsplit("/", 1)[-1]
    name, tag = (ref.rsplit(":", 1) if ":" in last else (ref, "latest"))
    full = img["repository"]
    for prefix in ("docker.io/library/", "docker.io/", "library/"):
        if name.startswith(prefix) and not full.startswith(prefix):
            name = name[len(prefix):]
    return name == full and tag == img["tag"]


def _images(df_images: list, containers: list) -> list:
    code, out, _e = _docker(["image", "ls", "--all", "--no-trunc", "--format", "{{json .}}"], timeout=60)
    rows = json_lines(out) if code == 0 else []
    code, dout, _e = _docker(["image", "ls", "--all", "--no-trunc", "--filter", "dangling=true", "--format",
                              "{{json .}}"], timeout=60)
    dangling_ids = {short_id(d.get("ID")) for d in json_lines(dout)} if code == 0 else set()
    dangling_failed = code != 0
    if not rows:
        rows = df_images
    df_by = {}
    for d in df_images:
        df_by[(short_id(d.get("ID")), str(d.get("Repository")), str(d.get("Tag")))] = d
        df_by.setdefault((short_id(d.get("ID")), None, None), d)
    imgs, seen = [], set()
    for d in rows:
        sid = short_id(d.get("ID"))
        repo, tag = str(d.get("Repository") or "<none>"), str(d.get("Tag") or "<none>")
        if (sid, repo, tag) in seen:
            continue
        seen.add((sid, repo, tag))
        extra = df_by.get((sid, repo, tag)) or df_by.get((sid, None, None)) or {}
        size = parse_size(d.get("Size") or extra.get("Size"))
        unique = extra.get("UniqueSize", d.get("UniqueSize"))
        shared = extra.get("SharedSize", d.get("SharedSize"))
        untagged = repo == "<none>" and tag == "<none>"
        imgs.append({
            "id": str(d.get("ID") or ""), "short_id": sid, "repository": repo, "tag": tag,
            "ref": f"{repo}:{tag}" if repo != "<none>" and tag != "<none>" else sid,
            "size": size,
            "unique_size": parse_size(unique) if unique not in (None, "", "N/A") else None,
            "shared_size": parse_size(shared) if shared not in (None, "", "N/A") else None,
            "created": parse_time(d.get("CreatedAt") or extra.get("CreatedAt")),
            "created_since": str(d.get("CreatedSince") or extra.get("CreatedSince") or ""),
            "df_containers": _int(extra.get("Containers")) if extra else 0,
            "dangling": untagged and (sid in dangling_ids or dangling_failed),
            "intermediate": untagged and not (sid in dangling_ids or dangling_failed),
            "in_use": False, "used_by": [], "old_tag": False, "tags_for_id": 1,
        })
    # in use: any container (running or not) whose image is this one
    for img in imgs:
        # A container keeps the image ID it was created from (the tag may have moved since): trust the ID.
        users = [c["name"] for c in containers
                 if (short_id(c["image_id"]) == img["short_id"] if c.get("image_id")
                     else _image_ref_matches(c.get("image", ""), img))]
        img["used_by"] = sorted(set(users))
        img["in_use"] = bool(users) or img["df_containers"] > 0
    per_id: dict = {}
    for img in imgs:
        per_id[img["short_id"]] = per_id.get(img["short_id"], 0) + 1
    for img in imgs:
        img["tags_for_id"] = per_id[img["short_id"]]
    # old tags: same repository, not the newest-created, not used by any container
    by_repo: dict = {}
    for img in imgs:
        if img["repository"] != "<none>" and img["tag"] != "<none>":
            by_repo.setdefault(img["repository"], []).append(img)
    for repo_imgs in by_repo.values():
        if len(repo_imgs) < 2:
            continue
        newest = max(repo_imgs, key=lambda i: i["created"])
        for img in repo_imgs:
            if img["short_id"] != newest["short_id"] and not img["in_use"]:
                img["old_tag"] = True
    imgs.sort(key=lambda i: (i["unique_size"] if i["unique_size"] is not None else i["size"]), reverse=True)
    return imgs


def _volumes(df_volumes: list, containers: list) -> list:
    code, out, _e = _docker(["volume", "ls", "--format", "{{json .}}"], timeout=60)
    rows = json_lines(out) if code == 0 else []
    df_by = {str(v.get("Name")): v for v in df_volumes}
    names = [str(r.get("Name")) for r in rows] or list(df_by)
    by_row = {str(r.get("Name")): r for r in rows}
    mounted: dict = {}
    for c in containers:
        for m in c.get("mounts", []):
            mounted.setdefault(m, []).append(c["name"])
    res = []
    for n in names:
        r, extra = by_row.get(n, {}), df_by.get(n, {})
        labels = parse_labels(r.get("Labels") if r.get("Labels") not in (None, "") else extra.get("Labels"))
        links = _int(extra.get("Links")) if extra else 0
        size = parse_size(extra.get("Size")) if extra.get("Size") not in (None, "", "N/A") else None
        users = sorted(set(mounted.get(n, [])))
        res.append({
            "name": n, "driver": str(r.get("Driver") or extra.get("Driver") or "local"),
            "mountpoint": str(r.get("Mountpoint") or extra.get("Mountpoint") or ""),
            "project": labels.get(PROJECT_LABEL, ""), "compose_volume": labels.get("com.docker.compose.volume", ""),
            "anonymous": bool(re.fullmatch(r"[0-9a-f]{64}", n)) or "com.docker.volume.anonymous" in labels,
            "links": links, "size": size, "used_by": users,
            "unused": links == 0 and not users,
        })
    res.sort(key=lambda v: v["size"] or 0, reverse=True)
    return res


def _build_cache(df_build: list) -> tuple:
    rows = []
    for b in df_build:
        rows.append({"id": short_id(b.get("ID")), "type": str(b.get("CacheType") or ""),
                     "description": str(b.get("Description") or "")[:200], "size": parse_size(b.get("Size")),
                     "in_use": _bool(b.get("InUse")), "shared": _bool(b.get("Shared")),
                     "last_used": str(b.get("LastUsedSince") or b.get("LastUsedAt") or ""),
                     "created": str(b.get("CreatedSince") or b.get("CreatedAt") or ""),
                     "usage": _int(b.get("UsageCount"))})
    rows.sort(key=lambda r: r["size"], reverse=True)
    total = sum(r["size"] for r in rows)
    reclaimable = sum(r["size"] for r in rows if not r["in_use"] and not r["shared"])
    return rows, total, reclaimable


def _unavailable(reason: str, disks: list) -> dict:
    res = {"available": False, "reason": reason, "vm_disks": disks,
           "vm_disk": max(disks, key=lambda d: d["allocated"]) if disks else None,
           "engine": max(disks, key=lambda d: d["allocated"])["engine"] if disks else None,
           "cli": have("docker"), "scanned": int(time.time())}
    db.kv_set(KEY, res)
    return res


def scan(log=print) -> dict:
    disks = vm_disks()
    if not have("docker"):
        log("docker CLI not found")
        return _unavailable("The docker command isn't installed (or not on PATH).", disks)
    progress.update(current="docker version")
    info, why = _ping(log)
    if info is None:
        reason = "Docker didn't answer" if "timed out" in why else "Docker isn't running"
        log(f"{reason}: {why[:600]}")
        res = _unavailable(reason, disks)
        res["detail"] = why[:600]
        db.kv_set(KEY, res)
        return res
    # Extra details (root dir...) from `docker info`; optional, it can be slow (it runs every CLI plugin).
    code, out, _err = _docker(["info", "--format", "{{json .}}"], timeout=20)
    more = (json_lines(out) or [{}])[0] if code == 0 else {}
    if isinstance(more, dict):
        info = dict(more, **{k: v for k, v in info.items() if v})
    jobs.checkpoint()
    engine = _engine(info, disks)
    log(f"Docker engine: {engine} {info.get('ServerVersion', '')}".rstrip())

    progress.update(current="docker system df -v")
    summary = _system_df_summary()
    verbose = _system_df_verbose()
    jobs.checkpoint()
    progress.update(current="docker ps -a --size")
    containers, sized = _containers(with_size=True)
    if not sized:
        log("container sizes unavailable (docker ps --size failed or timed out)")
    jobs.checkpoint()
    progress.update(current="docker image ls")
    images = _images((verbose or {}).get("Images") or [], containers)
    jobs.checkpoint()
    progress.update(current="docker volume ls")
    volumes = _volumes((verbose or {}).get("Volumes") or [], containers)
    build_rows, build_total, build_recl = _build_cache((verbose or {}).get("BuildCache") or [])

    # Totals: prefer Docker's own summary (it de-duplicates shared layers), else derive from the lists.
    top_level = [i for i in images if not i["intermediate"]]
    img_unique = lambda i: i["unique_size"] if i["unique_size"] is not None else i["size"]
    seen_ids, unused_img_bytes = set(), 0
    for i in images:
        if not i["in_use"] and i["short_id"] not in seen_ids and not i["intermediate"]:
            seen_ids.add(i["short_id"])
            unused_img_bytes += img_unique(i)
    s_img = summary.get("images", {})
    s_cont = summary.get("containers", {})
    s_vol = summary.get("volumes", {})
    s_bc = summary.get("build_cache", {})
    dangling = [i for i in images if i["dangling"]]
    stopped = [c for c in containers if c["state"] not in ("running", "restarting", "paused")]
    unused_vols = [v for v in volumes if v["unused"]]
    totals = {
        "images": {"count": len(top_level), "size": s_img.get("size", sum({i["short_id"]: img_unique(i) for i in images}.values())),
                   "reclaimable": s_img.get("reclaimable", unused_img_bytes),
                   "dangling_count": len(dangling), "dangling_size": sum(img_unique(i) for i in dangling),
                   "old_tag_count": sum(1 for i in images if i["old_tag"]),
                   "old_tag_size": sum(img_unique(i) for i in images if i["old_tag"]),
                   "unused_count": len({i["short_id"] for i in top_level if not i["in_use"]})},
        "containers": {"count": len(containers), "running": len(containers) - len(stopped), "stopped": len(stopped),
                       "size": s_cont.get("size", sum(c["size"] or 0 for c in containers)),
                       "reclaimable": s_cont.get("reclaimable", sum(c["size"] or 0 for c in stopped))},
        "volumes": {"count": len(volumes), "size": s_vol.get("size", sum(v["size"] or 0 for v in volumes)),
                    "unused_count": len(unused_vols),
                    "unused_size": sum(v["size"] or 0 for v in unused_vols) if verbose else s_vol.get("reclaimable", 0),
                    "anonymous_unused_count": sum(1 for v in unused_vols if v["anonymous"]),
                    "anonymous_unused_size": sum(v["size"] or 0 for v in unused_vols if v["anonymous"]),
                    "reclaimable": s_vol.get("reclaimable", sum(v["size"] or 0 for v in unused_vols))},
        "build_cache": {"count": len(build_rows) or s_bc.get("count", 0), "size": s_bc.get("size", build_total),
                        "reclaimable": s_bc.get("reclaimable", build_recl)},
    }
    used = sum(totals[k]["size"] for k in ("images", "containers", "volumes", "build_cache"))
    reclaimable = sum(totals[k]["reclaimable"] for k in ("images", "containers", "build_cache"))
    vm = next((d for d in disks if d["engine"] == engine), None) or (max(disks, key=lambda d: d["allocated"])
                                                                     if disks else None)
    res = {
        "available": True, "engine": engine, "server_version": str(info.get("ServerVersion") or ""),
        "os": str(info.get("OperatingSystem") or ""), "root_dir": str(info.get("DockerRootDir") or ""),
        "verbose_df": verbose is not None, "container_sizes": sized,
        "vm_disks": disks, "vm_disk": vm,
        "used": used, "reclaimable": reclaimable,
        "reclaimable_with_volumes": reclaimable + totals["volumes"]["reclaimable"],
        "totals": totals, "images": images, "containers": containers, "volumes": volumes,
        "build_cache": build_rows[:MAX_BUILD_ROWS], "build_cache_rows": len(build_rows),
        "scanned": int(time.time()),
    }
    db.kv_set(KEY, res)
    log(f"Docker: {len(top_level)} images, {len(containers)} containers, {len(volumes)} volumes, "
        f"{human(used / 1024)} used, {human(reclaimable / 1024)} reclaimable"
        + (f"; VM disk {human(vm['allocated'] / 1024)} allocated" if vm else ""))
    return res


# ------------------------------------------------------------------ actions
def _stream(cmd: list, log) -> tuple:
    lines: list = []

    def on_line(line):
        lines.append(line)
        log(line)
    log("$ " + " ".join(cmd))
    code = stream(cmd, on_line, timeout=3600, env=_DOCKER_ENV)
    return code, "\n".join(lines)


def _rescan(log) -> None:
    log("Refreshing the Docker panel ...")
    try:
        scan(log=log)
    except jobs.Cancelled:
        raise
    except Exception as ex:  # the action itself succeeded; a failed refresh is only logged
        log(f"refresh failed: {ex}")


def _require_running() -> None:
    info, why = _ping()
    if info is None:
        raise RuntimeError(f"Docker isn't reachable: {why}")


def rm_image(target: str, log=print) -> dict:
    _require_running()
    target = (target or "").strip()
    if not target or target.startswith("-"):
        raise ValueError("bad image reference")
    containers, _s = _containers(with_size=False)
    images = _images([], containers)
    matches = [i for i in images if i["ref"] == target or i["id"] == target or i["short_id"] == short_id(target)
               or _image_ref_matches(target, i)]
    if not matches:
        raise RuntimeError(f"No image {target}")
    by_id = [i for i in images if i["short_id"] == matches[0]["short_id"]]
    if any(i["in_use"] for i in matches):
        users = sorted({u for i in matches for u in i["used_by"]})
        raise RuntimeError(f"Refusing: {target} is used by container(s) {', '.join(users) or '(unknown)'}"
                           " — remove the container first")
    prev = db.kv_get(KEY) or {}
    old = next((i for i in prev.get("images", []) if i.get("short_id") == matches[0]["short_id"]), {})
    # Removing one tag of an image that has several only untags it: nothing is freed.
    last_ref = len(by_id) <= 1 or not (target == matches[0]["ref"] and ":" in target)
    est = (old.get("unique_size") if old.get("unique_size") is not None else old.get("size", 0)) if last_ref else 0
    code, out = _stream(["docker", "image", "rm", target], log)
    ok = code == 0
    db.log_action("docker", f"image {target}", ok, int((est or 0) / 1024) if ok else 0, out[-1000:])
    _rescan(log)
    if not ok:
        raise RuntimeError(f"docker image rm failed (exit {code})")
    return {"ok": True, "freed_kb": int((est or 0) / 1024)}


def rm_container(target: str, log=print) -> dict:
    _require_running()
    target = (target or "").strip()
    if not target or target.startswith("-"):
        raise ValueError("bad container reference")
    containers, _s = _containers(with_size=False)
    c = next((c for c in containers if target in (c["id"], c["short_id"], c["name"])
              or (len(target) >= 12 and c["id"].startswith(target))), None)
    if not c:
        raise RuntimeError(f"No container {target}")
    if c["state"] in ("running", "restarting", "paused"):
        raise RuntimeError(f"Refusing: container {c['name']} is {c['state']} — stop it first")
    code, out = _stream(["docker", "rm", c["id"]], log)
    ok = code == 0
    prev = db.kv_get(KEY) or {}
    old = next((x for x in prev.get("containers", []) if x.get("id") == c["id"]), {})
    freed = (old.get("size") or 0) if ok else 0
    db.log_action("docker", f"container {c['name']}", ok, int(freed / 1024), out[-1000:])
    _rescan(log)
    if not ok:
        raise RuntimeError(f"docker rm failed (exit {code})")
    return {"ok": True, "freed_kb": int(freed / 1024)}


def rm_volume(target: str, log=print) -> dict:
    _require_running()
    target = (target or "").strip()
    if not target or target.startswith("-"):
        raise ValueError("bad volume name")
    containers, _s = _containers(with_size=False)
    verbose = _system_df_verbose()
    vols = _volumes((verbose or {}).get("Volumes") or [], containers)
    v = next((v for v in vols if v["name"] == target), None)
    if not v:
        raise RuntimeError(f"No volume {target}")
    if not v["unused"]:
        raise RuntimeError(f"Refusing: volume {target} is used by "
                           f"{', '.join(v['used_by']) or str(v['links']) + ' container(s)'}")
    code, out = _stream(["docker", "volume", "rm", target], log)
    ok = code == 0
    freed = (v.get("size") or 0) if ok else 0
    db.log_action("docker", f"volume {target}", ok, int(freed / 1024), out[-1000:])
    _rescan(log)
    if not ok:
        raise RuntimeError(f"docker volume rm failed (exit {code})")
    return {"ok": True, "freed_kb": int(freed / 1024)}


def _prune(what: str, cmd: list, log, fallback: list | None = None) -> dict:
    _require_running()
    code, out = _stream(cmd, log)
    if code != 0 and fallback and "unknown flag" in out:
        log("This Docker version has no --all flag; its volume prune already removes every unused volume.")
        code, out = _stream(fallback, log)
    ok = code == 0
    freed = reclaimed_bytes(out) if ok else 0
    log(f"Reclaimed {human(freed / 1024)}")
    db.log_action("docker", what, ok, int(freed / 1024), out[-1500:])
    _rescan(log)
    if not ok:
        raise RuntimeError(f"{' '.join(cmd)} failed (exit {code})")
    return {"ok": True, "freed_kb": int(freed / 1024)}


def prune_dangling(log=print):
    return _prune("prune dangling images", ["docker", "image", "prune", "-f"], log)


def prune_unused_images(log=print):
    return _prune("prune unused images", ["docker", "image", "prune", "-a", "-f"], log)


def prune_stopped(log=print):
    return _prune("prune stopped containers", ["docker", "container", "prune", "-f"], log)


def prune_volumes(log=print):
    return _prune("prune anonymous volumes", ["docker", "volume", "prune", "-f"], log)


def prune_volumes_all(log=print):
    return _prune("prune unused volumes (incl. named)", ["docker", "volume", "prune", "-f", "--all"], log,
                  fallback=["docker", "volume", "prune", "-f"])


def prune_build_cache(log=print):
    return _prune("prune build cache", ["docker", "builder", "prune", "-f"], log)


ACTIONS = {
    "docker-rm-image": {"label": lambda t: f"docker image rm {t}", "fn": rm_image, "target": True, "lane": "heavy"},
    "docker-rm-container": {"label": lambda t: f"docker rm {short_id(t) if re.fullmatch(r'[0-9a-f]{64}', t or '') else t}",
                            "fn": rm_container, "target": True, "lane": "heavy"},
    "docker-rm-volume": {"label": lambda t: f"docker volume rm {t}", "fn": rm_volume, "target": True, "lane": "heavy"},
    "docker-prune-dangling": {"label": "Remove dangling Docker images", "fn": prune_dangling, "target": False,
                              "lane": "heavy"},
    "docker-prune-unused-images": {"label": "Remove Docker images not used by any container",
                                   "fn": prune_unused_images, "target": False, "lane": "heavy"},
    "docker-prune-stopped": {"label": "Remove stopped Docker containers", "fn": prune_stopped, "target": False,
                             "lane": "heavy"},
    "docker-prune-volumes": {"label": "Remove unused anonymous Docker volumes", "fn": prune_volumes,
                             "target": False, "lane": "heavy"},
    "docker-prune-volumes-all": {"label": "Remove all unused Docker volumes (incl. named)", "fn": prune_volumes_all,
                                 "target": False, "lane": "heavy"},
    "docker-prune-build-cache": {"label": "Remove Docker build cache", "fn": prune_build_cache, "target": False,
                                 "lane": "heavy"},
}


# ------------------------------------------------------------------ alerts
def alerts(cfg) -> list:
    d = db.kv_get(KEY) or {}
    if not d.get("available"):
        return []
    rec = d.get("reclaimable", 0)
    limit = float(cfg.get("docker_alert_gb", 10)) * 1000 ** 3
    if rec <= limit:
        return []
    week = int(time.time() // (7 * 86400))
    return [{"id": f"docker:reclaimable:{week}", "level": "notice", "notify": True,
             "title": f"Docker: {human(rec / 1024)} reclaimable (unused images, stopped containers, build cache)",
             "section": "docker"}]
