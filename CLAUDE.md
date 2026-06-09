# CLAUDE.md

Repo-root guidance for Claude Code. Place a copy at the root of **each** repo (`candid-api`, `candid-app`); the command sections are split by repo below. The `/docs` set is the full source of truth — this file is coding-time guardrails, not a summary.

## Project context
**Candid** (working codename) — a private, invite-only app that captures candid group moments during an event. Each member is prompted at their own random times to take a photo or short video; everything lands in a shared, event-scoped feed. We're building the **MVP for a single bachelor party**, distributed via TestFlight + APK, not the public. Read `/docs/02-product-design.md` for behavior and `/docs/04-build-phases.md` for what's in scope right now. Don't build ahead of the current phase.

**DIRECTION (current):** Candid is an event-scoped, BeReal-inspired group camera app that embraces social engagement (reactions, comments) and the prompt pressure mechanic. Differentiator = event-scoping + an end-of-event reel built from "originals" (prompted captures). Authoritative spec: candid-requirements and candid-phases. The original /docs set and all keepsake-first material are superseded background only.

## Non-negotiables — these are bugs, not tradeoffs
If you find yourself doing any of these "for convenience," stop — it's a defect, not a shortcut:
1. **Added a gallery/library picker, or allowed non-live capture for EITHER originals OR voluntary posts.** Capture is live-only in both modes. Bug.
2. **Blurred ORIGINALS and VOLUNTARY posts.** Prompted captures are originals (color, reel-eligible); voluntary captures are forced B&W, daily-capped, behind an admin toggle, and never reel-eligible (MVP). Conflating them is a bug.
3. **Let a member bypass a live unanswered prompt to reach the feed/social surfaces.** The prompt gate must hold until the prompt is answered or its window expires (a missed prompt does NOT re-lock). Basic account/settings must stay reachable. Bug to bypass.
4. **Exceeded the bounded social surface:** reactions must be a FIXED small emoji set (not the full keyboard); comments are text-only; there is NO group chat; there are NO cross-user leaderboards or rankings (MVP). Per-photo reaction counts and reactor identity ARE allowed. Bug to exceed.
5. **Synchronized prompts across users**, or generated them non-independently. Bug.
6. **Used `scheduled_at` or client open-time as the response-window anchor**, or trusted client `captured_at` to decide on-time/late/missed. The server and `dispatched_at` own that. Bug.
7. **Let a missing/denied notification permission fail silently.** Must be detected and surfaced. Bug.
8. **Required location** or blocked a capture on a missing fix. It's optional. Bug.
9. **Put any user data or media outside an EU region**, or shipped a secret (service-role key, R2 secret, Firebase admin creds) into the mobile bundle. Bug.
10. **Pulled a voluntary (B&W) post into the reel, or built the reel from anything other than originals (MVP).** The reel is originals-only for now. Bug.
11. **Built ahead of the current phase, or pulled a post-MVP item** (separate-lane layout, voluntary-in-reel, leaderboards, comment moderation, group chat, reusable groups, retention, public-readiness, map, reverse geocoding, export) into the build without an explicit instruction. See candid-phases. Bug.

## Repo structure
Split repos. See `/docs/03-technical-architecture.md` §7 for the full module layout.
- `candid-api` (Python/FastAPI): `src/app/{routers,services,workers,clients,models,auth,db}`
- `candid-app` (Expo/RN): `app/` (Expo Router) + `src/{features,api,auth,notifications,providers,stores}`

## Stack
Python 3.12 + FastAPI + uv · Supabase (Postgres, magic-link auth, RLS) · Cloudflare R2 via boto3 · firebase-admin (FCM) · Resend (email) · Render (web service + cron workers). Mobile: Expo + dev client, TypeScript strict, Expo Router, React Query, Zustand, react-native-vision-camera, expo-location, expo-secure-store, RN Firebase messaging.

## Build / test / lint commands
**`candid-api`**
- Install: `uv sync`
- Run: `uv run uvicorn app.main:app --reload`
- Test: `uv run pytest`
- Lint/format: `uv run ruff check . && uv run ruff format --check .`

**`candid-app`**
- Install: `npm install` *(TODO: confirm npm vs pnpm)*
- Run: `npx expo start --dev-client`
- Dev client build: `eas build --profile development --platform <ios|android>`
- Lint/format: `npm run lint` / `npm run format`
- Test: *TODO — testing toolchain not yet settled (likely Jest + React Native Testing Library)*

## Key code patterns
- **Auth:** every protected route depends on `get_current_user` (verifies Supabase JWT, HS256). RLS is the second line of defense — write policies, don't rely on app code alone.
- **Uploads:** media goes **direct to R2 via presigned PUT**; the API only mints URLs and verifies the object on `confirm`. Never proxy bytes through the API. `confirm` is **idempotent on `post_id`**.
- **Prompt state:** computed by the workers and the `confirm` handler from server time + `dispatched_at` + group settings. Never in the client.
- **Workers:** generator (hourly), dispatcher (per-minute), expirer (per-minute) as Render cron entrypoints under `src/app/workers/`.
- **Mobile data:** server state via React Query; local/UI/offline-queue state via Zustand. No secrets in the bundle — only the Supabase anon key and public config.
- **Timezones:** prompt windows are per-user local; the client keeps `profiles.timezone` current.

## Testing strategy
- Backend: pytest. Prioritize the prompt state machine (on-time/late/missed boundaries around `dispatched_at`), idempotent `confirm`, RLS policy behavior, and the generator's window/gap/timezone logic — that's where correctness lives.
- Mobile: smoke-test the capture→upload→confirm path and the notification permission priming/recovery flow. *(Toolchain TODO above.)*
- Each build phase has acceptance criteria in `/docs/04-build-phases.md` — treat those as the manual test pass for the phase.

## Conventions
- Backend: ruff (line length 100, py312); Pydantic v2 models for all request/response bodies; thin routers, logic in `services/`.
- Mobile: TypeScript strict; Prettier (single quotes, semicolons, trailing commas, 100 cols); path alias `@/* → src/*`.
- Keep functions small and the API contract explicit. Match existing patterns before introducing new ones.

## Never do
- Don't add infra/services beyond the stack above without flagging it first.
- Don't widen scope past the current phase or pull in a post-MVP item (see candid-phases' post-MVP backlog). If a request implies one, name it and confirm before expanding.
- Don't weaken any non-negotiable above for convenience or speed.
- Don't commit secrets; don't put data/media outside the EU region.
- Don't ship a competitor's trademark in identifiers (replace any leftover `bereal-trips` with `candid`).
- Don't invent product behavior the docs don't specify — ask.
