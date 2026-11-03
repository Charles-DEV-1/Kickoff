# Kickoff Backend MVP: Build Prompts and Frontend API Guide

## Quick local smoke test

From PowerShell in the Kickoff folder, install the pinned dependencies, run the Flask API, then call the health routes:

    py -3.12 -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install -r requirements.txt
    python -m flask --app api.index run --host 127.0.0.1 --port 5000

In a second PowerShell window:

    Invoke-RestMethod http://127.0.0.1:5000/v1/health
    Invoke-RestMethod http://127.0.0.1:5000/v1/health/ready

The first route should return status ok. The second should return status ready if SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY are valid and Supabase Auth is reachable. It does not prove every database migration/RPC is present. Keep .env local and never paste its values into chat.
## Stage 1 implementation notes

The current Supabase project exposes an ES256 signing key at its public JWKS endpoint. The API uses that key set to verify access-token signatures, checks issuer, audience, expiry, issue time, subject, session ID, and authenticated role, and caches public keys for ten minutes. Unknown key IDs trigger a JWKS refresh. The API also calls Supabase Auth's server-side user lookup for each protected request so a revoked session is rejected while its JWT is still unexpired and a deleted Auth account receives the account-deleted response. The Supabase signing secret is not needed for this asymmetric configuration. If the project is later changed to legacy HS256, set `SUPABASE_JWT_SECRET` only in the backend's server-side environment and implement a fixed HS256 verification path before enabling that configuration. Do not use `SUPABASE_SECRET_KEY` for JWT verification; it is a different credential. Never add either secret to frontend code.

Stage 1 routes: GET /v1/me and PATCH /v1/me. Both require Authorization: Bearer <Supabase access token>. The migration at supabase/migrations/202611020001_auth_profile.sql creates the users and reward_state tables, self-only RLS policies, restricted column grants, and two SECURITY INVOKER RPC functions. Apply that SQL migration once in the Supabase Dashboard SQL Editor before testing signed-in profile routes. The RPC functions do not accept a user ID; Postgres gets the owner from auth.uid().

For Stage 1, set these backend environment variable names (never expose server secrets to the frontend):
- Required: `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `ALLOWED_ORIGINS`. `ALLOWED_ORIGINS` must list only the deployed frontend origins, comma-separated; it defaults to an empty allowlist.
- Configurable with secure current-project defaults: `SUPABASE_JWT_AUDIENCE` defaults to `authenticated`; `SUPABASE_JWT_ALGORITHMS` defaults to `ES256`; `SUPPORTED_LANGUAGES` defaults to `en`. The actual project was verified to use ES256, so do not change the algorithm unless the project's signing configuration is changed and the verifier is reviewed.
- Optional overrides: `SUPABASE_JWKS_URL` and `SUPABASE_JWT_ISSUER`. Both are derived from `SUPABASE_URL` by default.
- Legacy HS256 only: `SUPABASE_JWT_SECRET`, server-side only. It is not used with the current ES256 project, and the current implementation intentionally refuses to enable HS256 without a reviewed fixed-algorithm verification path.
- Not used by Stage 1: `SUPABASE_SECRET_KEY`. A later account deletion stage may require it, server-side only.

Run focused checks after installing the dev dependencies:

    python -m pip install -r requirements-dev.txt
    python -m pytest -q

These unit tests use generated ES256 keys and a fake Supabase Auth/Data API. A real profile request also requires the SQL migration above and a valid access token from a signed-in Supabase user. Never paste the token or any `.env` values into chat.

## Stage 2 implementation notes

The Stage 1 migration remains unchanged. Apply `supabase/migrations/202611020002_online_mvp_schema.sql` after it. The migration adds `templates`, `user_consents`, `tasks`, `steps`, append-only `reward_events`, `ai_breakdown_cache`, `ai_usage_daily`, `api_idempotency`, and `api_rate_limit_windows`. The Stage 1 `users` and `reward_state` tables remain in place. No fake or unreviewed template content is seeded.

All timestamps use `timestamptz` (UTC instants). Tasks and steps have versions and soft-delete timestamps; task/step ownership is checked with RLS and a composite owner foreign key. Tasks enforce title/notes limits and the 500 active-task cap. Steps enforce text/time/position limits and a deferred check for at most 30 active steps with unique positions. Reward events are append-only and do not allow authenticated clients to insert or modify reward history directly. The reward state remains rebuildable from those events. AI cache rows store a request hash and suggested result, not the original user prompt; AI usage is tracked per user and day.

All user-owned tables have RLS enabled and `auth.uid()` policies. Authenticated users can read only their own data; direct writes are limited by table grants. Templates are readable by anon/authenticated users only when active. Rate-limit and idempotency operations use narrow `SECURITY INVOKER` RPCs, so RLS runs as the authenticated user. Table mutation triggers require a transaction-local flag set by those RPCs; direct PostgREST table writes cannot reset counters or forge replay rows. No multi-table business transaction is added in this stage. Later task/reward operations that must be atomic should use reviewed `SECURITY INVOKER` RPCs as directed in the Stage 3/4 prompts.

Lifecycle and reward mutation triggers expect future reviewed RPCs to set these transaction-local flags only after validating the authenticated owner and business rules: `kickoff.task_lifecycle_write`, `kickoff.step_lifecycle_write`, `kickoff.reward_event_write`, and `kickoff.reward_state_write`. The current Stage 2 routes do not call those mutations. Do not set these flags from application request data or expose a generic RPC that sets them.

### Apply the Stage 2 migration without a database password

Use the Supabase Dashboard SQL Editor for this project:

1. Open the project’s **SQL Editor**.
2. Open `supabase/migrations/202611020002_online_mvp_schema.sql` in the Kickoff folder and copy the complete file.
3. Paste it into a new SQL Editor query and run it once. Confirm the Editor reports success before testing routes that use the new RPCs.
4. Keep the versioned migration file in the repository. Do not put a database password or `DATABASE_URL` in `.env` or frontend configuration.

The migration defines grants and policies, but source code cannot prove the project has actually applied this file or that Supabase is honoring the resulting RLS/grants. Confirm the SQL Editor success message, then inspect `pg_policies`, table grants, and function security modes in the project before deployment. The API continues to use Supabase Data API RPC requests with `SUPABASE_PUBLISHABLE_KEY` and the verified user JWT; it does not open a direct PostgreSQL connection.

### Common request protections

- API writes with a body under `/v1` require JSON objects and honor Flask’s 1 MiB body limit; bodyless DELETE requests are allowed. Invalid JSON returns `400 invalid_json`, a non-JSON content type returns `415 unsupported_media_type`, invalid fields return `422 validation_failed`, and oversized bodies return `413 payload_too_large`.
- Every response has `X-Request-Id`. Unexpected-error logs contain only request ID, route, and method; exception text, request bodies, Authorization headers, and tokens are not logged or returned.
- `authenticated_user_id()` and `user_scope_filter()` derive owner filters only from the verified auth context; they do not accept a body/path/header user ID.
- `PersistentRateLimiter` calls `consume_api_rate_limit` atomically. Stage 1’s PATCH `/v1/me` currently has a persistent limit of 30 requests per user per 60-second window. It returns `429 rate_limited` with `Retry-After` and fails closed with 503 if the counter service is unavailable.
- `idempotent_write` requires a valid `Idempotency-Key`. It hashes method, path, query, relevant conditional headers, and canonical JSON body; stores only that hash plus the JSON response/status for 24 hours; replays the original result for the same request; and returns `409 idempotency_key_reused` for the same key with a different request. Concurrent identical requests receive `409 request_in_progress` while the first lease is active. Replays get a fresh request ID. Rate-limited `429` and server `5xx` results release the reservation so the same action can be retried.
- PATCH `/v1/me` now uses both protections. Future write routes should place `@rate_limited` inside `@idempotent_write`, both inside `@require_auth`, and add a route-specific bucket. The generic middleware records the result after the route returns; future multi-table task/reward writes must also commit the idempotency result inside their transaction RPC to close the crash window between business commit and response recording.

Run the focused suite from PowerShell in the Kickoff folder:

    .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
    .\.venv\Scripts\python.exe -m pytest -q

The Stage 2 tests cover sanitized errors/logs, request IDs, input/body limits, auth-derived user scope, persistent rate-limit behavior, idempotency replay (including bodyless 204 results), and key reuse. The Stage 2 migration was applied to the Supabase project; a live `PATCH /v1/me` retry returned `Idempotent-Replay: true`.

## Stage 3 implementation notes

Stage 3 adds authenticated task and step APIs in `app/modules/tasks/`. Migration `supabase/migrations/202611020003_task_step_api.sql` is designed to run after Stage 2 through the Supabase Dashboard SQL Editor; it adds SECURITY INVOKER functions and write guards while reusing the Stage 2 tables and RLS policies. The migration was reported applied; run the live smoke script below to verify the RPCs.

Implemented routes are `GET/POST /v1/tasks`, `GET/PATCH/DELETE /v1/tasks/{task_id}`, `POST /v1/tasks/{task_id}/steps`, `PATCH/DELETE /v1/steps/{step_id}`, and `PUT /v1/tasks/{task_id}/steps/order`. Protected reads return `Cache-Control: private, no-store`. Every write requires JSON, an `Idempotency-Key`, a current version for update/delete/reorder, and its own persistent rate-limit bucket. Task list/detail reads allow 120 calls per user per minute; write buckets allow 30 or 60 calls per user per minute depending on operation.

Task create accepts `title`, optional `notes`, `source`, `template_id`, `due_at`, and up to 30 initial `{text, planned_minutes}` steps. It returns server-generated UUIDs. Step creation appends to the first free position. Task patch accepts `version` plus task fields; step patch accepts `version` plus `text` and/or `planned_minutes`; delete bodies contain `version`. A task's version increments when its steps change. Reorder accepts `{ "version": <task-version>, "step_ids": [<all-active-step-ids-in-new-order>] }`, validates the complete set, and commits all positions atomically. `GET /v1/tasks` supports `status` (`active` by default; `completed`, `archived`, or `all`), exact UTC `due_date` (`YYYY-MM-DD`), `updated_since` (timezone-qualified ISO 8601), `cursor`, and `limit` (1–50; default 20). Results sort by `updated_at` descending and UUID descending.

The RPCs get ownership from `auth.uid()` and never accept a caller-supplied user ID. They lock task rows while updating child collections, check versions inside the transaction, and return `not_found` for missing, deleted, or another user's resources. A stale version returns `409 version_conflict` with the current resource scoped to its owner. SQL guards require Stage 3 RPCs for all task/step writes, so direct Data API edits cannot skip version checks. Future lifecycle RPCs must set the matching `kickoff.task_api_write` or `kickoff.step_api_write` flag as well as the Stage 2 lifecycle flag when changing protected task/step lifecycle fields. Local tests use a stateful fake Data API and do not execute the Postgres migration itself.

Stage 3 focused tests cover field boundaries, the task cap, one-RPC task creation, list pagination, cross-user 404s, version conflicts, parent/child soft deletion, step editing, and complete atomic reordering. The current local suite passes 48 tests.

For live verification, start Flask in one PowerShell window:

    .\.venv\Scripts\python.exe -m flask --app api.index run --host 127.0.0.1 --port 5000

From a second PowerShell window in the Kickoff folder, run:

    .\smoke_stage3.ps1

The script prompts locally for the Supabase test user's email/password, holds the access token only in memory, exercises task/step creation, reads, list filters, updates, reordering, idempotency replay, and soft deletion, then removes its temporary task. It does not print or save the access token. If a run stops after creating a task, clean it up with ` .\smoke_stage3.ps1 -CleanupTaskId "<task-id>" `. No local PostgreSQL server is configured in this checkout, so Supabase applies and validates the migration.

## Stage 4 implementation notes

Stage 4 adds `POST /v1/steps/{step_id}/start`, `/complete`, and `/reopen` in `app/modules/tasks/routes.py`. The request must include the step's current `version` and an `Idempotency-Key`. Only completion accepts optional `actual_minutes` (integer 0–600). The backend timestamps start/completion from its own UTC clock; completing a pending step sets `started_at` and `completed_at` to the same instant. Without client `actual_minutes`, elapsed minutes are computed from those server timestamps and capped at the FRD maximum of 600. `reopen` moves completed → started, and also supports the FRD's started → not_started undo transition. Invalid moves return `422 invalid_transition`; stale versions return `409 version_conflict`; another user's ID remains `404 not_found`.

The single `act_on_my_step` `SECURITY INVOKER` function in `supabase/migrations/202611020004_step_actions_rewards.sql` locks the caller-owned step/task and updates the step, task completion status, reward events, reward state, and the successful idempotency response in one Postgres transaction. It gets ownership from `auth.uid()`, runs with RLS enabled, and sets the existing transaction-local write guards only after validation. If an insert, constraint, or update fails, Postgres rolls back the whole function call. Event rows have per-user unique dedupe keys and event-order snapshots. The rebuild-only RPC `rebuild_my_reward_state()` restores the caller's cached reward state from the latest append-only event snapshot; no service key or database password is needed.

Point values and plant-stage thresholds live in `public.reward_config`, so the team can tune them in a reviewed SQL change. Initial defaults are 1 point for a step start, 10 for completion, and plant thresholds 0, 25, 75, 150, and 300. The FRD requires these values to be configurable but does not prescribe launch values; confirm these defaults with the product team before launch. Points are only added when their step-specific dedupe event is first inserted. Reopen and re-completion keep the original points and cannot subtract them. One `step_started` active-day marker per local date drives streaks; local dates are generated by Postgres from the user's saved IANA timezone. Freezes are automatically used for missed dates, capped at 3, and earned on each 7-day streak milestone when below the cap.

To apply this stage, use the Supabase Dashboard **SQL Editor**:

1. Open `supabase/migrations/202611020004_step_actions_rewards.sql` from this folder.
2. Copy and run the complete file after migrations `202611020001` through `202611020003` have succeeded.
3. Do not add `DATABASE_URL` or a database password. The API calls the RPC with the publishable key and verified user's JWT.
4. After starting the local Flask API, run `powershell -ExecutionPolicy Bypass -File .\smoke_stage4.ps1`. It prompts securely for the test user's credentials, validates never-started completion, reward response, idempotency replay, reopen/re-completion, and cleanup. It holds the access token in memory and does not print it.

Focused local tests: `tests/test_step_actions.py` covers the route transition matrix, request validation, replay/no-double-award behavior, safe failure responses, timezone day conversion, streak freeze use/cap, and event-snapshot reconstruction. These use a stateful fake Supabase API plus deterministic reward-policy reference functions; they do not execute the SQL migration. The migration must be applied and the live smoke script must pass before claiming the Supabase RPCs work against the project.

## Stage 5 implementation notes

Stage 5 adds public template routes in `app/modules/templates/routes.py` and template matching/variant validation in `app/modules/templates/service.py`. `GET /v1/templates/manifest`, `GET /v1/templates`, and `GET /v1/templates/{template_id}` need no sign-in. They return ETags and `Cache-Control: public, max-age=120`; an unchanged `If-None-Match` gets `304`. The server keeps a 30-second per-process catalog cache to limit Data API reads. The Data API request uses the publishable key without a user token and is limited by the existing anon RLS policy to active rows.

Template list query parameters are `task_type`, `language`, `limit` (1–100, default 50), and `cursor`. The manifest accepts optional `language`. The public template routes have a 60-request/minute per-IP in-process limiter. `PUBLIC_RATE_LIMIT_TRUSTED_PROXY_HOPS` defaults to `0`; set it to the exact trusted proxy count only when the deployment ingress overwrites forwarding headers. This limiter is local to each Python process. A multi-instance/serverless deployment also needs an IP rate limit at its trusted edge; do not treat this local counter as shared across instances.

`POST /v1/ai/breakdown` is signed-in and limited to 5 requests/minute per user. It validates title (3–200), description (up to 500), optional timezone-qualified `due_at`, supported language, detail level (1–5), and optional task type. Stage 5 used templates only. Stage 6 adds an optional provider boundary while keeping templates as the always-available fallback. The endpoint never creates a task. The variant counts returned are the minimum in each FRD range: levels 1–5 return 3, 5, 8, 12, and 18 steps. Each step is bounded to 280 characters and 1–120 minutes; the first step is capped at 10 minutes.

Template selection is deterministic: a keyword match wins within the task type and language, ties sort by template key and UUID, an unavailable translation falls back to English, and no keyword match falls back to the generic template. Invalid or undersized variants are never padded with invented copy. `templates_unavailable` is returned if no reviewed generic template with a valid variant exists.

Migration `supabase/migrations/202611030005_public_templates.sql` adds the `keywords` array and `short`/`standard`/`detailed` `variants` to the existing template table, with database validation for required variant counts and step lengths/minutes. At the user's request, `supabase/migrations/202611030006_provisional_generic_english_template.sql` adds one clearly marked provisional English generic template for MVP testing. The copy is neutral and practical, but it has not yet been reviewed by the product/specialist team. Edit it through a reviewed forward SQL migration later; the template trigger increments the version when content changes. Apply migration 005, then migration 006, in the Supabase Dashboard SQL Editor before testing the breakdown path.

To live-smoke Stage 5 after applying both SQL files and restarting Flask, run `powershell -ExecutionPolicy Bypass -File .\smoke_stage5.ps1`. It checks the public manifest and ETag, list/detail responses, and a signed-in level-2 generic breakdown. It prompts for the test user's credentials and keeps the access token in memory only. It creates no task.

Focused tests in `tests/test_templates_breakdown.py` cover keyword choice and tie ordering, English/generic fallbacks, manifest versions, ETag/304, public IP limits, list/detail reads, all five step-count levels, malformed input, and the provisional seed's structure and limits. The user applied the Stage 5 schema and seed SQL, then the live `smoke_stage5.ps1` run passed: manifest 200, conditional manifest 304, filtered list 200, detail 200, and signed-in template breakdown 200 with five validated steps. Local automated tests use test fixtures and a fake Data API; the live smoke confirms the applied Supabase template rows and breakdown path. Deployed edge rate limiting remains unverified.

## Stage 6 implementation notes

`POST /v1/ai/breakdown` still computes a validated template response first. When a concrete provider adapter is configured, `app/modules/ai/service.py` may return validated AI steps; any limit, timeout, malformed response, provider error, or missing configuration returns the already validated template response. `source` is `ai` only when a provider response passes validation and usage is committed. AI output is reduced to `{text, planned_minutes}`; unexpected properties are discarded, HTML tags and control characters are rejected, and returned text is treated as plain text. The fixed system prompt version is `kickoff-breakdown-v1`.

Provider input contains only task title, description, language, task type, detail level, and target step count. Email addresses and phone-like strings are redacted before the request. No user ID, name, email, access token, or full prompt is logged. The provider adapter contract receives a per-attempt timeout; the service permits at most one retry inside an 8-second AI-call budget. A bounded daemon-call guard limits stuck adapter calls to 16 concurrent threads; a saturated guard falls back to templates.

The team selected Groq. `app/modules/ai/groq_provider.py` implements its Chat Completions HTTP adapter without adding an SDK dependency. It sends the fixed prompt and redacted task fields to Groq, requests strict JSON-schema output, parses the response, and hands it to our own validator. The endpoint and bearer-auth format follow [Groq's API reference](https://console.groq.com/docs/api-reference). The default model is `openai/gpt-oss-20b`, configurable with `GROQ_MODEL`; Groq currently lists it as a production model and a strict structured-output model ([model list](https://console.groq.com/docs/models), [structured outputs](https://console.groq.com/docs/structured-outputs)). The adapter is activated only when `AI_PROVIDER=groq` and `GROQ_API_KEY` is non-empty. If the key is absent or any call fails, the endpoint returns the validated template instead.

The Groq adapter uses a fixed endpoint, does not pass an end-user ID, sets no provider `user` field, sends no profile name/email/token, and never logs the key, prompt, task text, response body, or upstream error body. The fake-provider tests exercise success, cache replay, malformed/empty/malicious output, slow and unavailable providers, retry limits, PII redaction, budget exhaustion, and log privacy. Groq adapter tests mock the HTTP response; no real API call was made in this stage.

When an AI attempt falls back, Flask logs a fixed `reason=` code and request ID only (for example `provider_permission_denied` or `cache_store_unavailable`). These diagnostics are safe to share for troubleshooting; do not share `.env` values.

Before production use, the team should document approval of the applicable Groq Services Agreement and model terms and confirm user notice/consent for sending redacted task text. Groq's current data guide says inference inputs/outputs are not retained by default, but may be temporarily logged for reliability or abuse investigation for up to 30 days; ZDR is available in the console. It says retained customer data is held in US-based GCP buckets. The Services Agreement says customer inputs/outputs are not used for model training unless the customer permits or instructs that. Review the current [data guide](https://console.groq.com/docs/your-data) and [Services Agreement](https://console.groq.com/docs/legal/services-agreement), including the organization's ZDR setting, before sending real student task text. These are Groq's published terms; the team still owns product/privacy approval.

Migration `supabase/migrations/202611030007_ai_generation_controls.sql` adds RLS-scoped cache and usage RPCs, short-lived usage reservations, and a global provider/day request counter. Cache keys are HMACs of normalized, redacted input plus the fixed prompt version; raw input is never stored. The default cache retention is 24 hours (`AI_CACHE_TTL_SECONDS=86400`, supported range 60 seconds to 7 days); expired rows are ignored and old rows for a user are purged on that user's next cache write. Provider request counts are retained for 90 days. Pending usage reservations expire after 30 seconds. A reservation becomes counted usage only after the provider result passes validation; template fallback and cache hits do not increment usage.

Apply migration 007 after migrations 001–006 in the Supabase SQL Editor or with the already agreed Supabase CLI migration workflow. It uses no database URL/password. User cache and allowance functions are `SECURITY INVOKER`, derive identity from `auth.uid()`, and remain RLS-scoped. The cross-user provider budget has no user data and is RLS-enabled with no client policies; its narrowly granted RPC is callable only by `service_role`. The backend invokes only that RPC using the existing server-only `SUPABASE_SECRET_KEY`; never expose this key to the frontend. Supabase secret keys bypass RLS, so the key must remain in server configuration and should be configured only when enabling a reviewed adapter.

Environment variables for this stage: `AI_PROVIDER=groq`, `GROQ_API_KEY` (server-only key from Groq; add it to local `.env`, never to source control or frontend config), `GROQ_MODEL` (default `openai/gpt-oss-20b`), `AI_DAILY_ALLOWANCE` (default 10 successful generations/user/UTC day), `AI_PROVIDER_DAILY_BUDGET` (default 1000 Groq requests/UTC day), `AI_CACHE_TTL_SECONDS` (default 86400), `AI_CACHE_HMAC_KEY` (server-only random secret of at least 32 characters), `AI_PROVIDER_TIMEOUT_SECONDS` (default 5), and `AI_TOTAL_BUDGET_SECONDS` (maximum 8). The existing `SUPABASE_SECRET_KEY` is required by the server-only global-budget RPC if generation is enabled. The local `.env` has a `GROQ_API_KEY` entry; if it is blank, fill it locally without sharing its value. A random cache HMAC secret is prepared in `.env` if one was not already present.

After adding the Groq key and applying migration 007, restart Flask and run `powershell -ExecutionPolicy Bypass -File .\smoke_stage6.ps1`. The script signs in the Supabase test user, requests a five-step AI breakdown, verifies the returned fields, repeats the identical request to verify a cache hit, and confirms the usage count did not increase. It never prints the Groq key or access token. Do not run it before adding the key and migration; without those, the intended behavior is template fallback.

Remaining decisions: team sign-off on commercial use and the applicable model terms; user-facing notice/consent for task text processing; whether to enable Groq ZDR; acceptance of Groq's documented US data location; and an operations schedule to physically purge expired cache rows for inactive users. Groq is selected, the API key variable and default model are configured, but these privacy/commercial decisions still need to be recorded before production use.

## Purpose

This guide turns the Kickoff Backend FRD into an ordered set of prompts for implementing the **online-only MVP**. Run one stage at a time in the Kickoff backend repository. Each stage prompt asks the coding agent to implement that stage, explain the code and decisions in plain language, and stop before starting the next stage.

The source file is named Kickoff Backend FRD v0.2, while its cover identifies it as version 0.3 and a draft for review. Where the team has not finalized a decision, the prompts require the implementer to flag it rather than silently treating it as approved.

## MVP boundary

Build the online core:

- Supabase Auth token verification in the Flask API.
- Profile and settings.
- Tasks and steps, including state changes and timing records.
- Reward events and reward state.
- Task templates and task breakdown with a reliable template fallback.
- Optional configured LLM breakdown provider, with privacy controls and limits.
- Bootstrap and delta reads for the online frontend.
- Data export and account deletion.
- Common error handling, rate limiting, idempotency, API documentation, and deployment readiness.

### Explicitly deferred

Do not implement offline queues or any behavior that depends on replaying actions after a period without internet. This includes batch action replay, provisional client rewards, client-side queued task IDs, action-time conflict resolution, 14-day replay rules, and late-event streak repair. The normal online API should still use idempotency keys for safe retries over unreliable connections.

V1 features such as reminders, push notifications, routines, analytics, feedback, localization beyond the launch language, deadlines/insights, and peer check-ins are outside this MVP guide.

## How to use these prompts

1. Open the actual Kickoff backend repository as the working folder.
2. Paste the **Shared instructions** below together with **one stage prompt**.
3. Review the implementation and its explanation before moving on.
4. If the stage reports a product decision or secret that is missing, resolve that before asking for the next stage.
5. At the end, use Stage 10 to reconcile the API catalog in this guide against the routes that were actually implemented.

The repository was not present in the workspace where this guide was prepared, so these prompts instruct the coding agent to inspect the repo before editing. They should adapt to existing files rather than assuming a blank project.

## Shared instructions: paste with every stage

~~~text
We are implementing the Kickoff online-only MVP from the backend FRD. Work only on the stage stated below.

Before editing:
1. Read the repository instructions, inspect the existing structure and implementation, and check the current git status.
2. Preserve existing user changes and project conventions. Do not overwrite unrelated files or replace the project with a new scaffold.
3. Compare the requested stage with existing code. Implement only what is missing or incorrect for this stage.

Security and product rules:
- Flask modular monolith, Supabase Postgres through the Supabase Data API (PostgREST), Supabase Auth. Use the publishable key plus the signed-in user JWT for normal data access, with Row Level Security enabled. Do not configure a direct DATABASE_URL for this project. Treat the FRD as requirements, but identify unresolved decisions as unresolved.
- The user ID comes only from a verified Supabase access token. Scope every user-data query by that ID. Return 404 for a resource owned by another user.
- Never log passwords, tokens, Authorization headers, task titles, notes, step text, AI prompts, or other personal request bodies. Never return stack traces, secrets, or SQL to a client.
- Keep Supabase secret keys, JWT signing secrets, LLM keys, and cron secrets on the server. Never put them in frontend code or commit them. A publishable key is public by design and is not a database password.
- Validate request data at the boundary and enforce important invariants in PostgreSQL too. Use parameterized database access.
- Use one SECURITY INVOKER Postgres RPC call for operations that must succeed or fail together, so the transaction runs under the verified user JWT and RLS. Make write retries safe with idempotency keys where required by the API contract.
- Do not add fake, sample, placeholder, or hardcoded user/product data. Seed only approved template/reference content.
- This first product is online-only. Do not implement offline write queues, batch replay, provisional rewards, or late offline-event reconciliation.
- Do not build V1 or later features unless the stage explicitly includes them.

After implementing this stage, stop. Do not begin another stage. Provide a learner-friendly report with:
1. What changed and why.
2. A simple explanation of how each important code path works, naming the relevant files and functions.
3. The security protections added and the threat each one addresses.
4. APIs added or changed, including method, path, authentication, request fields, and response shape.
5. Environment variables/configuration required, without revealing values.
6. What you checked, how you checked it, and the result. Add focused automated tests for security-sensitive behavior and run the relevant checks for this stage.
7. Remaining decisions, limitations, and the exact next stage.

Explain technical terms the first time you use them. Do not claim a feature or check succeeded unless you verified it.
~~~

# Staged implementation prompts

## Stage 0 — Inspect the repository and map the MVP

~~~text
Stage 0: Repository and FRD review. Do not edit code.

Inspect the repository, its instructions, current implementation, and git status. Read the supplied Kickoff Backend FRD if it is available in the repo or attachments. Summarize the actual framework, app entry point, database/migration status, authentication status, routes, deployment configuration, and existing security controls.

Create a concise gap map for the online-only MVP in this README. Separate:
- already implemented and verified,
- partially implemented,
- missing,
- blocked on team decisions or credentials.

Use this order for the remaining stages: auth/profile, database foundation, task/step APIs, rewards, templates/breakdown, optional LLM, cache/bootstrap, data rights, release handoff. Do not assume offline support. Identify any discrepancy between the FRD filename/version/status and report what source document was reviewed.
~~~

## Stage 0.5 — Create the Flask API foundation

~~~text
Stage 0.5: Create only the backend foundation needed for the later MVP stages.

Inspect the Stage 0 findings. If this repository is genuinely empty, create a small Flask modular-monolith foundation with an app factory, recognized Vercel Python entry point, environment-based configuration, requirements, .env.example without secrets, .gitignore, request IDs, a consistent sanitized JSON error shape, strict CORS allowlist handling, security headers, GET /v1/health, and GET /v1/health/ready that checks the Supabase Auth health endpoint using SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY. Clearly label this as a Supabase configuration/connectivity check, not a Postgres readiness proof; add a database health RPC with the schema migration stage. Load local .env values without overriding environment variables set by Vercel. Keep liveness independent of Supabase. Do not add authentication or product data routes yet.

If the repository already has a working Flask app, adapt it instead of scaffolding over it. Avoid adding unnecessary packages, keep versions pinned, and do not add Vercel configuration that current framework detection does not need. Do not deploy. At the end, explain the app entry path, app factory, health/readiness distinction, configuration, and how later auth code will plug in. Stop before Stage 1.
~~~
## Stage 1 — Supabase authentication and profile

~~~text
Stage 1: Supabase Auth integration and profile/settings.

Implement protected-route authentication for the online Flask API. Verify Supabase access-token signature, issuer, audience, expiry, and required claims using the signing method configured by the actual Supabase project. Prefer asymmetric JWKS verification when the project is configured for it; cache public keys safely and refresh on an unknown key ID. Never accept a decoded-but-unverified token. If the project uses a legacy signing secret, do not invent or commit it; document the server-only configuration needed.

Create the request authentication context from the verified token subject. Reject missing, malformed, expired, wrong-project, anonymous-role, deleted-account, or revoked tokens with the correct 401 error codes. Never accept user_id from the body, URL, or custom headers. On the first valid request, safely upsert the local user row and initial reward-state row.

Implement GET /v1/me and PATCH /v1/me. Support merge semantics for display_name, language, timezone, and whitelisted settings. Validate IANA timezone names and reject unknown settings keys with 422. Set Africa/Lagos as the configured default unless existing project decisions say otherwise.

Security requirements: redact Authorization headers and tokens from logs and errors; strict ALLOWED_ORIGINS CORS allowlist; no Supabase secret key in frontend code. Restrict each profile query by the authenticated user ID.

Add focused tests for valid/invalid token cases, cross-user access, settings allowlisting, and merge behavior. Report exact environment variables needed, not their values.
~~~

## Stage 2 — Database schema, migrations, shared API protections

~~~text
Stage 2: Online MVP schema and common request protections.

Inspect Stage 1 and the repository’s database stack first. Implement versioned SQL migrations for the MVP data model required by the FRD: users, consents if needed for MVP privacy handling, tasks, steps, append-only reward_events, rebuildable reward_state, templates, AI cache/usage if the AI stage will use them, idempotency keys, and persistent rate-limit counters. Include UUID primary keys, user ownership, UTC timestamps, soft-delete/version fields where appropriate, foreign keys, check constraints, and indexes. Enable RLS on all user-owned tables, add least-privilege grants and auth.uid()-based policies, and explain how the migration is applied through the Supabase dashboard or CLI without exposing the database password.

Choose one migration/query approach consistent with the existing project. Use Supabase Data API requests with the publishable key and the verified end-user JWT for user data operations. Enable RLS on every user-owned table and write policies using auth.uid(). For multi-table operations that must be atomic, use reviewed SECURITY INVOKER Postgres RPC functions with explicit grants and RLS-aware execution. Keep SQL migrations versioned in the repository and apply them through the agreed Supabase dashboard/CLI workflow. Do not add a direct database password or DATABASE_URL.

Implement the shared API error shape with stable machine-readable codes, request IDs on responses/logs, strict JSON/content-type and body-size handling, input validation helpers, persistent rate limits, and idempotency middleware/service for write routes. For a repeated key and same request, return the original result; reject key reuse with a different request body. Keep request/response storage minimal and never store personal text in logs.

Security requirements: parameterized queries, least-privilege database role where available, user-scope helper, safe transactions, server-only configuration, and database constraints mirroring critical validation. Document any RLS/role configuration that cannot be proven from code.

Add focused tests for error sanitization, request IDs, user scoping helper, rate-limit persistence behavior, and idempotency replay/key reuse.
~~~

## Stage 3 — Tasks and steps CRUD

~~~text
Stage 3: Online task and step resource APIs.

Implement the MVP task and step endpoints in the API catalog below, adapting paths to the existing API style only if the OpenAPI contract is updated too. Support task creation with steps in one transaction; task listing with status, due-date, updated_since, cursor, and limit filters; fetching one task; updating fields with version checks; and soft deletion of a task and its steps.

Implement adding, editing, deleting, and atomic reordering of steps. Enforce FRD limits: task title 1–200 characters, notes up to 500, up to 30 steps, step text 1–280, planned time 1–240 minutes, and maximum 500 active tasks per user. Use server-generated UUIDs for this online-only version unless existing frontend contract already requires client-generated IDs. Do not add offline ID conflict behavior.

All resource queries must use the authenticated user scope. A guessed ID from another account must return 404. Enforce version checks for updates/deletes and return 409 with a safe current-version/resource response on conflict. Validate the full reorder list and persist it atomically.

Add focused tests for validation boundaries, task limits, atomic create/reorder, soft delete, version conflict, and user A being unable to access user B’s data.
~~~

## Stage 4 — Step lifecycle, timing, and rewards

~~~text
Stage 4: Step actions and transactional rewards.

Implement POST /v1/steps/{step_id}/start, /complete, and /reopen using the specified state machine. Store started_at, completed_at, and actual_minutes with range checks. The backend records time; it does not run client timers. When completing a never-started step, set start and completion times as required by the FRD.

Implement the rewards service with append-only reward events, unique per-user dedupe keys, non-decreasing points, configurable point values and plant-stage thresholds, streak calculation in the user’s timezone, best streak, and freezes. Keep step mutation, reward-event insertions, and reward-state update in one transaction. Return reward_delta in the same step-action response so the frontend does not need a second request. Reopening or repeating a step action must not award twice or remove points.

Do not implement late offline-event reconciliation, reward corrections for queued actions, or batch action replay. Action timestamps are online request times, validated against the online contract.

Add focused tests for every allowed/forbidden transition, duplicate requests, transaction rollback behavior, timezone/day boundaries, freeze cap/use, and reward-state reconstruction from reward events.
~~~

## Stage 5 — Templates and guaranteed breakdown fallback

~~~text
Stage 5: Public template APIs and template-backed task breakdown.

Implement the template manifest, list, and detail APIs. Make reads public, rate-limited by IP, ETagged, and cacheable for a short period. Select templates deterministically using task type, language, and keyword score; fall back to English where a translation is unavailable, then to the generic template. Do not invent final student-facing copy: use approved seed content and report any missing reviewed template content.

Implement POST /v1/ai/breakdown so it always returns validated usable steps through a template path, even when no LLM key is configured. Validate title, description, language, detail_level, and task_type. Apply the FRD’s target step counts and text/minutes limits. The endpoint returns suggestions only; it must not create a task. Include source, template_id, notice, prompt_version, and usage fields in the response. Keep user-supplied text out of logs and error reports.

Add focused tests for template matching/fallback, manifest/version behavior, ETag responses, rate limits, detail-level count rules, and malformed input.
~~~

## Stage 6 — Optional LLM breakdown provider

~~~text
Stage 6: Add configured LLM generation without weakening the fallback.

Inspect project configuration and ask for the provider/key only if required; never guess a provider or embed a key. Implement a provider adapter behind the existing breakdown service. Add per-user cache keyed by normalized input and prompt version, a configurable daily allowance, persistent shared provider budget counters, a per-user minute rate limit, provider timeout inside an 8-second overall budget, strict output schema validation, one retry only when time permits, and immediate template fallback when limits or errors occur.

Minimize provider data: send no user ID, email, or name; redact email addresses and phone numbers; use a fixed versioned system prompt; discard fields outside the schema; treat provider output as untrusted plain text. Never log task text or full prompts. Store only the minimum cache data and expire it after the configured retention period. Cache hits and template fallback must not count as new AI usage.

Do not enable a provider whose data-use terms have not been reviewed by the team. Report remaining provider, commercial-use, retention, region, and data-use decisions.

Add focused tests with a fake provider for valid, empty, malformed, slow, malicious, and unavailable responses; verify that every failure returns a template and sensitive text is absent from captured logs.
~~~

## Stage 7 — Bootstrap and online delta synchronization

~~~text
Stage 7: Efficient online reads for the frontend.

Implement GET /v1/bootstrap with profile/settings, reward state, up to 50 active tasks with steps, active task count, template manifest, server_time, and api_version. Implement task list filters and updated_since delta reads, including soft-deleted records and next_since generated from server time. Return full_refresh_required when the delta retention window has expired.

Add ETag and If-None-Match support, 304 responses, Cache-Control: private and must-revalidate for personal data, compression, cursor pagination, and bounded response sizes. Never publicly cache a user’s personal data. Ensure every query remains user-scoped and the server, not the device clock, determines delta cursors.

This is online cache support only. Do not add offline write synchronization or a batch action endpoint.

Add focused tests for bootstrap contents, 304 behavior, privacy cache headers, pagination, soft deletes in deltas, expired delta cursors, and cross-user isolation.
~~~

### Stage 7 implementation notes

Apply `supabase/migrations/202611030008_bootstrap_delta_reads.sql` after migrations 001–007 using the Supabase Dashboard SQL Editor or the already agreed Supabase CLI workflow. The migration adds only `SECURITY INVOKER` RPCs; it does not add a database password or direct database connection. The RPCs derive ownership from `auth.uid()`, require an authenticated caller, and have execution revoked from `public` and `anon`. They remain subject to RLS and require the user-owned table select grants from earlier migrations.

`GET /v1/bootstrap` returns the signed-in profile, reward snapshot, up to 50 active tasks with active steps, total active task count, the active template manifest for the profile language, an API version (`v1`), and a database-generated `server_time`. It is capped at 2 MiB uncompressed. Personal read responses use a weak ETag and `Cache-Control: private, max-age=0, must-revalidate`; they are never publicly cacheable. Gzip is applied to JSON responses of at least 1 KiB when requested. `GET /v1/me`, task lists, and a single task also support private conditional reads.

`GET /v1/tasks?updated_since=<ISO-8601 UTC>` enters delta mode. Delta pages include changed tasks and tombstones, their deleted steps, and `matches_filter` so the frontend can remove records that no longer match the selected status/date filter. Retention is fixed at 30 days in the SQL RPC. Older cursors return HTTP 200 with `full_refresh_required: true`, an empty `items` list, and no `next_since`; the frontend should fetch `/v1/bootstrap` and replace its local online cache. Delta queries are paginated by the normal `cursor` and `limit` parameters. Keep the original `updated_since` and follow `next_cursor` until it is null, passing the first page's server-issued `snapshot_at` on later pages and when revalidating a cached page; only then persist `next_since` as the next delta's starting cursor. This keeps concurrent updates from shifting the result set between pages. The server bounds results by database time, not the device clock. A weak ETag omits only the moving `server_time` and `next_since` fields; `X-Server-Time`, `X-Snapshot-At`, and `X-Next-Since` are returned on 200 and 304 so the client can retain/advance server-issued cursors safely.

No new environment variables are required. To run the focused checks from the repository virtual environment on Windows, use `.\.venv\Scripts\python.exe -m pytest tests\test_tasks.py -q`; run the complete suite with `.\.venv\Scripts\python.exe -m pytest -q`. After applying the migration and starting the local API on port 5000, run `powershell -ExecutionPolicy Bypass -File .\smoke_stage7.ps1` for signed-in live verification. It prompts for the Supabase test-user credentials without printing the password or access token. SQL/RLS behavior still requires applying the migration to the Supabase project and exercising the signed-in routes there.

## Stage 8 — Export, deletion, and retention

~~~text
Stage 8: MVP data rights and account deletion.

Implement GET /v1/me/export and DELETE /v1/me as specified by the FRD. Export all user-owned data that exists in the MVP schema, in a complete JSON response with safe download headers and an export rate limit. Require an explicit deletion confirmation string. On deletion, mark the account deleted immediately so API access is denied and personal rows are hidden, then request Supabase Auth deletion using the server-only Supabase secret key. Record pending deletion and make failures retryable without restoring access.

Implement a protected daily retention-purge job if the deployed platform supports the agreed schedule. Make it idempotent and safe to run twice. Use constant-time secret comparison. Do not expose a public route that can trigger deletion or purge without authorization.

Never log exported data, deletion request bodies, secrets, or task content. Report any retention period or legal/compliance assumption that still requires counsel.

Add focused tests for export completeness against the MVP schema, export limits, deletion confirmation, immediate account lockout, Supabase deletion failure/retry, and purge idempotency.
~~~

### Stage 8 implementation notes

Apply `supabase/migrations/202611030009_data_rights_and_retention.sql` after migrations 001–008. It adds an `account_deletions` tombstone and account-lock-aware RLS. The tombstone is deliberately independent of `users`, so removing the profile during final purge cannot let a still-valid JWT recreate that user's rows. User-facing export/deletion RPCs are `SECURITY INVOKER`, derive identity from `auth.uid()`, and are granted only to `authenticated`. Retention RPCs are granted only to `service_role` and reject other database roles.

`GET /v1/me/export` is authenticated, limited to 3 exports per user per 24 hours, and returns a JSON attachment with `Cache-Control: private, no-store` and `X-Content-Type-Options: nosniff`. It exports the Auth email and profile, consent rows, all retained tasks and steps (including soft-deleted records), reward events/state, cached AI suggestions, and daily AI usage. It excludes internal rate-limit/idempotency/provider-budget bookkeeping. Routines, reminders, push subscriptions, feedback, analytics, and check-ins do not exist in the online MVP schema yet, so there are no such rows to export.

`DELETE /v1/me` requires the exact JSON body `{"confirm":"DELETE"}` and an `Idempotency-Key`. It returns HTTP 202 and a `purge_after` timestamp 30 days after the request. One database transaction sets `users.deleted_at`, invalidates older profile tokens, writes the deletion tombstone, and blocks access through API checks and the RLS active-account helper. The server then asks Supabase Auth to soft-delete the identity. If that request fails, the account stays locked and the daily job retries it. At/after `purge_after`, the job deletes retained application rows and requests permanent Auth deletion. Any failure remains pending for another daily attempt; it never reactivates access.

Frontend request shapes:

~~~http
GET /v1/me/export
Authorization: Bearer <supabase_access_token>
~~~

~~~http
DELETE /v1/me
Authorization: Bearer <supabase_access_token>
Content-Type: application/json
Idempotency-Key: <unique-action-key>

{"confirm":"DELETE"}
~~~

The delete response is `202 Accepted`; show the returned `purge_after` date and then clear local authenticated data/session state. Do not retry a completed deletion with a new idempotency key.

Run focused local checks with `.\.venv\Scripts\python.exe -m pytest tests\test_data_rights.py -q`; the full suite is `.\.venv\Scripts\python.exe -m pytest -q`. These use a fake Supabase adapter and prove application behavior, not the applied project RLS/migration or deployed Vercel schedule.

### Retention job configuration

The repository includes a Vercel Cron entry in `vercel.json` for `GET /internal/jobs/purge` at 03:00 UTC daily and allows up to 60 seconds for the Flask function. The route requires a constant-time match of the complete `Authorization: Bearer <CRON_SECRET>` header. The job uses `SUPABASE_SECRET_KEY` only server-side for Auth administration and service-only retention RPCs. It limits each Auth retry batch to three users, uses `FOR UPDATE SKIP LOCKED` for concurrent due-deletion batches, and is safe to invoke repeatedly. Vercel Hobby schedules run once daily within the specified hour rather than at an exact minute; production schedule timing needs verification after deployment.

Set these backend environment variable names in the deployment and local server environment:

- `SUPABASE_URL`
- `SUPABASE_PUBLISHABLE_KEY`
- `SUPABASE_SECRET_KEY` — server-only Supabase secret/service key; never set this as a frontend variable.
- `CRON_SECRET` — long random server-only value configured in Vercel and sent by Vercel Cron.

For a local job check, use an isolated test Supabase project and send `Authorization: Bearer <your-local-CRON_SECRET>`. This job permanently removes records whose `purge_after` has passed, so do not manually invoke it against production. Never use a production secret in chat, source files, or screenshots. Vercel Cron runs in production and is best-effort; schedules can be delayed or retried, which is why the database work is idempotent. Apply the migration in Supabase Dashboard SQL Editor by copying the complete versioned file, or use the team's already configured Supabase CLI workflow. This implementation uses no database password or `DATABASE_URL`.

### Data rights and legal review still needed

**Data rights and legal review still needed, it will be changed later.**

## Stage 9 — OpenAPI contract, deployment, and frontend handoff

~~~text
Stage 9: Publish the MVP API contract and prepare deployment handoff.

Create or complete openapi/openapi.yaml as the single source of truth for the implemented MVP routes. Include authentication, request and response schemas, field limits, error codes, status codes, idempotency headers, ETag behavior, pagination, and examples using non-sensitive placeholder values. Ensure the document does not list V1 or deferred offline endpoints as implemented.

Review deployment setup for the actual selected Vercel project and Supabase database. Document runtime entry point, environment-variable names, migration command, health/readiness URLs, allowed frontend origins, Data API, RLS, and RPC setup, key rotation notes, and secret placement. Do not deploy or change live secrets unless explicitly asked.

Produce a frontend integration guide explaining how the frontend gets a Supabase access token, sends Authorization: Bearer, handles 401/409/422/429/5xx responses, uses idempotency keys for write retries, caches bootstrap responses using ETags, and configures its API base URL. Never tell frontend developers to use the Supabase secret key or a database password.

Add contract/security checks appropriate to the existing CI. Compare the final route inventory with the API catalog in this README and update the catalog to match reality. Clearly label any endpoint not implemented.
~~~

### Stage 9 implementation notes

`openapi/openapi.yaml` is the contract for only the routes currently registered in Flask. It documents the bearer schemes, internal cron scheme, request/response schemas, field limits, errors, status codes, ETags, idempotency, and pagination. The file is JSON-compatible YAML so the dependency-free contract test can parse it with the Python standard library. `tests/test_openapi_contract.py` compares its operations to Flask's route map and checks security/access boundaries, required headers, field limits, unresolved references, and absence of secrets/deferred routes.

The repository had no existing CI workflow, so `.github/workflows/ci.yml` now runs Python compilation and the full test suite on pushes, pull requests, and manual runs. The detailed handoffs are [the frontend integration guide](docs/frontend-integration.md) and [the deployment handoff](docs/deployment.md).

Local deployment inspection found `api/index.py` as the Vercel entry point and a configured local Supabase environment, but there is no `.vercel/project.json` link. The Stage 9 work was pushed to `origin/main` at commit `e114e5d` after the user initialized the Git checkout. The selected Vercel project, deployed environment values, production API/frontend domains, and live Supabase RLS/RPC state remain unverified. No deployment or live secret was changed.

## Stage 10 — MVP acceptance review and teaching handoff

~~~text
Stage 10: Final online MVP review. Do not add V1 features.

Review the implementation against the FRD’s online MVP acceptance criteria and this README. Check signup/signin token flow assumptions, empty home/bootstrap path, template or LLM breakdown fallback, task creation, start/complete step actions with one-response reward delta, online retry/idempotency behavior, export, account deletion, and absence of task text/secrets in logs. Verify that no offline queue, batch replay, provisional reward, or late offline-event reconciliation was accidentally added.

Do not claim deployment/live behavior unless it was actually verified. Update this README’s API catalog based on the code and OpenAPI file. Provide a final learner-friendly walkthrough from frontend request to Flask route, service, repository/database transaction, and JSON response. Explain the key security boundaries and list any remaining product, hosting, legal, or secret-configuration decisions.
~~~

### Stage 10 review result

The review used the supplied file `Kickoff Backend FRD v0.2.pdf` (its cover identifies it as draft v0.3), this README, the Flask route map, the OpenAPI contract, SQL migrations, and the local tests. The FRD describes offline action replay as part of its MVP, while the team's explicit product direction for this build is online-only. That scope decision is retained: this backend has no offline action queue, replay/batch endpoint, provisional client reward, client-generated task IDs, or late-event reconciliation. `queue.Queue` in `app/modules/ai/service.py` only bounds a timed provider call; it is not an offline action queue.

#### Acceptance paths checked

| Area | Review result and evidence |
|---|---|
| Signup/signin and authorization | Signup, sign-in, Google OAuth, email verification, and password reset belong to the Supabase frontend client; Flask does not receive passwords or provide signup routes. `require_auth` verifies the configured Supabase JWT signature and claims, checks the live Auth user/session, derives the owner from `sub`, and calls `ensure_user_profile` so the first valid request creates the profile/reward state. Local auth tests cover malformed, expired, revoked, deleted-account, and cross-user cases. Supabase provider/redirect/email configuration was not rechecked in this review. |
| Empty home/bootstrap | `GET /v1/bootstrap` returns the authenticated profile, reward state, an empty task array for a new account, manifest, server time, and API version. `tests/test_tasks.py` checks the empty second user's bootstrap, private ETag behavior, a 50-task bound, gzip, delta pagination, tombstones, and expired-cursor refresh. |
| Breakdown | `POST /v1/ai/breakdown` validates a suggestion request and does not create a task. It validates a template first, then may call the configured provider; provider/cache/allowance failures fall back to the template. Fake-provider tests cover valid, malformed, malicious, slow, and unavailable output plus sensitive-text redaction. A usable response depends on the provisional template migration being applied and active; absent template data returns `503 templates_unavailable`. Earlier user-run Stage 5/6 smoke outputs reported template and Groq success; this review did not make a live provider call. |
| Task and step workflow | Task creation with initial steps is one RPC transaction; updates use versions; step reorder is atomic. Step start/complete/reopen call `act_on_my_step`, which updates state, append-only events, reward state, and the idempotency result in one transaction. Responses include `reward_delta`; local tests use a stateful fake Data API for boundaries, transitions, rollback/replay behavior, and scoping, so they do not execute the SQL functions or prove live RLS. Earlier user-run Stage 3/4 smoke outputs reported success. |
| Online retry safety | Writes use a per-user idempotency reservation; task and reward RPCs store their successful response atomically with the business mutation. Same-key/same-body retries replay; changed-body reuse conflicts. Local tests cover replay and key reuse. Earlier user-run smoke outputs reported an idempotent replay. |
| Export/deletion | Export and deletion routes are implemented and focused tests cover completeness, limits, confirmation, lockout, retries, and repeated purge behavior using fake adapters. The export includes data from the MVP schema. This review did not verify migration 009, live Auth deletion, or the deployed scheduled purge. |
| Logs and secrets | Application request logs contain request ID, route, method, and status; error handlers omit exception text and request bodies. AI diagnostics use fixed reason codes. Tests assert task text, provider text, tokens, and secrets are absent from captured logs. `.env` is ignored; `.env.example` contains names/placeholders only. Production proxy/provider logging settings still need review. |

#### FRD differences and decisions to close

The OpenAPI file describes the code as implemented; these items are differences from the attached FRD or product decisions, not claims that the current API already supports them:

- **Offline features:** intentionally deferred by the online-only scope above, despite the FRD's MVP offline acceptance criterion.
- **Task filters:** this API supports exact `due_date`, status, `updated_since`, cursor, and limit. The FRD also describes `due_before` and `due_after`; those range filters are not implemented.
- **Task cap error:** the implementation returns `409 task_limit_reached`; the FRD specifies `422`. Keep the OpenAPI aligned to the running code until the team chooses one contract.
- **Reward tuning:** migration 004 seeds 1 start point and 10 completion points, with plant thresholds `[0, 25, 75, 150, 300]`. The FRD lists proposed examples of 2, 5, and a 10-point task-completion bonus. The current `task_completed` event is recorded with zero points and there is no configurable task-completion bonus. Product/specialist approval is needed before tuning these values; any applied change must be a forward migration.
- **Status names:** the API/database use `pending`/`started`/`completed` for steps and `active`/`completed`/`archived` for tasks. The FRD uses `not_started`/`started`/`done` and task `done`. Frontend code should follow the published OpenAPI enums unless the team deliberately changes the contract.
- **Retention and safety:** the default per-user AI cache TTL is 24 hours and idempotency records are retained for 24 hours, while the FRD proposes 30 days for both. The provider-output validator enforces shape, text length, minutes, and safe plain text, but does not yet apply a configurable blocked-word list from the FRD. Confirm retention periods and the content-safety list.
- **Template coverage:** the requested provisional English generic seed is present. It is not the full set of reviewed subject templates described in the FRD and needs content review before student-facing launch.
- **Operational checks:** the FRD calls for rate-limit limit/remaining/reset headers; current 429 responses include `Retry-After`, but not those three counters. The code bounds the AI provider budget, but the under-10-second end-to-end target and shared edge rate limiting need deployed verification.
- **Sensitive account actions:** export and deletion require a valid session but do not enforce a recent sign-in challenge. The FRD marks recent-authentication behavior as an open decision; decide it before production.

#### Remaining launch decisions and verification

Approve the reward values/task bonus, final reviewed template copy, AI blocked terms and cache retention, Groq commercial/data-use terms and user notice/consent, and the recent-authentication rule for export/deletion. Confirm which rate-limit headers and task due filters the frontend needs. Confirm Supabase migrations 001–009 and RLS/RPC grants in the selected project. Link the actual Vercel project, set server-only secrets there, verify `ALLOWED_ORIGINS`, cron behavior and production health/readiness, then run authenticated staging smoke tests. The 30-day account retention/legal note remains the temporary statement already recorded above. No production deployment or secret change was performed during this review.

Local verification for this review: `python -m pytest -q -p no:cacheprovider` passed **123 tests** on 2026-11-03. Most database-focused tests use a fake Data API and do not execute the SQL against Supabase. GitHub Actions status and production behavior are separate checks and were not inspected here.

# Frontend-to-backend API: how the pieces fit together

The backend is a separate HTTPS service. The frontend calls it with HTTP requests and JSON. The frontend does not connect directly to the backend’s PostgreSQL database.

1. **Sign in:** the frontend uses Supabase Auth (email/password or a configured OAuth provider). Supabase returns an access token. The backend never handles the password; there are no Flask signup/signin routes.
2. **Call the API:** the frontend sends `Authorization: Bearer <access_token>` to the API origin. `api/index.py` is Vercel's Python entry point and imports the Flask app factory in `app/__init__.py`.
3. **Check the identity:** protected routes use `require_auth` in `app/common/auth.py`. It verifies the JWT signature with the project's public JWKS keys, then validates issuer, audience, expiry, required claims, authenticated role, and live Supabase Auth/account status. It takes the user ID only from the verified token subject. `ensure_user_profile` safely creates a first-use profile and reward state.
4. **Validate and scope:** the matching Flask blueprint parses the request and runs its schema validators. Rate-limit and idempotency middleware run before writes. Resource RPCs derive the owner from `auth.uid()` and Row Level Security (RLS) checks access again inside Postgres.
5. **Run the operation:** `app/common/supabase_data.py` calls Supabase Auth or the Data API using the publishable key plus the verified end-user JWT. Multi-row actions use narrowly granted `SECURITY INVOKER` SQL RPCs so the changes happen in one transaction under the user's RLS policies.
6. **Return a safe response:** Flask returns JSON with `X-Request-Id`. Personal data is private/no-store or private/revalidated; public template responses use short caching. Errors have a stable code and do not include stack traces, credentials, or request text.

For example, `POST /v1/steps/{step_id}/complete` reaches the task blueprint, validates the current version, and claims the idempotency key. The `act_on_my_step` SQL function locks the caller's step/task, records server time, appends deduplicated reward events, updates reward state, and stores the successful response in the same transaction. Flask returns the updated step/task and `reward_delta`; a retry with the same key/body replays that result instead of awarding twice.

For `POST /v1/ai/breakdown`, the AI route validates the title and request options, chooses a template, and asks the configured provider adapter for a response when allowed. The service removes email/phone patterns, validates provider output as untrusted text, and returns the template when AI is unavailable or rejected. This endpoint only suggests steps; the frontend creates a task separately.

The frontend must never receive SUPABASE_SECRET_KEY, an LLM API key, or CRON_SECRET. Normal user data calls use the public SUPABASE_PUBLISHABLE_KEY together with the signed-in user access token.

## API base URL

During local development, the frontend uses the local backend URL. In staging and production, it uses the deployed API origin. Set this through the frontend’s environment configuration, for example an API base URL variable. The exact variable name depends on whether the frontend uses Vite, Next.js, or another framework.

The backend’s ALLOWED_ORIGINS setting must list the real frontend origins. CORS is a browser rule that allows the frontend’s website origin to call the API; it is not a replacement for authentication.

## Example: get the user’s first screen

~~~http
GET https://api.example.com/v1/bootstrap
Authorization: Bearer <supabase_access_token>
Accept: application/json
If-None-Match: "<previous-etag>"
~~~

The backend returns profile/settings, current reward state, active tasks and steps, a template manifest, server_time, and api_version. If the ETag is unchanged, the backend may return 304 with no response body; the frontend keeps using its cached response.

## Example: create a task

~~~http
POST https://api.example.com/v1/tasks
Authorization: Bearer <supabase_access_token>
Content-Type: application/json
Idempotency-Key: <unique-key-for-this-user-action>
~~~

~~~json
{
  "title": "Prepare biology presentation",
  "notes": "",
  "source": "template",
  "template_id": "<template-uuid>",
  "steps": [
    {
      "text": "Choose the presentation topic",
      "planned_minutes": 10
    }
  ]
}
~~~

The response contains the created task and its server-generated IDs. The frontend stores those IDs and uses them for later step calls. If the network drops after a write, retry with the **same** idempotency key and same request body so the server can safely return the original result. Generate a new key for a new user action.

## Example: start a step

~~~http
POST https://api.example.com/v1/steps/<step-id>/start
Authorization: Bearer <supabase_access_token>
Content-Type: application/json
Idempotency-Key: <unique-key-for-this-start-action>
~~~

~~~json
{
  "version": 1
}
~~~

The response contains the updated step, task status, and reward_delta. The frontend can update the screen from this one response.

## Authentication and error handling

- **401 token_expired:** ask the Supabase client to refresh the session, then retry the API request once.
- **401 token_invalid / account_deleted / token_revoked:** stop using cached account data as authenticated data and take the user through the appropriate sign-in/account state.
- **404 not_found:** the item may not exist or may belong to another account. Remove stale cached references without revealing ownership.
- **409 version_conflict:** fetch the current resource and let the user’s edit be reapplied carefully.
- **409 request_in_progress:** wait briefly, then retry with the same idempotency key.
- **422 validation_failed / invalid_transition:** show the field or state error in the frontend’s own language.
- **429 rate_limited:** respect Retry-After and slow down.
- **500 / 503:** show a calm retry option. For an idempotent write, reuse the same key and body.

## API inventory for the online MVP

This catalog is compared with the Flask route map and `openapi/openapi.yaml`. ✅ means the route is implemented in this checkout; it does not imply deployment or live Supabase verification. `Not implemented` means no Flask route exists. A dagger (†) means authentication is not required. The migrations must also be applied to the selected Supabase project for database-backed routes to work. Deferred V1/offline operations are omitted from OpenAPI.

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | /v1/health | † Public | ✅ Liveness check; does not touch the database. |
| GET | /v1/health/ready | † Public | ✅ Checks Supabase Auth reachability; does not prove database readiness. |
| GET | /v1/me | Signed-in | ✅ Read profile and settings. |
| PATCH | /v1/me | Signed-in | ✅ Merge-update profile and whitelisted settings. |
| GET | /v1/me/export | Signed-in | ✅ Rate-limited private JSON attachment of data in the MVP schema. |
| DELETE | /v1/me | Signed-in | ✅ Requires `{"confirm":"DELETE"}` and an idempotency key; locks access and queues 30-day deletion. |
| GET | /v1/bootstrap | Signed-in | ✅ Initial online app data; supports private ETag/304 and gzip. |
| GET | /v1/tasks | Signed-in | ✅ List tasks with status, exact `due_date`, `updated_since`, cursor, and limit filters; range filters `due_before`/`due_after` are not implemented. |
| POST | /v1/tasks | Signed-in | ✅ Create a task and initial steps atomically. |
| GET | /v1/tasks/{task_id} | Signed-in | ✅ Read one task and its active steps. |
| PATCH | /v1/tasks/{task_id} | Signed-in | ✅ Update task fields with version check. |
| DELETE | /v1/tasks/{task_id} | Signed-in | ✅ Soft-delete task and its steps. |
| POST | /v1/tasks/{task_id}/steps | Signed-in | ✅ Add a step to a task. |
| PATCH | /v1/steps/{step_id} | Signed-in | ✅ Edit step text or planned time with version check. |
| DELETE | /v1/steps/{step_id} | Signed-in | ✅ Soft-delete a step with version check. |
| PUT | /v1/tasks/{task_id}/steps/order | Signed-in | ✅ Atomically reorder all active task steps. |
| POST | /v1/steps/{step_id}/start | Signed-in | ✅ Start a step and return the step, task status, and reward delta. |
| POST | /v1/steps/{step_id}/complete | Signed-in | ✅ Complete a started or never-started step and return reward delta. |
| POST | /v1/steps/{step_id}/reopen | Signed-in | ✅ Reopen a completed step or undo a started step; no points are removed. |
| POST | /v1/ai/breakdown | Signed-in | ✅ Template-backed validated suggestions; no task is created. Requires reviewed active template seeds. |
| GET | /v1/ai/usage | Signed-in | **Not implemented.** The breakdown response includes usage; there is no separate usage endpoint. |
| GET | /v1/templates/manifest | † Public | ✅ List template IDs, languages, and versions; supports ETag. |
| GET | /v1/templates | † Public | ✅ List/filter templates; supports pagination and ETag. |
| GET | /v1/templates/{template_id} | † Public | ✅ Read one template with its variants; supports ETag. |
| GET | /v1/rewards/state | Signed-in | **Not implemented.** Reward state is available through `/v1/bootstrap` and step-action responses. |
| GET | /internal/jobs/purge | † Vercel Cron bearer secret | ✅ Retry Auth deletion and run daily retention purge; protected by `CRON_SECRET`. |

### Shared API conventions

- Base path is /v1. The deployed API origin is separate from the frontend origin.
- Protected routes use Authorization: Bearer <access_token>. Health and template reads are public. The internal purge job uses a separate server-only cron bearer credential and is never a frontend route.
- JSON requests use Content-Type: application/json. Responses are UTF-8 JSON.
- Every response includes X-Request-Id. Error responses have a stable code and request ID.
- Implemented profile, task, step, and account-deletion writes require `Idempotency-Key`. Retry the same action with the same key and body; a changed request under that key returns `409 idempotency_key_reused`. `POST /v1/ai/breakdown` is suggestion-only and does not require an idempotency key.
- Task delta reads support `updated_since`, server-generated `next_since`/snapshot cursors, tombstones, pagination, and `full_refresh_required` after the retention window.
- Personal responses use private cache policy; do not place them in shared/public caches. ETags allow efficient refreshes. Public template responses are short-lived and cacheable.
- A 404 is used for resources the user does not own, to avoid confirming that another user’s record exists.
- `openapi/openapi.yaml` is the API contract; the CI route comparison fails if it drifts from Flask.

See [docs/frontend-integration.md](docs/frontend-integration.md) for token handling, error behavior, idempotency, ETags, and bootstrap/delta caching. See [docs/deployment.md](docs/deployment.md) for runtime, migration, environment-variable names, CORS, Supabase Data API/RLS, and key rotation.

## What this API guide does not mean

The ✅ mark describes code in this checkout, not a deployed service. Apply and verify migrations 001–009 before using all database-backed routes against Supabase. Deployment, scheduled-job operation, and live frontend integration still require the selected Vercel project and environment configuration to be linked and verified. Authenticated calls require a valid Supabase session. The selected deployment project is not linked in this local folder; see [the handoff](docs/deployment.md).

The MVP does not provide reminders, routines, push subscriptions, analytics, feedback, peer/tutor check-ins, or offline action replay. Add those only in separately agreed product phases.






