-- E1 — client_events: a tiny, group-scoped, EU-resident analytics sink.
--
-- Per candid-measurement-and-debrief §3, a handful of small client events
-- (starting with `feed_opened`) are the whole client-instrumentation
-- footprint. No third-party SDK, no PII beyond the group/user the product
-- already holds, nothing leaves the EU — these rows live in the same Supabase
-- Postgres as everything else.
--
-- RLS mirrors `posts` (20260529212325_create_mvp_tables.sql §14): a member can
-- read their group's events, and a user may insert only their own rows in
-- groups they belong to. There is no UPDATE/DELETE policy — events are
-- append-only for clients; the service-role server bypasses RLS for any reads
-- (saved debrief queries) it needs.

CREATE TABLE public.client_events (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  group_id   UUID NOT NULL REFERENCES public.groups(id)   ON DELETE CASCADE,
  user_id    UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  name       TEXT NOT NULL CHECK (char_length(name) BETWEEN 1 AND 64),
  payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Debrief queries slice by group and walk newest-first within the event window.
CREATE INDEX client_events_group_idx ON public.client_events (group_id, created_at DESC);
-- Some reads filter by event name (e.g. just `feed_opened`) across a group.
CREATE INDEX client_events_group_name_idx ON public.client_events (group_id, name);


ALTER TABLE public.client_events ENABLE ROW LEVEL SECURITY;

-- Mirrors posts_select_member: any member of the group can read its events.
CREATE POLICY client_events_select_member ON public.client_events FOR SELECT TO authenticated
  USING (public.is_group_member(group_id));

-- Mirrors posts_insert_self: a user may insert only their own events, and only
-- into a group they belong to.
CREATE POLICY client_events_insert_self ON public.client_events FOR INSERT TO authenticated
  WITH CHECK (user_id = auth.uid() AND public.is_group_member(group_id));
