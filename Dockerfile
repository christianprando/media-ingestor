# The same image runs the watcher on your dev machine and on TrueNAS SCALE.
FROM python:3.12-slim

# exiftool reads capture dates across every photo/video format (primary).
# ffmpeg (ffprobe) and Pillow [photo] are fallbacks when exiftool can't help.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY media_ingestor ./media_ingestor
RUN pip install --no-cache-dir ".[photo]"

# Config is mounted at /config/config.toml; data dataset at /data.
ENTRYPOINT ["media-ingest"]
CMD ["watch", "-c", "/config/config.toml"]
