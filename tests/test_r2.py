"""Unit tests for clients/r2.py.

Two things to lock down:

1. Lazy behaviour — importing and calling /health must not require R2 envs.
   Direct R2 calls without envs should raise RuntimeError cleanly.
2. Helper shape — head_object returns False on 404, delete_prefix paginates
   and batches at the S3 1000-key cap.

R2 envs are never required. We monkeypatch get_r2 to return a fake S3-style
client.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from app.clients import r2
from app.config import Settings, get_settings
from app.main import create_app


@pytest.fixture(autouse=True)
def _reset_lazy_client() -> None:
    """Each test starts with no cached R2 client."""
    r2._client = None
    get_settings.cache_clear()


# --- Lazy boot --------------------------------------------------------


def test_app_boots_without_r2_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-negotiable: missing R2 envs must not break /health."""
    for env in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(env, raising=False)

    client = TestClient(create_app())
    response = client.get("/health")
    assert response.status_code == 200


def test_get_r2_raises_when_envs_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.clients.r2.get_settings",
        lambda: Settings(r2_account_id=None, r2_access_key_id=None, r2_secret_access_key=None),
    )
    with pytest.raises(RuntimeError, match="R2 client requires"):
        r2.get_r2()


def test_bucket_helper_raises_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.clients.r2.get_settings",
        lambda: Settings(r2_bucket=None),
    )
    with pytest.raises(RuntimeError, match="R2_BUCKET"):
        r2._bucket()


# --- Helpers (S3 client monkeypatched) --------------------------------


@pytest.fixture
def fake_r2(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    fake = MagicMock()
    monkeypatch.setattr("app.clients.r2.get_r2", lambda: fake)
    monkeypatch.setattr("app.clients.r2._bucket", lambda: "test-bucket")
    return fake


def test_generate_presigned_put_url_passes_content_type(fake_r2: MagicMock) -> None:
    fake_r2.generate_presigned_url.return_value = "https://signed.example/put"
    url = r2.generate_presigned_put_url("groups/a/posts/b/media.jpg", "image/jpeg", ttl_seconds=600)
    assert url == "https://signed.example/put"
    fake_r2.generate_presigned_url.assert_called_once_with(
        "put_object",
        Params={
            "Bucket": "test-bucket",
            "Key": "groups/a/posts/b/media.jpg",
            "ContentType": "image/jpeg",
        },
        ExpiresIn=600,
    )


def test_generate_presigned_get_url_uses_ttl(fake_r2: MagicMock) -> None:
    fake_r2.generate_presigned_url.return_value = "https://signed.example/get"
    url = r2.generate_presigned_get_url("groups/a/posts/b/media.jpg", ttl_seconds=3600)
    assert url == "https://signed.example/get"
    fake_r2.generate_presigned_url.assert_called_once_with(
        "get_object",
        Params={"Bucket": "test-bucket", "Key": "groups/a/posts/b/media.jpg"},
        ExpiresIn=3600,
    )


def test_head_object_returns_true_on_success(fake_r2: MagicMock) -> None:
    fake_r2.head_object.return_value = {"ContentLength": 123}
    assert r2.head_object("groups/a/posts/b/media.jpg") is True


@pytest.mark.parametrize("code", ["404", "NoSuchKey", "NotFound"])
def test_head_object_returns_false_on_missing(fake_r2: MagicMock, code: str) -> None:
    fake_r2.head_object.side_effect = ClientError(
        {"Error": {"Code": code, "Message": "not found"}}, "HeadObject"
    )
    assert r2.head_object("groups/a/posts/b/media.jpg") is False


def test_head_object_reraises_on_other_errors(fake_r2: MagicMock) -> None:
    fake_r2.head_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "nope"}}, "HeadObject"
    )
    with pytest.raises(ClientError):
        r2.head_object("groups/a/posts/b/media.jpg")


def test_delete_object_calls_through(fake_r2: MagicMock) -> None:
    r2.delete_object("groups/a/posts/b/media.jpg")
    fake_r2.delete_object.assert_called_once_with(
        Bucket="test-bucket", Key="groups/a/posts/b/media.jpg"
    )


def test_delete_prefix_empty(fake_r2: MagicMock) -> None:
    fake_r2.list_objects_v2.return_value = {"IsTruncated": False}
    assert r2.delete_prefix("groups/a/") == 0
    fake_r2.delete_objects.assert_not_called()


def test_delete_prefix_single_page(fake_r2: MagicMock) -> None:
    fake_r2.list_objects_v2.return_value = {
        "Contents": [{"Key": "groups/a/x"}, {"Key": "groups/a/y"}],
        "IsTruncated": False,
    }
    assert r2.delete_prefix("groups/a/") == 2
    fake_r2.delete_objects.assert_called_once_with(
        Bucket="test-bucket",
        Delete={"Objects": [{"Key": "groups/a/x"}, {"Key": "groups/a/y"}]},
    )


def test_delete_prefix_paginates(fake_r2: MagicMock) -> None:
    page1 = {
        "Contents": [{"Key": f"groups/a/{i}"} for i in range(10)],
        "IsTruncated": True,
        "NextContinuationToken": "TOK",
    }
    page2 = {
        "Contents": [{"Key": f"groups/a/{i}"} for i in range(10, 15)],
        "IsTruncated": False,
    }
    fake_r2.list_objects_v2.side_effect = [page1, page2]

    assert r2.delete_prefix("groups/a/") == 15

    # Second list call must include the continuation token.
    calls = fake_r2.list_objects_v2.call_args_list
    assert calls[0].kwargs == {"Bucket": "test-bucket", "Prefix": "groups/a/"}
    assert calls[1].kwargs == {
        "Bucket": "test-bucket",
        "Prefix": "groups/a/",
        "ContinuationToken": "TOK",
    }


def test_delete_prefix_chunks_at_1000(fake_r2: MagicMock) -> None:
    fake_r2.list_objects_v2.return_value = {
        "Contents": [{"Key": f"groups/a/{i}"} for i in range(1500)],
        "IsTruncated": False,
    }
    assert r2.delete_prefix("groups/a/") == 1500
    # 1500 / 1000 → two batches, sizes 1000 and 500.
    calls: list[Any] = fake_r2.delete_objects.call_args_list
    assert len(calls) == 2
    assert len(calls[0].kwargs["Delete"]["Objects"]) == 1000
    assert len(calls[1].kwargs["Delete"]["Objects"]) == 500
