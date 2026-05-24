# Candid — Project Docs

**Working codename: "Candid"** (final product name undecided — see Open Items). A private, invite-only mobile app that captures candid group moments during an event. Each member is prompted at their own random times to take a photo or short video; everything lands in a shared, event-scoped feed. Inspired by the random-prompt mechanic of daily-capture apps, but reframed as **group + event scoped, with independent per-person timing**.

> Earlier scaffolding used the placeholder slug `bereal-trips`. Replace it everywhere with a neutral codename (`candid`) — do not ship a competitor's trademark in identifiers.

---

## What each doc is for

| Doc | Purpose |
|---|---|
| `README.md` | This file. Index, attach-guide, open items, stack at a glance. |
| `01-product-brief.md` | Vision, the core bet, target user, operating principles, success, and "what this is NOT". The *why*. |
| `02-product-design.md` | The heart. Core mechanics in depth (prompt engine, photo-booth, capture, feed), full user journey, every lifecycle state, edge cases, UX patterns. The *what*. |
| `03-technical-architecture.md` | Stack + rationale, data models, architectural patterns, integrations, security/privacy, cost/scaling, module layout. The *how*. |
| `04-build-phases.md` | Phase 0 → MVP, each with scope, acceptance criteria (manual scenarios), and explicit non-goals. The *order*. |
| `05-future-features.md` | Parking lot for everything out of v1, with rough timing, required pre-work, and an "explicitly never" list. The *scope guard*. |
| `project-instructions.md` | Lean block to paste into the Claude Project's custom instructions. Guardrails, not a summary. |
| `../CLAUDE.md` | Repo-root guidance for Claude Code at coding time. Lives at the root of each code repo. |

**The docs are the source of truth.** `project-instructions.md` and `CLAUDE.md` only point at them and encode guardrails — they deliberately do not duplicate doc content.

---

## Which doc to attach when (Claude Code)

Attach the smallest relevant subset — smaller context windows produce better output.

| Task | Attach |
|---|---|
| Any coding session (always) | `CLAUDE.md` is auto-read at repo root; no need to attach |
| Scaffolding / infra / deploy (Phase 0) | `03-technical-architecture.md`, `04-build-phases.md` (Phase 0 section) |
| DB schema + migrations + RLS | `02-product-design.md` (schema/state sections), `03-technical-architecture.md` |
| Auth | `02` (user journey + auth), `03` (integrations/security) |
| Groups & membership | `02` (group lifecycle + schema), `04` (relevant phase) |
| Capture + media pipeline | `02` (capture + photo-booth mechanics), `03` (R2 + signed URLs) |
| Prompt engine + push | `02` (prompt engine — the deep section), `03` (workers + FCM) |
| Feed | `02` (feed mechanics + states) |
| MVP hardening / release | `02` (edge cases), `04` (final phase), `03` (cost/scaling) |
| Product/scope questions, "should we add X?" | `01`, `05` |

Rule of thumb: **`02` for behavior, `03` for plumbing, `04` for what's in-scope right now, `05` to say no.**

---

## Open items still undecided at start of build

- **Product name.** "Candid" is a working codename; pick a final, trademark-clear name before TestFlight submission. Drives slug, bundle IDs, domain (Porkbun), and Resend sending domain.
- **Bundle identifiers / package name.** Placeholder `app.candid.mobile` / `app.candid` — set real ones once the name lands (changing them later forces a new app identity).
- **Sending domain for magic-link email.** Need a domain verified in Resend (and DNS on Porkbun). Until then, dev can use Supabase's default email with its rate limits.
- **Default values for group settings.** Sketched in `02` (prompts/day, windows, etc.) — confirm the numbers feel right with a real party in mind before locking defaults in code.
- **Apple Developer + Play Console accounts.** Required for TestFlight / internal testing and for push (APNs key). Not a design decision, but a prerequisite gate for the release phase.

---

## Stack at a glance

Built on the established default stack; per-project choices confirmed below.

| Layer | Choice | Notes |
|---|---|---|
| Mobile | React Native via **Expo** (+ dev client) | Default mobile stack. Dev client needed for vision-camera + Firebase. |
| Backend | **Python + FastAPI** | Python chosen for this project (default allows Node or Python). |
| DB & Auth | **Supabase** (Postgres) | EU region. Magic-link auth only for MVP. |
| Media storage | **Cloudflare R2** | EU jurisdiction. Zero egress fees — material cost de-risk for media. |
| Push | **Firebase Cloud Messaging** | Cross-platform incl. iOS (APNs key). Loop-critical. |
| Transactional email | **Resend** | Magic-link delivery. |
| Backend hosting | **Render** | Web service (Docker) + cron jobs for the prompt workers. |
| Camera | **react-native-vision-camera** | Photo + video, in-app only. |
| Location | **expo-location** | Optional geocode at capture. |
| Payments | **Stripe** | Deferred — only when pay-to-keep ships (see `05`). |
| Domain | **Porkbun** | For invite links + email domain, post-naming. |

**Deviation from defaults:** the default web/PWA frontend is Svelte, but this product is mobile-only, so the mobile default (Expo) governs and there is no web frontend in v1. Stripe is in the default stack but unused until a future phase.
