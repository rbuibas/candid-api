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
    delete = MagicMock(return_value=None)
    monkeypatch.setattr("app.services.posts.r2.generate_presigned_put_url", put)
    monkeypatch.setattr("app.services.posts.r2.generate_presigned_get_url", get)
    monkeypatch.setattr("app.services.posts.r2.head_object", head)
    monkeypatch.setattr("app.services.posts.r2.delete_object", delete)
    return {"put": put, "get": get, "head": head, "delete": delete}


# --- POST /posts/upload-url ------------------------------------------


def test_upload_url_happy_path(
    auth_client: TestClient,
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    user_id = uuid4()
    group_id = uuid4()

    # Two .maybe_single() chains: the two-eq membership pre-check, and the
    # one-eq group lifecycle fetch (_load_unlocked_group). Wire both — the
    # group must be active so upload-url proceeds.
    member_call = MagicMock()
    member_call.data = {"id": "membership-row-id"}
    fake_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value = member_call  # noqa: E501

    group_call = MagicMock()
    group_call.data = _unlocked_group_data()
    fake_sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = group_call  # noqa: E501

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
    # Group lifecycle fetch (one-eq + maybe_single) — active group.
    group_call = MagicMock()
    group_call.data = _unlocked_group_data()
    fake_sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = group_call  # noqa: E501

    response = posts_service.create_upload_url(
        fake_sb,
        user_id,
        UploadUrlRequest(
            group_id=group_id,
            kind=PostKind.PROMPT,
            media_type=PostMediaType.VIDEO,
            extension="mp4",
        ),
    )
    # Video mints two PUT slots: the mp4 media first, then a JPEG poster frame.
    calls = _stub_r2["put"].call_args_list
    assert len(calls) == 2
    assert calls[0].args[1] == "video/mp4"
    assert calls[1].args[1] == "image/jpeg"
    assert calls[1].args[0].endswith("/thumbnail.jpg")
    assert response.thumbnail_storage_path is not None
    assert response.thumbnail_storage_path.endswith("/thumbnail.jpg")
    assert response.thumbnail_upload_url is not None


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


def _unlocked_group_data(view_delay_seconds: int = 30, *, locked: bool = False) -> dict[str, Any]:
    """The groups row _load_unlocked_group() reads: view_delay + lifecycle dates.

    Far-past start and far-future end keep the group `active` regardless of
    when the suite runs; `locked=True` pushes end_date into the real past.
    """
    return {
        "view_delay_seconds": view_delay_seconds,
        "start_date": "2024-01-01",
        "end_date": "2024-06-01" if locked else "2099-12-31",
    }


def _wire_confirm_chains(
    fake_sb: MagicMock,
    *,
    existing_post: dict | None,
    is_member: bool,
    view_delay_seconds: int = 30,
    inserted_row: dict | None = None,
    locked: bool = False,
) -> None:
    """Configure the four supabase chains confirm() walks.

    1. posts select by id (idempotency): .table().select().eq().is_().maybe_single().execute()
    2. membership pre-check: .table().select().eq().eq().maybe_single().execute()
    3. groups lifecycle+view_delay select: .table().select().eq().maybe_single().execute()
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

    # Group lifecycle + view_delay_seconds leaf — one-eq + maybe_single.
    group_resp = MagicMock()
    group_resp.data = _unlocked_group_data(view_delay_seconds, locked=locked)
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

    # A strip is its own poster — no thumbnail probe, no thumbnail_path.
    assert insert_payload["thumbnail_path"] is None
    _stub_r2["head"].assert_called_once_with(payload.storage_path)


def test_confirm_video_attaches_poster_when_present(
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    """A video confirm probes the canonical thumbnail key and attaches it.

    The poster probe keys off media_type, not kind, so a photobooth video
    exercises the branch without the prompt-window wiring a kind=prompt confirm
    would need.
    """
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    _wire_confirm_chains(
        fake_sb,
        existing_post=None,
        is_member=True,
        inserted_row=_post_row(post_id, user_id, group_id),
    )

    payload = _confirm_payload(
        post_id,
        group_id,
        media_type=PostMediaType.VIDEO,
        storage_path=f"groups/{group_id}/posts/{post_id}/media.mp4",
    )
    posts_service.confirm(fake_sb, user_id, payload)

    insert_payload = fake_sb.table.return_value.insert.call_args.args[0]
    assert insert_payload["thumbnail_path"] == f"groups/{group_id}/posts/{post_id}/thumbnail.jpg"
    # Two HEADs: the media object, then the poster.
    assert _stub_r2["head"].call_count == 2


def test_confirm_video_no_poster_when_missing(
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    """A missing poster never blocks the post — thumbnail_path stays None."""
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    _wire_confirm_chains(
        fake_sb,
        existing_post=None,
        is_member=True,
        inserted_row=_post_row(post_id, user_id, group_id),
    )
    # Media exists, poster doesn't.
    _stub_r2["head"].side_effect = lambda path: not path.endswith("thumbnail.jpg")

    payload = _confirm_payload(
        post_id,
        group_id,
        media_type=PostMediaType.VIDEO,
        storage_path=f"groups/{group_id}/posts/{post_id}/media.mp4",
    )
    posts_service.confirm(fake_sb, user_id, payload)

    insert_payload = fake_sb.table.return_value.insert.call_args.args[0]
    assert insert_payload["thumbnail_path"] is None


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


# --- Phase 6: locked-group write gate --------------------------------


def test_upload_url_locked_group_returns_409_group_locked(
    auth_client: TestClient,
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    user_id = uuid4()
    group_id = uuid4()

    # Membership passes; the group lifecycle fetch reports locked.
    member_call = MagicMock()
    member_call.data = {"id": "membership-row-id"}
    fake_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value = member_call  # noqa: E501

    group_call = MagicMock()
    group_call.data = _unlocked_group_data(locked=True)
    fake_sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = group_call  # noqa: E501

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
    assert response.status_code == 409, response.text
    assert response.json() == {"error": "group_locked"}
    # No presigned URL minted for a locked group.
    _stub_r2["put"].assert_not_called()


def test_upload_url_locked_group_service_raises(
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    user_id = uuid4()
    group_id = uuid4()

    member_call = MagicMock()
    member_call.data = {"id": "membership-row-id"}
    fake_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value = member_call  # noqa: E501
    group_call = MagicMock()
    group_call.data = _unlocked_group_data(locked=True)
    fake_sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value = group_call  # noqa: E501

    with pytest.raises(posts_service.GroupLockedError):
        posts_service.create_upload_url(
            fake_sb,
            user_id,
            UploadUrlRequest(
                group_id=group_id,
                kind=PostKind.PHOTOBOOTH,
                media_type=PostMediaType.STRIP,
                extension="jpg",
            ),
        )
    _stub_r2["put"].assert_not_called()


def test_confirm_locked_group_returns_409_group_locked(
    auth_client: TestClient,
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    _wire_confirm_chains(fake_sb, existing_post=None, is_member=True, locked=True)

    response = auth_client.post(
        "/posts/confirm",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={
            "post_id": str(post_id),
            "group_id": str(group_id),
            "kind": "photobooth",
            "media_type": "strip",
            "storage_path": f"groups/{group_id}/posts/{post_id}/media.jpg",
            "captured_at": datetime.now(UTC).isoformat(),
        },
    )
    assert response.status_code == 409, response.text
    assert response.json() == {"error": "group_locked"}
    # Lock gate precedes the R2 head + insert.
    _stub_r2["head"].assert_not_called()
    fake_sb.table.return_value.insert.assert_not_called()


def test_confirm_locked_group_photobooth_service_raises(
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    _wire_confirm_chains(fake_sb, existing_post=None, is_member=True, locked=True)

    payload = _confirm_payload(post_id, group_id)
    with pytest.raises(posts_service.GroupLockedError):
        posts_service.confirm(fake_sb, user_id, payload)

    _stub_r2["head"].assert_not_called()
    fake_sb.table.return_value.insert.assert_not_called()


def test_confirm_locked_group_prompt_kind_raises_before_prompt_check(
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    """For kind=prompt the lock gate (step 3) precedes prompt-window
    enforcement (step 6), so a locked group surfaces as GroupLockedError, not
    a stale-prompt 409. The prompt lookup is never reached."""
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    _wire_confirm_chains(fake_sb, existing_post=None, is_member=True, locked=True)

    payload = _confirm_payload(
        post_id,
        group_id,
        kind=PostKind.PROMPT,
        media_type=PostMediaType.PHOTO,
        prompt_id=uuid4(),
    )
    with pytest.raises(posts_service.GroupLockedError):
        posts_service.confirm(fake_sb, user_id, payload)

    _stub_r2["head"].assert_not_called()
    fake_sb.table.return_value.insert.assert_not_called()


def test_confirm_active_group_inserts(
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    """Counterpart to the locked case: an active group confirms normally."""
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    _wire_confirm_chains(
        fake_sb,
        existing_post=None,
        is_member=True,
        locked=False,
        inserted_row=_post_row(post_id, user_id, group_id),
    )

    result = posts_service.confirm(fake_sb, user_id, _confirm_payload(post_id, group_id))
    assert result.id == post_id
    fake_sb.table.return_value.insert.assert_called_once()


# --- Phase 6: offline captured_at preserved ---------------------------


def test_confirm_stores_client_captured_at_not_server_time(
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    """captured_at in the DB insert is the client-supplied value, not the
    server-receipt time — so a photo taken offline 45 min before reconnect
    still carries the original capture moment."""
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    offline_captured_at = datetime.now(UTC) - timedelta(minutes=45)

    _wire_confirm_chains(
        fake_sb,
        existing_post=None,
        is_member=True,
        inserted_row=_post_row(post_id, user_id, group_id),
    )

    posts_service.confirm(fake_sb, user_id, _confirm_payload(post_id, group_id, captured_at=offline_captured_at))

    insert_payload = fake_sb.table.return_value.insert.call_args.args[0]
    stored = datetime.fromisoformat(insert_payload["captured_at"])
    # Must match client-provided time, not server now (~45 min later).
    assert abs((stored - offline_captured_at).total_seconds()) < 1


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


# --- DELETE /posts/{id} ----------------------------------------------


def _wire_delete_chains(
    fake_sb: MagicMock,
    *,
    existing_post: dict | None,
    update_rows: list | None,
) -> None:
    """Configure the two posts chains delete_post() walks.

    1. _select_post (deleted_at IS NULL):
         .table().select().eq().is_().maybe_single().execute()
    2. tombstone UPDATE (guarded by deleted_at IS NULL):
         .table().update().eq().is_().execute()
    """
    select_leaf = fake_sb.table.return_value.select.return_value.eq.return_value.is_.return_value.maybe_single.return_value.execute  # noqa: E501
    sel = MagicMock()
    sel.data = existing_post
    select_leaf.return_value = sel

    upd = MagicMock()
    upd.data = update_rows
    fake_sb.table.return_value.update.return_value.eq.return_value.is_.return_value.execute.return_value = upd  # noqa: E501


def test_delete_post_happy_path_tombstones_and_purges_r2(
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    row = _post_row(
        post_id,
        user_id,
        group_id,
        thumbnail_path=f"groups/{group_id}/posts/{post_id}/thumb.jpg",
    )
    _wire_delete_chains(fake_sb, existing_post=row, update_rows=[row])

    posts_service.delete_post(fake_sb, user_id, post_id)

    # Tombstone UPDATE sets deleted_at.
    update_call = fake_sb.table.return_value.update.call_args
    assert update_call is not None
    assert "deleted_at" in update_call.args[0]

    # Both R2 objects hard-deleted (media + thumbnail).
    deleted_paths = {c.args[0] for c in _stub_r2["delete"].call_args_list}
    assert deleted_paths == {row["storage_path"], row["thumbnail_path"]}


def test_delete_post_without_thumbnail_only_deletes_media(
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    row = _post_row(post_id, user_id, group_id, thumbnail_path=None)
    _wire_delete_chains(fake_sb, existing_post=row, update_rows=[row])

    posts_service.delete_post(fake_sb, user_id, post_id)

    _stub_r2["delete"].assert_called_once_with(row["storage_path"])


def test_delete_post_non_author_raises_forbidden(
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    real_owner = uuid4()
    attacker = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    row = _post_row(post_id, real_owner, group_id)
    _wire_delete_chains(fake_sb, existing_post=row, update_rows=[row])

    with pytest.raises(posts_service.PostNotAccessibleError):
        posts_service.delete_post(fake_sb, attacker, post_id)

    # Neither tombstone nor R2 delete should fire for a non-author.
    fake_sb.table.return_value.update.assert_not_called()
    _stub_r2["delete"].assert_not_called()


def test_delete_post_already_deleted_raises_not_found(
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    # _select_post filters deleted_at IS NULL, so a tombstoned/absent post
    # comes back as None → 404.
    _wire_delete_chains(fake_sb, existing_post=None, update_rows=None)

    with pytest.raises(posts_service.PostNotFoundError):
        posts_service.delete_post(fake_sb, uuid4(), uuid4())

    fake_sb.table.return_value.update.assert_not_called()
    _stub_r2["delete"].assert_not_called()


def test_delete_post_lost_race_raises_not_found(
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    # Row was visible at select time but a concurrent delete tombstoned it
    # before our guarded UPDATE → 0 rows affected → 404, no R2 purge.
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    row = _post_row(post_id, user_id, group_id)
    _wire_delete_chains(fake_sb, existing_post=row, update_rows=[])

    with pytest.raises(posts_service.PostNotFoundError):
        posts_service.delete_post(fake_sb, user_id, post_id)

    _stub_r2["delete"].assert_not_called()


def test_delete_post_route_returns_204(
    auth_client: TestClient,
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    row = _post_row(post_id, user_id, group_id)
    _wire_delete_chains(fake_sb, existing_post=row, update_rows=[row])

    response = auth_client.delete(
        f"/posts/{post_id}",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
    )
    assert response.status_code == 204, response.text
    assert response.content == b""


def test_delete_post_route_non_author_returns_403(
    auth_client: TestClient,
    fake_sb: MagicMock,
    _stub_r2: dict[str, MagicMock],
) -> None:
    attacker = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    row = _post_row(post_id, uuid4(), group_id)
    _wire_delete_chains(fake_sb, existing_post=row, update_rows=[row])

    response = auth_client.delete(
        f"/posts/{post_id}",
        headers={"Authorization": f"Bearer {_mint(attacker)}"},
    )
    assert response.status_code == 403, response.text
