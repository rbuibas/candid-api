# 04 — Build Phases

Each phase is **independently shippable** and carries **acceptance criteria** (manual scenarios that must pass), not just scope. Don't pull anything from `05-future-features.md` into a phase without an explicit instruction. Acceptance criteria are the definition of done.

> Repos: `candid-api` (backend) and `candid-app` (mobile). Phases touch one or both.

---

## Phase 0 — Foundation

**Scope**
- Scaffold both repos (see the existing Phase-1 scaffolding prompts; rename the `bereal-trips` placeholder to `candid`).
- Backend: FastAPI skeleton, config, lazy clients (Supabase, R2, Firebase, Resend), `/health`, Dockerfile, `render.yaml`.
- Mobile: Expo + dev client, Expo Router shell, providers (React Query), Supabase client, API client, a health-check screen.
- Set Supabase region and R2 bucket jurisdiction to **EU**.
- Deploy the API to Render; build dev clients via EAS.

**Acceptance criteria**
- Hitting the deployed `/health` returns `{status:"ok"}`.
- The mobile health screen, pointed at the deployed API, displays "ok" end-to-end.
- Dev client builds install and launch on a real iOS device and a real Android device.

**Non-goals**
- Any auth, schema, or business logic.

---

## Phase 1 — Schema + Auth

**Scope**
- Supabase migrations for all MVP tables (`02`/`03`), enums, indexes, and RLS policies.
- Magic-link auth on mobile; Resend wired up for delivery.
- Backend JWT verification (`get_current_user`).
- Profile row created on first sign-in; `timezone` captured/updated from the device.

**Acceptance criteria**
- A new user enters their email, taps the magic link, and lands authenticated.
- A `profiles` row exists for them with their timezone set.
- An authenticated request to a protected backend route succeeds; an unauthenticated one returns 401.
- RLS blocks reading another user's profile/group data directly.

**Non-goals**
- Groups, prompts, capture, feed. SSO (deferred).

---

## Phase 2 — Groups & membership

**Scope**
- Create group with settings (defaults from `02` §2.5).
- Generate an active invite code/link.
- Join via code.
- Group detail + member list.
- Creator can delete the group (purges media — relevant later, but wire the action).

**Acceptance criteria**
- Creator creates a group and sees its invite link.
- A second user joins via the link and both appear in the member list for both users.
- A non-member cannot read the group (enforced by RLS).
- Group lifecycle (`upcoming`/`active`/`locked`) derives correctly from the dates.

**Non-goals**
- Prompts, capture, feed. Leave/kick/co-admin/transfer (deferred).

---

## Phase 3 — Capture & media pipeline

**Scope**
- In-app camera (vision-camera): photo + video with length cap. No gallery.
- Photo-booth strip flow (3 front-camera frames → one post; one frame → avatar).
- Optional geocode at capture (expo-location, ~3s timeout, never blocking).
- Presigned-URL upload to R2; idempotent `confirm`.
- A temporary manual "trigger capture" affordance so this phase is testable without the scheduler.

**Acceptance criteria**
- User can capture a photo and a (capped) video and both upload to R2; `posts` rows are created and the media is retrievable via signed GET URL.
- Photo-booth produces one strip post and sets the avatar.
- With location denied, a capture still succeeds with null coordinates.
- A retried `confirm` does not create a duplicate post.

**Non-goals**
- Prompt scheduling/push (next phase). Feed UI polish. Offline queue (Phase 6).

---

## Phase 4 — Prompt engine + push

**Scope**
- Generator, dispatcher, expirer workers (Render cron) per `02`/`03`.
- Device registration (FCM tokens); push send on dispatch.
- `dispatched_at`-anchored window; server-authoritative on-time/late/missed.
- Photo-booth-on-join wired to fire immediately.
- Notification permission priming + denial recovery.

**Acceptance criteria**
- Joining a group fires the photo-booth immediately.
- Within an active group, prompts arrive as push notifications at random times respecting the daily window and `min_prompt_gap_minutes`.
- Responding within the window records on-time; responding after the window but before the late deadline records **late**; ignoring it flips to **missed** server-side.
- No prompts generate outside `start_date`–`end_date`.
- Denying notifications surfaces a clear in-app explainer (no silent failure).

**Non-goals**
- Feed (next phase). Offline resilience (Phase 6).

---

## Phase 5 — Feed

**Scope**
- Feed endpoint: chronological, newest-first, respects `visible_at`, cursor pagination, signed read URLs.
- Mobile feed UI; photo + video + strip rendering; "late" badge.
- Delete own post (purges R2 objects).

**Acceptance criteria**
- Posts appear newest-first; a post with a view delay stays hidden until `visible_at`.
- Late posts show the badge; on-time posts don't.
- Deleting your own post removes it from every member's feed and deletes its R2 media.
- A member sees only their group's posts (RLS).

**Non-goals**
- Comments/reactions/likes (never — `01`/`05`). "Someone posted" notifications (deferred).

---

## Phase 6 — MVP hardening & release

**Scope**
- **Offline capture-and-queue** (MVP-critical for the abroad use case): capture persists locally and flushes `confirm` on reconnect.
- Robust error/empty/missed states; upload retries.
- Group `locked` (read-only) behavior after `end_date`.
- Client-side video compression to keep R2 objects small.
- Release builds: TestFlight (iOS) + signed APK / Play internal testing (Android).

**Acceptance criteria**
- A full end-to-end "party simulation" with 3+ devices passes: joins, photo-booths, random prompts, on-time/late/missed, feed, deletes.
- Capturing with no connectivity queues the post; it uploads and appears once back online.
- After `end_date`, the group is read-only — no prompts, no captures, feed still viewable.
- Friends can install via a TestFlight invite and a shared APK and complete onboarding unaided.

**Non-goals**
- Everything in `05-future-features.md`.
