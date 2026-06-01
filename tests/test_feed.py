"""Unit tests for the /groups/{group_id}/feed endpoint and the feed service.

Hermetic: the supabase client is a MagicMock whose ``.table(name)`` dispatches
to a per-table fluent builder (each builder method returns ``self``, so the
test is robust to the exact chain shape — with or without the cursor's
``or_()`` branch). R2 is patched out at both call sites
(``services.feed.r2`` and the ``services.profile.r2`` used by avatar
resolution). JWT minted with the HS256 test secret.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, call
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from app.clients.supabase import get_supabase
from app.config import Settings, get_settings
from app.main import create_app
from app.services import feed as feed_service

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


# --- fluent-builder mock plumbing ------------------------------------

_BUILDER_METHODS = ("select", "eq", "is_", "lte", "or_", "order", "limit", "maybe_single")


def _resp(data: Any) -> MagicMock:
    r = MagicMock()
    r.data = data
    return r


def _builder(*, execute_return: Any = None, execute_side: Any = None) -> MagicMock:
    b = MagicMock()
    for name in _BUILDER_METHODS:
        getattr(b, name).return_value = b
    if execute_side is not None:
        b.execute.side_effect = execute_side
    else:
        b.execute.return_value = execute_return
    return b


def _fake_sb(
    *,
    is_member: bool = True,
    feed_execute_return: Any = None,
    feed_execute_side: Any = None,
) -> MagicMock:
    sb = MagicMock()
    members = _builder(execute_return=_resp({"id": "m"} if is_member else None))
    posts = _builder(execute_return=feed_execute_return, execute_side=feed_execute_side)
    mapping = {"group_members": members, "posts": posts}
    sb.table.side_effect = lambda name: mapping[name]
    sb.members = members  # type: ignore[attr-defined]
    sb.posts = posts  # type: ignore[attr-defined]
    return sb


def _feed_row(
    post_id: UUID,
    user_id: UUID,
    group_id: UUID,
    visible_at: str,
    *,
    thumbnail_path: str | None = None,
    display_name: str | None = "Ann",
    avatar_url: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    base = {
        "id": str(post_id),
        "group_id": str(group_id),
        "prompt_id": None,
        "user_id": str(user_id),
        "kind": "photobooth",
        "media_type": "strip",
        "storage_path": f"groups/{group_id}/posts/{post_id}/media.jpg",
        "thumbnail_path": thumbnail_path,
        "duration_seconds": None,
        "captured_at": visible_at,
        "is_late": False,
        "visible_at": visible_at,
        "latitude": None,
        "longitude": None,
        "location_accuracy_meters": None,
        "created_at": visible_at,
        "profiles": {"display_name": display_name, "avatar_url": avatar_url},
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _stub_r2(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch the single R2 GET-signer the feed hydration path hits.

    feed.r2 and profile.r2 are the *same* module object, so one patch covers
    both the media/thumbnail signing in ``_hydrate`` and the avatar signing
    in ``profile_service.resolve_avatar_url``. The signed URL embeds the
    input path so different keys yield distinguishable URLs.
    """

    def fake_get(path: str, ttl_seconds: int = 3600) -> str:
        return f"https://r2.example/get?path={path}"

    get = MagicMock(side_effect=fake_get)
    monkeypatch.setattr("app.services.feed.r2.generate_presigned_get_url", get)
    return get


def _signed(path: str) -> str:
    return f"https://r2.example/get?path={path}"


# --- cursor helpers --------------------------------------------------


def test_encode_decode_cursor_round_trip() -> None:
    visible_at = datetime(2026, 6, 1, 12, 0, 0, 123456, tzinfo=UTC)
    post_id = uuid4()

    cursor = feed_service.encode_cursor(visible_at, post_id)
    assert isinstance(cursor, str)
    # Opaque: no raw uuid / iso leaking in plaintext.
    assert str(post_id) not in cursor

    decoded = feed_service.decode_cursor(cursor)
    assert decoded == (visible_at, post_id)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "!!!not-base64!!!",
        # base64 of a string with no pipe separator.
        feed_service.base64.urlsafe_b64encode(b"no-separator-here").decode(),
        # base64 of "iso|uuid" but neither parses.
        feed_service.base64.urlsafe_b64encode(b"notatime|notauuid").decode(),
        # valid iso, garbage uuid.
        feed_service.base64.urlsafe_b64encode(b"2026-06-01T12:00:00+00:00|nope").decode(),
    ],
)
def test_decode_bad_cursor_returns_none(bad: str) -> None:
    assert feed_service.decode_cursor(bad) is None


# --- list_feed: filters, ordering, membership ------------------------


def test_list_feed_applies_visibility_filters_and_compound_ordering() -> None:
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()
    now_iso = datetime.now(UTC).isoformat()

    sb = _fake_sb(feed_execute_return=_resp([_feed_row(post_id, user_id, group_id, now_iso)]))

    page = feed_service.list_feed(sb, user_id, group_id, limit=20)

    assert len(page.items) == 1
    assert page.next_cursor is None  # 1 row < limit+1 → no more pages

    # deleted_at IS NULL filter applied.
    sb.posts.is_.assert_called_once_with("deleted_at", "null")
    # visible_at <= now filter applied (value is an ISO timestamp).
    assert sb.posts.lte.call_args.args[0] == "visible_at"
    # group scoping.
    sb.posts.eq.assert_called_once_with("group_id", str(group_id))
    # Compound ordering: visible_at DESC, then id DESC.
    assert sb.posts.order.call_args_list == [
        call("visible_at", desc=True),
        call("id", desc=True),
    ]
    # Over-fetch by one to detect a next page.
    sb.posts.limit.assert_called_once_with(21)


def test_list_feed_non_member_raises_group_not_found() -> None:
    sb = _fake_sb(is_member=False)
    with pytest.raises(feed_service.GroupNotFoundError):
        feed_service.list_feed(sb, uuid4(), uuid4())


def test_list_feed_pagination_two_pages_no_dupes_no_gaps() -> None:
    """6 posts, page size 3 → two clean pages, no duplicates, no gaps."""
    user_id = uuid4()
    group_id = uuid4()

    # Descending visible_at so the rows arrive already in feed order.
    base = datetime(2026, 6, 1, 20, 0, 0, tzinfo=UTC)
    rows = [
        _feed_row(uuid4(), user_id, group_id, (base - timedelta(minutes=i)).isoformat())
        for i in range(6)
    ]

    # Page 1: server returns limit+1 = 4 rows (it over-fetches).
    sb1 = _fake_sb(feed_execute_return=_resp(rows[:4]))
    page1 = feed_service.list_feed(sb1, user_id, group_id, limit=3)
    assert len(page1.items) == 3
    assert page1.next_cursor is not None

    # The cursor points at the last item of page 1.
    decoded = feed_service.decode_cursor(page1.next_cursor)
    assert decoded is not None
    assert str(decoded[1]) == rows[2]["id"]

    # Page 2: server returns the remaining 3 rows (< limit+1 → last page).
    sb2 = _fake_sb(feed_execute_return=_resp(rows[3:6]))
    page2 = feed_service.list_feed(sb2, user_id, group_id, cursor=page1.next_cursor, limit=3)
    assert len(page2.items) == 3
    assert page2.next_cursor is None

    # Page 2 applied the keyset cursor (the or_ branch).
    sb2.posts.or_.assert_called_once()

    seen = [str(it.id) for it in page1.items] + [str(it.id) for it in page2.items]
    expected = [r["id"] for r in rows]
    assert seen == expected  # exact order: no gaps, no reordering
    assert len(set(seen)) == 6  # no duplicates


def test_list_feed_hydrates_signed_urls_and_author() -> None:
    user_id = uuid4()
    group_id = uuid4()
    post_id = uuid4()
    now_iso = datetime.now(UTC).isoformat()

    row = _feed_row(
        post_id,
        user_id,
        group_id,
        now_iso,
        thumbnail_path=f"groups/{group_id}/posts/{post_id}/thumb.jpg",
        display_name="Beatrix",
        avatar_url="users/x/avatars/a.jpg",
    )
    sb = _fake_sb(feed_execute_return=_resp([row]))

    page = feed_service.list_feed(sb, user_id, group_id)
    item = page.items[0]

    assert item.media_url == _signed(row["storage_path"])
    assert item.thumbnail_url == _signed(row["thumbnail_path"])
    assert item.media_url != item.thumbnail_url  # distinct keys → distinct URLs
    assert item.author.user_id == user_id
    assert item.author.display_name == "Beatrix"
    # Avatar resolved to a signed URL via profile_service.
    assert item.author.avatar_url == _signed("users/x/avatars/a.jpg")


def test_list_feed_null_thumbnail_yields_no_thumbnail_url() -> None:
    user_id = uuid4()
    group_id = uuid4()
    now_iso = datetime.now(UTC).isoformat()

    sb = _fake_sb(
        feed_execute_return=_resp([_feed_row(uuid4(), user_id, group_id, now_iso)]),
    )
    page = feed_service.list_feed(sb, user_id, group_id)
    assert page.items[0].thumbnail_url is None
    assert page.items[0].author.avatar_url is None


# --- router ----------------------------------------------------------


def _client(sb: MagicMock) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(supabase_jwt_secret=TEST_SECRET)
    app.dependency_overrides[get_supabase] = lambda: sb
    return TestClient(app)


def test_get_feed_route_returns_page_without_internal_keys() -> None:
    user_id = uuid4()
    group_id = uuid4()
    now_iso = datetime.now(UTC).isoformat()
    sb = _fake_sb(
        feed_execute_return=_resp([_feed_row(uuid4(), user_id, group_id, now_iso)]),
    )

    client = _client(sb)
    response = client.get(
        f"/groups/{group_id}/feed",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["next_cursor"] is None
    assert len(body["items"]) == 1
    item = body["items"][0]
    # Internal R2 keys must never reach the client.
    assert "storage_path" not in item
    assert "thumbnail_path" not in item
    assert item["media_url"].startswith("https://r2.example/get?path=groups/")
    assert item["author"]["display_name"] == "Ann"


def test_get_feed_route_non_member_returns_404() -> None:
    user_id = uuid4()
    group_id = uuid4()
    sb = _fake_sb(is_member=False)

    client = _client(sb)
    response = client.get(
        f"/groups/{group_id}/feed",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
    )
    assert response.status_code == 404, response.text


def test_get_feed_route_limit_over_max_returns_422() -> None:
    user_id = uuid4()
    group_id = uuid4()
    sb = _fake_sb(feed_execute_return=_resp([]))

    client = _client(sb)
    response = client.get(
        f"/groups/{group_id}/feed?limit=51",
        headers={"Authorization": f"Bearer {_mint(user_id)}"},
    )
    assert response.status_code == 422, response.text
