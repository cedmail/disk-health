"""Docker panel tests with a fake `docker` CLI (realistic JSON output, stateful removals).

    python3 -m unittest discover -s tests          (also runs on Python 3.9)
"""
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------- environment (before importing diskhealth)
# diskhealth.config reads HOME / DISK_HEALTH_DATA once at import. Share test_engine's environment when it is
# around (unittest discover imports both modules in one process), else set up our own temp dirs.
if "diskhealth" not in sys.modules:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import test_engine  # noqa: F401,E402  (sets HOME, DISK_HEALTH_DATA, PATH, imports diskhealth)
    except Exception:
        _tmp = Path(tempfile.mkdtemp(prefix="dh-docker-"))
        (_tmp / "home").mkdir()
        (_tmp / "data").mkdir()
        os.environ["HOME"] = str(_tmp / "home")
        os.environ["DISK_HEALTH_DATA"] = str(_tmp / "data")
sys.path.insert(0, str(ROOT))

from diskhealth import db  # noqa: E402
from diskhealth.features import docker as dk  # noqa: E402

# ---------------------------------------------------------------- fake docker
# Python script; state (removed objects, "daemon down") lives in $FAKE_DOCKER_STATE (JSON).
FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys

STATE = os.environ.get("FAKE_DOCKER_STATE", "")
st = {"removed": [], "down": False, "log": []}
if STATE and os.path.exists(STATE):
    with open(STATE) as f:
        st = json.load(f)
st["log"].append(sys.argv[1:])

def save():
    if STATE:
        with open(STATE, "w") as f:
            json.dump(st, f)

H = "0123456789abcdef"
def iid(n):  # full sha256 image id
    return "sha256:" + (n * 64)[:64]
def cid(n):
    return (n * 64)[:64]

IMAGES = [  # repo, tag, id char, size, created, unique, containers
    ("jahia/jahia-ee", "8.2.1.0", "a1", "1.82GB", "2024-09-20 10:00:00 +0200 CEST", "412MB", 1),
    ("jahia/jahia-ee", "8.2.0.0", "a2", "1.79GB", "2024-05-02 09:00:00 +0200 CEST", "380MB", 0),
    ("jahia/jahia-ee", "8.1.6.0", "a3", "1.71GB", "2023-11-14 09:00:00 +0100 CET", "1.71GB", 1),
    ("jahia/jahia-discovery", "8.2.1.0", "b1", "2.05GB", "2024-09-21 10:00:00 +0200 CEST", "2.05GB", 0),
    ("jcustomer/jcustomer", "2.5.0", "c1", "612MB", "2024-06-01 12:00:00 +0000 UTC", "612MB", 1),
    ("docker.elastic.co/elasticsearch/elasticsearch", "7.17.10", "d1", "1.15GB", "2023-04-20 12:00:00 +0000 UTC", "1.15GB", 1),
    ("mariadb", "10.11", "e1", "404MB", "2024-08-01 12:00:00 +0000 UTC", "404MB", 1),
    ("postgres", "16", "f1", "453MB", "2024-08-10 12:00:00 +0000 UTC", "453MB", 0),
    ("postgres", "15", "f2", "425MB", "2024-02-10 12:00:00 +0000 UTC", "425MB", 0),
    ("<none>", "<none>", "90", "1.78GB", "2024-04-02 09:00:00 +0200 CEST", "350MB", 0),
    ("<none>", "<none>", "91", "12.5kB", "2024-04-03 09:00:00 +0200 CEST", "12.5kB", 0),
]
INTERMEDIATE = ("<none>", "<none>", "77", "1.70GB", "2024-04-02 08:00:00 +0200 CEST", "0B", 0)
DANGLING = {"90", "91"}
CONTAINERS = [  # name, image ref, image id char, state, status, size, project, mounts
    ("jahia-jahia-1", "jahia/jahia-ee:8.2.1.0", "a1", "running", "Up 3 hours", "48.2MB", "jahia", "jahia_jahia-data"),
    ("jahia-jcustomer-1", "jcustomer/jcustomer:2.5.0", "c1", "running", "Up 3 hours", "1.2MB", "jahia", ""),
    ("jahia-elasticsearch-1", "docker.elastic.co/elasticsearch/elasticsearch:7.17.10", "d1", "running", "Up 3 hours", "85kB", "jahia", "jahia_es-data"),
    ("jahia-mariadb-1", "mariadb:10.11", "e1", "exited", "Exited (0) 2 days ago", "2B", "jahia", "jahia_mariadb-data"),
    ("old-jahia", "jahia/jahia-ee:8.1.6.0", "a3", "exited", "Exited (143) 5 weeks ago", "312MB", "", ""),
]
ANON = "c" * 64
VOLUMES = [  # name, links, size, labels
    ("jahia_jahia-data", 1, "3.21GB", "com.docker.compose.project=jahia,com.docker.compose.version=2.24.6,com.docker.compose.volume=jahia-data"),
    ("jahia_es-data", 1, "1.02GB", "com.docker.compose.volume=es-data,com.docker.compose.project=jahia"),
    ("jahia_mariadb-data", 1, "512MB", "com.docker.compose.project=jahia,com.docker.compose.volume=mariadb-data"),
    ("oldproj_db-data", 0, "2.1GB", "com.docker.compose.project=oldproj,com.docker.compose.volume=db-data"),
    (ANON, 0, "300MB", "com.docker.volume.anonymous="),
]
BUILD = [
    ("k1abcdef1234", "regular", "1.1GB", False, False, "2 weeks ago", "mount / from exec /bin/sh -c mvn package"),
    ("k2abcdef1234", "source.local", "45MB", True, False, "3 hours ago", "local source for context"),
    ("k3abcdef1234", "regular", "220MB", False, True, "5 days ago", "pulled from docker.io/library/maven"),
]

removed = set(st["removed"])
args = sys.argv[1:]
a = " ".join(args)
fmt_json = "{{json .}}" in args

if st.get("down") and args[:1] != ["context"]:
    sys.stderr.write("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?\n")
    if args[:1] == ["info"]:
        print(json.dumps({"ServerErrors": ["Cannot connect to the Docker daemon"], "ClientInfo": {}}))
    save(); sys.exit(1)

def img_rows(include_intermediate=False, dangling_only=False):
    out = []
    lst = IMAGES + ([INTERMEDIATE] if include_intermediate and not dangling_only else [])
    for repo, tag, ch, size, created, uniq, nc in lst:
        if iid(ch) in removed or ("%s:%s" % (repo, tag)) in removed:
            continue
        if dangling_only and ch not in DANGLING:
            continue
        out.append((repo, tag, ch, size, created, uniq, nc))
    return out

def live_containers():
    return [c for c in CONTAINERS if cid(c[2] + c[0][:2]) not in removed]

def ccid(c):
    return cid(c[2] + c[0][:2])

def vol_links(name):
    return sum(1 for c in live_containers() if c[7] == name)

def live_volumes():
    return [v for v in VOLUMES if v[0] not in removed]

if args[:1] == ["context"]:
    print("desktop-linux"); save(); sys.exit(0)
if args[:1] == ["version"]:
    print(json.dumps({"Platform": {"Name": "Docker Desktop 4.34.0 (165256)"}, "Version": "27.3.1", "Os": "linux",
                      "KernelVersion": "6.10.4-linuxkit"}))
    save(); sys.exit(0)
if args[:1] == ["info"]:
    if "{{json .ServerVersion}}" in args:
        print('"27.3.1"')
    else:
        print(json.dumps({"ServerVersion": "27.3.1", "OperatingSystem": "Docker Desktop", "Name": "docker-desktop",
                          "DockerRootDir": "/var/lib/docker", "ServerErrors": None}))
    save(); sys.exit(0)

if args[:2] == ["system", "df"]:
    imgs = img_rows()
    if "-v" in args:
        if os.environ.get("FAKE_DOCKER_OLD"):
            sys.stderr.write("the verbose and the format options conflict\n"); sys.exit(1)
        print(json.dumps({
            "Images": [{"Containers": str(sum(1 for c in live_containers() if c[2] == ch)), "CreatedAt": created,
                        "CreatedSince": "x ago", "Digest": "<none>", "ID": iid(ch)[7:19], "Repository": repo,
                        "SharedSize": "1.2GB" if repo.startswith("jahia/jahia-ee") else "0B", "Size": size,
                        "Tag": tag, "UniqueSize": uniq, "VirtualSize": size}
                       for repo, tag, ch, size, created, uniq, nc in imgs],
            "Containers": [{"ID": ccid(c)[:12], "Image": c[1], "Names": c[0], "State": c[3], "Status": c[4],
                            "Size": c[5], "LocalVolumes": "1" if c[7] else "0", "Mounts": c[7]} for c in live_containers()],
            "Volumes": [{"Driver": "local", "Labels": lab, "Links": str(vol_links(n)), "Mountpoint": "/var/lib/docker/volumes/%s/_data" % n,
                         "Name": n, "Scope": "local", "Size": size} for n, links, size, lab in live_volumes()],
            "BuildCache": [{"CacheType": t, "CreatedSince": "a month ago", "Description": d, "ID": i, "InUse": "true" if u else "false",
                            "LastUsedSince": last, "Shared": "true" if sh else "false", "Size": s, "UsageCount": "3"}
                           for i, t, s, u, sh, last, d in BUILD if "build" not in removed],
        }))
    else:
        print('{"Active":"4","Reclaimable":"7.3GB (52%%)","Size":"13.9GB","TotalCount":"%d","Type":"Images"}' % len(imgs))
        print('{"Active":"3","Reclaimable":"312MB (86%%)","Size":"361.5MB","TotalCount":"%d","Type":"Containers"}' % len(live_containers()))
        print('{"Active":"3","Reclaimable":"2.4GB (33%%)","Size":"7.142GB","TotalCount":"%d","Type":"Local Volumes"}' % len(live_volumes()))
        print('{"Active":"1","Reclaimable":"1.1GB","Size":"1.365GB","TotalCount":"3","Type":"Build Cache"}' if "build" not in removed
              else '{"Active":"0","Reclaimable":"0B","Size":"0B","TotalCount":"0","Type":"Build Cache"}')
    save(); sys.exit(0)

if args[:2] == ["image", "ls"]:
    dangling = "dangling=true" in args
    for repo, tag, ch, size, created, uniq, nc in img_rows("--all" in args, dangling):
        print(json.dumps({"Containers": "N/A", "CreatedAt": created, "CreatedSince": "x ago", "Digest": "<none>",
                          "ID": iid(ch) if "--no-trunc" in args else iid(ch)[7:19], "Repository": repo, "SharedSize": "N/A",
                          "Size": size, "Tag": tag, "UniqueSize": "N/A", "VirtualSize": size}))
    save(); sys.exit(0)

if args[:1] == ["ps"]:
    if "--size" in args and os.environ.get("FAKE_DOCKER_SLOW_SIZE"):
        sys.exit(124)
    for c in live_containers():
        d = {"Command": "\"/entrypoint.sh\"", "CreatedAt": "2024-09-30 08:00:00 +0200 CEST", "ID": ccid(c),
             "Image": c[1], "Labels": ("com.docker.compose.project=%s,com.docker.compose.service=%s,desc=a,b" % (c[6], c[0].split("-")[1])) if c[6] else "maintainer=Jahia",
             "LocalVolumes": "1" if c[7] else "0", "Mounts": c[7], "Names": c[0], "Networks": "jahia_default",
             "Ports": "", "RunningFor": "3 hours ago", "State": c[3], "Status": c[4]}
        if "--size" in args:
            d["Size"] = "%s (virtual 1.8GB)" % c[5]
        print(json.dumps(d))
    save(); sys.exit(0)

if args[:2] == ["container", "inspect"]:
    ids = [x for x in args[2:] if not x.startswith("-") and "{{" not in x]
    for c in live_containers():
        if ccid(c) in ids:
            print("%s %s" % (ccid(c), iid(c[2])))
    save(); sys.exit(0)

if args[:2] == ["image", "rm"]:
    t = args[2]
    for repo, tag, ch, size, created, uniq, nc in img_rows():
        if t in ("%s:%s" % (repo, tag), iid(ch), iid(ch)[7:19]):
            if any(c[2] == ch for c in live_containers()):
                sys.stderr.write("Error response from daemon: conflict: unable to remove repository reference\n"); sys.exit(1)
            st["removed"].append(iid(ch)); print("Untagged: %s:%s" % (repo, tag)); print("Deleted: " + iid(ch)); save(); sys.exit(0)
    sys.stderr.write("Error: No such image: %s\n" % t); save(); sys.exit(1)

if args[:1] == ["rm"]:
    for c in live_containers():
        if args[1] in (ccid(c), c[0]):
            if c[3] == "running":
                sys.stderr.write("Error: cannot remove a running container\n"); sys.exit(1)
            st["removed"].append(ccid(c)); print(args[1]); save(); sys.exit(0)
    sys.stderr.write("Error: No such container\n"); save(); sys.exit(1)

if args[:2] == ["volume", "rm"]:
    st["removed"].append(args[2]); print(args[2]); save(); sys.exit(0)

if args[:2] == ["volume", "ls"]:
    for n, links, size, lab in live_volumes():
        print(json.dumps({"Driver": "local", "Labels": lab, "Links": "N/A", "Mountpoint": "/var/lib/docker/volumes/%s/_data" % n,
                          "Name": n, "Scope": "local", "Size": "N/A"}))
    save(); sys.exit(0)

if args[:2] == ["volume", "prune"]:
    if "--all" in args and os.environ.get("FAKE_DOCKER_OLD"):
        print("unknown flag: --all"); save(); sys.exit(125)
    print("Deleted Volumes:")
    for n, links, size, lab in live_volumes():
        if vol_links(n) == 0 and ("--all" in args or n == ANON):
            print(n); st["removed"].append(n)
    print("")
    print("Total reclaimed space: %s" % ("2.4GB" if "--all" in args else "300MB"))
    save(); sys.exit(0)

if args[:2] in (["image", "prune"], ["container", "prune"], ["builder", "prune"]):
    if args[0] == "builder":
        st["removed"].append("build")
        print("ID\t\tRECLAIMABLE\tSIZE\tLAST ACCESSED"); print("k1abcdef1234\ttrue\t1.1GB\t2 weeks ago")
        print("Total:\t1.1GB")
        print("Total reclaimed space: 1.1GB")  # older format prints this line
    elif args[0] == "container":
        print("Deleted Containers:")
        for c in live_containers():
            if c[3] != "running":
                print(ccid(c)); st["removed"].append(ccid(c))
        print(""); print("Total reclaimed space: 312MB")
    else:
        print("Deleted Images:")
        for repo, tag, ch, size, created, uniq, nc in img_rows():
            if ch in DANGLING or ("-a" in args and not any(c[2] == ch for c in live_containers())):
                print("deleted: " + iid(ch)); st["removed"].append(iid(ch))
        print(""); print("Total reclaimed space: %s" % ("7.3GB" if "-a" in args else "350MB"))
    save(); sys.exit(0)

sys.stderr.write("fake docker: unsupported: %s\n" % a)
save(); sys.exit(1)
'''


def write_fake(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    p = bin_dir / "docker"
    p.write_text(FAKE_DOCKER.replace("#!/usr/bin/env python3", "#!" + sys.executable, 1))
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return p


class DockerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dh-dk-"))
        self.bin = self.tmp / "bin"
        write_fake(self.bin)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.state = self.tmp / "state.json"
        self._env = {k: os.environ.get(k) for k in ("PATH", "HOME", "FAKE_DOCKER_STATE", "FAKE_DOCKER_OLD",
                                                    "FAKE_DOCKER_SLOW_SIZE", "COLIMA_HOME")}
        os.environ["PATH"] = f"{self.bin}{os.pathsep}{self._env['PATH'] or ''}"
        os.environ["HOME"] = str(self.home)
        os.environ["FAKE_DOCKER_STATE"] = str(self.state)
        for k in ("FAKE_DOCKER_OLD", "FAKE_DOCKER_SLOW_SIZE", "COLIMA_HOME"):
            os.environ.pop(k, None)
        self.logs = []

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def log(self, msg=""):
        self.logs.append(str(msg))

    def set_state(self, **kw):
        st = json.loads(self.state.read_text()) if self.state.exists() else {"removed": [], "down": False, "log": []}
        st.update(kw)
        self.state.write_text(json.dumps(st))

    def calls(self):
        return json.loads(self.state.read_text())["log"] if self.state.exists() else []

    def make_vm_disk(self, rel="Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw",
                     apparent=64 * 1024 ** 3, written=3 * 1024 ** 2):
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"\1" * written)
            f.truncate(apparent)
        return p


class ParseTests(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(dk.parse_size("1.23GB"), 1230000000)
        self.assertEqual(dk.parse_size("512MB"), 512000000)
        self.assertEqual(dk.parse_size("0B"), 0)
        self.assertEqual(dk.parse_size("12.5kB"), 12500)
        self.assertEqual(dk.parse_size("2B"), 2)
        self.assertEqual(dk.parse_size("1.2GB (45%)"), 1200000000)
        self.assertEqual(dk.parse_size("48.2MB (virtual 1.8GB)"), 48200000)
        self.assertEqual(dk.parse_size("1GiB"), 1024 ** 3)
        self.assertEqual(dk.parse_size("N/A"), 0)
        self.assertEqual(dk.parse_size(""), 0)
        self.assertEqual(dk.parse_size(None), 0)
        self.assertEqual(dk.parse_size(1234), 1234)

    def test_labels(self):
        self.assertEqual(dk.parse_labels("com.docker.compose.project=jahia,desc=a,b,x="),
                         {"com.docker.compose.project": "jahia", "desc": "a,b", "x": ""})
        self.assertEqual(dk.parse_labels(""), {})
        self.assertEqual(dk.parse_labels({"a": 1}), {"a": "1"})

    def test_time(self):
        self.assertEqual(dk.parse_time("2024-05-01 10:00:00 +0200 CEST"), 1714550400)
        self.assertEqual(dk.parse_time("2024-05-01T08:00:00.123456789Z"), 1714550400)
        self.assertEqual(dk.parse_time("garbage"), 0)

    def test_reclaimed(self):
        out = "Deleted Images:\ndeleted: sha256:aa\n\nTotal reclaimed space: 1.5GB\nTotal reclaimed space: 500MB"
        self.assertEqual(dk.reclaimed_bytes(out), 2000000000)
        self.assertEqual(dk.reclaimed_bytes("nothing"), 0)

    def test_json_lines(self):
        self.assertEqual(dk.json_lines('{"a":1}\nWARNING: x\n{"b":2}\n'), [{"a": 1}, {"b": 2}])
        self.assertEqual(dk.json_lines('[{"a":1}]'), [{"a": 1}])
        self.assertEqual(dk.json_lines(""), [])

    def test_short_id(self):
        self.assertEqual(dk.short_id("sha256:" + "ab" * 32), "ab" * 6)


class ScanTests(DockerTestBase):
    def scan(self):
        return dk.scan(log=self.log)

    def test_scan_full(self):
        vm = self.make_vm_disk()
        r = self.scan()
        self.assertTrue(r["available"])
        self.assertEqual(r["engine"], "Docker Desktop")
        self.assertTrue(r["verbose_df"])
        self.assertTrue(r["container_sizes"])
        # VM disk: allocated (st_blocks*512) vs apparent size of the sparse file
        self.assertEqual(r["vm_disk"]["path"], str(vm))
        self.assertEqual(r["vm_disk"]["apparent"], 64 * 1024 ** 3)
        self.assertLess(r["vm_disk"]["allocated"], 64 * 1024 ** 2)
        self.assertEqual(r["vm_disk"]["allocated"], os.stat(vm).st_blocks * 512)

        imgs = {i["ref"]: i for i in r["images"]}
        self.assertTrue(imgs["jahia/jahia-ee:8.2.1.0"]["in_use"])
        self.assertEqual(imgs["jahia/jahia-ee:8.2.1.0"]["used_by"], ["jahia-jahia-1"])
        self.assertFalse(imgs["jahia/jahia-ee:8.2.1.0"]["old_tag"])          # newest
        self.assertTrue(imgs["jahia/jahia-ee:8.2.0.0"]["old_tag"])            # older, unused
        self.assertFalse(imgs["jahia/jahia-ee:8.1.6.0"]["old_tag"])           # older but used by a stopped container
        self.assertTrue(imgs["jahia/jahia-ee:8.1.6.0"]["in_use"])
        self.assertTrue(imgs["postgres:15"]["old_tag"])
        self.assertFalse(imgs["postgres:16"]["old_tag"])
        self.assertFalse(imgs["jahia/jahia-discovery:8.2.1.0"]["old_tag"])    # only tag of its repo
        self.assertFalse(imgs["jahia/jahia-discovery:8.2.1.0"]["in_use"])
        self.assertTrue(imgs["mariadb:10.11"]["in_use"])
        self.assertEqual(imgs["jahia/jahia-ee:8.2.0.0"]["unique_size"], 380000000)
        self.assertEqual(imgs["jahia/jahia-ee:8.2.0.0"]["size"], 1790000000)
        dangling = [i for i in r["images"] if i["dangling"]]
        self.assertEqual({i["short_id"] for i in dangling}, {"909090909090", "919191919191"})
        inter = [i for i in r["images"] if i["intermediate"]]
        self.assertEqual([i["short_id"] for i in inter], ["777777777777"])
        self.assertEqual(r["totals"]["images"]["count"], 11)
        self.assertEqual(r["totals"]["images"]["dangling_count"], 2)
        self.assertEqual(r["totals"]["images"]["old_tag_count"], 2)
        self.assertEqual(r["totals"]["images"]["reclaimable"], 7300000000)

        conts = {c["name"]: c for c in r["containers"]}
        self.assertEqual(conts["jahia-jahia-1"]["state"], "running")
        self.assertEqual(conts["jahia-jahia-1"]["project"], "jahia")
        self.assertEqual(conts["jahia-jahia-1"]["size"], 48200000)
        self.assertEqual(conts["jahia-jahia-1"]["virtual"], 1800000000)
        self.assertEqual(conts["old-jahia"]["project"], "")
        self.assertEqual(r["totals"]["containers"]["running"], 3)
        self.assertEqual(r["totals"]["containers"]["stopped"], 2)

        vols = {v["name"]: v for v in r["volumes"]}
        self.assertFalse(vols["jahia_jahia-data"]["unused"])
        self.assertEqual(vols["jahia_jahia-data"]["used_by"], ["jahia-jahia-1"])
        self.assertEqual(vols["jahia_jahia-data"]["project"], "jahia")
        self.assertFalse(vols["jahia_mariadb-data"]["unused"])               # stopped container still references it
        self.assertTrue(vols["oldproj_db-data"]["unused"])
        self.assertFalse(vols["oldproj_db-data"]["anonymous"])
        self.assertTrue(vols["c" * 64]["unused"])
        self.assertTrue(vols["c" * 64]["anonymous"])
        self.assertEqual(vols["oldproj_db-data"]["size"], 2100000000)
        self.assertEqual(r["totals"]["volumes"]["unused_size"], 2400000000)
        self.assertEqual(r["totals"]["volumes"]["anonymous_unused_size"], 300000000)

        self.assertEqual(r["totals"]["build_cache"]["size"], 1365000000)
        self.assertEqual(r["totals"]["build_cache"]["reclaimable"], 1100000000)
        self.assertEqual(len(r["build_cache"]), 3)
        self.assertEqual(r["reclaimable"], 7300000000 + 312000000 + 1100000000)

        stored = db.kv_get("docker")
        self.assertTrue(stored["available"])
        self.assertEqual(len(stored["images"]), len(r["images"]))

    def test_scan_old_docker_without_verbose_format(self):
        os.environ["FAKE_DOCKER_OLD"] = "1"
        r = self.scan()
        self.assertTrue(r["available"])
        self.assertFalse(r["verbose_df"])
        imgs = {i["ref"]: i for i in r["images"]}
        self.assertTrue(imgs["jahia/jahia-ee:8.2.0.0"]["old_tag"])
        self.assertIsNone(imgs["jahia/jahia-ee:8.2.0.0"]["unique_size"])
        vols = {v["name"]: v for v in r["volumes"]}
        self.assertTrue(vols["oldproj_db-data"]["unused"])         # no links info: falls back to container mounts
        self.assertFalse(vols["jahia_es-data"]["unused"])
        self.assertIsNone(vols["oldproj_db-data"]["size"])
        self.assertEqual(r["totals"]["volumes"]["unused_size"], 2400000000)  # from system df summary
        self.assertEqual(r["totals"]["build_cache"]["size"], 1365000000)

    def test_scan_without_container_sizes(self):
        os.environ["FAKE_DOCKER_SLOW_SIZE"] = "1"
        r = self.scan()
        self.assertFalse(r["container_sizes"])
        self.assertTrue(all(c["size"] is None for c in r["containers"]))
        self.assertEqual(len(r["containers"]), 5)

    def test_daemon_down(self):
        self.make_vm_disk()
        self.set_state(down=True)
        r = self.scan()
        self.assertFalse(r["available"])
        self.assertIn("isn't running", r["reason"])
        self.assertEqual(r["engine"], "Docker Desktop")
        self.assertEqual(r["vm_disk"]["apparent"], 64 * 1024 ** 3)
        self.assertFalse(db.kv_get("docker")["available"])
        self.assertEqual(dk.alerts({}), [])

    def test_cli_missing(self):
        os.environ["PATH"] = str(self.tmp / "empty")
        r = self.scan()
        self.assertFalse(r["available"])
        self.assertFalse(r["cli"])

    def test_engines(self):
        self.make_vm_disk("Library/Group Containers/HUAQ24HBR6.dev.orbstack/data/data.img", apparent=8 * 1024 ** 3)
        self.make_vm_disk(".colima/_lima/_disks/colima/datadisk", apparent=100 * 1024 ** 3)
        disks = dk.vm_disks()
        self.assertEqual(sorted(d["engine"] for d in disks), ["Colima", "OrbStack"])
        self.assertEqual(dk._engine({"OperatingSystem": "OrbStack", "Name": "orbstack"}, disks), "OrbStack")
        self.assertEqual(dk._engine({"Name": "colima", "OperatingSystem": "Ubuntu 24.04"}, disks), "Colima")

    def test_alerts(self):
        self.scan()
        a = dk.alerts({"docker_alert_gb": 5})
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["section"], "docker")
        self.assertEqual(dk.alerts({"docker_alert_gb": 50}), [])
        self.assertEqual(len(dk.alerts({})), 0)  # 8.7 GB reclaimable < 10 GB default


class ActionTests(DockerTestBase):
    def setUp(self):
        super().setUp()
        dk.scan(log=self.log)

    def removed(self):
        return json.loads(self.state.read_text())["removed"]

    def test_rm_image_refuses_in_use(self):
        with self.assertRaises(RuntimeError) as cm:
            dk.rm_image("jahia/jahia-ee:8.2.1.0", log=self.log)
        self.assertIn("jahia-jahia-1", str(cm.exception))
        with self.assertRaises(RuntimeError):  # used by a *stopped* container: still refused
            dk.rm_image("jahia/jahia-ee:8.1.6.0", log=self.log)
        self.assertFalse(any(c[:2] == ["image", "rm"] for c in self.calls()))

    def test_rm_image_old_tag(self):
        r = dk.rm_image("jahia/jahia-ee:8.2.0.0", log=self.log)
        self.assertTrue(r["ok"])
        self.assertEqual(r["freed_kb"], int(380000000 / 1024))
        self.assertIn(["image", "rm", "jahia/jahia-ee:8.2.0.0"], self.calls())
        refs = {i["ref"] for i in db.kv_get("docker")["images"]}  # panel refreshed
        self.assertNotIn("jahia/jahia-ee:8.2.0.0", refs)
        row = db.query("SELECT * FROM actions WHERE kind='docker' ORDER BY rowid DESC LIMIT 1")[0]
        self.assertEqual(row["target"], "image jahia/jahia-ee:8.2.0.0")
        self.assertEqual(row["ok"], 1)

    def test_rm_image_rejects_options(self):
        with self.assertRaises(ValueError):
            dk.rm_image("--force", log=self.log)

    def test_rm_container(self):
        running = next(c for c in db.kv_get("docker")["containers"] if c["name"] == "jahia-jahia-1")
        with self.assertRaises(RuntimeError) as cm:
            dk.rm_container(running["id"], log=self.log)
        self.assertIn("running", str(cm.exception))
        self.assertFalse(any(c[:1] == ["rm"] for c in self.calls()))
        old = next(c for c in db.kv_get("docker")["containers"] if c["name"] == "old-jahia")
        r = dk.rm_container(old["id"], log=self.log)
        self.assertEqual(r["freed_kb"], int(312000000 / 1024))
        self.assertIn(["rm", old["id"]], self.calls())
        # The old image is no longer in use now -> removable, and it becomes an old tag.
        img = next(i for i in db.kv_get("docker")["images"] if i["ref"] == "jahia/jahia-ee:8.1.6.0")
        self.assertFalse(img["in_use"])
        self.assertTrue(img["old_tag"])

    def test_rm_volume(self):
        with self.assertRaises(RuntimeError) as cm:
            dk.rm_volume("jahia_mariadb-data", log=self.log)
        self.assertIn("jahia-mariadb-1", str(cm.exception))
        with self.assertRaises(RuntimeError):
            dk.rm_volume("nope", log=self.log)
        r = dk.rm_volume("oldproj_db-data", log=self.log)
        self.assertEqual(r["freed_kb"], int(2100000000 / 1024))
        self.assertIn("oldproj_db-data", self.removed())

    def test_prunes(self):
        r = dk.prune_dangling(log=self.log)
        self.assertEqual(r["freed_kb"], int(350000000 / 1024))
        self.assertIn(["image", "prune", "-f"], self.calls())
        self.assertEqual(db.kv_get("docker")["totals"]["images"]["dangling_count"], 0)
        dk.prune_stopped(log=self.log)
        self.assertIn(["container", "prune", "-f"], self.calls())
        self.assertEqual(db.kv_get("docker")["totals"]["containers"]["stopped"], 0)
        r = dk.prune_volumes(log=self.log)
        self.assertEqual(r["freed_kb"], int(300000000 / 1024))
        self.assertNotIn("oldproj_db-data", self.removed())   # named volume kept without --all
        r = dk.prune_volumes_all(log=self.log)
        self.assertIn(["volume", "prune", "-f", "--all"], self.calls())
        self.assertIn("oldproj_db-data", self.removed())
        r = dk.prune_build_cache(log=self.log)
        self.assertEqual(r["freed_kb"], int(1100000000 / 1024))
        r = dk.prune_unused_images(log=self.log)
        self.assertIn(["image", "prune", "-a", "-f"], self.calls())
        rows = db.query("SELECT target, freed_kb FROM actions WHERE kind='docker'")
        self.assertIn("prune build cache", [x["target"] for x in rows])

    def test_prune_volumes_all_old_docker(self):
        os.environ["FAKE_DOCKER_OLD"] = "1"
        dk.prune_volumes_all(log=self.log)
        self.assertIn(["volume", "prune", "-f"], self.calls())

    def test_actions_need_running_daemon(self):
        self.set_state(down=True)
        with self.assertRaises(RuntimeError):
            dk.prune_dangling(log=self.log)

    def test_action_specs(self):
        for name, spec in dk.ACTIONS.items():
            self.assertTrue(name.startswith("docker-"))
            self.assertEqual(spec["lane"], "heavy")
            label = spec["label"]("x") if callable(spec["label"]) else spec["label"]
            self.assertTrue(label)
        self.assertTrue(dk.ACTIONS["docker-rm-image"]["target"])
        self.assertFalse(dk.ACTIONS["docker-prune-build-cache"]["target"])


if __name__ == "__main__":
    unittest.main()
