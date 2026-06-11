"""Client-events service — record a single group-scoped event.

The only write path for `public.client_events`. Service-role bypasses RLS, so
membership is enforced manually here exactly as the feed/posts services do —
a caller may only record events for groups they belong to. RLS stays as the
second line of defence for any non-service-role caller.

Reads (the saved debrief queries in candid-measurement-and-debrief) run
server-side/ad-hoc; there is deliberately no read endpoint here.
"""

from uuid import UUID

from supabase import Client

from app.models.event import ClientEvent, ClientEventCreate


class GroupNotFoundError(Exception):
    """Caller is not a member of the group (or it doesn't exist).

    Router maps this to 404 — same anti-leak convention as groups/feed.
    """


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


def record(sb: Client, user_id: UUID, payload: ClientEventCreate) -> ClientEvent:
    if not _is_member(sb, user_id, payload.group_id):
        raise GroupNotFoundError()

    row = {
        "group_id": str(payload.group_id),
        "user_id": str(user_id),
        "name": payload.name,
        "payload": payload.payload,
    }
    result = sb.table("client_events").insert(row).execute()
    if not result.data:
        raise RuntimeError("client_events insert returned no row")
    return ClientEvent.model_validate(result.data[0])
