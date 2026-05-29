"""Integration test: handle_new_user trigger auto-creates a profiles row."""

from collections.abc import Callable
from uuid import UUID

import pytest
from supabase import Client

pytestmark = pytest.mark.integration


def test_handle_new_user_creates_profile(
    service_sb: Client, make_user: Callable[..., UUID]
) -> None:
    user_id = make_user()

    result = service_sb.table("profiles").select("*").eq("id", str(user_id)).single().execute()
    assert result.data is not None
    assert result.data["id"] == str(user_id)
    assert result.data["timezone"] == "UTC"
    assert result.data["display_name"] is None
    assert result.data["avatar_url"] is None
