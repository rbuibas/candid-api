# Lazy R2 client (S3-compatible via boto3). Bucket must be EU-jurisdiction.

from typing import Any

import boto3

from app.config import get_settings

_client: Any = None


def get_r2() -> Any:
    global _client
    if _client is not None:
        return _client

    settings = get_settings()
    if not (settings.r2_account_id and settings.r2_access_key_id and settings.r2_secret_access_key):
        raise RuntimeError(
            "R2 client requires R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, and R2_SECRET_ACCESS_KEY"
        )

    _client = boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name=settings.r2_region,
    )
    return _client
