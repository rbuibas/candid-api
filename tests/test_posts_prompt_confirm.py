"""Unit tests for kind=prompt confirm: lateness boundaries + prompt status flip.

Pattern follows tests/test_posts.py: MagicMock chains on the supabase client
plus monkeypatched R2 helpers. The two shared `.eq().maybe_single()` chain
leaves (prompts-by-id lookup and groups-by-id lookup) are driven via
`side_effect` lists so the order of calls in confirm() matters.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from app.models.post import ConfirmPostRequest, PostKind, PostMediaType
from app.services import posts as posts_service

RWS = 300  # response window seconds
LWS = 1800  # late window seconds


def _post_row(post_id: UUID, user_id: UUID, group_id: UUID, **overrides: Any) -> dict[str, Any]:
    base = {
        "id": str(post_id),
        "prompt_id": None,
        "group_id": str(group_id),
        "user_id": str(user_id),
        "kind": "prompt",
        "media_type": "photo",
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


def _prompt_row(
    prompt_id: UUID,
    user_id: UUID,
    group_id: UUID,
    *,
    dispatched_offset_seconds: int = 0,
    status: str = "active",
    rws: int = RWS,
    lws: int = LWS,
) -> dict[str, Any]:
    dispatched_at = datetime.now(UTC) + timedelta(seconds=dispatched_offset_seconds)
    return {
        "id": str(prompt_id),
        "group_id": str(group_id),
        "user_id": str(user_id),
        "scheduled_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        "dispatched_at": dispatched_at.isoformat(),
        "local_date": "2026-05-30",
        "media_type": "photo",
        "target_video_length_seconds": None,
        "status": status,
        "created_at": "2026-05-30T00:00:00+00:00",
        "groups": {
            "response_window_seconds": rws,
            "late_window_seconds": lws,
        },
    }


def _confirm_payload(
    post_id: UUID,
    group_id: UUID,
    prompt_id: UUID | None,
    **overrides: Any,
) -> ConfirmPostRequest:
    base = {
        "post_id": post_id,
        "group_id": group_id,
        "kind": PostKind.PROMPT,
        "media_type": PostMediaType.PHOTO,
        "prompt_id": prompt_id,
        "storage_path": f"groups/{group_id}/posts/{post_id}/media.jpg",
        "captured_at": datetime.now(UTC),
    }
    base.update(overrides)
    return ConfirmPostRequest(**base)


@pytest.fixture
def fake_sb() -> MagicMock:
    return MagicMock()


@pytest.fixture(autouse=True)
def _stub_r2(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    put = MagicMock(return_value="https://r2.example/put?sig=abc")
    get = MagicMock(return_value="https://r2.example/get?sig=def")
    head = MagicMock(return_value=True)
    monkeypatch.setattr("app.services.posts.r2.generate_presigned_put_url", put)
    monkeypatch.setattr("app.services.posts.r2.generate_presigned_get_url", get)
    monkeypatch.setattr("app.services.posts.r2.head_object", head)
    return {"put": put, "get": get, "head": head}


def _wire_prompt_confirm_chains(
    fake_sb: MagicMock,
    *,
    existing_post: dict | None,
    is_member: bool,
    prompt_row: dict | None,
    view_delay_seconds: int = 30,
    inserted_row: dict | None = None,
    prompt_update_raises: Exception | None = None,
) -> MagicMock:
    """Configure all chains confirm() walks for a kind=prompt confirm.

    Two distinct chains share leaves on the MagicMock — both use the
    single-eq + maybe_single shape:
      1. _load_active_prompt: prompts-by-id (joined with groups)
      2. group_row: groups-by-id for view_delay_seconds
    These are sequenced via `side_effect` in call order.

    Returns the prompt-update leaf so tests can inspect the new status arg.
    """
    # Posts select (idempotency) — three-eq variant.
    posts_select = fake_sb.table.return_value.select.return_value.eq.return_value.is_.return_value.maybe_single.return_value.execute  # noqa: E501
    existing_resp = MagicMock()
    existing_resp.data = existing_post
    posts_select.return_value = existing_resp

    # Membership pre-check — two-eq variant.
    member_resp = MagicMock()
    member_resp.data = {"id": "membership-row-id"} if is_member else None
    fake_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value = member_resp  # noqa: E501

    # Shared single-eq + maybe_single leaf, sequenced:
    #   1st call → prompt lookup
    #   2nd call → group view_delay lookup
    prompt_resp = MagicMock()
    prompt_resp.data = prompt_row
    group_resp = MagicMock()
    group_resp.data = {"view_delay_seconds": view_delay_seconds}
    shared = fake_sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute  # noqa: E501
    shared.side_effect = [prompt_resp, group_resp]

    # Insert leaf.
    insert_resp = MagicMock()
    insert_resp.data = [inserted_row] if inserted_row else []
    fake_sb.table.return_value.insert.return_value.execute.return_value = insert_resp

    # Prompt update leaf.
    update_leaf = fake_sb.table.return_value.update.return_value.eq.return_value.execute
    if prompt_update_raises is not None:
        update_leaf.side_effect = prompt_update_raises
    return update_leaf


# --- On-time boundary -------------------------------------------------


def test_confirm_prompt_on_time_marks_responded(fake_sb: MagicMock) -> None:
    user_id = uuid4()
    group_id = uuid4()
    prompt_id = uuid4()
    post_id = uuid4()

    update_leaf = _wire_prompt_confirm_chains(
        fake_sb,
        existing_post=None,
        is_member=True,
        prompt_row=_prompt_row(prompt_id, user_id, group_id, dispatched_offset_seconds=-10),
        inserted_row=_post_row(post_id, user_id, group_id, prompt_id=str(prompt_id)),
    )

    payload = _confirm_payload(post_id, group_id, prompt_id)
    result = posts_service.confirm(fake_sb, user_id, payload)
    assert result.is_late is False

    insert_call = fake_sb.table.return_value.insert.call_args
    assert insert_call.args[0]["is_late"] is False
    assert insert_call.args[0]["prompt_id"] == str(prompt_id)

    # Status flip: 'responded' for an on-time confirm.
    assert update_leaf.called
    update_call = fake_sb.table.return_value.update.call_args
    assert update_call.args[0] == {"status": "responded"}


def test_confirm_prompt_at_on_time_deadline_is_responded(fake_sb: MagicMock) -> None:
    user_id = uuid4()
    group_id = uuid4()
    prompt_id = uuid4()
    post_id = uuid4()

    # Receipt at on_time_deadline (== dispatched + rws) — boundary is inclusive.
    _wire_prompt_confirm_chains(
        fake_sb,
        existing_post=None,
        is_member=True,
        prompt_row=_prompt_row(prompt_id, user_id, group_id, dispatched_offset_seconds=-RWS),
        inserted_row=_post_row(post_id, user_id, group_id, prompt_id=str(prompt_id)),
    )

    payload = _confirm_payload(post_id, group_id, prompt_id)
    result = posts_service.confirm(fake_sb, user_id, payload)
    assert result.is_late is False


def test_confirm_prompt_past_on_time_is_late(fake_sb: MagicMock) -> None:
    user_id = uuid4()
    group_id = uuid4()
    prompt_id = uuid4()
    post_id = uuid4()

    # Dispatched 10 minutes ago: past on-time (5min) but within late_window (30min).
    update_leaf = _wire_prompt_confirm_chains(
        fake_sb,
        existing_post=None,
        is_member=True,
        prompt_row=_prompt_row(prompt_id, user_id, group_id, dispatched_offset_seconds=-600),
        inserted_row=_post_row(post_id, user_id, group_id, prompt_id=str(prompt_id), is_late=True),
    )

    payload = _confirm_payload(post_id, group_id, prompt_id)
    result = posts_service.confirm(fake_sb, user_id, payload)
    assert result.is_late is True

    insert_call = fake_sb.table.return_value.insert.call_args
    assert insert_call.args[0]["is_late"] is True

    assert update_leaf.called
    update_call = fake_sb.table.return_value.update.call_args
    assert update_call.args[0] == {"status": "late"}


def test_confirm_prompt_just_before_late_deadline_inserts_as_late(
    fake_sb: MagicMock,
) -> None:
    user_id = uuid4()
    group_id = uuid4()
    prompt_id = uuid4()
    post_id = uuid4()

    # Receipt ~1s inside late_deadline. (Avoids the microsecond race that an
    # exact-boundary check would have: dispatched_at is constructed at fixture
    # time, and now() advances before the service's now() reads.)
    _wire_prompt_confirm_chains(
        fake_sb,
        existing_post=None,
        is_member=True,
        prompt_row=_prompt_row(
            prompt_id, user_id, group_id, dispatched_offset_seconds=-(RWS + LWS - 1)
        ),
        inserted_row=_post_row(post_id, user_id, group_id, prompt_id=str(prompt_id), is_late=True),
    )

    payload = _confirm_payload(post_id, group_id, prompt_id)
    result = posts_service.confirm(fake_sb, user_id, payload)
    assert result.is_late is True


def test_confirm_prompt_past_late_deadline_raises_410(fake_sb: MagicMock) -> None:
    user_id = uuid4()
    group_id = uuid4()
    prompt_id = uuid4()
    post_id = uuid4()

    # Receipt clearly past late_deadline.
    update_leaf = _wire_prompt_confirm_chains(
        fake_sb,
        existing_post=None,
        is_member=True,
        prompt_row=_prompt_row(
            prompt_id, user_id, group_id, dispatched_offset_seconds=-(RWS + LWS + 60)
        ),
    )

    payload = _confirm_payload(post_id, group_id, prompt_id)
    with pytest.raises(posts_service.PromptExpiredError):
        posts_service.confirm(fake_sb, user_id, payload)

    # No insert, no status flip — expirer will mark missed.
    fake_sb.table.return_value.insert.assert_not_called()
    assert not update_leaf.called


# --- Validation errors -----------------------------------------------


def test_confirm_prompt_missing_prompt_id_raises(fake_sb: MagicMock) -> None:
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    _wire_prompt_confirm_chains(fake_sb, existing_post=None, is_member=True, prompt_row=None)

    payload = _confirm_payload(post_id, group_id, prompt_id=None)
    with pytest.raises(posts_service.PromptIdRequiredError):
        posts_service.confirm(fake_sb, user_id, payload)

    fake_sb.table.return_value.insert.assert_not_called()


def test_confirm_prompt_other_users_prompt_raises_forbidden(fake_sb: MagicMock) -> None:
    caller = uuid4()
    other = uuid4()
    group_id = uuid4()
    prompt_id = uuid4()
    post_id = uuid4()

    _wire_prompt_confirm_chains(
        fake_sb,
        existing_post=None,
        is_member=True,
        prompt_row=_prompt_row(prompt_id, other, group_id, dispatched_offset_seconds=-10),
    )

    payload = _confirm_payload(post_id, group_id, prompt_id)
    with pytest.raises(posts_service.PromptNotAccessibleError):
        posts_service.confirm(fake_sb, caller, payload)


def test_confirm_prompt_status_not_active_raises_409(fake_sb: MagicMock) -> None:
    user_id = uuid4()
    group_id = uuid4()
    prompt_id = uuid4()
    post_id = uuid4()

    _wire_prompt_confirm_chains(
        fake_sb,
        existing_post=None,
        is_member=True,
        prompt_row=_prompt_row(
            prompt_id, user_id, group_id, dispatched_offset_seconds=-10, status="responded"
        ),
    )

    payload = _confirm_payload(post_id, group_id, prompt_id)
    with pytest.raises(posts_service.PromptNotActiveError):
        posts_service.confirm(fake_sb, user_id, payload)


def test_confirm_prompt_not_found_raises_forbidden(fake_sb: MagicMock) -> None:
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()

    _wire_prompt_confirm_chains(fake_sb, existing_post=None, is_member=True, prompt_row=None)

    payload = _confirm_payload(post_id, group_id, prompt_id=uuid4())
    with pytest.raises(posts_service.PromptNotAccessibleError):
        posts_service.confirm(fake_sb, user_id, payload)


# --- Idempotency and status flip resilience -------------------------


def test_confirm_prompt_idempotent_does_not_reflip_status(fake_sb: MagicMock) -> None:
    user_id = uuid4()
    group_id = uuid4()
    prompt_id = uuid4()
    post_id = uuid4()

    existing = _post_row(post_id, user_id, group_id, prompt_id=str(prompt_id), is_late=False)
    _wire_prompt_confirm_chains(fake_sb, existing_post=existing, is_member=True, prompt_row=None)

    payload = _confirm_payload(post_id, group_id, prompt_id)
    result = posts_service.confirm(fake_sb, user_id, payload)
    assert result.id == post_id

    # No insert, no status update — idempotent re-confirm.
    fake_sb.table.return_value.insert.assert_not_called()
    fake_sb.table.return_value.update.assert_not_called()


def test_confirm_prompt_status_flip_failure_does_not_undo_post(
    fake_sb: MagicMock,
) -> None:
    user_id = uuid4()
    group_id = uuid4()
    prompt_id = uuid4()
    post_id = uuid4()

    _wire_prompt_confirm_chains(
        fake_sb,
        existing_post=None,
        is_member=True,
        prompt_row=_prompt_row(prompt_id, user_id, group_id, dispatched_offset_seconds=-10),
        inserted_row=_post_row(post_id, user_id, group_id, prompt_id=str(prompt_id)),
        prompt_update_raises=RuntimeError("postgrest down"),
    )

    payload = _confirm_payload(post_id, group_id, prompt_id)
    result = posts_service.confirm(fake_sb, user_id, payload)
    assert result.id == post_id
    # The post still made it in, even though the status flip failed.
