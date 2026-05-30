# Lazy R2 client (S3-compatible via boto3). Bucket must be EU-jurisdiction.
#
# Every helper below is callable on every request. They all funnel through
# `get_r2()`, which raises a RuntimeError if the R2 envs are missing. That
# keeps import + app boot + /health working when R2 isn't configured yet —
# only `/posts/*` and `/profile/avatar*` routes ever trip the credential
# check, and they do so cleanly with a 500.

from typing import Any

import boto3
from botocore.exceptions import ClientError

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


def _bucket() -> str:
    bucket = get_settings().r2_bucket
    if not bucket:
        raise RuntimeError("R2 client requires R2_BUCKET")
    return bucket


def generate_presigned_put_url(path: str, content_type: str, ttl_seconds: int = 600) -> str:
    """Mint a short-lived presigned PUT URL the client can stream bytes to.

    Content-Type is bound into the signature; the client must use the exact
    same Content-Type header on the PUT or R2 will reject it.
    """
    return get_r2().generate_presigned_url(
        "put_object",
        Params={"Bucket": _bucket(), "Key": path, "ContentType": content_type},
        ExpiresIn=ttl_seconds,
    )


def generate_presigned_get_url(path: str, ttl_seconds: int = 3600) -> str:
    """Mint a short-lived presigned GET URL for client-side fetch."""
    return get_r2().generate_presigned_url(
        "get_object",
        Params={"Bucket": _bucket(), "Key": path},
        ExpiresIn=ttl_seconds,
    )


def head_object(path: str) -> bool:
    """Return True if the object exists, False on 404. Re-raise other errors."""
    try:
        get_r2().head_object(Bucket=_bucket(), Key=path)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        # S3/R2 surface 404 as "404" (HeadObject) or "NoSuchKey".
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def delete_object(path: str) -> None:
    get_r2().delete_object(Bucket=_bucket(), Key=path)


def delete_prefix(prefix: str) -> int:
    """Delete every object under `prefix`. Returns the number deleted.

    Pages through list_objects_v2 and batches into delete_objects calls of
    ≤1000 keys (the S3 per-request cap). No-op if the prefix is empty.
    """
    client = get_r2()
    bucket = _bucket()
    total = 0
    continuation: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if continuation is not None:
            kwargs["ContinuationToken"] = continuation
        page = client.list_objects_v2(**kwargs)
        contents = page.get("Contents") or []
        for i in range(0, len(contents), 1000):
            chunk = contents[i : i + 1000]
            client.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": obj["Key"]} for obj in chunk]},
            )
            total += len(chunk)
        if not page.get("IsTruncated"):
            return total
        continuation = page.get("NextContinuationToken")
