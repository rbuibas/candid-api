"""Static assertions on the client_events migration.

No database required — we read the SQL and assert the shape the feature relies
on: the column contract, FK cascades, RLS enabled, and policies that **mirror
posts** (member-read, self+member-insert) with no client UPDATE/DELETE. These
catch a migration edit that would silently weaken the security model.
"""

import re
from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260610120000_create_client_events.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def _normalize(text: str) -> str:
    """Collapse whitespace and lowercase, so assertions ignore formatting."""
    return re.sub(r"\s+", " ", text).lower()


def test_migration_file_exists() -> None:
    assert MIGRATION.is_file(), f"missing migration: {MIGRATION}"


def test_table_and_columns() -> None:
    sql = _normalize(_sql())
    assert "create table public.client_events" in sql
    # Column contract the app + debrief queries depend on.
    assert "id uuid primary key" in sql
    assert "group_id uuid not null references public.groups(id) on delete cascade" in sql
    assert "user_id uuid not null references public.profiles(id) on delete cascade" in sql
    assert "name text not null" in sql
    assert "payload jsonb not null" in sql
    assert "created_at timestamptz not null" in sql


def test_rls_enabled() -> None:
    sql = _normalize(_sql())
    assert "alter table public.client_events enable row level security" in sql


def test_policies_mirror_posts() -> None:
    sql = _normalize(_sql())
    # SELECT: any group member can read (mirrors posts_select_member).
    assert "for select to authenticated using (public.is_group_member(group_id))" in sql
    # INSERT: only your own rows, only in groups you belong to
    # (mirrors posts_insert_self).
    assert (
        "for insert to authenticated with check (user_id = auth.uid() "
        "and public.is_group_member(group_id))" in sql
    )


def test_no_client_mutation_policies() -> None:
    """Events are append-only for clients — no UPDATE/DELETE policy exists."""
    sql = _normalize(_sql())
    assert "for update" not in sql
    assert "for delete" not in sql
