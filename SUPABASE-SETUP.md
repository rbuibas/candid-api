# Supabase + Resend setup (one-time, operator)

This runbook walks through the manual setup needed before Phase 1 verifies end-to-end. Code-side changes are tracked separately; this file covers dashboard work only.

Do these in order. Each section ends with a "you should now have" line — if that's not true, fix it before moving on.

## A. Supabase project

1. Sign up / log in at https://supabase.com. The free tier is fine for the MVP.
2. **New project**:
   - Organization: your default.
   - Name: `candid`.
   - Database password: generate a strong one and save it in your password manager. You'll need it to apply migrations.
   - **Region: `EU (Frankfurt)` or `EU (Ireland)`**. Non-EU is a non-negotiable bug per `CLAUDE.md`.
   - Pricing plan: Free.
3. Wait ~2 min for provisioning.
4. **Settings → API** (left sidebar → gear icon → API):
   - Copy **Project URL** → `SUPABASE_URL`.
   - Copy **`anon` `public` key** → `SUPABASE_ANON_KEY` (used by the mobile bundle; the API doesn't call Supabase with this).
   - Copy **`service_role` `secret` key** → `SUPABASE_SERVICE_ROLE_KEY`. **Server-only. Never paste this into any `EXPO_PUBLIC_*` variable or any mobile config.**
5. **Settings → API → JWT Settings**:
   - Copy **JWT Secret** → `SUPABASE_JWT_SECRET`. This is what the FastAPI app verifies tokens against.

You should now have four values saved: `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_JWT_SECRET`.

## B. Resend (magic-link email delivery)

1. Sign up at https://resend.com. Free tier: 100 emails/day, 3,000/month.
2. **Domains → Add Domain** → enter your domain (e.g. `candid.app` if you bought it via Porkbun) → Resend prints DNS records (TXT, MX, DKIM). Add them in Porkbun's DNS panel → wait 5–30 min → click **Verify** in Resend.
   - **Short-circuit for MVP testing**: you can skip domain verification and send from `onboarding@resend.dev`, but Resend only accepts the *Resend account email* as a destination on that sender. To send to any other address (i.e., your test users), you need a verified domain.
3. **API Keys → Create API Key** → name it `candid-supabase` → Permission: **Full access** → copy the key → `RESEND_API_KEY`.

You should now have a `RESEND_API_KEY` and either a verified domain address (e.g. `auth@candid.app`) or `onboarding@resend.dev` for self-tests only.

## C. Wire Resend into Supabase Auth (SMTP)

1. In Supabase dashboard → **Authentication → Emails → SMTP Settings** (sidebar → Authentication → "Emails" tab or similar; UI shifts occasionally).
2. **Enable Custom SMTP**.
3. Fill in:
   - **Sender email**: your verified address (`auth@candid.app`) or `onboarding@resend.dev` for testing.
   - **Sender name**: `Candid`.
   - **Host**: `smtp.resend.com`
   - **Port**: `465`
   - **Username**: `resend`
   - **Password**: the `RESEND_API_KEY` from B.3.
   - **Min interval between emails**: leave default (60s).
4. Click **Save**.
5. **Authentication → URL Configuration**:
   - **Site URL**: leave default (placeholder, not used for mobile-only).
   - **Redirect URLs**: add `candid://` — this matches the `scheme` in `candid-app/app.config.ts` and is what the magic link returns to.
6. (Optional) **Authentication → Email Templates → Magic Link**: edit the subject and body if you want to drop default Supabase branding. Not required for Phase 1.

You should now be able to:
- In Supabase **Authentication → Users → "Add user" → "Send magic link"**, enter your own email.
- Receive the email within 30s (check spam).
- The link won't fully resolve until the mobile Phase 1 lands (the deep link `candid://...` only does something on a device with the app installed), but the email arriving confirms SMTP works.

## D. Render env vars

In Render dashboard → **Env Groups → `candid-api-env`** (the group already attached to the web service + cron jobs), add these via the dashboard (do **not** put them in `render.yaml` — that file is checked in):

```
SUPABASE_URL=<from A.4>
SUPABASE_SERVICE_ROLE_KEY=<from A.4>
SUPABASE_JWT_SECRET=<from A.5>
RESEND_API_KEY=<from B.3>
RESEND_FROM_EMAIL=<your verified sender or onboarding@resend.dev>
```

`SUPABASE_ANON_KEY` is not used by the API — skip it here. (It belongs in `candid-app/.env`.)

After adding, click **Save**. Render will auto-trigger a redeploy of every service attached to the env group. Wait ~3 min, then verify the API still responds:

```bash
curl https://candid-api-7o72.onrender.com/health
# {"status":"ok"}
```

## E. Mobile `.env` (cross-repo)

At `C:\Work\claude\candid-app\.env` (gitignored, never committed):

```
EXPO_PUBLIC_API_URL=https://candid-api-7o72.onrender.com
EXPO_PUBLIC_SUPABASE_URL=<from A.4>
EXPO_PUBLIC_SUPABASE_ANON_KEY=<from A.4>
```

The anon key in the bundle is fine — that's its whole purpose, and it's RLS-protected. The service-role key and JWT secret stay server-side only. Ever.

## F. Apply the Phase 1 migration

The migration file lives at `supabase/migrations/<timestamp>_create_profiles.sql`. To apply it for the first time, the easiest path (no Supabase CLI install needed yet):

1. Open the file in your editor, copy its contents.
2. In Supabase dashboard → **SQL Editor → New query** → paste → **Run**.
3. **Table Editor**: confirm `public.profiles` now exists with the expected columns.
4. **Database → Triggers** (or run `SELECT tgname FROM pg_trigger WHERE tgname LIKE '%user%';` in SQL editor): confirm `on_auth_user_created` is present.

Once we have ≥2 migrations and a more frequent cadence, install the Supabase CLI and use `supabase db push`. For the first one, dashboard SQL editor is fine.

## End-to-end smoke test (after the code lands and is deployed)

1. **Supabase → Authentication → Users → Add user → "Send magic link"** → enter your email.
2. Open the email in your inbox; the link should look like `candid://...#access_token=...&refresh_token=...&...`.
3. **Don't click the link** (it won't resolve without the mobile app). Instead, in the Supabase dashboard, go to **Authentication → Users → click your user row → copy the `access_token`** (or use the SQL editor to query `auth.sessions`).
4. Test the protected endpoint:
   ```bash
   curl -H "Authorization: Bearer <jwt>" https://candid-api-7o72.onrender.com/profile
   # Expected: {"id":"...","display_name":null,"avatar_url":null,"timezone":"UTC", ...}
   ```
5. Test that auth is required:
   ```bash
   curl https://candid-api-7o72.onrender.com/profile
   # Expected: {"detail":"Missing Authorization header"} with HTTP 401
   ```
6. **Table Editor → profiles**: confirm a row exists for your user with `timezone='UTC'`.

That's Phase 1 backend acceptance cleared. The mobile half (magic-link UI, deep-link handler, profile sync) lands in a follow-up plan and completes Phase 1.
