"""Integration tests for /groups endpoints (live Supabase).

Marker-gated: skips when SUPABASE_* env vars aren't set (see conftest).
Auth user teardown in `make_user` cascades through profiles → groups →
group_members / invite_codes, so test data evaporates after each run.
"""

from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import UUID

import jwt
import pytest
from fastapi.testclient import TestClient
from supabase import Client, create_client

from app.clients.supabase import get_supabase
from app.config import Settings, get_settings
from app.main import create_app

pytestmark = pytest.mark.integration


def _mint(user_id: UUID, secret: str) -> str:
    now = datetime.now(tz=UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "aud": "authenticated",
            "role": "authenticated",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=1)).timestamp()),
        },
        secret,
        algorithm="HS256",
    )


@pytest.fixture
def app_client(integration_env: dict[str, str]) -> Generator[TestClient, None, None]:
    """TestClient bound to a fresh service-role Supabase client.

    Overrides both `get_settings` (so the JWT verifier knows the HS256 secret)
    and `get_supabase` (a fresh client per-test, not the module singleton).
    """
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        supabase_url=integration_env["SUPABASE_URL"],
        supabase_jwt_secret=integration_env["SUPABASE_JWT_SECRET"],
        supabase_service_role_key=integration_env["SUPABASE_SERVICE_ROLE_KEY"],
    )
    sb = create_client(
        integration_env["SUPABASE_URL"],
        integration_env["SUPABASE_SERVICE_ROLE_KEY"],
    )
    app.dependency_overrides[get_supabase] = lambda: sb
    client = TestClient(app)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


def _auth(user_id: UUID, integration_env: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {_mint(user_id, integration_env['SUPABASE_JWT_SECRET'])}"}


def _today_iso() -> str:
    return datetime.now(UTC).date().isoformat()


def _iso(delta_days: int) -> str:
    return (datetime.now(UTC).date() + timedelta(days=delta_days)).isoformat()


def test_create_and_list_groups(
    app_client: TestClient,
    make_user: Callable[..., UUID],
    integration_env: dict[str, str],
) -> None:
    creator = make_user()

    resp = app_client.post(
        "/groups",
        headers=_auth(creator, integration_env),
        json={"name": "Bach", "start_date": _today_iso(), "end_date": _iso(2)},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    code = body["invite_code"]
    assert len(code) == 6 and code.isalnum() and code.isupper()
    group_id = body["group"]["id"]

    listing = app_client.get("/groups", headers=_auth(creator, integration_env))
    assert listing.status_code == 200
    assert any(g["id"] == group_id for g in listing.json())


def test_join_is_idempotent(
    app_client: TestClient,
    make_user: Callable[..., UUID],
    integration_env: dict[str, str],
    service_sb: Client,
) -> None:
    creator = make_user()
    joiner = make_user()

    create_resp = app_client.post(
        "/groups",
        headers=_auth(creator, integration_env),
        json={"name": "Bach", "start_date": _today_iso(), "end_date": _iso(2)},
    )
    code = create_resp.json()["invite_code"]
    group_id = create_resp.json()["group"]["id"]

    first = app_client.post(
        "/groups/join", headers=_auth(joiner, integration_env), json={"code": code}
    )
    second = app_client.post(
        "/groups/join", headers=_auth(joiner, integration_env), json={"code": code}
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["id"] == group_id == second.json()["id"]

    # Ground truth: single membership row for the joiner.
    rows = (
        service_sb.table("group_members")
        .select("id")
        .eq("group_id", group_id)
        .eq("user_id", str(joiner))
        .execute()
        .data
    )
    assert len(rows) == 1


def test_join_invalid_code_returns_404(
    app_client: TestClient,
    make_user: Callable[..., UUID],
    integration_env: dict[str, str],
) -> None:
    user = make_user()
    resp = app_client.post(
        "/groups/join", headers=_auth(user, integration_env), json={"code": "ZZZZZZ"}
    )
    assert resp.status_code == 404, resp.text


def test_non_member_cannot_read_group(
    app_client: TestClient,
    make_user: Callable[..., UUID],
    integration_env: dict[str, str],
) -> None:
    creator = make_user()
    outsider = make_user()

    create_resp = app_client.post(
        "/groups",
        headers=_auth(creator, integration_env),
        json={"name": "Bach", "start_date": _today_iso(), "end_date": _iso(2)},
    )
    group_id = create_resp.json()["group"]["id"]

    detail = app_client.get(f"/groups/{group_id}", headers=_auth(outsider, integration_env))
    members = app_client.get(
        f"/groups/{group_id}/members", headers=_auth(outsider, integration_env)
    )
    assert detail.status_code == 404
    assert members.status_code == 404


def test_members_list_includes_creator_and_joiner(
    app_client: TestClient,
    make_user: Callable[..., UUID],
    integration_env: dict[str, str],
) -> None:
    creator = make_user()
    joiner = make_user()

    create_resp = app_client.post(
        "/groups",
        headers=_auth(creator, integration_env),
        json={"name": "Bach", "start_date": _today_iso(), "end_date": _iso(2)},
    )
    code = create_resp.json()["invite_code"]
    group_id = create_resp.json()["group"]["id"]
    app_client.post("/groups/join", headers=_auth(joiner, integration_env), json={"code": code})

    # Both members can read the roster, both rows show up.
    for caller in (creator, joiner):
        resp = app_client.get(f"/groups/{group_id}/members", headers=_auth(caller, integration_env))
        assert resp.status_code == 200, resp.text
        ids = {m["user_id"] for m in resp.json()}
        assert ids == {str(creator), str(joiner)}


def test_delete_by_non_creator_returns_403(
    app_client: TestClient,
    make_user: Callable[..., UUID],
    integration_env: dict[str, str],
    service_sb: Client,
) -> None:
    creator = make_user()
    joiner = make_user()

    create_resp = app_client.post(
        "/groups",
        headers=_auth(creator, integration_env),
        json={"name": "Bach", "start_date": _today_iso(), "end_date": _iso(2)},
    )
    code = create_resp.json()["invite_code"]
    group_id = create_resp.json()["group"]["id"]
    app_client.post("/groups/join", headers=_auth(joiner, integration_env), json={"code": code})

    resp = app_client.delete(f"/groups/{group_id}", headers=_auth(joiner, integration_env))
    assert resp.status_code == 403, resp.text

    # Group row still present.
    still_there = service_sb.table("groups").select("id").eq("id", group_id).execute().data
    assert len(still_there) == 1


def test_delete_by_creator_cascades(
    app_client: TestClient,
    make_user: Callable[..., UUID],
    integration_env: dict[str, str],
    service_sb: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creator = make_user()
    joiner = make_user()

    create_resp = app_client.post(
        "/groups",
        headers=_auth(creator, integration_env),
        json={"name": "Bach", "start_date": _today_iso(), "end_date": _iso(2)},
    )
    code = create_resp.json()["invite_code"]
    group_id = create_resp.json()["group"]["id"]
    app_client.post("/groups/join", headers=_auth(joiner, integration_env), json={"code": code})

    # DB cascade is the focus of this test; the R2 round-trip is covered by
    # tests/integration/test_posts.py (gated separately on R2 envs). Stub the
    # purge so this test still works against a Supabase-only environment.
    delete_prefix = MagicMock()
    monkeypatch.setattr("app.services.groups.r2.delete_prefix", delete_prefix)

    resp = app_client.delete(f"/groups/{group_id}", headers=_auth(creator, integration_env))
    assert resp.status_code == 204, resp.text
    delete_prefix.assert_called_once_with(f"groups/{group_id}/")

    # Group is gone; cascade removed members and invite codes.
    assert service_sb.table("groups").select("id").eq("id", group_id).execute().data == []
    assert (
        service_sb.table("group_members").select("id").eq("group_id", group_id).execute().data == []
    )
    assert (
        service_sb.table("invite_codes").select("id").eq("group_id", group_id).execute().data == []
    )

    # And a subsequent GET surfaces 404.
    follow_up = app_client.get(f"/groups/{group_id}", headers=_auth(creator, integration_env))
    assert follow_up.status_code == 404


def test_lifecycle_boundaries(
    app_client: TestClient,
    make_user: Callable[..., UUID],
    integration_env: dict[str, str],
) -> None:
    creator = make_user()

    cases = [
        ("upcoming", _iso(1), _iso(3)),
        ("active", _today_iso(), _iso(2)),
        ("locked", _iso(-3), _iso(-1)),
    ]
    for expected, start, end in cases:
        resp = app_client.post(
            "/groups",
            headers=_auth(creator, integration_env),
            json={"name": f"Bach-{expected}", "start_date": start, "end_date": end},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["group"]["lifecycle"] == expected
