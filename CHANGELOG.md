# Changelog

## Unreleased

### Fixed

- **Every Git clone was listed and measured twice.** `gitrepos.roots()` and the toolchain pin
  scanner de-duplicated their roots with `os.path.realpath()`, which resolves symlinks but not
  letter case. On macOS's default case-insensitive volume `~/projects` and `~/Projects` are one
  folder and `os.path.isdir()` is true for both, so both spellings survived: 204 entries for 102
  clones, each walked and `du`-ed twice, and a pinned toolchain version reported "Pinned by 2
  projects" when one project pinned it. Root lists are now de-duplicated by `(st_dev, st_ino)`
  (`util.dedupe_dirs` / `scan_roots`), which is what actually identifies a directory. The daily
  scan does half the Git work it used to.
- **A protected folder could be emptied if its path was spelled in another case.** The cache
  cleaner compared `realpath()` strings against its protected list, so an `extra_caches` entry
  naming `~/documents` did not match `~/Documents` and was treated as safe to empty. Containment
  and protection are now inode comparisons (`util.is_under`, `util.same_dir`).

### Security

- Dashboard scripts run under a per-response CSP nonce; `script-src` no longer allows
  `'unsafe-inline'`, and `base-uri`, `form-action` and `object-src` are locked down.
- Every response carries `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `Cross-Origin-Opener-Policy`, `Cross-Origin-Resource-Policy` and a `Permissions-Policy`.
- Writes are refused when a browser reports `Sec-Fetch-Site` other than `same-origin`.
  Non-browser clients (curl) are unaffected: the token check is what protects them.
- The token is reissued if its file is ever readable by anyone but the owner.
- `/api/reveal` checks containment by inode instead of by string prefix.
- The launcher validates the program it is told to run, scrubs `DYLD_*` / `LD_PRELOAD` out of
  the child's environment, and starts it with default signal dispositions and an empty mask.
  (This protects the child, not the launcher's own process: dyld acts before `main()` and only
  ignores `DYLD_*` for Developer ID signatures, which an ad-hoc signature is not.)
- The launcher no longer loses a SIGTERM that arrives while it is starting Python. It used to
  reach a handler that did not yet know the child's pid, after which the launcher sat in
  `waitpid()` on a child that was never asked to stop, so `launchctl stop` and logout hung until
  launchd escalated to SIGKILL.

  **Upgrading rebuilds `~/Applications/DiskHealth.app`, so its Full Disk Access grant is lost:
  remove it from the list with "-" and add it again.** `install.sh` prints a reminder.

### Performance

- HTTP keep-alive (HTTP/1.1). The dashboard polls `/api/progress` every second and the extension
  polls `/api/status`, so each poll no longer costs a TCP handshake.
- SQLite runs in WAL mode: the dashboard keeps reading while a scan writes its snapshot.

### Tests

- The suite runs in one process again: `python3 -m unittest discover -s tests -t .` (145 tests).
  Each module gets its own `$HOME` and data folder through `tests/_env.py` instead of depending
  on which module imported `diskhealth` first.
- New `tests/test_launcher.py` (11 tests) compiles `launcher.c` and drives it: argument checking,
  exit-status pass-through, environment scrubbing, signal forwarding, and the startup signal race
  — that one builds a deliberately unfixed control binary and asserts it fails, so the test
  cannot pass on broken code.

## 1.2.0 — 2026-11-12

- Homebrew install: the repository is its own tap (`Formula/disk-health.rb`); `make-release.sh --tag`
  points the formula at each new tag. `brew upgrade disk-health` updates, and the service restarts itself
  when the installed version changes.
- New `disk-health` command: setup, status, open, restart, stop, logs, extension, uninstall, version.
- Update alerts for Homebrew installs (compared with the formula version).
- `restart.sh` / `install.sh` status lines work with macOS's Python 3.9.

## 1.1.0 — 2026-11-04

- mise support in the Homebrew panel (now "Homebrew, mise & oh-my-zsh"): installed versions and sizes,
  outdated tools across your projects, `mise upgrade` (within requested ranges), `mise prune` of versions no
  project uses, `mise doctor`, self-update for non-Homebrew installs.
- Toolchains: mise-installed runtimes are listed; `mise.toml` / `.config/mise.toml` pins protect versions.
- Caches: mise cache entry (`mise cache clear`). Spotlight: `~/.local/share/mise` suggested for exclusion.

## 1.0.0 — 2026-10-16

First shareable release.

- Dashboard (localhost:7777) with accordion panels: growth, space map, caches, apps, trash, Homebrew,
  Git, Docker, toolchains, storage (Time Machine snapshots, iPhone backups, simulators, Mail/Messages,
  Spotlight), duplicates, activity, logs.
- Chrome extension: free-space badge, notifications, live progress.
- Jobs with live progress, logs and Cancel; heavy and light lanes.
- `brewed` routine, smarter Maven/Gradle cleanup, Spotlight exclusion suggestions.
- Packaging: release zip with prebuilt universal launcher, install from zip, daily update check.
