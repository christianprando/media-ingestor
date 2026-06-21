"""Unit tests for capture-date detection.

exiftool/ffprobe/Pillow aren't on the dev box, so the embedded-metadata
extractors are covered by monkeypatching; the pure parsers (EXIF-string
and filename) and the priority/fallback ordering are tested directly.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from media_ingestor import probe

# --- EXIF/exiftool datetime string parsing ---------------------------------

def test_parse_exif_naive_is_local():
    assert probe._parse_exif_dt("2023:06:15 14:30:22") == datetime(2023, 6, 15, 14, 30, 22)


def test_parse_exif_with_subseconds():
    assert probe._parse_exif_dt("2023:06:15 14:30:22.123") == datetime(2023, 6, 15, 14, 30, 22)


def test_parse_exif_zero_date_rejected():
    assert probe._parse_exif_dt("0000:00:00 00:00:00") is None


def test_parse_exif_garbage_rejected():
    assert probe._parse_exif_dt("not a date") is None


def test_parse_exif_timezone_converted_to_local():
    # An aware value must come back as the equivalent local wall-clock time.
    aware = datetime(2023, 6, 15, 14, 30, 22, tzinfo=timezone(timedelta(hours=2)))
    expected = aware.astimezone().replace(tzinfo=None)
    assert probe._parse_exif_dt("2023:06:15 14:30:22+02:00") == expected


# --- filename date parsing -------------------------------------------------

def test_filename_compact_datetime():
    assert probe._filename_datetime("IMG_20230615_143022.jpg") == datetime(2023, 6, 15, 14, 30, 22)


def test_filename_pixel_with_subseconds():
    assert probe._filename_datetime("PXL_20230615_143022123.mp4") == datetime(2023, 6, 15, 14, 30, 22)


def test_filename_whatsapp_date_only_defaults_to_noon():
    assert probe._filename_datetime("IMG-20230615-WA0001.jpg") == datetime(2023, 6, 15, 12, 0, 0)


def test_filename_separated_screenshot():
    assert probe._filename_datetime("Screenshot_2023-06-15-14-30-22.png") == datetime(2023, 6, 15, 14, 30, 22)


def test_filename_no_date_returns_none():
    assert probe._filename_datetime("DSC_0042.jpg") is None


def test_filename_implausible_date_rejected():
    # 1920x1080 -> month 10 day 80 is invalid, must not be taken as a date.
    assert probe._filename_datetime("clip_1920x1080.mov") is None


# --- priority / fallback ordering ------------------------------------------

def _touch(tmp_path: Path, name: str, *, mtime: str) -> Path:
    p = tmp_path / name
    p.write_bytes(b"x")
    ts = datetime.fromisoformat(mtime).timestamp()
    os.utime(p, (ts, ts))
    return p


def test_metadata_wins_over_filename_and_mtime(tmp_path, monkeypatch):
    p = _touch(tmp_path, "IMG_20230615_143022.jpg", mtime="2025-01-01T00:00:00")
    monkeypatch.setattr(probe, "_metadata_datetime", lambda path, mt: datetime(2020, 5, 5, 9, 0, 0))
    assert probe.capture_datetime(p, "photo") == datetime(2020, 5, 5, 9, 0, 0)


def test_filename_used_when_metadata_missing(tmp_path, monkeypatch):
    p = _touch(tmp_path, "IMG_20230615_143022.jpg", mtime="2025-01-01T00:00:00")
    monkeypatch.setattr(probe, "_metadata_datetime", lambda path, mt: None)
    assert probe.capture_datetime(p, "photo") == datetime(2023, 6, 15, 14, 30, 22)


def test_mtime_is_last_resort(tmp_path, monkeypatch):
    p = _touch(tmp_path, "no_date_here.jpg", mtime="2025-01-01T08:30:00")
    monkeypatch.setattr(probe, "_metadata_datetime", lambda path, mt: None)
    assert probe.capture_datetime(p, "photo") == datetime(2025, 1, 1, 8, 30, 0)


def test_filename_disabled_falls_straight_to_mtime(tmp_path, monkeypatch):
    p = _touch(tmp_path, "IMG_20230615_143022.jpg", mtime="2025-01-01T08:30:00")
    monkeypatch.setattr(probe, "_metadata_datetime", lambda path, mt: None)
    got = probe.capture_datetime(p, "photo", use_filename_date=False)
    assert got == datetime(2025, 1, 1, 8, 30, 0)
