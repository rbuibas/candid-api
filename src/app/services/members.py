"""Membership service — list members and join via invite code.

Join is idempotent: a re-join with the same valid code returns 200 and the
same group, never 500 on the (group_id, user_id) unique constraint. We
pre-check by SELECT and also catch the constraint as a race fallback.
"""

from uuid import UUID

from postgrest.exceptions import APIError
from supabase import Client

from app.models.group import Group, GroupMember, GroupWithLifecycle
from app.services import groups as groups_service
from app.services import profile as profile_service

_PG_UNIQUE_VIOLATION = "23505"


class InvalidInviteCodeError(Exception):
    """Invite code is unknown or inactive.

    Routers map this to 404 — same surface as "code not found" so we don't
    leak whether a guessed code exists but is inactive.
    """


def list_for_group(sb: Client, user_id: UUID, group_id: UUID) -> list[GroupMember]:
    # Reuse groups.get for the member check (raises GroupNotFoundError → 404).
    groups_service.get(sb, user_id, group_id)

    result = (
        sb.table("group_members")
        .select("user_id, joined_at, profiles!inner(display_name, avatar_url)")
        .eq("group_id", str(group_id))
        .execute()
    )
    rows = result.data or []
    out: list[GroupMember] = []
    for row in rows:
        profile = row.get("profiles") or {}
        out.append(
            GroupMember(
                user_id=row["user_id"],
                display_name=profile.get("display_name"),
                avatar_url=profile_service.resolve_avatar_url(profile.get("avatar_url")),
                joined_at=row["joined_at"],
            )
        )
    return out


def join(sb: Client, user_id: UUID, code: str) -> GroupWithLifecycle:
    invite_result = (
        sb.table("invite_codes")
        .select("group_id")
        .eq("code", code)
        .eq("active", True)
        .maybe_single()
        .execute()
    )
    if not invite_result or not invite_result.data:
        raise InvalidInviteCodeError()

    group_id = UUID(invite_result.data["group_id"])

    # Idempotency: if already a member, return the group.
    existing = (
        sb.table("group_members")
        .select("id")
        .eq("group_id", str(group_id))
        .eq("user_id", str(user_id))
        .maybe_single()
        .execute()
    )
    if existing and existing.data:
        return _fetch_with_lifecycle(sb, group_id)

    try:
        sb.table("group_members").insert(
            {"group_id": str(group_id), "user_id": str(user_id)}
        ).execute()
    except APIError as e:
        if e.code != _PG_UNIQUE_VIOLATION:
            raise
        # Race: another request inserted between our pre-check and insert.
        # Treat as success — re-jointness is the user-visible behaviour.

    return _fetch_with_lifecycle(sb, group_id)


def _fetch_with_lifecycle(sb: Client, group_id: UUID) -> GroupWithLifecycle:
    result = sb.table("groups").select("*").eq("id", str(group_id)).maybe_single().execute()
    if not result or not result.data:
        # Group disappeared between invite lookup and read. Treat as invalid.
        raise InvalidInviteCodeError()

    group = Group.model_validate(result.data)
    return GroupWithLifecycle(
        **group.model_dump(),
        lifecycle=groups_service.compute_lifecycle(group.start_date, group.end_date),
    )
