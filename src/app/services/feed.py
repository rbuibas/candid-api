"""Feed service — group-scoped chronological feed with keyset pagination.

Per /docs/02-product-design.md §5 the feed is shared within a group,
newest-visible first, members-only. A post is visible once
``visible_at <= now`` and it isn't tombstoned (``deleted_at IS NULL``).

### Ordering & pagination

Ordering is ``visible_at DESC, id DESC`` — a compound key so the cursor is
stable even when several posts share the same ``visible_at`` (e.g. a group
with ``view_delay_seconds = 0`` where confirms land in the same second).

The cursor is an opaque base64 blob of ``"{visible_at_iso}|{id}"``. Keyset
pagination then asks for the rows strictly *after* the cursor under the
DESC ordering::

    visible_at < cursor_visible_at
      OR (visible_at = cursor_visible_at AND id < cursor_id)

We over-fetch by one row (``limit + 1``) to learn whether another page
exists without a second count query; the extra row is dropped and its
predecessor becomes the next cursor.

Service-role bypasses RLS, so membership is enforced manually here — RLS is
the second line of defence.
"""

import base64
import logging
from datetime import UTC, datetime
from uuid import UUID

from supabase import Client

from app.clients import r2
from app.models.post import FeedItem, FeedPage, PostAuthor
from app.services import profile as profile_service

log = logging.getLogger(__name__)

_DOWNLOAD_TTL_SECONDS = 3600

# Public post columns + the embedded author. storage_path / thumbnail_path
# are selected for URL-signing but never returned to the client.
_FEED_SELECT = (
    "id, group_id, prompt_id, user_id, kind, media_type, storage_path, "
    "thumbnail_path, duration_seconds, captured_at, is_late, visible_at, "
    "latitude, longitude, location_accuracy_meters, created_at, "
    "profiles!inner(display_name, avatar_url)"
)


class GroupNotFoundError(Exception):
    """Caller is not a member of the group (or it doesn't exist).

    Router maps this to 404 — same anti-leak convention as groups.
    """


def encode_cursor(visible_at: datetime, post_id: UUID) -> str:
    """Opaque base64 of ``"{visible_at_iso}|{post_id}"``."""
    raw = f"{visible_at.isoformat()}|{post_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(raw: str) -> tuple[datetime, UUID] | None:
    """Inverse of :func:`encode_cursor`.

    Returns ``None`` on any malformed input — callers treat that as "no
    cursor / start from the top" rather than surfacing a 500. A tampered or
    truncated cursor degrades to a fresh first page, never an error.
    """
    try:
        decoded = base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8")
        iso, sep, id_part = decoded.partition("|")
        if not sep:
            return None
        return datetime.fromisoformat(iso), UUID(id_part)
    except (ValueError, TypeError):
        return None


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


def _hydrate(row: dict) -> FeedItem:
    """Turn a raw posts row (+ embedded profile) into a FeedItem with signed URLs."""
    media_url = r2.generate_presigned_get_url(
        row["storage_path"], ttl_seconds=_DOWNLOAD_TTL_SECONDS
    )
    thumbnail_url: str | None = None
    thumbnail_path = row.get("thumbnail_path")
    if thumbnail_path:
        thumbnail_url = r2.generate_presigned_get_url(
            thumbnail_path, ttl_seconds=_DOWNLOAD_TTL_SECONDS
        )

    profile = row.get("profiles") or {}
    return FeedItem(
        id=row["id"],
        group_id=row["group_id"],
        prompt_id=row.get("prompt_id"),
        user_id=row["user_id"],
        kind=row["kind"],
        media_type=row["media_type"],
        duration_seconds=row.get("duration_seconds"),
        captured_at=row["captured_at"],
        is_late=row["is_late"],
        visible_at=row["visible_at"],
        media_url=media_url,
        thumbnail_url=thumbnail_url,
        latitude=row.get("latitude"),
        longitude=row.get("longitude"),
        location_accuracy_meters=row.get("location_accuracy_meters"),
        created_at=row["created_at"],
        author=PostAuthor(
            user_id=row["user_id"],
            display_name=profile.get("display_name"),
            avatar_url=profile_service.resolve_avatar_url(profile.get("avatar_url")),
        ),
    )


def list_feed(
    sb: Client,
    user_id: UUID,
    group_id: UUID,
    *,
    cursor: str | None = None,
    limit: int = 20,
) -> FeedPage:
    if not _is_member(sb, user_id, group_id):
        raise GroupNotFoundError()

    now_iso = datetime.now(UTC).isoformat()
    query = (
        sb.table("posts")
        .select(_FEED_SELECT)
        .eq("group_id", str(group_id))
        .is_("deleted_at", "null")
        .lte("visible_at", now_iso)
    )

    decoded = decode_cursor(cursor) if cursor else None
    if decoded is not None:
        c_visible_at, c_id = decoded
        c_iso = c_visible_at.isoformat()
        # Keyset for DESC ordering: rows strictly past the cursor.
        query = query.or_(f"visible_at.lt.{c_iso},and(visible_at.eq.{c_iso},id.lt.{c_id})")

    query = query.order("visible_at", desc=True).order("id", desc=True).limit(limit + 1)
    rows = query.execute().data or []

    has_more = len(rows) > limit
    page = rows[:limit]
    items = [_hydrate(row) for row in page]

    next_cursor: str | None = None
    if has_more and page:
        last = page[-1]
        next_cursor = encode_cursor(datetime.fromisoformat(last["visible_at"]), UUID(last["id"]))

    return FeedPage(items=items, next_cursor=next_cursor)
