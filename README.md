# media-ingestor

Frictionless family-media pipeline for **photos and videos of any format**. One
physical action — insert the camera's SD card into a Raspberry Pi — and everything
else is hands-off: media lands in a dated archive on the NAS, deduplicated and
verified, and camera videos are published to YouTube as unlisted per-day videos
for the family to watch.

## Architecture

```
[Camera SD card]
      │  ① insert into the Raspberry Pi       ← your only physical action
      ▼
[ Raspberry Pi 3/4 ]  thin offloader: udev trigger → copy card → NAS share incoming/cam/
      ▼
[ TrueNAS SCALE ]  (Docker — all compute lives here)
      ├─ INGEST  (continuous): incoming/<source>/ → media/originals/, dedupe + verify
      └─ PUBLISH (daily):      camera videos of each past day → one lossless concat
                               → YouTube unlisted → playlist
      ▼
[ TrueNAS SCALE ]  = archive + off-site backup   (Immich planned as the album/search layer)
```

**Why this shape.** The NAS is tucked away, so the card goes into a cheap always-on
Pi whose only job is to copy the card to the NAS share. All real work runs on the
NAS (which has Docker), so nothing depends on a PC being awake.

**Generic ingest, scoped publish.** The ingest layer is source- and media-agnostic:
every *source* is just a subfolder under `incoming/` (the camera now; phones later),
and both photos and videos of any format are handled and tagged. Only sources marked
`publish = true` (the camera) are touched by the YouTube publish stage — so adding
phones later for *archival* is config-only, with no risk to publishing.

## Storage layout

Purely **date-driven**: no events or devices in the path (device is preserved in the
filename; events will live as albums in Immich later).

```
incoming/
  canon/  osmo-pocket-4/  phone-*/      ← one folder per device (config-driven)
originals/
  2026/06/20260621-141203_canon_IMG_0042.jpg   ← YYYY/MM/<YYYYMMDD-HHMMSS>_<source>_<name>
derived/
  2026/2026-06-21.mp4                          ← camera day video uploaded to YouTube
```

**No database.** The files are the only source of truth. Date, source, and original
name are parsed back out of the filename; "is this day published?" is just "does the
derived day video exist?". So there is no index to back up, rebuild, or accidentally
delete — your backup is simply the `media/` folder. Month-granularity folders match
Immich's default storage template; the full-timestamp filename keeps every file
self-describing and sortable even if moved.

## Status

- [x] **Ingest core**: generic source/media ingest, capture-date organise, dedupe,
      copy-verify-then-delete, crash-safe, discard proxies. Tested.
- [x] **Deploy**: GitHub Actions → GHCR image → TrueNAS SCALE Custom App + Watchtower
      auto-update. **Live and ingesting on the NAS.**
- [ ] **Publish**: per-day lossless concat (camera only) + YouTube unlisted upload + playlist.
- [ ] **Pi offloader**: udev rule → copy card → `incoming/<device>/` + "safe to remove" ping.
- [x] **Off-site backup** — handled by the existing nightly NAS backup (the archive
      lives in the already-backed-up photos dataset).
- [ ] *(later)* Phone ingestion via Immich album → `incoming/phone-*` (archive only).

## What the ingest core does

For every media file (photo or video, any source) under `incoming/<source>/`:

1. Read the **capture time** (metadata-first — see below) and compute the
   deterministic destination `originals/YYYY/MM/<YYYYMMDD-HHMMSS>_<source>_<name>`.
2. If that destination already holds this exact file (same size + identical bytes)
   → duplicate; the source is dropped. Because the same input always maps to the
   same destination, a re-inserted card lands on paths that already exist and is
   skipped — no stored hashes required. A content compare happens only on the rare
   name collision, between just the two files.
3. Otherwise **copy** it in, **re-hash the destination to verify** the copy landed
   intact (a local check, nothing stored), then remove the source.

Nothing leaves `incoming` without a verified archived copy.

**Pasted subfolders** (e.g. dragging in a whole camera card, `DCIM/100CANON/…`) are
handled: ingest recurses to any depth, archives the media by date, then removes the
emptied subfolders — but **never deletes files it doesn't recognise**, so a folder
holding an unknown file is left intact (and so is the watched source root). Symlinks
are not followed, so a looping link can't trap the scan.

**Disposable proxies** are dropped, not archived: any extension in
`discard_extensions` (default `.lrf`, DJI's low-res proxy video) is deleted from
`incoming/` during the same pass. Genuine sidecars (`.thm`, `.srt`, …) are still
carried alongside their media.

## Capture-date detection

File timestamps are unreliable (a backup/restore rewrites them), so the date is
resolved metadata-first, in this order of trust:

1. **Embedded capture metadata** via **exiftool** when installed — it reads every
   photo/video format and checks many date tags (`DateTimeOriginal`, QuickTime
   `CreationDate`, `CreateDate`, …) in priority order. Falls back to ffprobe (video)
   / Pillow EXIF (photo) if exiftool isn't present.
2. **A date in the filename** (`IMG_20230615_143022`, `PXL_…`, `Screenshot 2023-06-15`,
   WhatsApp `IMG-20230615-WA0001`) — for restored files whose timestamps are wrong but
   whose names survived. Disable with `use_filename_date = false`.
3. **File mtime** — last resort only.

The Docker image bundles exiftool. For local testing on Windows: `scoop install exiftool`
(or `pip install -e ".[photo]"` for photo EXIF via Pillow). `date_source = "mtime"`
forces mtime-only.

## Resilience (crashes & slow copies)

**Power loss / crash mid-run — never loses footage.** Each file is copied to a
`.staging/` folder, hash-verified, then **atomically renamed** into place; the
source in `incoming/` is deleted only *after* that. So an interruption leaves at
most a junk staging file (never a half-written archive file, never a deleted
source without a complete copy), and the next run **sweeps `.staging/`** and
re-imports anything unfinished. Safe to kill (`docker stop` sends SIGTERM, which
the watcher handles) or power off at any moment.

**Slow / large copies into `incoming/` — never ingests a partial file.** The
watcher ingests a file only once **its size is unchanged between two scans** (and
it has passed the `--settle` window). A file still being copied keeps growing, so
it simply waits until it's done — even if the copier preserved the source's old
mtime and slipped past the settle check. The Pi offloader will also write
atomically (temp name → rename) so partials never appear with a media extension.

**Crashed run leaving a stale lock — self-heals.** The one-shot `ingest` uses a
lock so overlapping cron runs can't race; if a run crashes, a lock older than an
hour is treated as stale and broken automatically, so ingestion never wedges. The
lock lives in the system temp dir, never in the archive.

**Disk full / NAS offline / card yanked mid-read** — the affected file errors out
and is left in `incoming/`; the source is never deleted, and it's retried on the
next run once the problem clears.

## Run it

```sh
pip install -e ".[dev,photo]"        # editable install + dev/photo extras
cp config.example.toml config.toml   # edit paths + sources

media-ingest ingest -c config.toml --dry-run   # preview, touches nothing
media-ingest ingest -c config.toml             # one-shot
media-ingest watch  -c config.toml             # poll incoming/ and ingest continuously
media-ingest status -c config.toml             # summarise the archive
media-ingest probe  path/to/photo.jpg          # show how a file's date is resolved
```

`probe` is the tool to reach for when a file lands on the wrong date: it prints
whether exiftool/ffprobe/Pillow are available, the embedded time tags exiftool sees,
and which tier (metadata → filename → mtime) won.

Without installing, the equivalent is `python -m media_ingestor <command> ...`.
Ingest needs only the Python standard library (3.11+ for `tomllib`); `ffmpeg`
(ffprobe) is recommended for accurate video dates; Pillow (the `[photo]` extra)
enables photo EXIF dates.

## Develop

The fast inner loop is fully local — no Docker needed:

```sh
media-ingest watch -c config.toml     # drop files into incoming/cam/ and watch them land
pytest                                # safety net
ruff check .                          # lint
```

Docker is only the deploy artifact (dev/prod parity); see [Dockerfile](Dockerfile),
[docker-compose.yml](docker-compose.yml), and [DEPLOY.md](DEPLOY.md) for the full
setup. Deploy flow: `git push` → GitHub Actions builds and pushes
`ghcr.io/christianprando/media-ingestor` → TrueNAS pulls (Watchtower auto-updates).

## Tests

```sh
pytest
```

## One-time manual prerequisites (for later stages)

- **YouTube Data API**: create a Google Cloud project, enable *YouTube Data API v3*,
  create an OAuth *desktop* client, authorise once to mint a refresh token. An upload
  costs 1600 of 10,000 daily quota units → ~6/day, which is why we publish one
  concatenated video per day.
- **Off-site backup**: configure an `rclone` remote (e.g. Backblaze B2). RAID and
  YouTube are *not* backups.
```
