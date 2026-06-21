"""The ingest core: incoming/<source>/ -> originals, safely and idempotently.

Per media file (video or photo, any source):
  1. read the capture time and compute the deterministic destination
     originals/YYYY/MM/<YYYYMMDD-HHMMSS>_<source>_<origname>
  2. if that destination already holds this exact file (same size + bytes)
     -> duplicate; drop the source
  3. otherwise copy it in, re-hash the destination to verify the copy, then
     remove the source

There is NO database. Dedupe is answered entirely by the filesystem: the
same input file always maps to the same destination name, so a re-inserted
card lands on paths that already exist and is skipped. A content compare
happens only on the rare name collision, between just the two files.

Nothing is ever deleted from `incoming` unless a verified archived copy
exists. Safe to run repeatedly (e.g. from cron); a lockfile in the CLI
prevents overlapping runs.
"""

from __future__ import annotations

import contextlib
import filecmp
import hashlib
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Config, Source
from .probe import capture_datetime

log = logging.getLogger("media_ingestor")

_CHUNK = 1024 * 1024  # 1 MiB
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class IngestStats:
    imported: int = 0
    duplicates: int = 0
    skipped: int = 0
    errors: int = 0

    def __str__(self) -> str:
        return (f"imported={self.imported} duplicates={self.duplicates} "
                f"skipped={self.skipped} errors={self.errors}")


def hash_file(path: Path, algorithm: str) -> str:
    h = hashlib.new(algorithm)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _media_files(root: Path, cfg: Config, settle_seconds: float) -> list[Path]:
    """Media files anywhere under root — recurses into pasted subfolders (e.g.
    a camera's DCIM/100CANON tree) — skipping any modified within the last
    `settle_seconds` (still being copied in). Symlinks are not followed, so a
    looping link can't trap the walk."""
    now = time.time()
    out: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for fn in filenames:
            p = Path(dirpath) / fn
            if cfg.media_type(p.suffix) is None:
                continue
            try:
                if settle_seconds > 0 and now - p.stat().st_mtime < settle_seconds:
                    log.debug("not settled yet, skipping: %s", p)
                    continue
            except OSError:
                continue
            out.append(p)
    return sorted(out)


def _prune_empty_dirs(cfg: Config) -> None:
    """Remove subfolders that are empty after their media was consumed (e.g. a
    pasted DCIM tree). Walks bottom-up; never removes the watched source roots,
    and leaves any folder that still holds unrecognised/leftover files."""
    for source in cfg.sources.values():
        root = cfg.incoming / source.name
        if not root.exists():
            continue
        for dirpath, _dirnames, _filenames in os.walk(root, topdown=False, followlinks=False):
            d = Path(dirpath)
            if d == root:
                continue
            # rmdir succeeds only if empty; a folder with leftover files is kept.
            with contextlib.suppress(OSError):
                d.rmdir()


def _sidecars_for(media: Path, cfg: Config) -> list[Path]:
    """Sidecar files sharing the media stem (e.g. DSC_0042.THM)."""
    return [
        p for p in media.parent.iterdir()
        if p.is_file()
        and p.stem == media.stem
        and p.suffix.lower() in cfg.sidecar_exts
    ]


def _archived_name(dt: datetime, source: str, original: str) -> str:
    """<YYYYMMDD-HHMMSS>_<source>_<original>, sanitised. The full timestamp
    prefix makes the file self-describing and sortable even if moved; the
    source keeps provenance; the original name is preserved so nothing is
    truly lost."""
    return f"{dt:%Y%m%d-%H%M%S}_{_SAFE.sub('-', source)}_{_SAFE.sub('_', original)}"


def _dest_path(originals: Path, dt: datetime, name: str) -> Path:
    # Month-granularity folders (YYYY/MM): friendlier to browse and matches
    # Immich's default storage template. Day grouping for publishing is a
    # filesystem query (see archive.py), independent of this layout.
    return originals / f"{dt:%Y}" / f"{dt:%m}" / name


def _existing_variants(dest: Path) -> list[Path]:
    """The destination and any prior collision variants (dest, dest__1, ...)
    that currently exist."""
    out = []
    if dest.exists():
        out.append(dest)
    i = 1
    while True:
        cand = dest.with_name(f"{dest.stem}__{i}{dest.suffix}")
        if not cand.exists():
            break
        out.append(cand)
        i += 1
    return out


def _free_slot(dest: Path) -> Path:
    """First non-existent path among dest, dest__1, dest__2, ..."""
    if not dest.exists():
        return dest
    i = 1
    while True:
        cand = dest.with_name(f"{dest.stem}__{i}{dest.suffix}")
        if not cand.exists():
            return cand
        i += 1


def _same_file(a: Path, b: Path) -> bool:
    """Same size and identical bytes (shallow=False does a real compare)."""
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
    except OSError:
        return False
    return filecmp.cmp(a, b, shallow=False)


_STAGING_DIRNAME = ".staging"


def _staging_dir(cfg: Config) -> Path:
    """A staging folder on the same filesystem as the archive, so the final
    move is an atomic rename."""
    return cfg.originals / _STAGING_DIRNAME


def sweep_staging(cfg: Config) -> None:
    """Delete orphaned staging files from a crash mid-copy. They are always
    junk: a completed copy is renamed *out* of staging, so anything left here
    predates this run. Cheap — one shallow directory, never the whole archive."""
    staging = _staging_dir(cfg)
    if not staging.exists():
        return
    for p in staging.iterdir():
        try:
            if p.is_file():
                p.unlink()
        except OSError:
            log.debug("could not remove stale staging file %s", p)


def _copy_verify(src: Path, dest: Path, algorithm: str, staging: Path) -> None:
    """Copy src->dest through staging, verify the bytes, then atomically rename
    into place. A crash leaves at most a junk staging file (swept next run) —
    never a half-written file in the archive, and the source is only deleted
    afterwards, so footage is never lost to an interrupted copy."""
    staging.mkdir(parents=True, exist_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = staging / (dest.name + ".part")
    src_hash = hash_file(src, algorithm)
    shutil.copy2(src, tmp)
    if hash_file(tmp, algorithm) != src_hash:
        tmp.unlink(missing_ok=True)
        raise OSError(f"hash mismatch after copy: {src} -> {dest}")
    os.replace(tmp, dest)


def _atomic_copy(src: Path, dest: Path, staging: Path) -> None:
    """Like _copy_verify but for sidecars (no hash check): copy then rename."""
    staging.mkdir(parents=True, exist_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = staging / (dest.name + ".part")
    shutil.copy2(src, tmp)
    os.replace(tmp, dest)


def _delete_source(media: Path, cfg: Config) -> None:
    media.unlink(missing_ok=True)
    for sc in _sidecars_for(media, cfg):
        sc.unlink(missing_ok=True)


def ingest_one(media: Path, source: Source, cfg: Config, *, dry_run: bool) -> str:
    """Process a single media file. Returns: imported|duplicate."""
    media_type = cfg.media_type(media.suffix)  # guaranteed non-None by caller
    dt = capture_datetime(media, media_type, cfg.date_source, cfg.use_filename_date)
    name = _archived_name(dt, source.name, media.name)
    dest = _dest_path(cfg.originals, dt, name)

    # Dedupe by the filesystem: does this exact file already sit at its
    # destination (or a prior collision variant of it)?
    for existing in _existing_variants(dest):
        if _same_file(media, existing):
            log.info("duplicate: %s/%s (already archived)", source.name, media.name)
            if cfg.delete_source_after_verify and not dry_run:
                _delete_source(media, cfg)
            return "duplicate"

    target = _free_slot(dest)
    if dry_run:
        log.info("would import: %s/%s -> %s", source.name, media.name, target)
        return "imported"

    staging = _staging_dir(cfg)
    _copy_verify(media, target, cfg.hash_algorithm, staging)
    for sc in _sidecars_for(media, cfg):
        _atomic_copy(sc, _free_slot(target.with_suffix(sc.suffix)), staging)
    log.info("imported: %s/%s -> %s", source.name, media.name, target)

    if cfg.delete_source_after_verify:
        _delete_source(media, cfg)
    return "imported"


_RESULT_FIELD = {"imported": "imported", "duplicate": "duplicates", "skipped": "skipped"}


def find_candidates(cfg: Config, settle_seconds: float) -> list[tuple[Source, Path, int]]:
    """(source, path, size) for settle-passing media files across all sources.
    The size lets the watcher confirm a file has stopped growing before it's
    ingested (robust against copiers that preserve the source mtime)."""
    items: list[tuple[Source, Path, int]] = []
    for source in cfg.sources.values():
        root = cfg.incoming / source.name
        if not root.exists():
            log.debug("source dir absent, skipping: %s", root)
            continue
        for p in _media_files(root, cfg, settle_seconds):
            try:
                items.append((source, p, p.stat().st_size))
            except OSError:
                continue
    return items


def ingest_paths(cfg: Config, items: list[tuple[Source, Path]], *, dry_run: bool = False) -> IngestStats:
    """Ingest a specific list of (source, path) pairs."""
    stats = IngestStats()
    if not dry_run:
        sweep_staging(cfg)  # clear any crash leftovers before writing
    for source, media in items:
        try:
            result = ingest_one(media, source, cfg, dry_run=dry_run)
        except Exception:  # noqa: BLE001 - one bad file must not abort the run
            log.exception("error ingesting %s", media)
            stats.errors += 1
            continue
        field = _RESULT_FIELD.get(result, "skipped")
        setattr(stats, field, getattr(stats, field) + 1)
    if not dry_run and cfg.prune_empty_dirs:
        _prune_empty_dirs(cfg)
    return stats


def run(cfg: Config, *, dry_run: bool = False, settle_seconds: float = 0.0) -> IngestStats:
    """One-shot ingest of everything currently settled in incoming/."""
    items = [(source, path) for source, path, _size in find_candidates(cfg, settle_seconds)]
    if items:
        log.info("found %d media file(s) to ingest", len(items))
    return ingest_paths(cfg, items, dry_run=dry_run)
