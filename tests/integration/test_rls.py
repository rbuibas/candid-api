"""Integration tests: RLS denies cross-user reads via PostgREST."""

from collections.abc import Callable
from uuid import UUID

import httpx
import pytest
from supabase import Client

pytestmark = pytest.mark.integration


def test_user_can_read_their_own_profile(
    make_user: Callable[..., UUID],
    postgrest_get: Callable[..., httpx.Response],
) -> None:
    user_a = make_user()

    response = postgrest_get(user_a, "profiles", id=f"eq.{user_a}")

    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["id"] == str(user_a)


def test_user_cannot_read_another_users_profile(
    make_user: Callable[..., UUID],
    postgrest_get: Callable[..., httpx.Response],
) -> None:
    user_a = make_user()
    user_b = make_user()

    # User A asks PostgREST for user B's row — RLS should yield empty.
    response = postgrest_get(user_a, "profiles", id=f"eq.{user_b}")

    assert response.status_code == 200, response.text
    assert response.json() == []


def test_non_member_cannot_read_group(
    service_sb: Client,
    make_user: Callable[..., UUID],
    postgrest_get: Callable[..., httpx.Response],
) -> None:
    creator = make_user()
    outsider = make_user()

    # Set up: creator owns a group and is a member. Done via service-role so
    # we're testing the read-side policy, not the join flow (Phase 2).
    group_insert = (
        service_sb.table("groups")
        .insert(
            {
                "name": "rls test group",
                "created_by": str(creator),
                "start_date": "2026-06-01",
                "end_date": "2026-06-02",
            }
        )
        .execute()
    )
    group_id = group_insert.data[0]["id"]
    try:
        service_sb.table("group_members").insert(
            {"group_id": group_id, "user_id": str(creator)}
        ).execute()

        # Creator (a member) sees the group.
        creator_resp = postgrest_get(creator, "groups", id=f"eq.{group_id}")
        assert creator_resp.status_code == 200, creator_resp.text
        assert len(creator_resp.json()) == 1

        # Outsider (not a member) sees nothing.
        outsider_resp = postgrest_get(outsider, "groups", id=f"eq.{group_id}")
        assert outsider_resp.status_code == 200, outsider_resp.text
        assert outsider_resp.json() == []
    finally:
        # Cascade removes group_members; explicit cleanup keeps the test
        # idempotent if it ever bails partway.
        service_sb.table("groups").delete().eq("id", group_id).execute()
