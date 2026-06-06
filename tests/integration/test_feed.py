"""Integration test for the Phase 5 feed + delete flow.

Marker-gated like the rest of tests/integration/, plus a module-level skip
when R2 envs aren't set. The full acceptance pass:

  upload-url → PUT bytes → confirm → GET /groups/{id}/feed lists the post
  with a working signed media_url → DELETE /posts/{id} → feed no longer
  lists it AND HEAD on the R2 object returns 404.

Run with:

    SUPABASE_URL=... SUPABASE_ANON_KEY=... \\
    SUPABASE_SERVICE_ROLE_KEY=... SUPABASE_JWT_SECRET=... \\
    R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... \\
    R2_BUCKET=... \\
      uv run pytest tests/integration/test_feed.py -v
"""

from __future__ import annotations

import os
import time
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
    keys: list[str] = []
    yield keys
    for k in keys:
        try:
            r2.delete_object(k)
        except Exception:
            pass


def _create_group(app_client: TestClient, creator: UUID, env: dict[str, str]) -> str:
    resp = app_client.post(
        "/groups",
        headers=_auth(creator, env),
        json={"name": "FeedE2E", "start_date": _today_iso(), "end_date": _iso(2)},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["group"]["id"]


def _upload_confirm(
    app_client: TestClient,
    user: UUID,
    group_id: str,
    env: dict[str, str],
) -> tuple[str, str]:
    """Upload + confirm a photobooth post; return (post_id, storage_path)."""
    upload = app_client.post(
        "/posts/upload-url",
        headers=_auth(user, env),
        json={
            "group_id": group_id,
            "kind": "photobooth",
            "media_type": "strip",
            "extension": "jpg",
        },
    ).json()
    post_id = upload["post_id"]
    storage_path = upload["storage_path"]

    put_resp = httpx.put(
        upload["upload_url"],
        content=_FIXTURE_BYTES,
        headers={"Content-Type": _FIXTURE_CT},
        timeout=15.0,
    )
    assert put_resp.status_code == 200, put_resp.text

    confirm = app_client.post(
        "/posts/confirm",
        headers=_auth(user, env),
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
    return post_id, storage_path


def test_feed_lists_post_then_delete_removes_it_everywhere(
    app_client: TestClient,
    make_user: Callable[..., UUID],
    integration_env: dict[str, str],
    created_paths: list[str],
    service_sb: Client,
) -> None:
    user = make_user()
    group_id = _create_group(app_client, user, integration_env)
    post_id, storage_path = _upload_confirm(app_client, user, group_id, integration_env)
    # Don't pre-register for teardown: the DELETE flow should purge it. We
    # re-add only if the delete assertion is skipped.

    # 1. Feed lists the post with a working signed media_url.
    feed = app_client.get(f"/groups/{group_id}/feed", headers=_auth(user, integration_env))
    assert feed.status_code == 200, feed.text
    body = feed.json()
    ids = [item["id"] for item in body["items"]]
    assert post_id in ids
    item = next(it for it in body["items"] if it["id"] == post_id)
    assert "storage_path" not in item  # internal key never leaks
    assert item["author"]["user_id"] == str(user)

    fetched = httpx.get(item["media_url"], timeout=15.0)
    assert fetched.status_code == 200
    assert fetched.content == _FIXTURE_BYTES

    # R2 object exists before delete.
    assert r2.head_object(storage_path) is True

    # 2. Delete the post (author).
    delete = app_client.delete(f"/posts/{post_id}", headers=_auth(user, integration_env))
    assert delete.status_code == 204, delete.text

    # 3. Feed no longer lists it.
    feed_after = app_client.get(f"/groups/{group_id}/feed", headers=_auth(user, integration_env))
    assert feed_after.status_code == 200, feed_after.text
    assert post_id not in [item["id"] for item in feed_after.json()["items"]]

    # 4. GET /posts/{id} on a tombstoned post → 404.
    get_after = app_client.get(f"/posts/{post_id}", headers=_auth(user, integration_env))
    assert get_after.status_code == 404, get_after.text

    # 4b. posts row has deleted_at set (scenario 9 of the Phase 5 test plan).
    row = service_sb.table("posts").select("deleted_at").eq("id", post_id).single().execute()
    assert row.data["deleted_at"] is not None

    # 5. R2 object is hard-deleted.
    assert r2.head_object(storage_path) is False

    # 6. Re-delete is idempotent → 404.
    redelete = app_client.delete(f"/posts/{post_id}", headers=_auth(user, integration_env))
    assert redelete.status_code == 404, redelete.text


def test_feed_paginates_across_pages_without_duplicates(
    app_client: TestClient,
    make_user: Callable[..., UUID],
    integration_env: dict[str, str],
    created_paths: list[str],
) -> None:
    user = make_user()
    group_id = _create_group(app_client, user, integration_env)

    created_ids: set[str] = set()
    for _ in range(5):
        post_id, storage_path = _upload_confirm(app_client, user, group_id, integration_env)
        created_ids.add(post_id)
        created_paths.append(storage_path)

    # Walk the feed in pages of 2; collect every id across pages.
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):  # generous page-walk cap
        url = f"/groups/{group_id}/feed?limit=2"
        if cursor:
            url += f"&cursor={cursor}"
        resp = app_client.get(url, headers=_auth(user, integration_env))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert cursor is None  # walk terminated
    assert created_ids.issubset(set(seen))  # every post surfaced
    assert len(seen) == len(set(seen))  # no duplicates across pages


def test_view_delay_hides_post_until_visible_at(
    app_client: TestClient,
    make_user: Callable[..., UUID],
    integration_env: dict[str, str],
    created_paths: list[str],
) -> None:
    """Scenario 14: a post is invisible until view_delay_seconds elapses.

    Uses a 5-second delay (instead of the 60s in the manual test plan) so the
    test completes quickly. The key invariant under test is that confirm sets
    visible_at = now + view_delay_seconds and the feed filters lte(visible_at).
    """
    _DELAY = 5

    user = make_user()

    group_resp = app_client.post(
        "/groups",
        headers=_auth(user, integration_env),
        json={
            "name": "ViewDelayE2E",
            "start_date": _today_iso(),
            "end_date": _iso(2),
            "settings": {"view_delay_seconds": _DELAY},
        },
    )
    assert group_resp.status_code == 201, group_resp.text
    group_id = group_resp.json()["group"]["id"]

    post_id, storage_path = _upload_confirm(app_client, user, group_id, integration_env)
    created_paths.append(storage_path)

    # Immediately after confirm the post must NOT appear (visible_at is in the future).
    feed_before = app_client.get(f"/groups/{group_id}/feed", headers=_auth(user, integration_env))
    assert feed_before.status_code == 200, feed_before.text
    assert post_id not in [item["id"] for item in feed_before.json()["items"]]

    # Wait for view_delay to elapse (with a small buffer).
    time.sleep(_DELAY + 2)

    # Now the post must be visible.
    feed_after = app_client.get(f"/groups/{group_id}/feed", headers=_auth(user, integration_env))
    assert feed_after.status_code == 200, feed_after.text
    assert post_id in [item["id"] for item in feed_after.json()["items"]]
