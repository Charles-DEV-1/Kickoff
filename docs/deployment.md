# Deployment handoff

This document records what is present in the local Kickoff checkout and what still needs a selected deployment. It is a handoff only: it does not deploy or alter live secrets.

## Current inspection

- Runtime entry point: `api/index.py` imports `app.create_app()` and exports `app` for Vercel's Python runtime.
- Local server command: `python -m flask --app api.index run --host 127.0.0.1 --port 5000`.
- `vercel.json` configures the `api/index.py` function and a daily `GET /internal/jobs/purge` cron.
- This Kickoff folder has no `.vercel/project.json` link, so the selected Vercel team/project, deployment domain, environment values, and production frontend origin could not be verified here.
- The local `.env` points to `mqwamtwfcehvlbmeuyeg.supabase.co` and sets development CORS origins `http://localhost:3000,http://localhost:5173`. These are local settings; this guide cannot prove they match any Vercel environment or the frontend's final production domain.
- The code uses Supabase Auth and PostgREST Data API RPC calls. There is no direct PostgreSQL client, database password, or `DATABASE_URL` in the backend stack.
- Migrations are versioned in `supabase/migrations/`. The repository code expects migrations 001–009; check the dashboard migration history before treating Stage 8 as applied.
- The local Kickoff folder is not a Git checkout and has no remote. The GitHub URL provided by the team could not be accessed from this checkout with available Git credentials, so no push or remote CI result is claimed.

## Runtime and URLs

Local API:

- Liveness: `http://127.0.0.1:5000/v1/health`
- Readiness: `http://127.0.0.1:5000/v1/health/ready`
- API origin for local frontend: `http://127.0.0.1:5000`

After deployment, substitute the selected HTTPS API domain, for example `https://<vercel-api-domain>`:

- `https://<vercel-api-domain>/v1/health`
- `https://<vercel-api-domain>/v1/health/ready`

`/v1/health` only proves Flask is answering. `/v1/health/ready` checks Supabase Auth reachability using the URL and publishable key; it does **not** prove migrations, Postgres RPC execution, RLS, or template availability. Use an authenticated smoke check after applying migrations to verify those dependencies.

## Backend environment variable names

Set these in the selected backend runtime environment. `.env.example` is the local name/reference list; `.env` is ignored and must not be committed.

| Variable | Use |
|---|---|
| `SUPABASE_URL` | Supabase project origin, used for Auth, JWKS, and PostgREST. |
| `SUPABASE_PUBLISHABLE_KEY` | Public Supabase API key used with the verified end-user token for ordinary Data API calls. |
| `SUPABASE_JWKS_URL` | Optional signing-key URL override; otherwise derived from `SUPABASE_URL`. |
| `SUPABASE_JWT_ISSUER` | Optional issuer override; otherwise derived from `SUPABASE_URL`. |
| `SUPABASE_JWT_AUDIENCE` | Expected JWT audience; current default is `authenticated`. |
| `SUPABASE_JWT_ALGORITHMS` | Allowed signing algorithm; the reviewed current project uses `ES256`. |
| `SUPPORTED_LANGUAGES` | API-supported language values; current default is `en`. |
| `ALLOWED_ORIGINS` | Comma-separated exact frontend origins. Local example: `http://localhost:3000,http://localhost:5173`. Add the actual production frontend origin only after selecting it. |
| `APP_ENV` | Optional environment label; code defaults to `local`. It is not required for authentication. |
| `PUBLIC_RATE_LIMIT_TRUSTED_PROXY_HOPS` | Optional integer, default `0`; only set to the known number of trusted proxies. |
| `SUPABASE_SECRET_KEY` | Server-only Supabase secret/service key for Auth account administration and narrowly granted server RPCs. |
| `CRON_SECRET` | Server-only random bearer secret used by Vercel Cron for the internal retention route. |
| `AI_PROVIDER`, `GROQ_API_KEY`, `GROQ_MODEL` | Optional server-side Groq configuration; the key never goes to the frontend. |
| `AI_DAILY_ALLOWANCE`, `AI_PROVIDER_DAILY_BUDGET`, `AI_CACHE_TTL_SECONDS`, `AI_CACHE_HMAC_KEY`, `AI_PROVIDER_TIMEOUT_SECONDS`, `AI_TOTAL_BUDGET_SECONDS` | AI controls. `AI_CACHE_HMAC_KEY` is server-only. |

The browser may receive the Supabase URL, publishable key, and its own API base URL. It must never receive `SUPABASE_SECRET_KEY`, `CRON_SECRET`, `GROQ_API_KEY`, `AI_CACHE_HMAC_KEY`, `SUPABASE_JWT_SECRET`, a database password, or `DATABASE_URL`.

## Supabase Data API, RLS, and RPCs

For normal signed-in API calls, Flask sends the Supabase publishable key plus the already verified user's access token to PostgREST. The SQL RPC derives the account with `auth.uid()` and runs under `SECURITY INVOKER`/RLS. There is no database URL in `.env.example` and no direct database driver in `requirements.txt`.

The server-only Supabase secret key is used only for Auth user soft/hard deletion and the narrow service-role retention/provider-budget RPCs. Do not use it for routine user queries. Keep RLS enabled and inspect policies/grants/function security after migrations; source files cannot prove what is currently applied to the selected Supabase project.

### Apply versioned SQL migrations

The team's established password-free path is Supabase Dashboard → SQL Editor: apply each unapplied migration in filename order, `202611020001` through `202611030009`. Check which files have already been executed so no migration is run twice. From PowerShell in the project folder, this command copies one migration to the clipboard:

~~~powershell
Get-Content .\supabase\migrations\202611030009_data_rights_and_retention.sql -Raw | Set-Clipboard
~~~

Paste and run that complete SQL in the Supabase SQL Editor. Change the filename to the next migration only after confirming the previous one succeeded. This workflow does not need a database password or `DATABASE_URL`.

For a CLI-managed project, the command is `supabase db push --remote` after `supabase link` and migration-history setup. This folder currently has no `supabase/config.toml`/CLI link. Applying SQL manually in the Dashboard does not automatically add the migration version to `supabase_migrations.schema_migrations`; a later CLI push can therefore try to replay those files. Because this project has used the Dashboard path, continue with it until an operator reconciles migration history. Do not run `db push` blindly. The CLI may require database connection credentials; this handoff does not configure or store them. [Supabase migration deployment guide](https://supabase.com/docs/guides/deployment/database-migrations)

## Vercel setup to complete when the team selects the project

The selected Vercel project is not linked in this folder, so these are later handoff actions, not completed setup:

1. Link the correct Vercel team/project and confirm the repository root and `api/index.py` runtime entry point.
2. Set backend environment variable names from the table above in the appropriate Vercel environments. Keep secret values in Vercel's encrypted environment settings; do not commit or paste them into frontend configuration.
3. Set `ALLOWED_ORIGINS` to only the selected frontend origins, including scheme and any non-default port.
4. Confirm the Cron definition from `vercel.json` appears after deployment and configure `CRON_SECRET` in Vercel. The daily purge is destructive for expired data; don't test it manually against production.
5. Deploy only when explicitly approved, then check the production liveness/readiness URLs and run a signed-in, non-destructive API smoke test.

Vercel Cron automatically sends its configured `CRON_SECRET` in an Authorization bearer header. On the Hobby plan a daily schedule may run at any time within its specified UTC hour, so treat the configured `03:00 UTC` schedule as an hour window, not an exact-minute guarantee. [Vercel cron security and timing](https://vercel.com/docs/cron-jobs/manage-cron-jobs)

## Key rotation notes

- **Supabase JWT signing keys:** keep the configured asymmetric algorithm fixed to the reviewed project setting. During signing-key rotation, publish/activate the replacement through Supabase, allow overlapping public keys while existing tokens expire, then retire the old signing key. The API caches JWKS keys for ten minutes and refreshes on an unknown key ID. Do not paste signing secrets here.
- **Supabase publishable key:** update frontend and backend configuration together if rotated, then rebuild/redeploy both sides. It is public, but should still be kept to the project's intended configuration.
- **Supabase secret key:** rotate only in Supabase and the backend server environment; never in frontend configuration. Verify Auth administration and service-role RPC calls after the backend environment is updated.
- **Groq key:** rotate in the provider console and the backend environment; never commit it or send it in an API request from the browser.
- **Cron secret:** rotate the Vercel environment value and redeploy the project so Cron and the route use the same value. The compare uses constant-time equality.
- **AI cache HMAC key:** rotating it safely invalidates prior keyed cache lookup results; the next requests regenerate/cache under the new key.

No live Vercel deployment, environment value, Supabase key, or database configuration was changed during Stage 9.
