# Usage Metering & Billing Engine

A deliberately small SaaS billing backend. It records usage once despite retries, enforces monthly quotas, calculates token costs using integer microcents, and mirrors Stripe test-mode subscription events.

## Architecture

```text
client -> POST /generate -> transaction: idempotency check -> quota check -> usage event
                                  |                                 |
                                  +-> stored original response       +-> 429 / 402 when blocked
GET /usage/{tenant} -> usage-event rollup -> usage, limits, cost
Stripe Checkout -> signed webhook -> verified + deduplicated -> tenant plan/status
```

The initial schema is documented in `migrations/001_initial.sql`; the app applies an idempotent local equivalent at startup.

## Rules that matter

- A `(tenant_id, idempotency_key)` unique constraint plus one write transaction prevents duplicate metering.
- The exact quota is allowed; only the request above it returns `429`.
- Usage quotas and costs cover the UTC calendar month: first-day midnight inclusive through next-month midnight exclusive. `GET /usage/{tenant_id}` returns these boundaries in `period`. This is independent of Stripe's subscription anniversary billing dates.
- A new month starts with fresh quota automatically through timestamp filtering; historical events are retained. Idempotency keys remain tenant-wide across months, so retrying an old request returns its original response without new usage. Use a new key for a new action.
- All money is integer microcents. Cached input has its own lower price; reasoning tokens use output-token pricing.
- Stripe is test mode only. Webhook signature verification happens before state changes; each Stripe event ID is processed once.

## Run locally

Tenant routes now require `Authorization: Bearer <tenant-api-key>`. Missing/invalid keys return 401; another tenant's ID returns 403 before database usage or Stripe actions. Tenant creation, key issuance/rotation and all job routes require a separate admin bearer key. Stripe webhooks retain signature authentication and do not require a tenant key.

Generate an admin secret locally with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and put it in `.env` as `ADMIN_API_KEY`. Never commit it. A missing/placeholder admin key fails closed. Restart the server after changing `.env`.

```powershell
Copy-Item .env.example .env
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe seed.py
.\venv\Scripts\uvicorn.exe main:app --reload --env-file .env
```

In another terminal, enter your admin secret privately and issue a key for the seeded tenant (issuing again invalidates its old key):

```powershell
$adminCredential = Get-Credential -UserName admin -Message "Enter ADMIN_API_KEY as the password"
$adminHeaders = @{Authorization = "Bearer " + $adminCredential.GetNetworkCredential().Password}
$issued = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/tenants/demo-free/api-key" -Headers $adminHeaders
$tenantHeaders = @{Authorization = "Bearer " + $issued.api_key}
Invoke-RestMethod -Uri "http://127.0.0.1:8000/usage/demo-free" -Headers $tenantHeaders
```

Keys are random 256-bit values stored only as SHA-256 hashes in `tenant_api_keys`; key responses use `Cache-Control: no-store`. Save the returned key privately; it cannot be retrieved later. Seed does not assign default keys or reset existing subscriptions. Outside localhost, use HTTPS. Browser login, user accounts, automatic key expiry and rate limiting are not implemented.

To meter usage for an active tenant:

```powershell
$tenantHeaders["Idempotency-Key"] = "demo-1"
Invoke-RestMethod -Method Post http://localhost:8000/generate -Headers $tenantHeaders -ContentType application/json -Body '{"tenant_id":"demo-free","cached_input_tokens":100,"reasoning_tokens":25}'
Invoke-RestMethod http://localhost:8000/usage/demo-free -Headers $tenantHeaders
```

Run checks with `.\venv\Scripts\python.exe -m pytest -q`.

## Stripe test mode

Set `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` in `.env`, then run `stripe listen --forward-to localhost:8000/webhooks/stripe`. Use only Stripe test-mode keys. The checkout endpoint creates a $10/month Pro Checkout session.

## Background work

`POST /jobs/rollup` queues a rollup/reconciliation job and `GET /jobs/{job_id}` exposes its status. In production this task would run in a durable worker queue; the compact local implementation uses FastAPI background tasks.

## Limitations

This is a learning-sized engine: SQLite, current UTC calendar-month summaries only, no invoices, proration, or production job queue. Historical events remain stored, but there is no historical-month query endpoint. It should not process live payments without further operational hardening.
