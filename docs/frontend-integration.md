# Frontend integration guide

This guide describes the implemented online MVP API. It does not define routes for reminders, routines, analytics, rewards-state reads, AI-usage reads, or offline synchronization; those endpoints are not implemented.

## 1. Configure the API origin

Configure a frontend build-time value for the API origin only, for example:

- Vite: `VITE_API_BASE_URL=http://127.0.0.1:5000`
- Next.js: `NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:5000`

In production, use the deployed API's HTTPS origin. Do not append `/v1` to this value because each request path already begins with `/v1`. The API's `ALLOWED_ORIGINS` server variable must list the exact browser origins (scheme, host, and port) that load the frontend.

The frontend also needs the Supabase project URL and publishable key to initialize Supabase Auth. Those are the public client credentials. Never put `SUPABASE_SECRET_KEY`, `CRON_SECRET`, a Groq key, a database password, or `DATABASE_URL` in frontend variables.

## 2. Sign in with Supabase, then call the API

The Flask API does not implement signup or password sign-in. The frontend signs in through the Supabase client, then takes the current session's `access_token` and sends it to this API as a bearer token. Let the Supabase SDK manage refresh-token storage.

Example using `@supabase/supabase-js`:

~~~ts
const { data: { session }, error } = await supabase.auth.getSession();
if (error || !session) throw new Error("Sign in is required");

const response = await fetch(`${API_BASE_URL}/v1/me`, {
  headers: {
    Authorization: `Bearer ${session.access_token}`,
    Accept: "application/json",
  },
});
~~~

Do not send a `user_id` field or user ID header. The API verifies the token and Postgres derives the owner from the verified JWT using `auth.uid()`.

## 3. Make writes retry-safe

All implemented profile, task, step, reorder, and step-action writes require an `Idempotency-Key`. Generate one key for one user action, then retain that same key and the identical method, path, query, headers, and JSON body when retrying after a timeout or network loss. Generate a different key for the next intentional action. Do not add this header to `POST /v1/ai/breakdown`; that endpoint returns suggestions and does not mutate an app resource.

~~~ts
const key = `task-create:${crypto.randomUUID()}`;
const body = {
  title: "Prepare a project demo",
  notes: "",
  source: "manual",
  steps: [{ text: "Review the project brief", planned_minutes: 15 }],
};

async function createTask() {
  return fetch(`${API_BASE_URL}/v1/tasks`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${session.access_token}`,
      "Content-Type": "application/json",
      "Idempotency-Key": key,
    },
    body: JSON.stringify(body),
  });
}
~~~

Keep `key` and `body` stable until the API returns a final result. A retry with the same key and same request replays the original response. Reusing the key with a different request returns `409 idempotency_key_reused`. A concurrent retry can return `409 request_in_progress`; wait briefly and retry with the same key and body.

The task and step update/delete/reorder requests include the current `version` in the JSON body. On `409 version_conflict`, fetch the resource again and let the user review/reapply their edit. A guessed resource ID owned by another account returns `404`.

## 4. Read bootstrap and keep an online cache

Use `GET /v1/bootstrap` after sign-in. It returns the profile, reward snapshot, at most 50 active tasks with steps, total active-task count, template manifest, server time, and API version. Keep the ETag together with the cached bootstrap for that signed-in account:

~~~ts
const headers: Record<string, string> = {
  Authorization: `Bearer ${session.access_token}`,
};
if (cachedBootstrap?.etag) headers["If-None-Match"] = cachedBootstrap.etag;

const response = await fetch(`${API_BASE_URL}/v1/bootstrap`, { headers });
if (response.status === 304) {
  // Keep the cached body; the representation has not changed.
} else if (response.ok) {
  cachedBootstrap = {
    body: await response.json(),
    etag: response.headers.get("ETag"),
  };
}
~~~

Personal API reads use private revalidation headers; never put them in a shared/public cache. Remove account cache data on sign-out, account deletion, or user switch. `GET /v1/me`, task list/detail, delta reads, bootstrap, and public template reads support ETags. Export is a download and is `no-store`.

For task delta reads, pass the last server-issued `next_since` as `updated_since`. Follow `next_cursor` through the page set using the first page's `snapshot_at`. Persist the new `next_since` only after every page has been applied. If the server returns `full_refresh_required: true`, replace the online cache from `/v1/bootstrap`. Do not use the device clock to create a delta cursor. This API has no offline write queue, batch replay, or late event reconciliation.

## 5. Handle API errors

Errors use this shape:

~~~json
{
  "error": {
    "code": "validation_failed",
    "message": "One or more request fields are invalid.",
    "request_id": "55555555-5555-4555-8555-555555555555",
    "details": [{"field": "title", "code": "invalid_value"}]
  }
}
~~~

Use `code` for application behavior and show a safe, localized message in the frontend. Keep `X-Request-Id` if the user reports a problem; never log or display the access token.

| HTTP status | Frontend behavior |
|---|---|
| 400 / 413 / 415 | Fix malformed JSON, body size, or content type before retrying. |
| 401 `token_expired` | Refresh the Supabase session, then retry once with the refreshed access token. Preserve an idempotency key/body for a write. |
| 401 `token_invalid` / `token_revoked` | Stop the request and return the user to sign-in. |
| 401 `account_deleted` | Clear local authenticated data and show the account-deleted state. |
| 404 `not_found` | Treat the resource as unavailable; ownership is intentionally not disclosed. |
| 409 `version_conflict` | Refetch the current resource and resolve the edit. |
| 409 `request_in_progress` | Wait briefly, then retry with the same key/body. |
| 409 `idempotency_key_reused` | Do not retry that key with changed data; create a new key only for a new user action. |
| 422 `validation_failed` / `invalid_transition` | Use the safe field/state details to guide correction. |
| 429 `rate_limited` | Respect `Retry-After` and apply backoff. |
| 500 / 503 | Show a retry option. For a write, reuse the same idempotency key and body. |

## 6. Export and deletion

`GET /v1/me/export` returns a JSON file and is limited to three exports per user per 24 hours. `DELETE /v1/me` requires an `Idempotency-Key` and `{"confirm":"DELETE"}`. A `202 Accepted` response means local API access is already blocked and deletion is pending; clear authenticated state and display the returned `purge_after` date. Do not call `/internal/jobs/purge` from frontend code.

## Request flow in the backend

1. Supabase Auth issues the end-user access token; the frontend sends it with the API request.
2. `api/index.py` exports the Flask app from `create_app()`. Global middleware assigns a request ID, checks JSON/body limits, and adds security/CORS headers.
3. Protected routes run `require_auth`: signature, issuer, audience, time, subject, role, session, and the Supabase Auth user are checked. The request owner comes from the verified subject, never from client-supplied IDs.
4. The route validates query/body fields, then uses the user JWT and publishable key to call a narrowly scoped Supabase Data API RPC. Writes also pass the persistent rate limit and idempotency checks.
5. The `SECURITY INVOKER` Postgres function uses `auth.uid()` and RLS. Multi-row task, step, reward, and deletion operations run within database transactions; reward actions return step and reward data together.
6. Flask serializes the route result as JSON, attaches the request ID and cache headers, and returns it to the frontend.

Public template reads use active-template RLS and public cache headers. Supabase Auth administration and retention maintenance use a server-only key and a separate protected cron route; those credentials never belong in frontend requests.
