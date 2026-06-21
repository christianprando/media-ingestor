"""Capture-date detection — metadata first, file time only as a last resort.

File timestamps lie (a backup/restore rewrites them), so the embedded
capture date must win. Order of trust, highest → lowest:

  1. Embedded capture metadata, read with **exiftool** when available — it
     covers photos and videos of every format and checks many possible date
     tags in a sensible priority. Falls back to ffprobe (video) / Pillow
     EXIF (photo) when exiftool isn't installed.
  2. A date encoded in the **filename** (e.g. IMG_20230615_143022, PXL_…,
     "Screenshot 2023-06-15") — invaluable for backup-restored files whose
     timestamps are wrong but whose names survived. Can be disabled.
  3. File **mtime** — last resort only.

Returns a naive *local* datetime; the archive's date folder and the
filename timestamp prefix both derive from it.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("media_ingestor")

# exiftool date tags in priority order (most authoritative capture time first).
# We ask for all of them in one call and take the first that parses.
_EXIFTOOL_TAGS = [
    "SubSecDateTimeOriginal",   # EXIF, with sub-seconds
    "DateTimeOriginal",         # EXIF — when the shutter fired (gold standard)
    "CreationDate",             # QuickTime/Apple — local time with offset
    "SubSecCreateDate",
    "CreateDate",               # EXIF DateTimeDigitized / QuickTime CreateDate
    "DateCreated",              # XMP/IPTC
    "MediaCreateDate",
    "TrackCreateDate",
    "DateTimeDigitized",
    "GPSDateTime",              # UTC, but better than a wrong file time
]

# Matches YYYY[?]MM[?]DD with an optional time, year constrained to 19xx/20xx.
_FILENAME_DATE_RE = re.compile(
    r"(?<!\d)(?P<y>19\d{2}|20\d{2})(?P<sep>[-_.:]?)(?P<mo>\d{2})(?P=sep)(?P<d>\d{2})"
    r"(?:[ T_.-]?(?P<h>\d{2})(?P<tsep>[-_.:]?)(?P<mi>\d{2})(?P=tsep)(?P<s>\d{2}))?"
)

# exiftool/EXIF datetime, e.g. "2023:06:15 14:30:22", optionally ".sss" and tz.
_EXIF_DT_RE = re.compile(
    r"(?P<y>\d{4})[:-](?P<mo>\d{2})[:-](?P<d>\d{2})[ T]"
    r"(?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})(?:\.\d+)?"
    r"\s*(?P<tz>Z|[+-]\d{2}:?\d{2})?"
)


def _tz(offset: str) -> timezone:
    if offset in ("Z", "z"):
        return UTC
    body = offset.replace(":", "")
    sign = 1 if body[0] == "+" else -1
    return timezone(sign * timedelta(hours=int(body[1:3]), minutes=int(body[3:5])))


def _parse_exif_dt(text: str) -> datetime | None:
    """Parse an EXIF/exiftool datetime to a naive *local* datetime, or None.
    Timezone-aware values are converted to local; naive values are assumed
    local. Zero/invalid dates (e.g. '0000:00:00 00:00:00') return None."""
    m = _EXIF_DT_RE.search(text)
    if not m:
        return None
    y, mo, d = int(m.group("y")), int(m.group("mo")), int(m.group("d"))
    if 0 in (y, mo, d):
        return None
    try:
        dt = datetime(y, mo, d, int(m.group("h")), int(m.group("mi")), int(m.group("s")))
    except ValueError:
        return None
    if m.group("tz"):
        dt = dt.replace(tzinfo=_tz(m.group("tz"))).astimezone().replace(tzinfo=None)
    return dt


def _exiftool_datetime(path: Path) -> datetime | None:
    """Best embedded capture date via exiftool, or None if exiftool is absent
    or no usable date tag is present."""
    exe = shutil.which("exiftool")
    if not exe:
        return None
    cmd = [exe, "-json", "-api", "QuickTimeUTC=1", *[f"-{t}" for t in _EXIFTOOL_TAGS], str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None
    if not data:
        return None
    tags = data[0]
    for tag in _EXIFTOOL_TAGS:
        raw = tags.get(tag)
        if raw:
            dt = _parse_exif_dt(str(raw))
            if dt is not None:
                log.debug("exiftool: %s=%r -> %s", tag, raw, dt)
                return dt
    log.debug("exiftool: no usable date tag in %s", path.name)
    return None


def _ffprobe_datetime(path: Path) -> datetime | None:
    """Fallback for video when exiftool is absent: ffprobe creation_time."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_entries", "format_tags=creation_time:stream_tags=creation_time", str(path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None
    candidates: list[str] = []
    fmt_tags = (data.get("format") or {}).get("tags") or {}
    if fmt_tags.get("creation_time"):
        candidates.append(fmt_tags["creation_time"])
    for stream in data.get("streams") or []:
        ct = (stream.get("tags") or {}).get("creation_time")
        if ct:
            candidates.append(ct)
    for raw in candidates:
        dt = _parse_exif_dt(raw.replace("-", ":", 2))  # ISO date -> EXIF-ish for the shared parser
        if dt is not None:
            return dt
    return None


def _pillow_exif_datetime(path: Path) -> datetime | None:
    """Fallback for photos when exiftool is absent: Pillow EXIF DateTimeOriginal."""
    try:
        from PIL import ExifTags, Image  # optional dependency
    except ImportError:
        return None
    tag_id = next((k for k, v in ExifTags.TAGS.items() if v == "DateTimeOriginal"), None)
    if tag_id is None:
        return None
    try:
        with Image.open(path) as img:
            raw = img.getexif().get(tag_id)
    except Exception:  # noqa: BLE001 - unreadable/odd image must not crash ingest
        return None
    return _parse_exif_dt(str(raw)) if raw else None


def _metadata_datetime(path: Path, media_type: str) -> datetime | None:
    """Embedded capture date: exiftool first, then per-type fallbacks."""
    dt = _exiftool_datetime(path)
    if dt is not None:
        return dt
    if media_type == "video":
        return _ffprobe_datetime(path)
    if media_type == "photo":
        return _pillow_exif_datetime(path)
    return None


def _filename_datetime(name: str) -> datetime | None:
    """A plausible capture date parsed from the filename, or None."""
    m = _FILENAME_DATE_RE.search(name)
    if not m:
        return None
    try:
        if m.group("h") is not None:
            return datetime(int(m.group("y")), int(m.group("mo")), int(m.group("d")),
                            int(m.group("h")), int(m.group("mi")), int(m.group("s")))
        # No time in the name: noon avoids any day-boundary ambiguity.
        return datetime(int(m.group("y")), int(m.group("mo")), int(m.group("d")), 12, 0, 0)
    except ValueError:
        return None


def capture_datetime(path: Path, media_type: str, date_source: str = "metadata",
                     use_filename_date: bool = True) -> datetime:
    """Best-effort local capture datetime, metadata first, mtime last."""
    if date_source == "metadata":
        dt = _metadata_datetime(path, media_type)
        if dt is not None:
            log.debug("%s: date from embedded metadata -> %s", path.name, dt)
            return dt
        if use_filename_date:
            dt = _filename_datetime(path.name)
            if dt is not None:
                log.debug("%s: date from filename -> %s", path.name, dt)
                return dt
    dt = datetime.fromtimestamp(path.stat().st_mtime)
    log.debug("%s: date from file mtime (LAST RESORT) -> %s", path.name, dt)
    return dt


def _pillow_available() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


def available_tools() -> dict[str, bool]:
    """Which metadata-reading tools are present on this machine."""
    return {
        "exiftool": shutil.which("exiftool") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
        "pillow": _pillow_available(),
    }


def diagnose(path: Path, media_type: str) -> dict[str, object]:
    """Per-tier results for one file — powers the `probe` command so you can
    see exactly why a given date was chosen."""
    exe = shutil.which("exiftool")
    raw = ""
    if exe:
        try:
            out = subprocess.run([exe, "-s", "-G1", "-time:all", str(path)],
                                 capture_output=True, text=True, timeout=30, check=False)
            raw = out.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            raw = "(exiftool call failed)"
    return {
        "exiftool_path": exe,
        "exiftool_time_tags": raw,
        "exiftool_dt": _exiftool_datetime(path),
        "ffprobe_dt": _ffprobe_datetime(path) if media_type == "video" else None,
        "pillow_dt": _pillow_exif_datetime(path) if media_type == "photo" else None,
        "filename_dt": _filename_datetime(path.name),
        "mtime_dt": datetime.fromtimestamp(path.stat().st_mtime),
        "final": capture_datetime(path, media_type),
    }
