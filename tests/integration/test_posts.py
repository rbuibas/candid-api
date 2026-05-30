"""Integration test for the /posts pipeline against live Supabase + R2.

Marker-gated like the rest of tests/integration/, plus an additional
module-level skip when the R2 env vars aren't set. The full Phase 3
acceptance test:

  1. upload-url → PUT bytes to R2 → confirm → GET /posts/{id} → fetch
     the signed media_url, byte-compare to what we uploaded.
  2. confirm a second time for the same post_id → idempotent (no dupes).

The teardown cleans up the test object directly via r2.delete_object.
Run with:

    SUPABASE_URL=... SUPABASE_ANON_KEY=... \\
    SUPABASE_SERVICE_ROLE_KEY=... SUPABASE_JWT_SECRET=... \\
    R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... \\
    R2_BUCKET=... \\
      uv run pytest tests/integration/test_posts.py -v
"""

from __future__ import annotations

import os
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from supabase import Client, create_client

from app.clients import r2
from app.clients.supabase import get_supabase
from app.config import Settings, get_settings
from app.main import create_app


def _r2_envs_present() -> bool:
    return all(
        os.getenv(v)
        for v in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
    )


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _r2_envs_present(), reason="R2 envs not set"),
]


# A tiny fixture image: a 4-byte stand-in. R2 doesn't care that this isn't
# a real JPEG — we just want round-trip byte-fidelity and a HeadObject hit.
_FIXTURE_BYTES = b"\xff\xd8\xff\xd9"  # JPEG SOI + EOI markers
_FIXTURE_CT = "image/jpeg"


def _mint(user_id: UUID, secret: str) -> str:
    now = datetime.now(tz=UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "aud": "authenticated",
            "role": "authenticated",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        secret,
        algorithm="HS256",
    )


def _auth(user_id: UUID, env: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {_mint(user_id, env['SUPABASE_JWT_SECRET'])}"}


def _today_iso() -> str:
    return datetime.now(UTC).date().isoformat()


def _iso(delta_days: int) -> str:
    return (datetime.now(UTC).date() + timedelta(days=delta_days)).isoformat()


@pytest.fixture(autouse=True)
def _reset_lazy_clients() -> Generator[None, None, None]:
    """Force a fresh R2 / Settings client per test so env overrides apply."""
    r2._client = None
    get_settings.cache_clear()
    yield
    r2._client = None


@pytest.fixture
def app_client(integration_env: dict[str, str]) -> Generator[TestClient, None, None]:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        supabase_url=integration_env["SUPABASE_URL"],
        supabase_jwt_secret=integration_env["SUPABASE_JWT_SECRET"],
        supabase_service_role_key=integration_env["SUPABASE_SERVICE_ROLE_KEY"],
        r2_account_id=os.environ["R2_ACCOUNT_ID"],
        r2_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        r2_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        r2_bucket=os.environ["R2_BUCKET"],
    )
    sb = create_client(
        integration_env["SUPABASE_URL"],
        integration_env["SUPABASE_SERVICE_ROLE_KEY"],
    )
    app.dependency_overrides[get_supabase] = lambda: sb
    client = TestClient(app)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def created_paths() -> Generator[list[str], None, None]:
    """Tracks R2 keys for teardown."""
    keys: list[str] = []
    yield keys
    for k in keys:
        try:
            r2.delete_object(k)
        except Exception:
            pass


def _create_group(
    app_client: TestClient,
    creator: UUID,
    env: dict[str, str],
) -> str:
    resp = app_client.post(
        "/groups",
        headers=_auth(creator, env),
        json={"name": "PhotoBoothE2E", "start_date": _today_iso(), "end_date": _iso(2)},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["group"]["id"]


def test_upload_then_confirm_then_get_round_trip(
    app_client: TestClient,
    make_user: Callable[..., UUID],
    integration_env: dict[str, str],
    service_sb: Client,
    created_paths: list[str],
) -> None:
    user = make_user()
    group_id = _create_group(app_client, user, integration_env)

    # 1. Mint upload URL.
    upload = app_client.post(
        "/posts/upload-url",
        headers=_auth(user, integration_env),
        json={
            "group_id": group_id,
            "kind": "photobooth",
            "media_type": "strip",
            "extension": "jpg",
        },
    )
    assert upload.status_code == 200, upload.text
    body = upload.json()
    post_id = body["post_id"]
    upload_url = body["upload_url"]
    storage_path = body["storage_path"]
    created_paths.append(storage_path)

    # 2. PUT the bytes directly to R2.
    put_resp = httpx.put(
        upload_url,
        content=_FIXTURE_BYTES,
        headers={"Content-Type": _FIXTURE_CT},
        timeout=15.0,
    )
    assert put_resp.status_code == 200, put_resp.text

    # 3. Confirm.
    confirm = app_client.post(
        "/posts/confirm",
        headers=_auth(user, integration_env),
        json={
            "post_id": post_id,
            "group_id": group_id,
            "kind": "photobooth",
            "media_type": "strip",
            "storage_path": storage_path,
            "captured_at": datetime.now(UTC).isoformat(),
        },
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["id"] == post_id
    assert confirm.json()["is_late"] is False

    # Ground truth in posts table.
    db_row = service_sb.table("posts").select("*").eq("id", post_id).maybe_single().execute()
    assert db_row.data is not None
    assert db_row.data["storage_path"] == storage_path

    # 4. GET /posts/{id} returns a signed URL that actually works.
    get_resp = app_client.get(f"/posts/{post_id}", headers=_auth(user, integration_env))
    assert get_resp.status_code == 200, get_resp.text
    media_url = get_resp.json()["media_url"]

    fetched = httpx.get(media_url, timeout=15.0)
    assert fetched.status_code == 200
    assert fetched.content == _FIXTURE_BYTES


def test_confirm_is_idempotent(
    app_client: TestClient,
    make_user: Callable[..., UUID],
    integration_env: dict[str, str],
    service_sb: Client,
    created_paths: list[str],
) -> None:
    user = make_user()
    group_id = _create_group(app_client, user, integration_env)

    upload = app_client.post(
        "/posts/upload-url",
        headers=_auth(user, integration_env),
        json={
            "group_id": group_id,
            "kind": "photobooth",
            "media_type": "strip",
            "extension": "jpg",
        },
    ).json()
    post_id = upload["post_id"]
    storage_path = upload["storage_path"]
    created_paths.append(storage_path)

    httpx.put(
        upload["upload_url"],
        content=_FIXTURE_BYTES,
        headers={"Content-Type": _FIXTURE_CT},
        timeout=15.0,
    )

    payload = {
        "post_id": post_id,
        "group_id": group_id,
        "kind": "photobooth",
        "media_type": "strip",
        "storage_path": storage_path,
        "captured_at": datetime.now(UTC).isoformat(),
    }
    first = app_client.post("/posts/confirm", headers=_auth(user, integration_env), json=payload)
    second = app_client.post("/posts/confirm", headers=_auth(user, integration_env), json=payload)
    assert first.status_code == 200 == second.status_code
    assert first.json()["id"] == second.json()["id"] == post_id

    # Exactly one row in the posts table.
    rows = service_sb.table("posts").select("id").eq("id", post_id).execute().data
    assert len(rows) == 1


def test_delete_group_purges_r2_prefix(
    app_client: TestClient,
    make_user: Callable[..., UUID],
    integration_env: dict[str, str],
    service_sb: Client,
    created_paths: list[str],
) -> None:
    """End-to-end check on the Phase 2 stub now that it's wired to R2:
    DELETE /groups/{id} must remove every object under groups/{id}/."""
    creator = make_user()
    group_id = _create_group(app_client, creator, integration_env)

    upload = app_client.post(
        "/posts/upload-url",
        headers=_auth(creator, integration_env),
        json={
            "group_id": group_id,
            "kind": "photobooth",
            "media_type": "strip",
            "extension": "jpg",
        },
    ).json()
    storage_path = upload["storage_path"]
    # Don't add to created_paths: the DELETE flow should purge it for us.
    httpx.put(
        upload["upload_url"],
        content=_FIXTURE_BYTES,
        headers={"Content-Type": _FIXTURE_CT},
        timeout=15.0,
    )
    app_client.post(
        "/posts/confirm",
        headers=_auth(creator, integration_env),
        json={
            "post_id": upload["post_id"],
            "group_id": group_id,
            "kind": "photobooth",
            "media_type": "strip",
            "storage_path": storage_path,
            "captured_at": datetime.now(UTC).isoformat(),
        },
    )

    assert r2.head_object(storage_path) is True

    delete_resp = app_client.delete(f"/groups/{group_id}", headers=_auth(creator, integration_env))
    assert delete_resp.status_code == 204, delete_resp.text

    # Object is gone from R2.
    assert r2.head_object(storage_path) is False
    # And the post row is cascaded out.
    assert service_sb.table("posts").select("id").eq("id", upload["post_id"]).execute().data == []


def test_avatar_round_trip(
    app_client: TestClient,
    make_user: Callable[..., UUID],
    integration_env: dict[str, str],
    created_paths: list[str],
) -> None:
    user = make_user()

    upload = app_client.post(
        "/profile/avatar/upload-url",
        headers=_auth(user, integration_env),
        json={"extension": "jpg"},
    ).json()
    storage_path = upload["storage_path"]
    created_paths.append(storage_path)

    httpx.put(
        upload["upload_url"],
        content=_FIXTURE_BYTES,
        headers={"Content-Type": _FIXTURE_CT},
        timeout=15.0,
    )

    patch = app_client.patch(
        "/profile/avatar",
        headers=_auth(user, integration_env),
        json={"storage_path": storage_path},
    )
    assert patch.status_code == 200, patch.text
    signed = patch.json()["avatar_url"]
    assert signed.startswith("http")
    fetched = httpx.get(signed, timeout=15.0)
    assert fetched.status_code == 200
    assert fetched.content == _FIXTURE_BYTES
