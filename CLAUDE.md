# CLAUDE.md

Guidance for working in this repo. See [README.md](README.md) for the full
user-facing docs and [DEPLOY.md](DEPLOY.md) for deployment.

## What this is

`media-ingestor` — a frictionless family-media pipeline. It watches an
`incoming/<device>/` staging area and files photos & videos of any format into a
date-organised archive on a TrueNAS SCALE NAS, deduplicated and verified, with no
database. Camera videos will later be published to YouTube as per-day unlisted
videos (publish stage not built yet).

**Status:** ingest core is built, tested, and **live on the NAS**. Off-site backup
is already handled by the user's existing nightly NAS backup (archive lives in the
backed-up photos dataset). Still to come: YouTube publish, Raspberry Pi
card-offloader, Immich.

## Architecture

```
SD card → incoming/<device>/ → [ingest: organise + dedupe + verify] → originals/YYYY/MM/
                                                                     → derived/  (publish, later)
```
- One **source per device** = one subfolder under `incoming/` + a `publish` flag,
  defined in config. Only `publish=true` sources reach the YouTube stage.
- Runs as a Docker container on TrueNAS SCALE in `watch` mode (polling, not inotify
  — inotify is unreliable on network/SMB/bind-mounted storage).

## Non-negotiable design decisions (don't undo without discussing)

- **No database.** The filesystem is the only source of truth. Dedupe = does the
  deterministic destination already hold this exact file (size + byte compare on
  rare name collisions). Date/source/type are parsed from the **filename**;
  publish-state = does the derived day video exist. Nothing to back up but the
  files; nothing to rebuild or accidentally delete. (`archive.py` derives facts.)
- **Date-driven, flat `YYYY/MM`.** No events or devices in the path — device is a
  token in the filename (`<YYYYMMDD-HHMMSS>_<source>_<original>`). Events/albums are
  Immich's job later. Folder granularity is decoupled from publish day-grouping.
- **Capture date is metadata-first** (`probe.py`): exiftool → filename date → mtime.
  File timestamps lie after a backup/restore, so mtime is the last resort only.
- **Crash-safe writes** (`ingest.py`): copy into `originals/.staging/`, hash-verify,
  atomic `os.replace` into place, delete source LAST. `sweep_staging()` clears
  crash leftovers each run. Never lose footage to an interrupted copy/power-off.
- **`shutil.copyfile`, never `copy2`.** `copy2` chmods the destination, which the
  TrueNAS ZFS dataset (restricted NFSv4 ACLs) denies even for root (EPERM). Copy
  bytes only.
- **Source names**: `[A-Za-z0-9.-]+` only, no `_` (it's the filename field
  delimiter) — validated in `config.py`.

## Layout

- `media_ingestor/config.py` — TOML config, `Source`, extension sets, validation.
- `media_ingestor/probe.py` — capture-date resolution (exiftool/ffprobe/Pillow/
  filename/mtime); `diagnose()` powers the `probe` CLI command.
- `media_ingestor/ingest.py` — the core: find candidates, dedupe, copy-verify,
  discard disposables, prune empty dirs.
- `media_ingestor/archive.py` — read facts back from the filesystem (parse names,
  pending-publish days, status) — replaces what a DB would do.
- `media_ingestor/cli.py` — `ingest` / `watch` / `status` / `probe` subcommands.
- `tests/` — pytest; no real exiftool/ffmpeg on dev boxes, so those are mocked.

## Commands

```sh
pip install -e ".[dev,photo]"
pytest                       # must stay green
ruff check media_ingestor tests   # must stay clean
python -m media_ingestor watch -c local/config.toml --interval 5 --settle 2
```
`local/` is a gitignored sandbox for trying things; `config.toml` is gitignored too.

## Deploy

`git push` to `main` → GitHub Actions builds & pushes
`ghcr.io/christianprando/media-ingestor` → Watchtower on the NAS auto-pulls within
~5 min. That's the whole deploy. Roll back by pinning a previous `sha-…` tag.

## Conventions

- Keep `pytest` green and `ruff` clean before committing; add a test for each
  behaviour change (the suite mirrors real failure modes hit in production).
- Commit messages end with the `Co-Authored-By` trailer.
- Be careful with anything destructive on the archive — it's irreplaceable family
  media. Source files in `incoming/` are only deleted after a verified copy.
