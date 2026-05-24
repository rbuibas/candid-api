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
   - Confirm the region shows **Frankfurt** for every service (web + the three cron jobs). EU only — do not change.

3. **Create the env group.**
   - Render will prompt for values in the `candid-api-env` env group during Blueprint setup. (You can also create it ahead of time under **Env Groups** → **New Env Group**.)
   - Populate every key marked `sync: false` in `render.yaml`. The full list mirrors `.env.example`:
     - `SUPABASE_URL`, `SUPABASE_JWT_SECRET`, `SUPABASE_SERVICE_ROLE_KEY`
     - `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`
     - `FIREBASE_CREDENTIALS_JSON` (paste the whole JSON as a single-line string)
     - `RESEND_API_KEY`, `RESEND_FROM_EMAIL`
   - For Phase 0 these are not actually used at runtime (clients are lazy), but having them ready avoids a redeploy when the first endpoint that needs them ships.

4. **Apply the blueprint.** Render builds the Docker image from the `Dockerfile`, starts the web service, and schedules the three cron jobs.

5. **Verify.**
   ```sh
   curl https://candid-api.onrender.com/health
   # → {"status":"ok"}
   ```
   (Use whatever URL Render assigns; the web service name is `candid-api`.)

## What's NOT here yet
Cron jobs are wired but are no-op stubs (they print `noop` and exit). Auth, DB schema, RLS policies, and real prompt/media/feed logic land in later phases per `docs/04-build-phases.md`. Do not add anything beyond Phase 0 without explicit instruction.
