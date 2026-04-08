"""watcher.py — Local folder watcher that uploads new media files to R2.

Monitors two directories:
  - local-library/my-content/   → creator's own photos/videos/GIFs
  - local-library/promo-gifs/   → promotional GIFs for other creators

When a new file appears:
  1. Upload it to Cloudflare R2.
  2. For videos: auto-extract a thumbnail frame with ffmpeg, upload that too.
  3. Insert a record into the SQLite DB (with teaser_url set for videos).

Requires: ffmpeg installed (brew install ffmpeg).
Run as a daemon: python watcher.py
"""

import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv
from watchdog.events import FileSystemEventHandler, FileCreatedEvent
from watchdog.observers import Observer

import db
import r2

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Directories to watch
# ---------------------------------------------------------------------------

BASE_DIR       = Path(__file__).parent
MY_CONTENT_DIR = BASE_DIR / "local-library" / "my-content"
PROMO_GIFS_DIR = BASE_DIR / "local-library" / "promo-gifs"

MIN_FILE_SIZE = 1024   # 1 KB — skip empty/incomplete files

# Seconds into the video to grab the thumbnail frame.
# 5 s is usually past any intro black frames on short clips.
THUMBNAIL_OFFSET_SECONDS = 5

# GIFs larger than this are auto-converted to MP4 before upload.
GIF_SIZE_LIMIT_BYTES   = 50 * 1024 * 1024   # 50 MB (Telegram animation limit)
VIDEO_SIZE_LIMIT_BYTES = 45 * 1024 * 1024   # 45 MB — compress above this (Bot API max is 50 MB)


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------

def _ffmpeg_available() -> bool:
    """Return True if ffmpeg is on PATH."""
    return shutil.which("ffmpeg") is not None


def _extract_thumbnail(video_path: Path) -> Path | None:
    """Extract a single JPEG frame from a video using ffmpeg.

    Grabs the frame at THUMBNAIL_OFFSET_SECONDS, or at 10 % of the video
    duration if the video is shorter than the offset.

    Args:
        video_path: Path to the local video file.

    Returns:
        Path to a temporary JPEG file, or None on failure.
        Caller is responsible for deleting the temp file.
    """
    if not _ffmpeg_available():
        logger.warning("ffmpeg not found — skipping thumbnail generation. Install with: brew install ffmpeg")
        return None

    # Probe duration so we can fall back to 10 % if video is very short
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        duration = float(probe.stdout.strip())
        offset   = min(THUMBNAIL_OFFSET_SECONDS, duration * 0.1)
    except Exception:
        offset = THUMBNAIL_OFFSET_SECONDS

    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", str(offset),
                "-i", str(video_path),
                "-frames:v", "1",
                "-q:v", "3",        # JPEG quality 1-31, lower = better
                tmp.name,
            ],
            capture_output=True, timeout=60,
        )
        if result.returncode != 0:
            logger.error("ffmpeg thumbnail failed: %s", result.stderr.decode())
            Path(tmp.name).unlink(missing_ok=True)
            return None
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out for %s", video_path.name)
        Path(tmp.name).unlink(missing_ok=True)
        return None

    return Path(tmp.name)


def _convert_gif_to_mp4(gif_path: Path) -> Path | None:
    """Convert a GIF to a silent MP4 using ffmpeg.

    Used when a GIF exceeds Telegram's 50 MB animation limit.

    Args:
        gif_path: Path to the local GIF file.

    Returns:
        Path to a temporary MP4 file, or None on failure.
        Caller is responsible for deleting the temp file.
    """
    if not _ffmpeg_available():
        logger.warning("ffmpeg not found — cannot convert GIF to MP4.")
        return None

    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.close()

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(gif_path),
                "-movflags", "faststart",
                "-pix_fmt", "yuv420p",
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",  # ensure even dimensions
                "-an",       # no audio
                tmp.name,
            ],
            capture_output=True, timeout=120,
        )
        if result.returncode != 0:
            logger.error("GIF→MP4 conversion failed: %s", result.stderr.decode())
            Path(tmp.name).unlink(missing_ok=True)
            return None
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out converting GIF: %s", gif_path.name)
        Path(tmp.name).unlink(missing_ok=True)
        return None

    size_mb = Path(tmp.name).stat().st_size / 1024 / 1024
    logger.info("GIF converted to MP4: %.1f MB → %s", size_mb, tmp.name)
    return Path(tmp.name)


def _compress_video(video_path: Path) -> Path | None:
    """Compress a video to fit within Telegram's 50 MB Bot API limit.

    Targets 720p at CRF 28 (good quality, small size). Most videos compress
    to 20-40 MB. Falls back to 480p if still too large after first pass.

    Args:
        video_path: Path to the local video file.

    Returns:
        Path to a temporary compressed MP4, or None on failure.
        Caller is responsible for deleting the temp file.
    """
    if not _ffmpeg_available():
        logger.warning("ffmpeg not found — cannot compress video.")
        return None

    tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
    tmp.close()

    for scale, crf in [("1280:720", "28"), ("854:480", "30")]:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(video_path),
                "-vf", f"scale='min(iw,{scale.split(':')[0]})':'-2'",
                "-c:v", "libx264",
                "-crf", crf,
                "-preset", "fast",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "faststart",
                tmp.name,
            ],
            capture_output=True, timeout=1800,
        )
        if result.returncode != 0:
            logger.error("Video compression failed: %s", result.stderr.decode()[:200])
            Path(tmp.name).unlink(missing_ok=True)
            return None

        size = Path(tmp.name).stat().st_size
        size_mb = size / 1024 / 1024
        logger.info("Compressed video at %s CRF%s: %.1f MB", scale, crf, size_mb)

        if size <= VIDEO_SIZE_LIMIT_BYTES:
            return Path(tmp.name)

        logger.info("Still too large (%.1f MB), retrying at lower quality…", size_mb)

    # If still over limit after both passes, return what we have and let the bot handle it
    logger.warning("Could not compress video under 45 MB — uploading anyway.")
    return Path(tmp.name)


# ---------------------------------------------------------------------------
# File-readiness checks
# ---------------------------------------------------------------------------

def _is_supported(path: Path) -> bool:
    """Return True if the file extension is a known media type."""
    return r2.detect_file_type(path.name) != "unknown"


def _is_ready(path: Path, retries: int = 5, interval: float = 1.0) -> bool:
    """Return True once the file is fully written (size stable across two checks).

    Retries several times to handle macOS writing files in chunks.
    """
    last_size = -1
    for _ in range(retries):
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size >= MIN_FILE_SIZE and size == last_size:
            return True
        last_size = size
        time.sleep(interval)
    # Final check — accept if size is stable and large enough
    try:
        return path.stat().st_size == last_size and last_size >= MIN_FILE_SIZE
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Event handler
# ---------------------------------------------------------------------------

class MediaHandler(FileSystemEventHandler):
    """Handles file-creation events in the watched directories."""

    def __init__(self, is_promo: bool = False) -> None:
        """Initialise the handler.

        Args:
            is_promo: True when watching the promo-gifs folder.
        """
        super().__init__()
        self.is_promo = is_promo

    def on_created(self, event: FileCreatedEvent) -> None:
        """Called when a file is created in the watched directory.

        Args:
            event: The file system event.
        """
        if event.is_directory:
            return

        path = Path(event.src_path)

        if not _is_supported(path):
            logger.debug("Skipping unsupported file: %s", path.name)
            return

        if not _is_ready(path):
            logger.warning("File not ready (too small or still writing): %s", path.name)
            return

        self._process(path)

    def _process(self, path: Path) -> None:
        """Upload a media file (and its auto-thumbnail if video) to R2, then DB.

        Args:
            path: Absolute path to the media file.
        """
        folder    = "promo-gifs" if self.is_promo else "my-content"
        file_type = r2.detect_file_type(path.name)
        tmp_converted: Path | None = None

        # --- Auto-convert oversized GIFs to MP4 ---
        if file_type == "gif" and path.stat().st_size > GIF_SIZE_LIMIT_BYTES:
            size_mb = path.stat().st_size / 1024 / 1024
            logger.info(
                "GIF too large for Telegram (%.1f MB) — converting to MP4: %s",
                size_mb, path.name,
            )
            tmp_converted = _convert_gif_to_mp4(path)
            if tmp_converted is None:
                logger.error("Conversion failed — skipping %s", path.name)
                return
            # Treat the converted file as a video from here on
            upload_path = tmp_converted
            stem        = path.stem
            object_key  = f"{folder}/{stem}.mp4"
            file_type   = "video"
        elif file_type == "video" and path.stat().st_size > VIDEO_SIZE_LIMIT_BYTES:
            size_mb = path.stat().st_size / 1024 / 1024
            logger.info(
                "Video too large for Telegram (%.1f MB) — compressing: %s",
                size_mb, path.name,
            )
            tmp_converted = _compress_video(path)
            if tmp_converted is None:
                logger.error("Compression failed — skipping %s", path.name)
                return
            upload_path = tmp_converted
            # Keep original filename but ensure .mp4 extension
            stem       = path.stem
            object_key = f"{folder}/{stem}.mp4"
        else:
            upload_path = path
            object_key  = f"{folder}/{path.name}"

        if r2.object_exists(object_key):
            logger.info("Already in R2, skipping: %s", object_key)
            if tmp_converted:
                tmp_converted.unlink(missing_ok=True)
            return

        # --- Auto-thumbnail for videos (extract BEFORE deleting tmp_converted) ---
        # Use the compressed/converted file if available — much faster than the original.
        # Always key the thumbnail by the ORIGINAL filename stem so it's identifiable.
        teaser_url = ""
        if file_type == "video" and not self.is_promo:
            thumb_source = upload_path   # compressed MP4 or original if no conversion
            teaser_url = self._upload_thumbnail(thumb_source, name_stem=path.stem)

        # --- Upload main file ---
        logger.info("Uploading %s → %s", upload_path.name, object_key)
        try:
            file_url = r2.upload_file(str(upload_path), object_key)
        except Exception as exc:
            logger.error("Upload failed for %s: %s", upload_path.name, exc)
            return
        finally:
            if tmp_converted:
                tmp_converted.unlink(missing_ok=True)

        logger.info("Uploaded: %s", file_url)

        if self.is_promo:
            logger.info("Promo GIF ready — link to a creator in the DB: %s", file_url)
            return

        content_id = db.insert_content(
            file_url=file_url,
            file_type=file_type,
            caption="",
            teaser_url=teaser_url,
        )

        if teaser_url:
            logger.info(
                "DB record created: content.id=%d  type=%s  teaser=%s",
                content_id, file_type, teaser_url,
            )
        else:
            logger.info(
                "DB record created: content.id=%d  type=%s",
                content_id, file_type,
            )

    def _upload_thumbnail(self, video_path: Path, name_stem: str | None = None) -> str:
        """Extract a thumbnail from the video and upload it to R2.

        Args:
            video_path: Path to the local video file to extract from.
            name_stem: Stem to use for the R2 key (defaults to video_path.stem).
                       Pass the original filename stem when video_path is a temp file.

        Returns:
            Public R2 URL of the thumbnail, or empty string on failure.
        """
        thumb_local = _extract_thumbnail(video_path)
        if thumb_local is None:
            return ""

        stem = name_stem or video_path.stem
        thumb_key = f"my-content/thumbs/{stem}.jpg"
        try:
            thumb_url = r2.upload_file(str(thumb_local), thumb_key)
            logger.info("Thumbnail uploaded: %s", thumb_url)
            return thumb_url
        except Exception as exc:
            logger.error("Thumbnail upload failed: %s", exc)
            return ""
        finally:
            thumb_local.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Start watching both media directories and block until interrupted."""
    MY_CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    PROMO_GIFS_DIR.mkdir(parents=True, exist_ok=True)

    db.init_db()

    if not _ffmpeg_available():
        logger.warning(
            "ffmpeg not found — video thumbnails will not be auto-generated. "
            "Install with: brew install ffmpeg"
        )

    observer = Observer()
    observer.schedule(MediaHandler(is_promo=False), str(MY_CONTENT_DIR), recursive=False)
    observer.schedule(MediaHandler(is_promo=True),  str(PROMO_GIFS_DIR),  recursive=False)
    observer.start()

    logger.info("Watching %s", MY_CONTENT_DIR)
    logger.info("Watching %s", PROMO_GIFS_DIR)
    logger.info("Press Ctrl-C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Stopping watcher...")
        observer.stop()

    observer.join()
    logger.info("Watcher stopped.")


if __name__ == "__main__":
    main()
