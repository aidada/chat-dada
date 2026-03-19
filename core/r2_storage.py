"""
Cloudflare R2 Storage — upload files and generate presigned URLs for LLM consumption.

Import-safe: missing boto3 or unconfigured env vars → is_available() returns False.
"""

import logging
import os
from pathlib import Path

log = logging.getLogger("chatdada.r2_storage")

try:
    import boto3
    from botocore.config import Config as BotoConfig

    _HAS_BOTO3 = True
except ImportError:
    _HAS_BOTO3 = False

_ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
_ACCESS_KEY = os.getenv("R2_ACCESS_KEY_ID", "")
_SECRET_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
_BUCKET = os.getenv("R2_BUCKET_NAME", "chatdada-uploads")
_PRESIGN_TTL = int(os.getenv("R2_PRESIGN_EXPIRES", "1800"))


def is_available() -> bool:
    """Return True when boto3 is installed and R2 credentials are configured."""
    return _HAS_BOTO3 and bool(_ACCOUNT_ID and _ACCESS_KEY and _SECRET_KEY)


def _get_client():
    endpoint = f"https://{_ACCOUNT_ID}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=_ACCESS_KEY,
        aws_secret_access_key=_SECRET_KEY,
        config=BotoConfig(signature_version="s3v4"),
        region_name="auto",
    )


def upload_and_presign(file_path: str) -> str:
    """Upload *file_path* to R2 and return a presigned GET URL (default 30 min)."""
    p = Path(file_path)
    key = p.name  # already in {uuid}_{filename} format from /upload

    client = _get_client()
    client.upload_file(str(p), _BUCKET, key)
    log.info("R2 uploaded %s → %s/%s", p.name, _BUCKET, key)

    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": _BUCKET, "Key": key},
        ExpiresIn=_PRESIGN_TTL,
    )
    return url
