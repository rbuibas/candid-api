# 02 — Product Design

This is the behavioral source of truth. It defines how the product *acts*. Schemas here are behavioral sketches; the authoritative DB definition lives in `03-technical-architecture.md`, but the **states and transitions** below are canonical.

---

## 1. The full user journey (MVP)

1. **Invite.** The creator shares an invite link or short code (out of band — group chat, in person).
2. **Install.** Friend installs via TestFlight (iOS) or APK / Play internal testing (Android).
3. **Sign in.** Magic link: enter email → tap link in email → authenticated. No passwords, no SSO.
4. **Name + photo-booth.** Set a display name. Camera permission is requested *here, in context*. Then a **photo-booth strip** fires immediately: 3 front-camera selfies, a couple of seconds to prepare before each. One frame becomes the avatar; the strip becomes their first feed post.
5. **Permissions.** Notification permission is primed (rationale screen) *before* the OS prompt — the loop dies without it. Location permission is primed as optional, framed as "so your moments can go on the map later."
6. **Join the feed.** Land in the group's shared feed. Early in an event it may be sparse — that's expected.
7. **Get prompted.** At random times within the group's daily window, a push arrives: *"Time to capture."* Open → the camera is already up → the prompt is randomly a photo or a short video → capture → optional geocode is grabbed → upload → it appears in the feed.
8. **Look back.** Browse the shared, chronological feed. Late posts wear a badge. Depending on settings, others' posts appear instantly or after a delay.
9. **Curate lightly.** Delete your own posts if you want one gone.
10. **Event ends.** After the end date the group **locks**: no new prompts, no new captures, feed stays viewable.

---

## 2. Core mechanic: the prompt engine (deep treatment)

This is the defining system. Get this right and the product works.

### 2.1 Generation (rolling)

- Prompts are generated **per user, per group**, rolling ~24h ahead. Generation runs on a schedule and on relevant events (user joins, day rolls over).
- For each active member of each active group, ensure the next 24h contains `prompts_per_day` prompts, each at a **random time within the user's local daily window** (`daily_window_start`–`daily_window_end`, in the user's timezone).
- Enforce `min_prompt_gap_minutes` between a user's consecutive prompts — never stack two prompts close together.
- Per prompt, randomly assign `media_type` = photo | video. If video, randomly pick `target_video_length_seconds ≤ max_video_length_seconds`.
- Only generate for groups where `start_date ≤ today ≤ end_date`.

### 2.2 The window anchor (NON-NEGOTIABLE detail)

The response window is anchored to **`dispatched_at`** — the moment the server sends the push — **never** to `scheduled_at` and **never** to when the user opens the prompt.

- Anchoring to `scheduled_at` would let push-delivery delay eat the user's window (penalizing them for latency they didn't cause).
- Anchoring to *open time* is gameable: a user could ignore the buzz for hours, then open at a flattering moment and get a fresh window to stage a shot. That defeats the entire point.
- `dispatched_at` is server-authoritative and not gameable; the generous window (default 5 min) plus the late grace absorbs normal push latency (seconds).

### 2.3 Prompt states & transitions

```
              generator
                 │
                 ▼
           ┌───────────┐   dispatcher sends push
           │ scheduled │ ──────────────┐
           └───────────┘               ▼
                                  ┌──────────┐
                                  │  active  │  (push sent; window open)
                                  └──────────┘
                                   │   │    │
   responded within window ◄───────┘   │    └──────► missed
   (on-time, is_late=false)            │            (no post by late_deadline)
                                       ▼
                              responded after window
                              but before late_deadline
                              (late, is_late=true)
```

- `scheduled` → `active`: dispatcher finds a `scheduled` prompt with `scheduled_at ≤ now`, sends FCM push, records `dispatched_at`. Window timers run from `dispatched_at`.
- `active` → `responded` (on-time): user confirms a post and the **server's receipt time** of the confirm is `≤ dispatched_at + response_window_seconds`. `is_late = false`.
- `active` → `late`: server receipt time is within `(on-time deadline, late_deadline]`. Post is created, `is_late = true`, shown with a "late" badge.
- `active` → `missed`: no confirmed post by `late_deadline`. Expirer marks it `missed`.

**Server owns these decisions.** Lateness is computed from server-side confirm-receipt time, not from the client-supplied `captured_at`. `captured_at` is stored for display only.

### 2.4 Timing fields

| Field | Meaning |
|---|---|
| `scheduled_at` | Intended fire time (from generation). |
| `dispatched_at` | When the push was actually sent. **Window anchor.** |
| on-time deadline | `dispatched_at + response_window_seconds` (default 300s). |
| `late_deadline` | on-time deadline `+ late_window_seconds`. |

### 2.5 Default settings (confirm before locking in code)

| Setting | Default | Range intent |
|---|---|---|
| `prompts_per_day` | 4 | a few, not spammy |
| `daily_window` | 10:00–01:00 (local) | party hours, can wrap past midnight |
| `min_prompt_gap_minutes` | 45 | avoid clustering |
| `response_window_seconds` | 300 (5 min) | generous enough for real life |
| `late_window_seconds` | 1800 (30 min) | late-but-flagged grace |
| `max_video_length_seconds` | 10 | short, cheap, candid |

> A wrap-past-midnight daily window (e.g., 10:00–01:00) is a real party requirement — the generator must handle windows where `end < start` meaning "next day."

---

## 3. Core mechanic: the photo-booth join capture

Solves activation — the user experiences the capture loop in their *first* session instead of waiting hours for a random prompt.

- Fires **immediately on join**, after camera permission is granted, regardless of whether the group is currently in its active window.
- **Front camera.** A 3-shot strip: a short countdown ("get ready… 3-2-1"), capture, repeat ×3. Behaves like a physical photo booth — only a couple of seconds to prepare each frame, no retakes.
- The 3 frames are stored as **one post** (a strip), not three separate posts.
- The user picks (or the app auto-picks) **one frame as their avatar**; onboarding doubles as profile setup.
- Subject to the same delete rule as any post.

UX intent: playful, fast, a little chaotic — sets the tone that this app is about real faces in real moments, not curated grids.

---

## 4. Core mechanic: capture & media

- **In-app camera only.** No gallery/library access, ever. Capture must be live. (Non-negotiable.)
- **No dual camera** in v1 (front+back simultaneously is a future feature).
- **Photo:** single still.
- **Video:** records up to `target_video_length_seconds`, auto-stops at the cap. No manual length choice — the user just records until it stops.
- **Geocode (optional):** at confirm time, request current location via `expo-location` with a short timeout (~3s). If granted and a fix arrives, attach `latitude`/`longitude`/`accuracy`. If denied or no fix, **post anyway without coordinates** — never block on location.

### Upload flow (direct-to-R2, signed URL)

1. Client → `POST /prompts/{id}/upload-url` → server returns `{ post_id, upload_url, storage_path }` (presigned PUT, ~10 min TTL).
2. Client PUTs the media bytes directly to R2.
3. Client (optionally) grabs location, then → `POST /prompts/{id}/confirm` with `{ post_id, captured_at, duration_seconds?, latitude?, longitude?, accuracy? }`.
4. Server verifies the R2 object exists, sets prompt state (`responded`/`late`) from **server receipt time**, computes `visible_at` (see feed), persists location if present, returns the post.

`confirm` must be **idempotent** on `post_id` so a client retry after a flaky connection doesn't create duplicates.

---

## 5. Core mechanic: the feed

- Shared within the group, **chronological, newest first**.
- Group-scoped — only members see it.
- **View delay:** a post is hidden until `visible_at = server_confirm_time + view_delay_seconds`. Default `view_delay_seconds = 0` (instant). The delay is a knob to optionally hold posts so everyone reveals together; off by default for MVP simplicity.
- **Late posts** appear in chronological order with a small "late" badge.
- **Delete own post:** the author can delete their post; it disappears from everyone's feed and the media is removed from R2.
- **No** comments, reactions, likes, or counts. (Non-negotiable.)
- Pagination is cursor-based on the visibility/capture ordering for stable scroll.
- Media is served via short-lived signed GET URLs (~1h), refreshed on demand.

---

## 6. Lifecycle states

### Group

| State | Condition | Behavior |
|---|---|---|
| `upcoming` | `today < start_date` | Members can join; photo-booth fires on join; **no** random prompts yet. |
| `active` | `start_date ≤ today ≤ end_date` | Random prompts generate & fire; captures accepted. |
| `locked` | `today > end_date` | Read-only. No prompts, no captures. Feed viewable. Joining still possible but yields only the photo-booth post. |
| `deleted` | creator deletes | Group + all media purged. (Creator-only; the one admin action in MVP.) |

### Membership

MVP has a single state: **active**. Leave/kick/co-admin/transfer are deferred (`05`). The creator is just the member who made the group and can delete it.

### Prompt

`scheduled → active → (responded | late | missed)` as defined in §2.3.

### Post

- Created on `confirm` (state `visible` once `now ≥ visible_at`; before that, hidden).
- `deleted` when the author removes it (media purged).

---

## 7. Edge cases & how to handle them (MVP)

| Situation | Handling |
|---|---|
| **Notifications denied** | Detect on launch; show a persistent, friendly explainer that prompts won't arrive without it; deep-link to settings. The loop must never *silently* fail. |
| **No signal at prompt time** | Capture works offline; the upload **queues** and flushes on reconnect. Lateness is still judged by server receipt of `confirm`, so a long offline gap may land as `late`/`missed` — acceptable and honest. (Offline queue is MVP-critical; see Phase in `04`.) |
| **Location denied / no fix in ~3s** | Post without coordinates. Never block. |
| **App killed when push arrives** | Prompt is waiting on next open; because the window is `dispatched_at`-anchored, it may already be `late` or `missed` — show the correct state, don't pretend it's fresh. |
| **Video interrupted (incoming call)** | Save whatever recorded if past a tiny minimum; otherwise discard and let them re-trigger from the still-open prompt. |
| **Joins after `start_date`** | Photo-booth fires; rolling generation begins from join time. |
| **Joins after `end_date` (locked)** | Photo-booth only; no random prompts; feed is read-only. |
| **Clock skew on device** | Irrelevant to state — server time governs on-time/late/missed and visibility. |
| **Duplicate/retried `confirm`** | Idempotent on `post_id`; no duplicate posts. |
| **Two prompts too close** | Prevented at generation by `min_prompt_gap_minutes`. |
| **Empty/solo feed early in event** | Expected; photo-booth seeds at least the joiner's strip. No artificial seeding beyond that. |

---

## 8. UX patterns that carry the product

- **Camera-first capture screen.** Opening a prompt lands directly on a live camera with the countdown/limit visible — minimum taps between buzz and shot.
- **Countdown urgency, not anxiety.** Show remaining window time, but the 5-min default and the late-grace mean missing is rare and forgiving.
- **Contextual permission priming.** Camera at the photo-booth; notifications behind a one-line rationale before the OS ask; location framed as optional map magic. Never a wall of OS prompts up front.
- **Honest empty/late/missed states.** A missed prompt is shown plainly ("you missed this one") without nagging; a late post is badged, not hidden.
- **The feed is the reward.** Make returning to the feed feel like flipping through a developing roll of film, not scrolling a timeline.
