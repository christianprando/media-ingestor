"""End-to-end tests for the stateless ingest core, using temp dirs and fake
media. There is no database — dedupe and publish-state are derived from the
filesystem, so tests just inspect files.

ffprobe/EXIF aren't available on the dev box, so capture-time detection is
exercised via the mtime fallback (date_source="mtime"); the metadata path
is covered by monkeypatching ingest.capture_datetime.
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime
from pathlib import Path

from media_ingestor import archive, cli, ingest
from media_ingestor.config import Config, Source


def make_config(tmp_path: Path, *, sources=None, **overrides) -> Config:
    if sources is None:
        sources = {"cam": Source("cam", publish=True)}
    defaults = dict(
        incoming=tmp_path / "incoming",
        originals=tmp_path / "media" / "originals",
        derived=tmp_path / "media" / "derived",
        sources=sources,
        date_source="mtime",
    )
    defaults.update(overrides)
    cfg = Config(**defaults)
    for name in sources:
        (cfg.incoming / name).mkdir(parents=True, exist_ok=True)
    return cfg


def write_media(path: Path, content: bytes, *, when: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    ts = datetime.fromisoformat(when).timestamp()
    os.utime(path, (ts, ts))
    return path


def archived(cfg: Config):
    return sorted(p for p in cfg.originals.rglob("*") if p.is_file())


def test_imports_into_dated_folder_with_prefix(tmp_path):
    cfg = make_config(tmp_path)
    write_media(cfg.incoming / "cam" / "DSC_0001.mov", b"hello", when="2026-06-20T14:12:03")

    stats = ingest.run(cfg, dry_run=False)

    assert stats.imported == 1
    dest = cfg.originals / "2026" / "06" / "20260620-141203_cam_DSC_0001.mov"
    assert dest.exists()
    assert dest.read_bytes() == b"hello"
    assert not (cfg.incoming / "cam" / "DSC_0001.mov").exists()  # source removed


def test_reimport_same_file_is_deduped(tmp_path):
    cfg = make_config(tmp_path)
    write_media(cfg.incoming / "cam" / "DSC_0001.mov", b"same-bytes", when="2026-06-20T10:00:00")
    ingest.run(cfg, dry_run=False)

    # Same card re-inserted: identical file (name, content, capture time).
    write_media(cfg.incoming / "cam" / "DSC_0001.mov", b"same-bytes", when="2026-06-20T10:00:00")
    stats = ingest.run(cfg, dry_run=False)

    assert stats.duplicates == 1 and stats.imported == 0
    assert len(archived(cfg)) == 1


def test_name_and_time_collision_different_content_keeps_both(tmp_path):
    cfg = make_config(tmp_path)
    # Same source + capture second + original name, but different bytes ->
    # genuinely different file, must not be lost.
    write_media(cfg.incoming / "cam" / "DSC_0001.mov", b"first", when="2026-06-20T08:00:00")
    ingest.run(cfg, dry_run=False)
    write_media(cfg.incoming / "cam" / "DSC_0001.mov", b"second-x", when="2026-06-20T08:00:00")
    ingest.run(cfg, dry_run=False)

    month = cfg.originals / "2026" / "06"
    assert len(list(month.glob("*.mov"))) == 2


def test_dry_run_changes_nothing(tmp_path):
    cfg = make_config(tmp_path)
    write_media(cfg.incoming / "cam" / "DSC_0001.mov", b"data", when="2026-06-20T09:00:00")

    stats = ingest.run(cfg, dry_run=True)

    assert stats.imported == 1
    assert (cfg.incoming / "cam" / "DSC_0001.mov").exists()
    assert not archived(cfg)


def test_sidecar_follows_media_and_is_renamed(tmp_path):
    cfg = make_config(tmp_path)
    write_media(cfg.incoming / "cam" / "DSC_0001.mov", b"video", when="2026-06-20T07:30:00")
    write_media(cfg.incoming / "cam" / "DSC_0001.thm", b"thumb", when="2026-06-20T07:30:00")

    ingest.run(cfg, dry_run=False)

    month = cfg.originals / "2026" / "06"
    assert (month / "20260620-073000_cam_DSC_0001.mov").exists()
    assert (month / "20260620-073000_cam_DSC_0001.thm").exists()  # sidecar renamed to match


def test_photo_is_ingested(tmp_path):
    cfg = make_config(tmp_path)
    write_media(cfg.incoming / "cam" / "IMG_5.jpg", b"jpegdata", when="2026-06-20T12:00:00")

    ingest.run(cfg, dry_run=False)

    assert (cfg.originals / "2026" / "06" / "20260620-120000_cam_IMG_5.jpg").exists()


def test_metadata_time_used_when_available(tmp_path, monkeypatch):
    cfg = make_config(tmp_path, date_source="metadata")
    # mtime says the 25th; "metadata" says the 20th at 10:00:00
    write_media(cfg.incoming / "cam" / "DSC_0001.mov", b"v", when="2026-06-25T18:00:00")
    monkeypatch.setattr(ingest, "capture_datetime",
                        lambda p, mt, ds, uf: datetime(2026, 6, 20, 10, 0, 0))

    ingest.run(cfg, dry_run=False)

    assert (cfg.originals / "2026" / "06" / "20260620-100000_cam_DSC_0001.mov").exists()


# --- archive.py: facts derived from the filesystem, no database -------------

def test_parse_archived_name_roundtrip():
    d, source, original = archive.parse_archived_name("20260621-141203_cam_DSC_0042.mov")
    assert d == date(2026, 6, 21)
    assert source == "cam"
    assert original == "DSC_0042.mov"


def test_pending_publish_excludes_nonpublishable_and_future(tmp_path):
    sources = {"cam": Source("cam", publish=True),
               "phone-dad": Source("phone-dad", publish=False)}
    cfg = make_config(tmp_path, sources=sources)
    write_media(cfg.incoming / "cam" / "C.mov", b"camvid", when="2026-06-20T11:00:00")
    write_media(cfg.incoming / "phone-dad" / "P.mov", b"phonevid", when="2026-06-20T11:30:00")
    ingest.run(cfg, dry_run=False)

    today = date(2026, 6, 21)
    # only the publishable camera day is pending
    assert archive.pending_publish_dates(cfg, today) == [date(2026, 6, 20)]

    # a future-dated day (>= today) is not yet eligible
    assert archive.pending_publish_dates(cfg, date(2026, 6, 20)) == []


# --- source naming ---------------------------------------------------------

def test_multiple_device_sources_tag_files_distinctly(tmp_path):
    sources = {"cam-sony": Source("cam-sony", publish=True),
               "cam-canon": Source("cam-canon", publish=True)}
    cfg = make_config(tmp_path, sources=sources)
    write_media(cfg.incoming / "cam-sony" / "A.mov", b"s", when="2026-06-20T10:00:00")
    write_media(cfg.incoming / "cam-canon" / "B.mov", b"c", when="2026-06-20T11:00:00")

    ingest.run(cfg, dry_run=False)

    month = cfg.originals / "2026" / "06"
    assert (month / "20260620-100000_cam-sony_A.mov").exists()
    assert (month / "20260620-110000_cam-canon_B.mov").exists()
    # both parse back to their device, both publishable
    assert archive.pending_publish_dates(cfg, date(2026, 6, 21)) == [date(2026, 6, 20)]


def test_source_name_with_underscore_is_rejected(tmp_path):
    import pytest
    with pytest.raises(ValueError, match="source name"):
        make_config(tmp_path, sources={"cam_sony": Source("cam_sony", publish=True)})


# --- pasted subdirectories -------------------------------------------------

def test_media_in_nested_subdirs_is_ingested_and_pruned(tmp_path):
    cfg = make_config(tmp_path)
    # e.g. a whole camera card folder pasted in
    nested = cfg.incoming / "cam" / "DCIM" / "100CANON"
    write_media(nested / "DSC_0009.mov", b"nested", when="2026-06-20T09:00:00")

    stats = ingest.run(cfg, dry_run=False)

    assert stats.imported == 1
    assert (cfg.originals / "2026" / "06" / "20260620-090000_cam_DSC_0009.mov").exists()
    # the emptied pasted tree is removed, but the watched source root stays
    assert not (cfg.incoming / "cam" / "DCIM").exists()
    assert (cfg.incoming / "cam").exists()


def test_folder_with_leftover_nonmedia_is_kept(tmp_path):
    cfg = make_config(tmp_path)
    sub = cfg.incoming / "cam" / "trip"
    write_media(sub / "clip.mov", b"v", when="2026-06-20T09:00:00")
    (sub / "notes.txt").write_text("unrecognised, must not be deleted")

    ingest.run(cfg, dry_run=False)

    assert not (sub / "clip.mov").exists()      # media consumed
    assert (sub / "notes.txt").exists()         # unknown file left untouched
    assert sub.exists()                         # so its folder is kept


# --- resilience: crash leftovers and stale locks ---------------------------

def test_orphaned_staging_file_is_swept(tmp_path):
    cfg = make_config(tmp_path)
    staging = cfg.originals / ".staging"
    staging.mkdir(parents=True)
    junk = staging / "interrupted.mov.part"
    junk.write_bytes(b"half a file from a crash")

    ingest.run(cfg, dry_run=False)  # incoming empty, but a run still sweeps

    assert not junk.exists()


def test_import_recovers_from_staging_leftovers(tmp_path):
    cfg = make_config(tmp_path)
    staging = cfg.originals / ".staging"
    staging.mkdir(parents=True)
    (staging / "junk.part").write_bytes(b"leftover")
    write_media(cfg.incoming / "cam" / "DSC_0001.mov", b"hello", when="2026-06-20T10:00:00")

    stats = ingest.run(cfg, dry_run=False)

    assert stats.imported == 1
    assert (cfg.originals / "2026" / "06" / "20260620-100000_cam_DSC_0001.mov").exists()
    assert list(staging.iterdir()) == []  # staging left clean


def test_stale_lock_is_broken_but_live_lock_is_respected(tmp_path):
    lock = tmp_path / "ingest.lock"

    fd = cli._create_lock(lock)
    try:
        # A fresh (live) lock must block a second acquirer.
        assert cli._acquire_lock(lock) is None
    finally:
        os.close(fd)

    # Age the lock past the stale threshold -> next acquire breaks and retakes.
    old = time.time() - (cli._STALE_LOCK_SECONDS + 10)
    os.utime(lock, (old, old))
    fd2 = cli._acquire_lock(lock)
    assert fd2 is not None
    os.close(fd2)
    lock.unlink(missing_ok=True)


def test_published_day_is_derived_from_derived_video(tmp_path):
    cfg = make_config(tmp_path)
    write_media(cfg.incoming / "cam" / "C.mov", b"camvid", when="2026-06-20T11:00:00")
    ingest.run(cfg, dry_run=False)
    today = date(2026, 6, 21)
    assert archive.pending_publish_dates(cfg, today) == [date(2026, 6, 20)]

    # Once the derived video exists, the day counts as published.
    dv = archive.derived_video_path(cfg, date(2026, 6, 20))
    dv.parent.mkdir(parents=True, exist_ok=True)
    dv.write_bytes(b"concatenated")
    assert archive.pending_publish_dates(cfg, today) == []
