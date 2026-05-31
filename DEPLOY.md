# Deploying candid-api to Render

Phase 0 deploy. The result: a public `/health` endpoint that returns `{"status":"ok"}`. No auth, no schema, no business logic yet.

## Prerequisites
- A Render account.
- This repo pushed to GitHub (Render needs a repo to point at).
- Secrets ready: Supabase URL/keys, Cloudflare R2 credentials + bucket, Firebase service-account JSON, Resend API key.

## Steps

1. **Push the repo to GitHub.** Render reads `render.yaml` directly from the default branch.

2. **Create the Blueprint.**
   - Render dashboard → **New** → **Blueprint**.
   - Connect the GitHub repo. Render auto-detects `render.yaml`.
   - Confirm the region shows **Frankfurt** for every service (`candid-api` web + `candid-workers` background worker). EU only — do not change.

3. **Create the env group.**
   - Render will prompt for values in the `candid-api-env` env group during Blueprint setup. (You can also create it ahead of time under **Env Groups** → **New Env Group**.)
   - Populate every key marked `sync: false` in `render.yaml`. The full list mirrors `.env.example`:
     - `SUPABASE_URL`, `SUPABASE_JWT_SECRET`, `SUPABASE_SERVICE_ROLE_KEY`
     - `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`
     - `FIREBASE_SERVICE_ACCOUNT_B64` (paste base64-encoded service-account JSON; generate with `base64 -i service-account.json`)
     - `RESEND_API_KEY`, `RESEND_FROM_EMAIL`
   - `DEV_ENDPOINTS_ENABLED` defaults to `false` in `render.yaml` and MUST stay false in prod.
   - For Phase 0 these are not actually used at runtime (clients are lazy), but having them ready avoids a redeploy when the first endpoint that needs them ships.

4. **Apply the blueprint.** Render builds the Docker image from the `Dockerfile`, starts the web service, and starts the `candid-workers` Background Worker which runs the APScheduler-driven generator/dispatcher/expirer jobs in one process.

   > **First-deploy gotcha:** Render Blueprint sometimes won't auto-create a newly-declared `type: worker` service the first time you apply the manifest. If `candid-workers` is missing in the dashboard after the apply, add it manually: **New → Background Worker**, point it at this repo, attach the `candid-api-env` env group, and set the start command to `uv run python -m app.workers.main`. Region must be Frankfurt.

5. **Verify.**
   ```sh
   curl https://candid-api.onrender.com/health
   # → {"status":"ok"}
   ```
   (Use whatever URL Render assigns; the web service name is `candid-api`.)

## What's here as of Phase 4
- `/health` (public).
- `/profile/*`, `/groups/*`, `/posts/*` (Phases 1–3).
- `/devices/register`, DELETE `/devices/{fcm_token}` (Phase 4).
- `/prompts/active`, `/prompts/{id}` (Phase 4 read paths with computed UI state).
- `/dev/fire-prompt` (gated by `DEV_ENDPOINTS_ENABLED`; 404 in prod).
- `candid-workers` Background Worker running generator/dispatcher/expirer ticks.

## What's NOT here yet
Feed UI (Phase 5), offline-queue / retry resilience for confirm (Phase 6).
