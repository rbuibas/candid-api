"""Unit tests for /posts endpoints and the posts service.

Pattern mirrors tests/test_groups.py: hermetic, MagicMock chains on the
supabase client, JWT minted with the HS256 test secret. R2 is patched
out at the module level — no R2 envs needed.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from app.clients.supabase import get_supabase
from app.config import Settings, get_settings
from app.main import create_app
from app.models.post import (
    ConfirmPostRequest,
    PostKind,
    PostMediaType,
    UploadUrlRequest,
)
from app.services import posts as posts_service

TEST_SECRET = "test-jwt-secret-32-bytes-or-more-aaaaaaaa"


def _mint(user_id: UUID) -> str:
    now = datetime.now(tz=UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "aud": "authenticated",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        TEST_SECRET,
        algorithm="HS256",
    )


def _profile_row(user_id: UUID, **overrides: Any) -> dict[str, Any]:
    base = {
        "id": str(user_id),
        "display_name": None,
        "avatar_url": None,
        "timezone": "UTC",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def _post_row(
    post_id: UUID,
    user_id: UUID,
    group_id: UUID,
    **overrides: Any,
) -> dict[str, Any]:
    base = {
        "id": str(post_id),
        "prompt_id": None,
        "group_id": str(group_id),
        "user_id": str(user_id),
        "kind": "photobooth",
        "media_type": "strip",
        "storage_path": f"groups/{group_id}/posts/{post_id}/media.jpg",
        "thumbnail_path": None,
        "duration_seconds": None,
        "captured_at": "2026-05-30T12:00:00+00:00",
        "is_late": False,
        "visible_at": "2026-05-30T12:00:00+00:00",
        "latitude": None,
        "longitude": None,
        "location_accuracy_meters": None,
        "deleted_at": None,
        "created_at": "2026-05-30T12:00:00+00:00",
    }
    base.update(overrides)
    return base


@pytest.fixture
def fake_sb() -> MagicMock:
    return MagicMock()


@pytest.fixture
def auth_client(fake_sb: MagicMock) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(supabase_jwt_secret=TEST_SECRET)
    app.dependency_overrides[get_supabase] = lambda: fake_sb
    return TestClient(app)


@pytest.fixture(autouse=True)
def _stub_r2(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Patch every R2 call site in services/posts.py.

    Default behaviour: presigned URLs return a stub string, head_object
    returns True. Individual tests override per case.
    """
    put = MagicMock(return_value="https://r2.example/put?sig=abc")
    get = MagicMock(return_value="https://r2.example/get?sig=def")
    head = MagicMock(return_value=True)
    monkeypatch.setattr("app.services.posts.r2.generate_presigned_put_url", put)
    monkeypatch.setattr("app.services.posts.r2.generate_presigned_get_url", get)
    monkeypatch.setattr("app.services.posts.r2.head_object", head)
    return {"put": put, "get": get, "head": head}


# --- POST /posts/upload-url ------------------------------------------


def test_upload_url_happy_path(
    auth_client: TestClient,
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    user_id = uuid4()
    group_id = uuid4()

    # Both .maybe_single() chains in this request walk the same leaf:
    # profile load + membership pre-check. Return profile row first,
    # then a truthy membership row.
    leaf = fake_sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute  # noqa: E501
    profile_call = MagicMock()
    profile_call.data = _profile_row(user_id)
    leaf.return_value = profile_call

    member_call = MagicMock()
    member_call.data = {"id": "membership-row-id"}
    fake_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value = member_call  # noqa: E501

    response = auth_client.post(
        "/posts/upload-url",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={
            "group_id": str(group_id),
            "kind": "photobooth",
            "media_type": "strip",
            "extension": "jpg",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["upload_url"] == "https://r2.example/put?sig=abc"
    assert body["storage_path"].startswith(f"groups/{group_id}/posts/")
    assert body["storage_path"].endswith("/media.jpg")
    assert UUID(body["post_id"])
    # storage_path embeds post_id.
    assert str(body["post_id"]) in body["storage_path"]
    # expires_at ≈ now + 10 min.
    expires = datetime.fromisoformat(body["expires_at"])
    delta = (expires - datetime.now(UTC)).total_seconds()
    assert 540 < delta < 660

    # Content-Type must be image/jpeg for strip; not video.
    _stub_r2["put"].assert_called_once()
    assert _stub_r2["put"].call_args.args[1] == "image/jpeg"


def test_upload_url_uses_mp4_for_video(fake_sb: MagicMock, _stub_r2: dict[str, MagicMock]) -> None:
    user_id = uuid4()
    group_id = uuid4()
    fake_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = {  # noqa: E501
        "id": "membership-row-id"
    }

    posts_service.create_upload_url(
        fake_sb,
        user_id,
        UploadUrlRequest(
            group_id=group_id,
            kind=PostKind.PROMPT,
            media_type=PostMediaType.VIDEO,
            extension="mp4",
        ),
    )
    assert _stub_r2["put"].call_args.args[1] == "video/mp4"


def test_upload_url_non_member_returns_404(
    auth_client: TestClient,
    fake_sb: MagicMock,
) -> None:
    user_id = uuid4()
    group_id = uuid4()

    # Profile load → row; membership pre-check → None.
    leaf_one = fake_sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute  # noqa: E501
    profile_call = MagicMock()
    profile_call.data = _profile_row(user_id)
    leaf_one.return_value = profile_call

    fake_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = None  # noqa: E501

    response = auth_client.post(
        "/posts/upload-url",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={
            "group_id": str(group_id),
            "kind": "photobooth",
            "media_type": "strip",
            "extension": "jpg",
        },
    )
    assert response.status_code == 404, response.text


# --- POST /posts/confirm ----------------------------------------------


def _confirm_payload(post_id: UUID, group_id: UUID, **overrides: Any) -> ConfirmPostRequest:
    base = {
        "post_id": post_id,
        "group_id": group_id,
        "kind": PostKind.PHOTOBOOTH,
        "media_type": PostMediaType.STRIP,
        "storage_path": f"groups/{group_id}/posts/{post_id}/media.jpg",
        "captured_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ConfirmPostRequest(**base)


def _wire_confirm_chains(
    fake_sb: MagicMock,
    *,
    existing_post: dict | None,
    is_member: bool,
    view_delay_seconds: int = 30,
    inserted_row: dict | None = None,
) -> None:
    """Configure the four supabase chains confirm() walks.

    1. posts select by id (idempotency): .table().select().eq().is_().maybe_single().execute()
    2. membership pre-check: .table().select().eq().eq().maybe_single().execute()
    3. groups view_delay select: .table().select().eq().maybe_single().execute()
    4. posts insert: .table().insert().execute()

    All chains share leaves on the MagicMock, so we set side_effect lists.
    """
    # Idempotency leaf — three-eq variant: select.eq.is_.maybe_single.execute
    posts_select = fake_sb.table.return_value.select.return_value.eq.return_value.is_.return_value.maybe_single.return_value.execute  # noqa: E501
    existing_resp = MagicMock()
    existing_resp.data = existing_post
    posts_select.return_value = existing_resp

    # Membership pre-check leaf — two-eq variant.
    member_resp = MagicMock()
    member_resp.data = {"id": "membership-row-id"} if is_member else None
    fake_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value = member_resp  # noqa: E501

    # Group view_delay_seconds leaf — one-eq + maybe_single.
    group_resp = MagicMock()
    group_resp.data = {"view_delay_seconds": view_delay_seconds}
    fake_sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = group_resp  # noqa: E501

    # Insert leaf.
    insert_resp = MagicMock()
    insert_resp.data = [inserted_row] if inserted_row else []
    fake_sb.table.return_value.insert.return_value.execute.return_value = insert_resp


def test_confirm_happy_path_inserts_with_visible_at(
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    _wire_confirm_chains(
        fake_sb,
        existing_post=None,
        is_member=True,
        view_delay_seconds=30,
        inserted_row=_post_row(post_id, user_id, group_id),
    )

    payload = _confirm_payload(post_id, group_id, latitude=52.5, longitude=13.4, accuracy=8.7)
    result = posts_service.confirm(fake_sb, user_id, payload)

    assert result.id == post_id
    assert result.is_late is False

    # Find the insert call args.
    insert_call = fake_sb.table.return_value.insert.call_args
    assert insert_call is not None
    insert_payload = insert_call.args[0]
    assert insert_payload["is_late"] is False
    assert insert_payload["location_accuracy_meters"] == 9  # round(8.7)
    assert insert_payload["latitude"] == 52.5

    visible_at = datetime.fromisoformat(insert_payload["visible_at"])
    delta = (visible_at - datetime.now(UTC)).total_seconds()
    assert 25 < delta < 35  # ≈ now + 30s

    _stub_r2["head"].assert_called_once_with(payload.storage_path)


def test_confirm_is_idempotent_on_same_user(
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()
    existing = _post_row(post_id, user_id, group_id)

    _wire_confirm_chains(fake_sb, existing_post=existing, is_member=True)

    payload = _confirm_payload(post_id, group_id)
    result = posts_service.confirm(fake_sb, user_id, payload)

    assert result.id == post_id
    # Idempotent return must skip head_object and insert entirely.
    _stub_r2["head"].assert_not_called()
    fake_sb.table.return_value.insert.assert_not_called()


def test_confirm_tampered_recall_returns_forbidden(fake_sb: MagicMock) -> None:
    real_owner = uuid4()
    attacker = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    existing = _post_row(post_id, real_owner, group_id)
    _wire_confirm_chains(fake_sb, existing_post=existing, is_member=True)

    payload = _confirm_payload(post_id, group_id)
    with pytest.raises(posts_service.PostNotAccessibleError):
        posts_service.confirm(fake_sb, attacker, payload)


def test_confirm_missing_object_raises_422(
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    _wire_confirm_chains(fake_sb, existing_post=None, is_member=True)
    _stub_r2["head"].return_value = False

    payload = _confirm_payload(post_id, group_id)
    with pytest.raises(posts_service.MediaObjectMissingError):
        posts_service.confirm(fake_sb, user_id, payload)

    fake_sb.table.return_value.insert.assert_not_called()


def test_confirm_storage_path_mismatch_skips_head(
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    user_id = uuid4()
    group_id = uuid4()
    other_group = uuid4()
    post_id = uuid4()

    _wire_confirm_chains(fake_sb, existing_post=None, is_member=True)

    payload = _confirm_payload(
        post_id,
        group_id,
        storage_path=f"groups/{other_group}/posts/{post_id}/media.jpg",
    )
    with pytest.raises(posts_service.StoragePathMismatchError):
        posts_service.confirm(fake_sb, user_id, payload)

    _stub_r2["head"].assert_not_called()
    fake_sb.table.return_value.insert.assert_not_called()


def test_confirm_non_member_raises_404(
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    _wire_confirm_chains(fake_sb, existing_post=None, is_member=False)

    payload = _confirm_payload(post_id, group_id)
    with pytest.raises(posts_service.GroupNotFoundError):
        posts_service.confirm(fake_sb, user_id, payload)


def test_confirm_unique_violation_falls_back_to_existing(
    fake_sb: MagicMock,
) -> None:
    """A concurrent confirm race: insert hits 23505, we re-select and return."""
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    _wire_confirm_chains(fake_sb, existing_post=None, is_member=True)

    # First insert raises; the racing-row fetch happens via the same posts
    # select leaf. Override that leaf to return existing on the second call.
    posts_select = fake_sb.table.return_value.select.return_value.eq.return_value.is_.return_value.maybe_single.return_value.execute  # noqa: E501
    first = MagicMock()
    first.data = None
    racing = MagicMock()
    racing.data = _post_row(post_id, user_id, group_id)
    posts_select.side_effect = [first, racing]

    fake_sb.table.return_value.insert.return_value.execute.side_effect = APIError({"code": "23505"})

    payload = _confirm_payload(post_id, group_id)
    result = posts_service.confirm(fake_sb, user_id, payload)
    assert result.id == post_id


# --- GET /posts/{id} -------------------------------------------------


def test_get_post_member_returns_signed_url(
    auth_client: TestClient,
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    # Profile load (one-eq chain) returns the user; post select (one-eq + is_)
    # returns the post; membership pre-check (two-eq) returns truthy.
    profile_call = MagicMock()
    profile_call.data = _profile_row(user_id)
    fake_sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = profile_call  # noqa: E501

    post_call = MagicMock()
    post_call.data = _post_row(post_id, user_id, group_id)
    fake_sb.table.return_value.select.return_value.eq.return_value.is_.return_value.maybe_single.return_value.execute.return_value = post_call  # noqa: E501

    member_call = MagicMock()
    member_call.data = {"id": "membership-row-id"}
    fake_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value = member_call  # noqa: E501

    response = auth_client.get(
        f"/posts/{post_id}",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(post_id)
    assert body["media_url"] == "https://r2.example/get?sig=def"
    _stub_r2["get"].assert_called_once()


def test_get_post_not_found_returns_404(fake_sb: MagicMock) -> None:
    post_call = MagicMock()
    post_call.data = None
    fake_sb.table.return_value.select.return_value.eq.return_value.is_.return_value.maybe_single.return_value.execute.return_value = post_call  # noqa: E501

    with pytest.raises(posts_service.PostNotFoundError):
        posts_service.get_post(fake_sb, uuid4(), uuid4())


def test_get_post_non_member_returns_403(fake_sb: MagicMock) -> None:
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    post_call = MagicMock()
    post_call.data = _post_row(post_id, uuid4(), group_id)
    fake_sb.table.return_value.select.return_value.eq.return_value.is_.return_value.maybe_single.return_value.execute.return_value = post_call  # noqa: E501

    member_call = MagicMock()
    member_call.data = None
    fake_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value = member_call  # noqa: E501

    with pytest.raises(posts_service.PostNotAccessibleError):
        posts_service.get_post(fake_sb, user_id, post_id)
