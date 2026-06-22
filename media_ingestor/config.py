"""Configuration loading.

The ingest layer is deliberately generic: every *source* (the camera now,
phones later) is just a subfolder under `incoming/` plus a `publish` flag.
Only sources with publish=true are considered by the (camera-only) YouTube
publish stage.

There is no database. The archive of files is the only source of truth:
dedupe is answered by the filesystem, and metadata lives in the filenames.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Source names appear as a `_`-delimited token in every archived filename and
# are parsed back out, so they must not contain '_' or other unsafe chars.
_VALID_SOURCE = re.compile(r"[A-Za-z0-9.-]+")

# Recognised media. Videos and photos are both ingested; sidecars are carried
# along next to their media file.
DEFAULT_VIDEO_EXTS = [".mov", ".mp4", ".mts", ".m4v", ".avi", ".m2ts"]
DEFAULT_PHOTO_EXTS = [".jpg", ".jpeg", ".heic", ".heif", ".png", ".dng",
                      ".cr2", ".cr3", ".nef", ".arw", ".raf", ".rw2"]
DEFAULT_SIDECAR_EXTS = [".thm", ".xml", ".srt", ".gpx", ".aae"]
# Disposable companion files deleted from incoming instead of archived.
# .lrf = DJI low-resolution proxy video (redundant once you keep the MP4).
DEFAULT_DISCARD_EXTS = [".lrf"]


@dataclass(frozen=True)
class Source:
    name: str          # also the subfolder name under incoming/
    publish: bool      # eligible for YouTube publishing (camera only, for now)


@dataclass
class Config:
    # Root holding one subfolder per source: incoming/<source>/...
    incoming: Path
    # Pristine, date-organised originals: <originals>/YYYY/MM/...
    originals: Path
    # Derived day videos uploaded to YouTube: <derived>/YYYY/YYYY-MM-DD.mp4
    derived: Path

    sources: dict[str, Source]

    video_exts: list[str] = field(default_factory=lambda: list(DEFAULT_VIDEO_EXTS))
    photo_exts: list[str] = field(default_factory=lambda: list(DEFAULT_PHOTO_EXTS))
    sidecar_exts: list[str] = field(default_factory=lambda: list(DEFAULT_SIDECAR_EXTS))
    # Files deleted from incoming rather than archived (disposable proxies/junk).
    discard_exts: list[str] = field(default_factory=lambda: list(DEFAULT_DISCARD_EXTS))

    # Used only to verify a copy landed intact; never persisted.
    hash_algorithm: str = "blake2b"
    date_source: str = "metadata"      # "metadata" | "mtime"
    # When metadata yields no date, parse one from the filename before
    # falling back to mtime (helps backup-restored files).
    use_filename_date: bool = True
    delete_source_after_verify: bool = True
    # After consuming media, remove emptied subfolders left in incoming/
    # (e.g. a pasted DCIM tree). Never touches the watched source roots.
    prune_empty_dirs: bool = True

    def __post_init__(self) -> None:
        self.incoming = Path(self.incoming)
        self.originals = Path(self.originals)
        self.derived = Path(self.derived)
        self.video_exts = [e.lower() for e in self.video_exts]
        self.photo_exts = [e.lower() for e in self.photo_exts]
        self.sidecar_exts = [e.lower() for e in self.sidecar_exts]
        self.discard_exts = [e.lower() for e in self.discard_exts]
        if self.date_source not in ("metadata", "mtime"):
            raise ValueError(f"date_source must be 'metadata' or 'mtime', got {self.date_source!r}")
        if not self.sources:
            raise ValueError("config must define at least one [sources.<name>]")
        for name in self.sources:
            if not _VALID_SOURCE.fullmatch(name):
                raise ValueError(
                    f"source name {name!r} may contain only letters, digits, '.' and '-' "
                    "(no '_' or spaces) — it becomes a token in archived filenames"
                )

    def media_type(self, suffix: str) -> str | None:
        """'video', 'photo', or None for non-media files."""
        s = suffix.lower()
        if s in self.video_exts:
            return "video"
        if s in self.photo_exts:
            return "photo"
        return None

    def publishable_sources(self) -> list[str]:
        return [s.name for s in self.sources.values() if s.publish]


def load_config(path: str | Path) -> Config:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    paths = raw.get("paths", {})
    for required in ("incoming", "originals", "derived"):
        if required not in paths:
            raise ValueError(f"config [paths] must set '{required}'")

    sources_raw = raw.get("sources", {})
    sources = {
        name: Source(name=name, publish=bool(spec.get("publish", False)))
        for name, spec in sources_raw.items()
    }

    ingest = raw.get("ingest", {})
    organize = raw.get("organize", {})

    return Config(
        incoming=Path(paths["incoming"]),
        originals=Path(paths["originals"]),
        derived=Path(paths["derived"]),
        sources=sources,
        video_exts=ingest.get("video_extensions", list(DEFAULT_VIDEO_EXTS)),
        photo_exts=ingest.get("photo_extensions", list(DEFAULT_PHOTO_EXTS)),
        sidecar_exts=ingest.get("sidecar_extensions", list(DEFAULT_SIDECAR_EXTS)),
        discard_exts=ingest.get("discard_extensions", list(DEFAULT_DISCARD_EXTS)),
        hash_algorithm=ingest.get("hash_algorithm", "blake2b"),
        delete_source_after_verify=ingest.get("delete_source_after_verify", True),
        prune_empty_dirs=ingest.get("prune_empty_dirs", True),
        date_source=organize.get("date_source", "metadata"),
        use_filename_date=organize.get("use_filename_date", True),
    )
