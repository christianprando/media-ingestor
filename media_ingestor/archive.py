"""Reading facts back out of the archive — no database required.

Every fact the old ledger held is recoverable from the filesystem:
  - date / source / original name  -> parsed from the archived filename
  - "is this day published?"        -> does the derived day video exist?

So `status` and (later) the publish stage derive everything by scanning,
and there is no state file to protect, rebuild, or accidentally delete.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import Config

# Archived names look like: 20260621-141203_cam_DSC_0042.mov
# (optionally with a __N collision suffix on the stem). Source has no '_';
# the original name — which may contain '_' — is everything after it.
_NAME_RE = re.compile(r"^(?P<ymd>\d{8})-(?P<hms>\d{6})_(?P<source>[^_]+)_(?P<original>.+)$")


@dataclass(frozen=True)
class ArchivedFile:
    path: Path
    capture_date: date
    source: str
    media_type: str  # 'video' | 'photo'


def parse_archived_name(name: str) -> tuple[date, str, str] | None:
    """(capture_date, source, original) from an archived filename, or None."""
    m = _NAME_RE.match(name)
    if not m:
        return None
    ymd = m.group("ymd")
    try:
        d = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
    except ValueError:
        return None
    return d, m.group("source"), m.group("original")


def iter_archive(cfg: Config):
    """Yield every recognised media file in the archive as an ArchivedFile."""
    for p in sorted(cfg.originals.rglob("*")):
        if not p.is_file():
            continue
        media_type = cfg.media_type(p.suffix)
        if media_type is None:
            continue
        parsed = parse_archived_name(p.name)
        if parsed is None:
            continue
        d, source, _ = parsed
        yield ArchivedFile(path=p, capture_date=d, source=source, media_type=media_type)


def derived_video_path(cfg: Config, day: date) -> Path:
    return cfg.derived / f"{day:%Y}" / f"{day:%Y-%m-%d}.mp4"


def is_published(cfg: Config, day: date) -> bool:
    """A day is published iff its derived video exists — the artifact is the
    record, so nothing extra needs storing."""
    return derived_video_path(cfg, day).exists()


def pending_publish_dates(cfg: Config, today: date) -> list[date]:
    """Past days that have publishable (camera) video but no derived video yet."""
    pub = set(cfg.publishable_sources())
    days: set[date] = set()
    for f in iter_archive(cfg):
        if f.media_type == "video" and f.source in pub and f.capture_date < today:
            days.add(f.capture_date)
    return sorted(d for d in days if not is_published(cfg, d))


def summarize(cfg: Config, today: date) -> dict:
    """Counts by source/media_type plus pending publish days, all by scan."""
    by_kind: Counter = Counter()
    for f in iter_archive(cfg):
        by_kind[(f.source, f.media_type)] += 1
    return {
        "total": sum(by_kind.values()),
        "by_kind": dict(sorted(by_kind.items())),
        "pending": pending_publish_dates(cfg, today),
    }
