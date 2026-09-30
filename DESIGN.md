# One-page design

## Problem

SaaS teams need an auditable answer to what a tenant used, whether it is within its plan, and what that usage costs. This service meters one dummy AI-generation action safely under network retries.

## Data model

`plans` contain integer API-call and token limits. `tenants` reference a plan and subscription state. A `usage_event` is a single billable request, including its idempotency key and all integer token categories. `idempotency_responses` persists the original successful response. `processed_webhook_events` prevents Stripe replay effects. `jobs` tracks background work.

## API surface

`POST /tenants` creates a tenant. `POST /generate` meters one generation request and requires `Idempotency-Key`. `GET /usage/{tenant_id}` returns its rollup. `POST /checkout/{tenant_id}` creates Stripe test-mode Checkout. `POST /webhooks/stripe` accepts verified Stripe events. `POST /jobs/rollup` queues a background reconciliation/rollup job.

## Layers

HTTP routes in `main.py` validate request boundaries. The metering logic (`calculate_cost`, quota decision, and insert) runs inside a database transaction. SQLite persistence owns the constraints and indexes. Stripe is isolated to checkout and verified-webhook paths.

## Guardrails

The metering transaction begins with `BEGIN IMMEDIATE`, checks the stored idempotent response, reads usage, checks limits, writes exactly one event, and stores the returned response. The database also has a unique `(tenant_id, idempotency_key)` constraint. Exact limits are allowed; requests beyond them get `429`. Non-active subscriptions get `402`.

## Explicit non-goal

No live payments, invoices, proration, or overage billing. Stripe is test mode only.
