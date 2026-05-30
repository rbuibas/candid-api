"""Groups service — create / read / list / delete.

Service-role bypasses RLS at this layer, so every query manually scopes by
`user_id`. RLS stays as the second line of defence for any non-service-role
caller.

PostgREST doesn't expose multi-statement transactions and Phase 2 forbids
new migrations (rules out a wrapping RPC). `create` orchestrates the three
inserts sequentially and best-effort rolls back the groups row if a later
step fails. Acceptable for MVP scope; the worst case is an orphaned group
row with no members or invite — and the creator can re-DELETE.
"""

from datetime import UTC, date, datetime
from uuid import UUID

from supabase import Client

from app.models.group import (
    Group,
    GroupCreate,
    GroupCreateResponse,
    GroupWithLifecycle,
    Lifecycle,
)
from app.services import invites as invites_service


class GroupNotFoundError(Exception):
    """Group does not exist, or caller is not a member.

    Routers map this to 404 to avoid leaking the existence of groups the
    caller cannot see (same convention as ProfileNotFoundError).
    """


class NotGroupCreatorError(Exception):
    """Caller is not the creator of the group (delete is creator-only)."""


def compute_lifecycle(start_date: date, end_date: date) -> Lifecycle:
    """Lifecycle per /docs/02-product-design.md §6: compared to today's UTC date."""
    today = datetime.now(UTC).date()
    if today < start_date:
        return "upcoming"
    if today > end_date:
        return "locked"
    return "active"


def _attach_lifecycle(group: Group) -> GroupWithLifecycle:
    return GroupWithLifecycle(
        **group.model_dump(),
        lifecycle=compute_lifecycle(group.start_date, group.end_date),
    )


def _is_member(sb: Client, user_id: UUID, group_id: UUID) -> bool:
    result = (
        sb.table("group_members")
        .select("id")
        .eq("group_id", str(group_id))
        .eq("user_id", str(user_id))
        .maybe_single()
        .execute()
    )
    return bool(result and result.data)


def _purge_group_media(sb: Client, group_id: UUID) -> None:
    """Phase 3 will delete R2 objects for this group's posts.

    Wired in here so the call site exists; left empty so a Phase 3 patch
    only has to fill the body without touching the delete flow.
    """
    # TODO(Phase 3): list posts for group_id, delete corresponding R2 objects
    # before the DB cascade removes the rows.
    _ = (sb, group_id)


def create(sb: Client, user_id: UUID, payload: GroupCreate) -> GroupCreateResponse:
    settings_dict: dict = {}
    if payload.settings is not None:
        settings_dict = payload.settings.model_dump(exclude_unset=True, mode="json")

    insert_payload: dict = {
        "name": payload.name,
        "created_by": str(user_id),
        "start_date": payload.start_date.isoformat(),
        "end_date": payload.end_date.isoformat(),
        **settings_dict,
    }

    insert_result = sb.table("groups").insert(insert_payload).execute()
    if not insert_result.data:
        raise RuntimeError("groups insert returned no row")
    group = Group.model_validate(insert_result.data[0])

    try:
        sb.table("group_members").insert(
            {"group_id": str(group.id), "user_id": str(user_id)}
        ).execute()
        code = invites_service.create_for_group(sb, str(group.id))
    except Exception:
        # Best-effort rollback; cascade handles any partial member row.
        try:
            sb.table("groups").delete().eq("id", str(group.id)).execute()
        except Exception:
            pass
        raise

    return GroupCreateResponse(group=_attach_lifecycle(group), invite_code=code)


def list_for_user(sb: Client, user_id: UUID) -> list[GroupWithLifecycle]:
    result = (
        sb.table("groups")
        .select("*, group_members!inner(user_id)")
        .eq("group_members.user_id", str(user_id))
        .execute()
    )
    rows = result.data or []
    out: list[GroupWithLifecycle] = []
    for row in rows:
        # Drop the embedded join key before model_validate (Group has no
        # group_members field).
        row.pop("group_members", None)
        out.append(_attach_lifecycle(Group.model_validate(row)))
    return out


def get(sb: Client, user_id: UUID, group_id: UUID) -> GroupWithLifecycle:
    if not _is_member(sb, user_id, group_id):
        raise GroupNotFoundError()
    result = sb.table("groups").select("*").eq("id", str(group_id)).maybe_single().execute()
    if not result or not result.data:
        raise GroupNotFoundError()
    return _attach_lifecycle(Group.model_validate(result.data))


def delete(sb: Client, user_id: UUID, group_id: UUID) -> None:
    result = (
        sb.table("groups").select("created_by").eq("id", str(group_id)).maybe_single().execute()
    )
    if not result or not result.data:
        raise GroupNotFoundError()
    if result.data["created_by"] != str(user_id):
        raise NotGroupCreatorError()

    _purge_group_media(sb, group_id)
    sb.table("groups").delete().eq("id", str(group_id)).execute()
