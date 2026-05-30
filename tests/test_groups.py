"""Unit tests for /groups endpoints — hermetic, mirrors tests/test_profile.py.

Each test configures only the supabase MagicMock chains it needs. The
`get_current_user` dep also fetches the caller's profile, so most tests
stub the select chain to a profile row first; the per-test calls that
matter override `.return_value.data` for their own chain.

The MagicMock auto-creates the chained attributes — we only set `.data`
at the leaf for whichever path the code-under-test will walk.
"""

from datetime import UTC, date, datetime, timedelta
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
from app.services import groups as groups_service
from app.services import invites as invites_service
from app.services import members as members_service

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


def _group_row(
    group_id: UUID,
    creator_id: UUID,
    *,
    name: str = "Bach Weekend",
    start_date: str = "2026-06-01",
    end_date: str = "2026-06-03",
    **overrides: Any,
) -> dict[str, Any]:
    base = {
        "id": str(group_id),
        "name": name,
        "created_by": str(creator_id),
        "start_date": start_date,
        "end_date": end_date,
        "prompts_per_day": 4,
        "daily_window_start": "10:00:00",
        "daily_window_end": "01:00:00",
        "min_prompt_gap_minutes": 45,
        "response_window_seconds": 300,
        "late_window_seconds": 1800,
        "max_video_length_seconds": 10,
        "view_delay_seconds": 0,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def _stub_profile_select(fake_sb: MagicMock, row: dict[str, Any]) -> None:
    """Configure the get_current_user → profile load chain."""
    fake_sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = row  # noqa: E501


@pytest.fixture
def fake_sb() -> MagicMock:
    return MagicMock()


@pytest.fixture
def auth_client(fake_sb: MagicMock) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(supabase_jwt_secret=TEST_SECRET)
    app.dependency_overrides[get_supabase] = lambda: fake_sb
    return TestClient(app)


# --- compute_lifecycle ------------------------------------------------


def test_lifecycle_upcoming() -> None:
    today = datetime.now(UTC).date()
    assert (
        groups_service.compute_lifecycle(today + timedelta(days=1), today + timedelta(days=2))
        == "upcoming"
    )


def test_lifecycle_active_inclusive_start() -> None:
    today = datetime.now(UTC).date()
    assert groups_service.compute_lifecycle(today, today + timedelta(days=2)) == "active"


def test_lifecycle_active_inclusive_end() -> None:
    today = datetime.now(UTC).date()
    assert groups_service.compute_lifecycle(today - timedelta(days=2), today) == "active"


def test_lifecycle_locked_day_after_end() -> None:
    today = datetime.now(UTC).date()
    assert (
        groups_service.compute_lifecycle(today - timedelta(days=3), today - timedelta(days=1))
        == "locked"
    )


# --- invite code generator -------------------------------------------


def test_invite_code_shape() -> None:
    alphabet = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
    for _ in range(50):
        code = invites_service._generate_code()
        assert len(code) == 6
        assert set(code) <= alphabet


def test_invite_create_for_group_retries_on_collision() -> None:
    sb = MagicMock()
    insert_chain = sb.table.return_value.insert.return_value.execute
    insert_chain.side_effect = [APIError({"code": "23505"}), MagicMock()]

    code = invites_service.create_for_group(sb, "group-id")

    assert len(code) == 6
    assert insert_chain.call_count == 2


def test_invite_create_for_group_raises_after_max_retries() -> None:
    sb = MagicMock()
    sb.table.return_value.insert.return_value.execute.side_effect = APIError({"code": "23505"})

    with pytest.raises(invites_service.InviteCodeGenerationError):
        invites_service.create_for_group(sb, "group-id", max_retries=3)


# --- POST /groups ----------------------------------------------------


def test_create_group_happy_path(
    auth_client: TestClient,
    fake_sb: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    group_id = uuid4()

    _stub_profile_select(fake_sb, _profile_row(user_id))
    fake_sb.table.return_value.insert.return_value.execute.return_value.data = [
        _group_row(group_id, user_id)
    ]
    monkeypatch.setattr(invites_service, "create_for_group", lambda *_a, **_k: "K3J9PQ")

    response = auth_client.post(
        "/groups",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"name": "Bach Weekend", "start_date": "2026-06-01", "end_date": "2026-06-03"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["invite_code"] == "K3J9PQ"
    assert body["group"]["id"] == str(group_id)
    assert body["group"]["lifecycle"] in {"upcoming", "active", "locked"}


def test_create_group_applies_settings_overrides(
    fake_sb: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    group_id = uuid4()
    fake_sb.table.return_value.insert.return_value.execute.return_value.data = [
        _group_row(group_id, user_id, prompts_per_day=6)
    ]
    monkeypatch.setattr(invites_service, "create_for_group", lambda *_a, **_k: "ABCDEF")

    from app.models.group import GroupCreate, GroupSettings

    groups_service.create(
        fake_sb,
        user_id,
        GroupCreate(
            name="Bach",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 3),
            settings=GroupSettings(prompts_per_day=6),
        ),
    )

    # First insert is into `groups`; second is `group_members`. Find the
    # groups one by looking for the `name` key.
    insert_calls = [c.args[0] for c in fake_sb.table.return_value.insert.call_args_list]
    groups_insert = next(c for c in insert_calls if "name" in c)
    # Caller-set field is forwarded; unset fields stay out of the payload so
    # the DB default wins.
    assert groups_insert["prompts_per_day"] == 6
    assert "min_prompt_gap_minutes" not in groups_insert


def test_create_group_rolls_back_on_invite_failure(
    fake_sb: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    group_id = uuid4()
    fake_sb.table.return_value.insert.return_value.execute.return_value.data = [
        _group_row(group_id, user_id)
    ]

    def _boom(*_a: Any, **_k: Any) -> str:
        raise invites_service.InviteCodeGenerationError("nope")

    monkeypatch.setattr(invites_service, "create_for_group", _boom)

    from app.models.group import GroupCreate

    with pytest.raises(invites_service.InviteCodeGenerationError):
        groups_service.create(
            fake_sb,
            user_id,
            GroupCreate(name="Bach", start_date=date(2026, 6, 1), end_date=date(2026, 6, 3)),
        )

    # Best-effort rollback called the delete chain on the groups table.
    fake_sb.table.return_value.delete.return_value.eq.assert_called_with("id", str(group_id))


# --- POST /groups/join -----------------------------------------------


def test_join_invalid_code_returns_404(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    # Profile load + invite lookup both walk the same chain — set leaf .data
    # to a profile row for the first call, then patch the service to assert
    # the failure path.
    _stub_profile_select(fake_sb, _profile_row(user_id))
    # The invite lookup uses .eq().eq().maybe_single().execute(); MagicMock's
    # auto-chaining keeps the same leaf, so override after profile load runs.
    # Simpler: drive directly through TestClient with a code that the stubbed
    # supabase says doesn't exist.
    fake_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = None  # noqa: E501

    response = auth_client.post(
        "/groups/join",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
        json={"code": "NOPE12"},
    )
    assert response.status_code == 404


def test_join_idempotent_when_already_member(
    fake_sb: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = uuid4()
    group_id = uuid4()

    # Patch the helper to return a known lifecycle group; we only care that
    # `join` short-circuits without calling insert when membership exists.
    from app.models.group import Group, GroupWithLifecycle

    fake_group = GroupWithLifecycle(
        **Group.model_validate(_group_row(group_id, user_id)).model_dump(),
        lifecycle="active",
    )
    monkeypatch.setattr(members_service, "_fetch_with_lifecycle", lambda *_a, **_k: fake_group)

    # invite lookup → group_id; membership pre-check → existing row.
    invite_data = MagicMock()
    invite_data.data = {"group_id": str(group_id)}
    member_data = MagicMock()
    member_data.data = {"id": "membership-row-id"}

    leaf = fake_sb.table.return_value.select.return_value.eq.return_value
    leaf.eq.return_value.maybe_single.return_value.execute.side_effect = [invite_data, member_data]

    result = members_service.join(fake_sb, user_id, "K3J9PQ")

    assert result.id == group_id
    fake_sb.table.return_value.insert.assert_not_called()


# --- GET /groups/{id} ------------------------------------------------


def test_get_group_non_member_returns_404(auth_client: TestClient, fake_sb: MagicMock) -> None:
    user_id = uuid4()
    group_id = uuid4()
    # Profile load returns a row; membership pre-check returns None.
    # All select chains share a leaf — set it to None and override the very
    # first call (profile load) with a side_effect sequence.
    leaf = fake_sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute  # noqa: E501
    profile_call = MagicMock()
    profile_call.data = _profile_row(user_id)
    membership_call = MagicMock()
    membership_call.data = None
    leaf.side_effect = [profile_call, membership_call]
    # The two-eq chain (membership pre-check) lands here too:
    fake_sb.table.return_value.select.return_value.eq.return_value.eq.return_value.maybe_single.return_value.execute.return_value.data = None  # noqa: E501

    response = auth_client.get(
        f"/groups/{group_id}",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
    )
    assert response.status_code == 404


# --- DELETE /groups/{id} ---------------------------------------------


def test_delete_group_non_creator_returns_403(fake_sb: MagicMock) -> None:
    caller = uuid4()
    other_creator = uuid4()
    group_id = uuid4()

    leaf = fake_sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute  # noqa: E501
    leaf.return_value.data = {"created_by": str(other_creator)}

    with pytest.raises(groups_service.NotGroupCreatorError):
        groups_service.delete(fake_sb, caller, group_id)

    # Hard delete must not have run.
    fake_sb.table.return_value.delete.assert_not_called()


def test_delete_group_creator_runs_cascade(fake_sb: MagicMock) -> None:
    creator = uuid4()
    group_id = uuid4()

    leaf = fake_sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute  # noqa: E501
    leaf.return_value.data = {"created_by": str(creator)}

    groups_service.delete(fake_sb, creator, group_id)

    fake_sb.table.return_value.delete.return_value.eq.assert_called_with("id", str(group_id))


def test_delete_group_missing_returns_404(fake_sb: MagicMock) -> None:
    leaf = fake_sb.table.return_value.select.return_value.eq.return_value.maybe_single.return_value.execute  # noqa: E501
    leaf.return_value.data = None

    with pytest.raises(groups_service.GroupNotFoundError):
        groups_service.delete(fake_sb, uuid4(), uuid4())
