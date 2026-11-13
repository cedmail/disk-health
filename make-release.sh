#!/usr/bin/env bash
# Build a release zip you can copy to another Mac or share.
#
#   ./make-release.sh            build dist/disk-health-<version>.zip (+ the Chrome extension zip)
#   ./make-release.sh --tag      same, then tag v<version> and point the Homebrew formula
#                                (Formula/disk-health.rb) at it, in a commit of its own
#
# The repository is its own Homebrew tap. Its URL comes from `git remote get-url origin`
# (override with DH_REPO_URL=...).
#
# The zip contains the code, a prebuilt universal launcher (Apple silicon + Intel, so the other Mac
# needs no Xcode tools to build it) and a RELEASE marker that tells install.sh to copy the code to
# ~/Library/Application Support/disk-health/app (so the zip can be deleted afterwards).
# Bump the version first: edit VERSION and add a line to CHANGELOG.md.
set -euo pipefail

TAG=0
for arg in "$@"; do
  case "$arg" in
    --tag) TAG=1 ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$ROOT"
VERSION="$(tr -d ' \n' < VERSION)"
[ -n "$VERSION" ] || die "VERSION is empty"
NAME="disk-health-$VERSION"
DIST="$ROOT/dist"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STAGE="$WORK/$NAME"

if [ -d .git ] && command -v git >/dev/null 2>&1; then
  if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    echo "Warning: uncommitted changes; they are included in the zip."
  fi
fi

echo "==> Copying files ($NAME)"
mkdir -p "$STAGE"
# Everything except local/build stuff.
tar -C "$ROOT" \
  --exclude ./.git --exclude ./dist --exclude './Claude outputs' --exclude '*/__pycache__' \
  --exclude '*.pyc' --exclude .DS_Store --exclude '*.log' --exclude '*.sqlite3*' --exclude ./config.json \
  --exclude ./.venv --exclude ./venv --exclude ./Formula --exclude ./launcher/DiskHealth --exclude ./launcher/DiskHealth.sha256 \
  -cf - . | tar -C "$STAGE" -xf -
printf '%s\n' "$VERSION" > "$STAGE/RELEASE"

echo "==> Launcher"
if [ "$(uname -s)" = "Darwin" ] && command -v cc >/dev/null 2>&1; then
  cc -O2 -Wall -arch arm64 -arch x86_64 -mmacosx-version-min=11.0 \
     -o "$STAGE/launcher/DiskHealth" launcher/launcher.c
  shasum -a 256 launcher/launcher.c | cut -d' ' -f1 > "$STAGE/launcher/DiskHealth.sha256"
  echo "Built universal launcher ($(lipo -archs "$STAGE/launcher/DiskHealth" 2>/dev/null || echo ?))"
else
  echo "Not on a Mac with a C compiler: no prebuilt launcher (install.sh will compile it on the target Mac)."
fi

# Keep the extension version in step with the app.
python3 - "$STAGE/extension/manifest.json" "$VERSION" <<'PY'
import json, sys
p, v = sys.argv[1], sys.argv[2]
m = json.load(open(p))
m["version"] = ".".join(x for x in v.split("-")[0].split(".") if x.isdigit()) or "1.0.0"
open(p, "w").write(json.dumps(m, indent=2) + "\n")
PY

chmod 755 "$STAGE"/*.sh

echo "==> Zipping"
mkdir -p "$DIST"
rm -f "$DIST/$NAME.zip" "$DIST/disk-health-extension-$VERSION.zip"
( cd "$WORK" && zip -qry "$DIST/$NAME.zip" "$NAME" )
( cd "$STAGE/extension" && zip -qr "$DIST/disk-health-extension-$VERSION.zip" . )
( cd "$DIST" && shasum -a 256 "$NAME.zip" > "$NAME.zip.sha256" 2>/dev/null || sha256sum "$NAME.zip" > "$NAME.zip.sha256" )

if [ "$TAG" -eq 1 ]; then
  [ -d .git ] || die "--tag: not a git checkout"
  [ -z "$(git status --porcelain)" ] || die "--tag: commit your changes first (the tag must contain them)"
  REPO_URL="${DH_REPO_URL:-$(git remote get-url origin 2>/dev/null || true)}"
  [ -n "$REPO_URL" ] || die "--tag: no 'origin' remote; add one (git remote add origin <url>) or set DH_REPO_URL"
  if git rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null; then die "tag v$VERSION already exists: bump VERSION"; fi
  git tag -a "v$VERSION" -m "Disk Health $VERSION"
  REV="$(git rev-parse "v$VERSION^{commit}")"
  BRANCH="$(git rev-parse --abbrev-ref HEAD)"
  echo "==> Tagged v$VERSION ($REV)"

  # Point the formula at the new tag (https homepage, owner for the tap name).
  OWNER="$(python3 - Formula/disk-health.rb "$REPO_URL" "$VERSION" "$REV" "$BRANCH" <<'PY'
import re, sys
p, url, ver, rev, branch = sys.argv[1:]
m = (re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+?)(?:\.git)?/?$", url)
     or re.match(r"^https?://(?:[^@/]+@)?([^/]+)/(.+?)(?:\.git)?/?$", url))
host, path = (m.group(1), m.group(2)) if m else ("github.com", "OWNER/disk-health")
s = open(p).read()
s = re.sub(r'^  homepage ".*"$', f'  homepage "https://{host}/{path}"', s, flags=re.M)
s = re.sub(r'^  url ".*",$', f'  url "{url}",', s, flags=re.M)
s = re.sub(r'tag:(\s+)"[^"]*"', lambda x: f'tag:{x.group(1)}"v{ver}"', s)
s = re.sub(r'revision: "[0-9a-f]*"', f'revision: "{rev}"', s)
s = re.sub(r'^  head ".*"$', f'  head "{url}", branch: "{branch}"', s, flags=re.M)
open(p, "w").write(s)
print(path.split("/")[0].lower())
PY
)"
  git add Formula/disk-health.rb
  git commit -q -m "Formula: disk-health $VERSION" && echo "==> Formula updated and committed"
  cat <<EOF

Publish:
  git push origin $BRANCH v$VERSION

Colleagues install with:
  brew tap $OWNER/disk-health $REPO_URL
  brew install disk-health
  disk-health setup --open
and update with:  brew upgrade disk-health
EOF
fi

cat <<EOF

Done:
  $DIST/$NAME.zip
  $DIST/disk-health-extension-$VERSION.zip
  $DIST/$NAME.zip.sha256

On the other Mac:
  unzip $NAME.zip && cd $NAME && ./install.sh --open
EOF
