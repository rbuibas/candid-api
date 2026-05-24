# 05 — Future Features

This doc exists to **prevent scope creep**. When a tempting idea shows up mid-build, park it here instead of slipping it into v1. Each item notes roughly *when* it might land and any *pre-work* worth doing now to keep the option cheap. The schema in `03` is already shaped to absorb most of these without migrations.

---

## Near-term (right after MVP proves out)

### Intentional (unprompted) posts
Let a member post without a prompt, **rate-limited (default 1/hour, rolling)**, and **switchable off by the creator**. Live capture only; user chooses photo/video; same geocode flow; **badged distinctly** so prompted candids stay the signal.
- *Pre-work done:* `posts.kind` enum already extensible (`prompt`/`photobooth` → add `intentional`).
- *Watch:* keep the rate limit tight — this is the feature most likely to erode the anti-social-media stance.

### "Someone posted" notifications
Per-group, per-user toggle (default on), fires when a post becomes visible. Adds liveliness; excluded from MVP to avoid fan-out plumbing.
- *Pre-work done:* `devices` + FCM already in place from Phase 4.

### Media retention + purge
Default **2-month** retention measured **per-post from the event's `end_date`**; purge media (and thumbnails) from R2 at expiry, with **pre-expiry warnings (T-7d, T-24h)**. The first real cost-control lever.
- *Pre-work:* none needed; add an expirer-style cron + warning notifications.

---

## Medium-term

### Pay-to-keep ("kept while subscribed")
Creator (or co-admin) pays to retain media beyond the default window. **This is a whole workstream, not a setting:** Stripe recurring billing, failed-payment dunning, a lapse→grace→purge flow (grace ~14 days), and **EU VAT via Stripe Tax**. Never market it as literal "forever."
- *Pre-work:* keep retention logic centralized so a "paid" flag can simply exempt a group.

### Group continuity: co-admins, ownership transfer, owner-inactivity cleanup
Co-admins (all powers except delete/transfer); owner can transfer ownership; owner-inactivity backstop (proposed 90d → offer to co-admins → +30d → 30-day deletion notice). Removes the single-point-of-failure of one creator.
- *Pre-work:* a `role` on `group_members` would make this additive.

### Safety surface for public readiness
Report / block / remove + an **illegal-content escalation path** (mandatory before any public, non-personal-use distribution), plus a **formal age gate** (scope 18+). Required by app-store review and by stepping outside the personal/household context.
- *Pre-work:* `posts.deleted_at` already supports takedown; keep delete logic capable of admin-initiated removal.

### Map timeline
The marquee follow-up: plot a group's posts on an interactive map with a time scrubber to replay the event. Powered by the geocodes saved from MVP.
- *Pre-work done:* `latitude`/`longitude`/`accuracy` captured from day one. *Later:* add a PostGIS `geom` column + spatial index; consider clustering on the client.

---

## Longer-term / opportunistic

- **Reverse geocoding** — human-readable place labels on posts (server-side on confirm; small per-call API cost).
- **Reusable groups** — reactivate a group for a new event (per-post retention already supports this cleanly).
- **Leave / rejoin** — member-initiated leave with sane handling of their posts.
- **Group export** — download all media + metadata as a zip after an event.
- **Web companion** — read-only desktop viewer (this is where the default **Svelte** web stack would finally come in).
- **Per-post privacy controls** — strip location, hide from feed, etc.
- **SSO** — Google/Apple sign-in alongside magic link (Apple SSO becomes mandatory once other SSO + public App Store ship).

---

## Explicitly never

Holding the line on these is what keeps the product itself:

- **No gallery/library uploads.** Live capture is the whole point.
- **No likes, reactions, comment threads, or visible counts.** No performance surface.
- **No public feed, global discovery, or public profiles.** The group is the only audience, always.
- **No synchronized prompts** (everyone buzzed at once). Independent per-person timing is the differentiator.
- **No selling or sharing user data**, no ad targeting on user content.
- **No engagement mechanics** designed to fight the product's intentional dormancy between events.
