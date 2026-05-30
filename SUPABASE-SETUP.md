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

## F. Apply migrations with the Supabase CLI

There are now two migration files in `supabase/migrations/`:

- `20260526195531_create_profiles.sql` — profiles + handle_new_user trigger + RLS
- `20260529212325_create_mvp_tables.sql` — groups, group_members, invite_codes, prompts, posts, devices + enums + is_group_member helper + RLS on all

Recommended path — Supabase CLI:

```bash
# One-time install (pick the one for your OS):
npm i -g supabase                           # any platform with Node
scoop install supabase                      # Windows (scoop)
brew install supabase/tap/supabase          # macOS

# Authenticate + link to your project (one-time per machine):
supabase login                              # opens a browser
supabase link --project-ref <your-project-ref>
# Your project-ref is the part of the dashboard URL after /project/.

# Apply all pending migrations (idempotent — Supabase tracks applied ones):
supabase db push
```

Fallback (no CLI installed yet) — paste each file into the dashboard SQL editor in
timestamp order: open the file, copy contents, **SQL Editor → New query → Run**, then
do the same for the next file. Both files are independent transactions so an error
in one won't half-apply the other.

After applying, verify:

1. **Table Editor**: all seven tables exist (`profiles`, `groups`, `group_members`, `invite_codes`, `prompts`, `posts`, `devices`).
2. **Database → Types**: the five enums are present (`prompt_status`, `media_type`, `post_kind`, `post_media_type`, `device_platform`).
3. **Database → Triggers**: `on_auth_user_created`, `profiles_set_updated_at`, `groups_set_updated_at` are listed.
4. **Database → Functions**: `handle_new_user`, `is_group_member`, `set_updated_at` are listed.
5. **Authentication → Policies**: RLS is `enabled` on every Phase 1 table.

## G. Run integration tests against the live project

After migrations apply, run the integration suite to validate the trigger and RLS:

```bash
# From the candid-api repo root, with all four vars populated:
SUPABASE_URL=https://<proj>.supabase.co \
SUPABASE_ANON_KEY=eyJ...                     \
SUPABASE_SERVICE_ROLE_KEY=eyJ...             \
SUPABASE_JWT_SECRET=<32+ chars>              \
    uv run pytest tests/integration -v
```

Expected: all four integration tests pass —
- `test_handle_new_user_creates_profile` proves the trigger fires.
- `test_user_can_read_their_own_profile` proves RLS allows self-reads.
- `test_user_cannot_read_another_users_profile` proves RLS blocks cross-user reads.
- `test_non_member_cannot_read_group` proves group-scoped RLS blocks outsiders.

If any fail, the migration didn't fully apply — check the dashboard verification list above.

## H. End-to-end smoke test (post-deploy)

The "Add user" flow sends an *invite* link (`type=invite`), not a magic link — so there's no `access_token` fragment to copy. Supabase also doesn't persist raw JWTs in `auth.sessions`. Mint a token directly instead:

1. **Create a user** via Supabase → Authentication → Users → Add user → enter your email. (No need to click the invite link.)
2. **Mint a JWT** from the repo root:
   ```bash
   uv run python scripts/mint_smoke_token.py --email you@example.com
   # Prints the user ID and a 1-hour JWT.
   ```
3. Test the protected endpoint:
   ```bash
   curl -H "Authorization: Bearer <jwt>" https://candid-api-7o72.onrender.com/profile/me
   # Expected: {"id":"...","display_name":null,"avatar_url":null,"timezone":"UTC", ...}
   ```
4. Test that auth is required:
   ```bash
   curl https://candid-api-7o72.onrender.com/profile/me
   # Expected: {"detail":"Missing Authorization header"} with HTTP 401
   ```
5. **Table Editor → profiles**: confirm a row exists for your user with `timezone='UTC'`.

That's Phase 1 backend acceptance cleared. The mobile half (magic-link UI, deep-link handler, profile sync) lands in a follow-up plan and completes Phase 1.
