# 03 — Technical Architecture

The plumbing behind the behavior in `02`. Where this doc and `02` overlap, `02` is canonical for *behavior* and this doc is canonical for *structure*.

---

## 1. Stack & rationale

| Layer | Choice | Why for this product |
|---|---|---|
| Mobile | React Native + **Expo** (dev client) | Default mobile stack; dev client unlocks native modules (vision-camera, RN Firebase) while keeping Expo tooling + EAS Build for TestFlight/APK. |
| Backend | **Python + FastAPI** | Chosen for this project. Async, fast to write, Pydantic models map cleanly to the API contract. |
| DB & Auth | **Supabase** (Postgres) | Managed Postgres + magic-link auth + RLS in one. EU region. |
| Media | **Cloudflare R2** | S3-compatible (boto3). **Zero egress fees** — the single biggest cost de-risk given media + video. EU jurisdiction. |
| Push | **Firebase Cloud Messaging** | Only sane cross-platform push incl. iOS (via APNs key). The loop depends on it. |
| Email | **Resend** | Reliable magic-link delivery (Supabase's built-in email is rate-limited/branded). |
| Hosting | **Render** | Web service (Docker) for the API + Cron Jobs for the prompt workers. |
| Capture | **react-native-vision-camera** | Fine-grained photo/video control, length caps, no gallery dependency. |
| Location | **expo-location** | Foreground, optional, short-timeout fix at capture. |

**Deviations from default stack:** no Svelte web frontend (mobile-only product); Stripe present in the default stack but unused until pay-to-keep (`05`).

---

## 2. Data models (schema sketch — MVP)

Postgres / Supabase. Only the MVP tables and columns. Future columns noted where cheap to anticipate.

### `profiles` (extends `auth.users`)
- `id` uuid PK → `auth.users.id`
- `display_name` text
- `avatar_url` text (points at a frame from the photo-booth strip in R2)
- `timezone` text default `'UTC'` (IANA; client updates on app start — drives prompt windows)
- `created_at`, `updated_at`

### `groups`
- `id` uuid PK
- `name` text
- `created_by` uuid → `profiles.id`
- `start_date` date, `end_date` date
- `prompts_per_day` int
- `daily_window_start` time, `daily_window_end` time (if `end < start`, window wraps past midnight)
- `min_prompt_gap_minutes` int
- `response_window_seconds` int
- `late_window_seconds` int
- `max_video_length_seconds` int
- `view_delay_seconds` int (default 0)
- `created_at`, `updated_at`
- *Lifecycle is derived from dates (see `02` §6), not stored.*

### `group_members`
- `id` uuid PK
- `group_id` uuid → `groups.id`
- `user_id` uuid → `profiles.id`
- `joined_at` timestamptz
- Unique on (`group_id`, `user_id`)

### `invite_codes`
- `id` uuid PK
- `group_id` uuid → `groups.id`
- `code` text unique (~8 alphanumeric)
- `active` bool
- `created_at`

### `prompts`
- `id` uuid PK
- `group_id` uuid, `user_id` uuid
- `scheduled_at` timestamptz
- `dispatched_at` timestamptz null (set when push sent — **window anchor**)
- `local_date` date (user-local date the prompt belongs to)
- `media_type` enum(`photo`,`video`)
- `target_video_length_seconds` int null
- `status` enum(`scheduled`,`active`,`responded`,`late`,`missed`)
- `created_at`
- *Deadlines (on-time, late) are computed from `dispatched_at` + group settings; store `dispatched_at` and derive, or denormalize if it simplifies the expirer query.*

### `posts`
- `id` uuid PK
- `prompt_id` uuid null (null = photo-booth strip / future intentional post)
- `group_id` uuid, `user_id` uuid
- `kind` enum(`prompt`,`photobooth`) — extensible for future `intentional`
- `media_type` enum(`photo`,`video`,`strip`)
- `storage_path` text (R2 key), `thumbnail_path` text null
- `duration_seconds` int null
- `captured_at` timestamptz (client-supplied, display only)
- `is_late` bool default false
- `visible_at` timestamptz (server confirm time + `view_delay_seconds`)
- `latitude` double precision null, `longitude` double precision null, `location_accuracy_meters` int null
- `deleted_at` timestamptz null (author delete → media purged; row tombstoned)
- `created_at`

### `devices`
- `id` uuid PK
- `user_id` uuid
- `fcm_token` text unique
- `platform` enum(`ios`,`android`)
- `last_seen_at`, `created_at`

> The geocode columns ship in MVP (cheap) even though the map is a future feature. A PostGIS `geom` column + spatial index is the only schema change the map will later need.

---

## 3. Architectural patterns

- **Stateless API + JWT.** FastAPI verifies the Supabase JWT (HS256, `SUPABASE_JWT_SECRET`) on every request; `get_current_user` dependency yields the authenticated profile. RLS in Postgres is the second line of defense.
- **Direct-to-R2 uploads via presigned URLs.** Media bytes never transit the API — the backend only mints presigned PUT URLs and verifies the object on `confirm`. Keeps the API cheap and fast.
- **Server-authoritative prompt state.** On-time/late/missed and `visible_at` are computed from server time, never trusted from the client. (See `02` §2.3 — non-negotiable.)
- **Three logical workers (Render Cron Jobs):**
  - **Generator** — hourly (and on join): roll prompts ~24h ahead per active member, respecting window/gap/timezone.
  - **Dispatcher** — every minute: `scheduled` prompts with `scheduled_at ≤ now` → send FCM, set `dispatched_at`, → `active`.
  - **Expirer** — every minute: `active` prompts past `late_deadline` with no post → `missed`.
- **Idempotent confirm.** Keyed on `post_id` to survive client retries.
- **Offline capture queue (client).** Captured media + pending `confirm` persist locally and flush on reconnect.

---

## 4. Third-party integrations

| Service | Used for | Key config |
|---|---|---|
| Supabase | DB, auth (magic link), RLS | EU region; JWT secret; service-role key (backend only). |
| Resend | Magic-link email | Verified sending domain (Porkbun DNS); API key. Configure Supabase Auth SMTP/hook to send via Resend. |
| Cloudflare R2 | Media | boto3 S3 client; endpoint `https://{account_id}.r2.cloudflarestorage.com`; `region_name="auto"`; bucket in EU jurisdiction. |
| Firebase (FCM) | Push | APNs auth key uploaded to Firebase for iOS; `google-services.json` / `GoogleService-Info.plist` in the app; `firebase-admin` on backend. |
| Render | Hosting | Web service (Docker) + cron jobs; env group for secrets. |

---

## 5. Security & privacy

- **EU-resident data, by default.** Supabase region and R2 bucket jurisdiction both EU. (Non-negotiable — see `CLAUDE.md`.)
- **Minimal PII.** Email (for magic link), display name, avatar, device push token, and user-generated media + optional coordinates. Nothing else.
- **RLS everywhere.** A user can read/write only rows for groups they belong to; posts are readable only by group members; profiles minimally exposed.
- **Signed URLs only.** No public bucket. Upload (PUT, ~10 min) and read (GET, ~1h) URLs are short-lived and minted per request.
- **Secrets server-side.** Service-role key, R2 secret, Firebase admin creds live only on the backend / Render env group — never in the app bundle.
- **Author-controlled deletion.** Deleting a post purges its R2 objects, not just the row.
- **Location is optional and never required.** Treated as sensitive; never blocks a capture.
- **Private distribution.** Invite-only; no public listing in MVP. The "personal/household" context is what lets the heavier compliance surface (formal erasure flows, age gating, moderation infra) defer to `05` — revisit *before* any public launch.

---

## 6. Cost & scaling notes

- **R2 zero egress** is the headline win — serving media (and video) doesn't rack up bandwidth bills. Storage still accrues; MVP has no retention/purge (trivial at friend-group scale), but **media-retention is the first thing to add as usage grows** (parked in `05`).
- **Video is the cost driver.** Hard length cap (default 10s) + transcode/compress on the client or via a worker keeps objects small. ~half of prompts are video by design.
- **FCM is free**; fan-out is tiny at MVP scale.
- **Supabase / Render free-to-cheap tiers** comfortably cover one party. Workers are minute-level cron — negligible compute.
- **Failure modes to watch as it grows:** unbounded media storage (no purge), per-minute worker cost at many groups, and signed-URL minting volume on large feeds (mitigate with client-side URL caching within TTL).

---

## 7. Suggested module layout

### Backend (`candid-api`)
```
src/app/
  main.py                 # app factory, CORS, router mounting
  config.py               # pydantic-settings
  auth/jwt.py             # Supabase JWT verification dependency
  clients/                # supabase, r2 (boto3), firebase, resend
  routers/                # health, profile, groups, invites, prompts, feed
  services/               # group_, prompt_ (generation), media_, feed_ logic
  workers/                # generator, dispatcher, expirer (Render cron entrypoints)
  models/                 # pydantic request/response models
  db/                     # query helpers / repository layer
tests/
```

### Mobile (`candid-app`)
```
app/                      # Expo Router routes (auth gate, group, capture, feed)
src/
  api/                    # typed client + endpoints
  auth/                   # supabase client (SecureStore), session
  features/
    onboarding/           # magic link, name, photo-booth
    prompt/               # active-prompt handling, countdown
    capture/              # vision-camera, video cap, geocode, upload+offline queue
    feed/                 # feed list, post view, delete
  notifications/          # FCM registration, permission priming/recovery
  providers/              # React Query, etc.
  stores/                 # Zustand (UI/onboarding/offline-queue state)
  theme/  utils/  types/
```

Split repos (`candid-api`, `candid-app`); `CLAUDE.md` lives at the root of each.
