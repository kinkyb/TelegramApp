"""r2.py — Cloudflare R2 upload helpers.

Uses boto3 with a custom endpoint URL to talk to R2 (S3-compatible).
All credentials are read from environment variables via python-dotenv.
"""

import os
import mimetypes
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# R2 client singleton
# ---------------------------------------------------------------------------

_r2_client = None


def _get_client():
    """Return a cached boto3 S3 client pointed at Cloudflare R2."""
    global _r2_client
    if _r2_client is None:
        account_id = os.environ["R2_ACCOUNT_ID"]
        _r2_client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )
    return _r2_client


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------

def upload_file(local_path: str, object_key: str | None = None) -> str:
    """Upload a local file to R2 and return its public CDN URL.

    Args:
        local_path: Absolute or relative path to the file on disk.
        object_key: Destination key inside the R2 bucket.
                    Defaults to the file's basename.

    Returns:
        Public URL of the uploaded object (via R2_PUBLIC_URL).

    Raises:
        ClientError: If the upload fails.
        FileNotFoundError: If local_path does not exist.
    """
    path = Path(local_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {local_path}")

    if object_key is None:
        object_key = path.name

    bucket = os.environ["R2_BUCKET_NAME"]
    content_type, _ = mimetypes.guess_type(str(path))
    if content_type is None:
        content_type = "application/octet-stream"

    client = _get_client()
    client.upload_file(
        str(path),
        bucket,
        object_key,
        ExtraArgs={"ContentType": content_type},
    )

    public_base = os.environ["R2_PUBLIC_URL"].rstrip("/")
    return f"{public_base}/{object_key}"


def delete_file(object_key: str) -> None:
    """Delete an object from R2 by its key.

    Args:
        object_key: The key of the object to delete.

    Raises:
        ClientError: If the deletion fails.
    """
    bucket = os.environ["R2_BUCKET_NAME"]
    client = _get_client()
    client.delete_object(Bucket=bucket, Key=object_key)


def object_exists(object_key: str) -> bool:
    """Return True if an object with the given key already exists in R2.

    Args:
        object_key: The key to check.
    """
    bucket = os.environ["R2_BUCKET_NAME"]
    client = _get_client()
    try:
        client.head_object(Bucket=bucket, Key=object_key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return False
        raise


def public_url(object_key: str) -> str:
    """Build the public CDN URL for an R2 object without making an API call.

    Args:
        object_key: The key of the object inside the bucket.

    Returns:
        Full public URL string.
    """
    public_base = os.environ["R2_PUBLIC_URL"].rstrip("/")
    return f"{public_base}/{object_key}"


# ---------------------------------------------------------------------------
# File-type detection
# ---------------------------------------------------------------------------

IMAGE_EXTENSIONS  = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
VIDEO_EXTENSIONS  = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
GIF_EXTENSIONS    = {".gif"}


def detect_file_type(filename: str) -> str:
    """Return 'image', 'video', or 'gif' based on the file extension.

    Args:
        filename: Name of the file (with extension).

    Returns:
        One of 'image', 'video', 'gif', or 'unknown'.
    """
    ext = Path(filename).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in GIF_EXTENSIONS:
        return "gif"
    return "unknown"
