"""Command-line entry point: ``media-ingest <command>``.

Commands
--------
  ingest   one-shot scan of incoming/ (good for cron or a quick manual run)
  watch    poll incoming/ forever and ingest as files appear (dev + prod)
  status   summarise the archive (counts + days pending publish)

Polling (not inotify) is deliberate: inotify is unreliable on the network
/ SMB / bind-mounted storage this runs against, while a periodic rescan
works everywhere and is trivial to reason about.

There is no database. The only runtime file is an ephemeral lock kept in
the system temp dir (never in the archive), so nothing here is precious.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import logging
import os
import signal
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from . import archive, ingest, probe
from .config import (
    DEFAULT_PHOTO_EXTS,
    DEFAULT_VIDEO_EXTS,
    Config,
    load_config,
)

log = logging.getLogger("media_ingestor")


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("-c", "--config", required=True, help="path to config.toml")
    p.add_argument("--dry-run", action="store_true", help="report actions without copying or deleting")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="media-ingest", description="Ingest photos and videos into a dated archive.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="one-shot scan of incoming/")
    _add_common(p_ingest)
    p_ingest.add_argument("--settle", type=float, default=0.0,
                          help="skip files modified within N seconds (default 0)")

    p_watch = sub.add_parser("watch", help="poll incoming/ and ingest continuously")
    _add_common(p_watch)
    p_watch.add_argument("--interval", type=float, default=15.0,
                         help="seconds between scans (default 15)")
    p_watch.add_argument("--settle", type=float, default=10.0,
                         help="skip files modified within N seconds (default 10)")

    p_status = sub.add_parser("status", help="summarise the archive")
    p_status.add_argument("-c", "--config", required=True, help="path to config.toml")

    p_probe = sub.add_parser("probe", help="show how a file's capture date is resolved")
    p_probe.add_argument("file", help="path to a media file to inspect")

    return parser


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _lock_path(cfg: Config) -> Path:
    """An ephemeral lock in the system temp dir, keyed by the archive path so
    two configs don't collide. Never placed inside the archive itself."""
    key = hashlib.blake2b(str(cfg.originals.resolve()).encode(), digest_size=8).hexdigest()
    return Path(tempfile.gettempdir()) / f"media-ingest-{key}.lock"


# A lock older than this is assumed to be from a crashed run and is broken.
# Generously larger than any normal incremental run.
_STALE_LOCK_SECONDS = 3600


def _acquire_lock(lock_path: Path) -> int | None:
    """Best-effort exclusive lock via O_CREAT|O_EXCL. Returns fd or None.

    If a previous run crashed it leaves a stale lock; rather than block all
    future runs forever, an old-enough lock is broken and retaken."""
    try:
        return _create_lock(lock_path)
    except FileExistsError:
        pass
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return None
    if age <= _STALE_LOCK_SECONDS:
        return None  # a live run almost certainly holds it
    log.warning("breaking stale lock %s (age %.0fs, likely a crashed run)", lock_path, age)
    try:
        lock_path.unlink()
        return _create_lock(lock_path)
    except (OSError, FileExistsError):
        return None  # someone else won the race; just skip this run


def _create_lock(lock_path: Path) -> int:
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode())
    return fd


def _warn_if_missing_tools(cfg: Config) -> None:
    if cfg.date_source != "metadata":
        return
    tools = probe.available_tools()
    if not tools["exiftool"]:
        log.warning(
            "exiftool not found on PATH — embedded photo/video dates may be missed. "
            "Install it (the Docker image bundles it; locally: scoop install exiftool). "
            "Videos still try ffprobe=%s, photos Pillow=%s; otherwise filename/mtime.",
            tools["ffprobe"], tools["pillow"],
        )


def _cmd_ingest(cfg: Config, args) -> int:
    _warn_if_missing_tools(cfg)
    lock_path = _lock_path(cfg)
    fd = _acquire_lock(lock_path)
    if fd is None:
        log.warning("another ingest run holds %s; exiting", lock_path)
        return 0
    try:
        stats = ingest.run(cfg, dry_run=args.dry_run, settle_seconds=args.settle)
        log.info("done: %s", stats)
        return 1 if stats.errors else 0
    finally:
        os.close(fd)
        lock_path.unlink(missing_ok=True)


def _cmd_watch(cfg: Config, args) -> int:
    stopping = {"now": False}

    def _stop(signum, _frame):
        log.info("received signal %s, finishing current scan and exiting", signum)
        stopping["now"] = True

    _warn_if_missing_tools(cfg)
    signal.signal(signal.SIGINT, _stop)
    # SIGTERM is how `docker stop` asks us to exit; not always settable on Windows.
    with contextlib.suppress(ValueError, OSError, AttributeError):
        signal.signal(signal.SIGTERM, _stop)

    log.info("watching %s every %.0fs (settle %.0fs); Ctrl-C to stop",
             cfg.incoming, args.interval, args.settle)
    # A file is ingested only once its size is unchanged since the previous
    # scan — proof it has finished copying, even if the copier preserved the
    # source's (old) mtime and so slipped past the settle window.
    prev_sizes: dict[str, int] = {}
    while not stopping["now"]:
        sizes: dict[str, int] = {}
        ready: list[tuple] = []
        for source, path, size in ingest.find_candidates(cfg, args.settle):
            key = str(path)
            sizes[key] = size
            if prev_sizes.get(key) == size:
                ready.append((source, path))
            else:
                log.debug("waiting for %s to finish copying (size %d)", path, size)
        prev_sizes = sizes

        stats = ingest.ingest_paths(cfg, ready, dry_run=args.dry_run)
        if stats.imported or stats.duplicates or stats.errors:
            log.info("scan: %s", stats)
        # Sleep in short slices so signals are handled promptly.
        slept = 0.0
        while slept < args.interval and not stopping["now"]:
            time.sleep(min(0.5, args.interval - slept))
            slept += 0.5
    log.info("stopped")
    return 0


def _cmd_probe(file: str) -> int:
    path = Path(file)
    if not path.is_file():
        print(f"not a file: {path}")
        return 2
    ext = path.suffix.lower()
    if ext in DEFAULT_VIDEO_EXTS:
        mt = "video"
    elif ext in DEFAULT_PHOTO_EXTS:
        mt = "photo"
    else:
        mt = "photo"  # best-effort guess; still runs every tier
    info = probe.diagnose(path, mt)
    tools = probe.available_tools()

    print(f"file: {path}")
    print(f"media_type (guessed from extension): {mt}")
    print(f"exiftool on PATH: {info['exiftool_path'] or 'NOT FOUND'}")
    print(f"tools available: exiftool={tools['exiftool']} ffprobe={tools['ffprobe']} pillow={tools['pillow']}")
    if info["exiftool_time_tags"]:
        print("\n-- exiftool time tags --")
        print(info["exiftool_time_tags"])
    print("\n-- resolution tiers (first non-empty wins) --")
    print(f"  1. metadata exiftool: {info['exiftool_dt']}")
    if mt == "video":
        print(f"     metadata ffprobe:  {info['ffprobe_dt']}")
    if mt == "photo":
        print(f"     metadata pillow:   {info['pillow_dt']}")
    print(f"  2. filename:          {info['filename_dt']}")
    print(f"  3. mtime (last):      {info['mtime_dt']}")
    print(f"\nFINAL chosen date:    {info['final']}")
    return 0


def _cmd_status(cfg: Config) -> int:
    today = datetime.now().date()
    info = archive.summarize(cfg, today)
    print(f"archive: {cfg.originals}")
    print(f"total media: {info['total']}")
    if info["by_kind"]:
        print("\nby source / media_type:")
        for (source, mtype), n in info["by_kind"].items():
            print(f"  {source:<12} {mtype:<6} {n}")
    pub = cfg.publishable_sources()
    print(f"\npublishable sources: {', '.join(pub) or '(none)'}")
    print(f"days pending publish: {len(info['pending'])}")
    for d in info["pending"]:
        print(f"  {d.isoformat()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _setup_logging(getattr(args, "verbose", False))

    if args.command == "probe":  # no config needed — just inspect one file
        return _cmd_probe(args.file)

    cfg = load_config(args.config)
    if args.command == "ingest":
        return _cmd_ingest(cfg, args)
    if args.command == "watch":
        return _cmd_watch(cfg, args)
    if args.command == "status":
        return _cmd_status(cfg)
    return 2


if __name__ == "__main__":
    sys.exit(main())
