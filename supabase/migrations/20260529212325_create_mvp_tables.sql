-- Phase 1 (part 2) — remaining MVP tables, enums, RLS, helper.
--
-- Part 1 (20260526195531_create_profiles.sql) shipped profiles + the
-- handle_new_user trigger + RLS on profiles. This migration adds the
-- five other Phase 1 MVP tables (groups, group_members, invite_codes,
-- prompts, posts, devices) per /docs/03-technical-architecture.md §2,
-- plus the enum types they reference and RLS on every one.
--
-- The is_group_member SECURITY DEFINER helper bypasses RLS on
-- group_members when called from a policy on another table, sidestepping
-- the recursion that would otherwise happen.
--
-- Service-role (the FastAPI server) bypasses all RLS; these policies
-- bite when mobile/other clients hit PostgREST with the anon key.


-- ===========================================================================
-- 1. Enums
-- ===========================================================================

CREATE TYPE public.prompt_status   AS ENUM ('scheduled', 'active', 'responded', 'late', 'missed');
CREATE TYPE public.media_type      AS ENUM ('photo', 'video');
CREATE TYPE public.post_kind       AS ENUM ('prompt', 'photobooth');
CREATE TYPE public.post_media_type AS ENUM ('photo', 'video', 'strip');
CREATE TYPE public.device_platform AS ENUM ('ios', 'android');


-- ===========================================================================
-- 2. groups (defaults from /docs/02-product-design.md §2.5)
-- ===========================================================================

CREATE TABLE public.groups (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name                     TEXT NOT NULL,
  created_by               UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  start_date               DATE NOT NULL,
  end_date                 DATE NOT NULL,
  prompts_per_day          INT  NOT NULL DEFAULT 4    CHECK (prompts_per_day > 0),
  daily_window_start       TIME NOT NULL DEFAULT '10:00',
  daily_window_end         TIME NOT NULL DEFAULT '01:00',
  min_prompt_gap_minutes   INT  NOT NULL DEFAULT 45   CHECK (min_prompt_gap_minutes >= 0),
  response_window_seconds  INT  NOT NULL DEFAULT 300  CHECK (response_window_seconds > 0),
  late_window_seconds      INT  NOT NULL DEFAULT 1800 CHECK (late_window_seconds >= 0),
  max_video_length_seconds INT  NOT NULL DEFAULT 10   CHECK (max_video_length_seconds > 0),
  view_delay_seconds       INT  NOT NULL DEFAULT 0    CHECK (view_delay_seconds >= 0),
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TRIGGER groups_set_updated_at
  BEFORE UPDATE ON public.groups
  FOR EACH ROW EXECUTE FUNCTION public.set_updated_at();


-- ===========================================================================
-- 3. group_members
-- ===========================================================================

CREATE TABLE public.group_members (
  id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  group_id  UUID NOT NULL REFERENCES public.groups(id) ON DELETE CASCADE,
  user_id   UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (group_id, user_id)
);

CREATE INDEX group_members_user_idx ON public.group_members (user_id, group_id);


-- ===========================================================================
-- 4. invite_codes
-- ===========================================================================

CREATE TABLE public.invite_codes (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  group_id   UUID NOT NULL REFERENCES public.groups(id) ON DELETE CASCADE,
  code       TEXT NOT NULL UNIQUE CHECK (char_length(code) BETWEEN 6 AND 16),
  active     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Partial index makes "find an active invite for this group" a fast lookup
-- without bloating the index with deactivated codes.
CREATE INDEX invite_codes_group_active_idx
  ON public.invite_codes (group_id) WHERE active;


-- ===========================================================================
-- 5. prompts
-- ===========================================================================

CREATE TABLE public.prompts (
  id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  group_id                    UUID NOT NULL REFERENCES public.groups(id) ON DELETE CASCADE,
  user_id                     UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  scheduled_at                TIMESTAMPTZ NOT NULL,
  dispatched_at               TIMESTAMPTZ,  -- null until the push fires; window anchor
  local_date                  DATE NOT NULL,
  media_type                  public.media_type NOT NULL,
  target_video_length_seconds INT,
  status                      public.prompt_status NOT NULL DEFAULT 'scheduled',
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX prompts_user_status_idx ON public.prompts (user_id, status);
-- Dispatcher cron picks up prompts whose scheduled_at has passed and dispatched_at is null.
CREATE INDEX prompts_dispatcher_idx  ON public.prompts (scheduled_at) WHERE dispatched_at IS NULL;
-- Expirer cron walks active/late prompts to push them into late/missed.
CREATE INDEX prompts_expirer_idx     ON public.prompts (status, dispatched_at)
  WHERE status IN ('active', 'late');


-- ===========================================================================
-- 6. posts
-- ===========================================================================

CREATE TABLE public.posts (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  prompt_id                UUID REFERENCES public.prompts(id) ON DELETE SET NULL,
  group_id                 UUID NOT NULL REFERENCES public.groups(id) ON DELETE CASCADE,
  user_id                  UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  kind                     public.post_kind NOT NULL,
  media_type               public.post_media_type NOT NULL,
  storage_path             TEXT NOT NULL,
  thumbnail_path           TEXT,
  duration_seconds         INT,
  captured_at              TIMESTAMPTZ NOT NULL,
  is_late                  BOOLEAN NOT NULL DEFAULT FALSE,
  visible_at               TIMESTAMPTZ NOT NULL,
  latitude                 DOUBLE PRECISION,
  longitude                DOUBLE PRECISION,
  location_accuracy_meters INT,
  deleted_at               TIMESTAMPTZ,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Feed query: live posts for a group, newest visible first.
CREATE INDEX posts_feed_idx   ON public.posts (group_id, visible_at DESC) WHERE deleted_at IS NULL;
-- Author's own posts (for delete UI listing).
CREATE INDEX posts_author_idx ON public.posts (user_id, created_at DESC);


-- ===========================================================================
-- 7. devices
-- ===========================================================================

CREATE TABLE public.devices (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
  fcm_token    TEXT NOT NULL UNIQUE,
  platform     public.device_platform NOT NULL,
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX devices_user_idx ON public.devices (user_id);


-- ===========================================================================
-- 8. is_group_member helper
--    SECURITY DEFINER so it can read group_members regardless of who's asking;
--    this avoids recursion when group_members's own RLS policy calls it.
-- ===========================================================================

CREATE OR REPLACE FUNCTION public.is_group_member(group_id_to_check UUID)
RETURNS BOOLEAN
LANGUAGE SQL
SECURITY DEFINER
SET search_path = public
STABLE
AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.group_members
    WHERE group_id = group_id_to_check
      AND user_id = auth.uid()
  );
$$;

GRANT EXECUTE ON FUNCTION public.is_group_member(UUID) TO authenticated;


-- ===========================================================================
-- 9. RLS: enable on every table
-- ===========================================================================

ALTER TABLE public.groups        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.group_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.invite_codes  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.prompts       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.posts         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.devices       ENABLE ROW LEVEL SECURITY;


-- ===========================================================================
-- 10. Policies — groups
-- ===========================================================================

CREATE POLICY groups_select_member ON public.groups FOR SELECT TO authenticated
  USING (public.is_group_member(id));

CREATE POLICY groups_insert_creator ON public.groups FOR INSERT TO authenticated
  WITH CHECK (created_by = auth.uid());

CREATE POLICY groups_update_creator ON public.groups FOR UPDATE TO authenticated
  USING (created_by = auth.uid())
  WITH CHECK (created_by = auth.uid());

CREATE POLICY groups_delete_creator ON public.groups FOR DELETE TO authenticated
  USING (created_by = auth.uid());


-- ===========================================================================
-- 11. Policies — group_members
-- ===========================================================================

CREATE POLICY gm_select_member ON public.group_members FOR SELECT TO authenticated
  USING (public.is_group_member(group_id));

-- A user can insert their own membership row (Phase 2 invite-code flow gates this
-- at the application layer; RLS just makes sure they can't add anyone else).
CREATE POLICY gm_insert_self ON public.group_members FOR INSERT TO authenticated
  WITH CHECK (user_id = auth.uid());

CREATE POLICY gm_delete_self ON public.group_members FOR DELETE TO authenticated
  USING (user_id = auth.uid());


-- ===========================================================================
-- 12. Policies — invite_codes
-- ===========================================================================

CREATE POLICY ic_select_member ON public.invite_codes FOR SELECT TO authenticated
  USING (public.is_group_member(group_id));

CREATE POLICY ic_insert_creator ON public.invite_codes FOR INSERT TO authenticated
  WITH CHECK (
    EXISTS (
      SELECT 1 FROM public.groups g
      WHERE g.id = invite_codes.group_id
        AND g.created_by = auth.uid()
    )
  );

CREATE POLICY ic_update_creator ON public.invite_codes FOR UPDATE TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM public.groups g
      WHERE g.id = invite_codes.group_id
        AND g.created_by = auth.uid()
    )
  );


-- ===========================================================================
-- 13. Policies — prompts
--     Read-only for authenticated; workers (service-role) bypass RLS for writes.
-- ===========================================================================

CREATE POLICY prompts_select_own ON public.prompts FOR SELECT TO authenticated
  USING (user_id = auth.uid());


-- ===========================================================================
-- 14. Policies — posts
-- ===========================================================================

CREATE POLICY posts_select_member ON public.posts FOR SELECT TO authenticated
  USING (public.is_group_member(group_id) AND deleted_at IS NULL);

CREATE POLICY posts_insert_self ON public.posts FOR INSERT TO authenticated
  WITH CHECK (user_id = auth.uid() AND public.is_group_member(group_id));

-- UPDATE covers the soft-delete write path (set deleted_at).
CREATE POLICY posts_update_author ON public.posts FOR UPDATE TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());


-- ===========================================================================
-- 15. Policies — devices
-- ===========================================================================

CREATE POLICY devices_select_own ON public.devices FOR SELECT TO authenticated
  USING (user_id = auth.uid());

CREATE POLICY devices_insert_own ON public.devices FOR INSERT TO authenticated
  WITH CHECK (user_id = auth.uid());

CREATE POLICY devices_update_own ON public.devices FOR UPDATE TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

CREATE POLICY devices_delete_own ON public.devices FOR DELETE TO authenticated
  USING (user_id = auth.uid());
