"""Integration test fixtures.

These tests run against a live Supabase project. They skip cleanly when
the required env vars aren't set, so `uv run pytest` from a fresh checkout
does the right thing.

Run them manually after applying migrations:

    SUPABASE_URL=https://<proj>.supabase.co \\
    SUPABASE_ANON_KEY=<anon> \\
    SUPABASE_SERVICE_ROLE_KEY=<service-role> \\
    SUPABASE_JWT_SECRET=<jwt-secret> \\
        uv run pytest tests/integration -v
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import jwt
import pytest
from supabase import Client, create_client

REQUIRED_ENV = (
    "SUPABASE_URL",
    "SUPABASE_ANON_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_JWT_SECRET",
)


@pytest.fixture(scope="session")
def integration_env() -> dict[str, str]:
    missing = [v for v in REQUIRED_ENV if not os.getenv(v)]
    if missing:
        pytest.skip(
            f"Integration tests require env vars: {', '.join(missing)}. "
            "See SUPABASE-SETUP.md for how to populate them."
        )
    return {k: os.environ[k] for k in REQUIRED_ENV}


@pytest.fixture(scope="session")
def service_sb(integration_env: dict[str, str]) -> Client:
    """Admin client (service-role key). Bypasses RLS — use for fixture setup
    and assertions about ground truth, not for behavior under RLS."""
    return create_client(
        integration_env["SUPABASE_URL"],
        integration_env["SUPABASE_SERVICE_ROLE_KEY"],
    )


@pytest.fixture
def make_user(service_sb: Client) -> Generator[Callable[..., UUID], None, None]:
    """Create test auth users; auto-delete them at teardown.

    Profile rows are dropped via the ON DELETE CASCADE FK from
    profiles.id → auth.users.id. All other Phase 1 tables CASCADE off
    profiles, so deleting the auth user clears the test footprint.
    """
    created: list[UUID] = []

    def _make(email: str | None = None) -> UUID:
        addr = email or f"test+{uuid.uuid4().hex}@candid.test"
        result = service_sb.auth.admin.create_user({"email": addr, "email_confirm": True})
        user_id = UUID(result.user.id)
        created.append(user_id)
        return user_id

    yield _make

    for user_id in created:
        try:
            service_sb.auth.admin.delete_user(str(user_id))
        except Exception:
            # best-effort cleanup; if one test leaves a row behind, the next
            # run still works (test users are uuid-keyed).
            pass


def _mint_user_jwt(user_id: UUID, secret: str, ttl_seconds: int = 3600) -> str:
    now = datetime.now(tz=UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "aud": "authenticated",
            "role": "authenticated",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        },
        secret,
        algorithm="HS256",
    )


@pytest.fixture
def postgrest_get(
    integration_env: dict[str, str],
) -> Callable[..., httpx.Response]:
    """Factory: returns a callable that hits PostgREST as the given user.

    Args:
      user_id: who to authenticate as.
      path: the relative PostgREST path, e.g. 'profiles' or 'groups'.
      **params: PostgREST filter params, e.g. id='eq.<uuid>'.

    The request goes through PostgREST with the anon key as apikey and the
    user's JWT as the Authorization bearer — so RLS is enforced exactly as
    it would be for a real mobile client.
    """
    base_url = integration_env["SUPABASE_URL"].rstrip("/")
    anon_key = integration_env["SUPABASE_ANON_KEY"]
    jwt_secret = integration_env["SUPABASE_JWT_SECRET"]

    def _get(user_id: UUID, path: str, **params: Any) -> httpx.Response:
        token = _mint_user_jwt(user_id, jwt_secret)
        return httpx.get(
            f"{base_url}/rest/v1/{path}",
            headers={
                "apikey": anon_key,
                "Authorization": f"Bearer {token}",
            },
            params=params,
            timeout=15.0,
        )

    return _get
