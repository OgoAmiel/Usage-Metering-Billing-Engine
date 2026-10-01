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

```powershell
Copy-Item .env.example .env
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe seed.py
.\venv\Scripts\uvicorn.exe main:app --reload
```

In another terminal:

```powershell
Invoke-RestMethod -Method Post http://localhost:8000/generate -Headers @{"Idempotency-Key"="demo-1"} -ContentType application/json -Body '{"tenant_id":"demo-free","cached_input_tokens":100,"reasoning_tokens":25}'
Invoke-RestMethod http://localhost:8000/usage/demo-free
```

Run checks with `.\venv\Scripts\python.exe -m pytest -q`.

## Stripe test mode

Set `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` in `.env`, then run `stripe listen --forward-to localhost:8000/webhooks/stripe`. Use only Stripe test-mode keys. The checkout endpoint creates a $10/month Pro Checkout session.

## Background work

`POST /jobs/rollup` queues a rollup/reconciliation job and `GET /jobs/{job_id}` exposes its status. In production this task would run in a durable worker queue; the compact local implementation uses FastAPI background tasks.

## Limitations

This is a learning-sized engine: SQLite, current UTC calendar-month summaries only, no invoices, proration, or production job queue. Historical events remain stored, but there is no historical-month query endpoint. It should not process live payments without further operational hardening.
