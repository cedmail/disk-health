# Changelog

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
