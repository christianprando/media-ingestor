"""Frictionless family-media ingestion pipeline (photos + videos).

Stage 1 (this package): safely pull media out of an incoming staging
directory and into a date-organised archive, deduplicating against the
archive itself and never deleting a source file before a verified copy
exists.

Later stages (per-day video concat, YouTube upload, off-site backup)
build on the date-organised archive produced here. There is no database:
the files are the only source of truth (see archive.py).
"""

__version__ = "0.1.0"
