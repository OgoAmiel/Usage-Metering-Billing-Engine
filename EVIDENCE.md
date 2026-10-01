# Evidence

Paste real command output here as the system is run.

## Idempotent metering

`python -m pytest -q`:

```text
2 passed, 3 warnings in 4.50s
```

Manual request and replay both returned event `81ccc537-9a77-43ae-9c67-20b4fe3aabb1`; the subsequent usage rollup reported `api_calls.used: 1`.

## Exact quota boundary

The boundary test creates a plan with a limit of one call, accepts the first request, and receives `429` on the second (`test_exact_quota_is_allowed_next_is_rejected`).

## Stripe Checkout and webhook verification

### Completed sandbox Checkout upgrades the tenant

Observed on 2026-09-29. The following output was supplied by the developer from the Stripe CLI and the local API. Formatting was normalized from chat; no credentials are included.

The app and listener used the same Stripe sandbox account, `acct_1UInwOF3gJzrtdpO`. Before Checkout, `demo-free` had plan `free`, an API-call limit of 1,000, and a token limit of 100,000.

After completing the app-created Checkout session, the listener reported:

```text
2026-09-29 13:52:11   --> checkout.session.completed [evt_1UKzZuF3gJzrtdpO6vY11Bif]
2026-09-29 13:52:11  <--  [200] POST http://127.0.0.1:8000/webhooks/stripe [evt_1UKzZuF3gJzrtdpO6vY11Bif]
```

The subsequent usage check:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/usage/demo-free" | ConvertTo-Json -Depth 5
```

returned:

```json
{
  "tenant_id": "demo-free",
  "plan": "pro",
  "subscription_status": "active",
  "api_calls": {
    "used": 1,
    "limit": 10000
  },
  "ai_tokens": {
    "used": 125,
    "limit": 1000000
  },
  "cost_microcents": 1400,
  "pricing": {
    "api_call_microcents": 1000,
    "input_token_microcents": 3,
    "cached_input_token_microcents": 1,
    "output_token_microcents": 12,
    "reasoning_token_microcents": 12
  }
}
```

This demonstrates successful sandbox Checkout, webhook delivery and acceptance, and the tenant upgrade to Pro with higher quotas. Existing usage and cost were preserved.

### Forged webhook signature rejected

Executed by the developer on 2026-09-29 against the local API. Commands and output were normalized from the supplied chat transcript.

```powershell
curl.exe -i -X POST "http://127.0.0.1:8000/webhooks/stripe" -H "Content-Type: application/json" -H "Stripe-Signature: deliberately-invalid" --data-raw "{}"
```

Response:

```text
HTTP/1.1 400 Bad Request
date: Tue, 29 Sep 2026 12:05:16 GMT
server: uvicorn
content-length: 37
content-type: application/json

{"detail":"invalid Stripe signature"}
```

A subsequent `GET /usage/demo-free` returned the same values as before the forged request: plan `pro`, subscription status `active`, API calls used `1` with limit `10000`, AI tokens used `125` with limit `1000000`, and cost `1400` microcents. This confirms signature rejection and unchanged tenant usage/plan state through the API; a full database comparison was not performed.

### Duplicate delivery of the same event

The developer replayed the original successful Checkout event:

```powershell
stripe events resend evt_1UKzZuF3gJzrtdpO6vY11Bif
```

The listener confirmed a second delivery of that same event ID:

```text
2026-09-29 14:10:50   --> checkout.session.completed [evt_1UKzZuF3gJzrtdpO6vY11Bif]
2026-09-29 14:10:50  <--  [200] POST http://127.0.0.1:8000/webhooks/stripe [evt_1UKzZuF3gJzrtdpO6vY11Bif]
```

After replay, a read-only SQLite check queried the event count and original processing time, and the tenant's plan/status:

```sql
SELECT COUNT(*), MIN(processed_at)
FROM processed_webhook_events
WHERE stripe_event_id = 'evt_1UKzZuF3gJzrtdpO6vY11Bif';

SELECT plan_id, subscription_status FROM tenants WHERE id = 'demo-free';
```

Actual output:

```text
Stored event rows: 1
Originally processed at: 2026-09-29T11:52:11.168737+00:00
Tenant: ('pro', 'active')
```

The original processing timestamp is 13:52:11 SAST, before the replay at 14:10:50 SAST. The duplicate was acknowledged with HTTP 200 while the stored event count remained one, the original processing timestamp remained unchanged, and the tenant remained Pro/active. This supports duplicate-event protection for this replay scenario.

### Subscription update synchronization

Verified on 2026-09-30 against the existing Stripe sandbox subscription `sub_1UKzZtF3gJzrtdpOhgBrosOK`. Adding `tenant_id: demo-free` to its metadata generated a real `customer.subscription.updated` event without changing its price or billing schedule. The developer resent the event after starting the listener:

```powershell
stripe events resend evt_1ULKxlF3gJzrtdpO6zY3eEuq
```

Relevant fields from the Stripe response (excerpt; unrelated fields omitted):

```json
{
  "id": "evt_1ULKxlF3gJzrtdpO6zY3eEuq",
  "type": "customer.subscription.updated",
  "livemode": false,
  "data": {
    "object": {
      "id": "sub_1UKzZtF3gJzrtdpOhgBrosOK",
      "metadata": {"tenant_id": "demo-free"},
      "status": "active"
    }
  }
}
```

The developer also supplied the listener's delivery confirmation (URLs and escaped underscores normalized from chat):

```text
2026-09-30 12:43:48   --> customer.subscription.updated [evt_1ULKxlF3gJzrtdpO6zY3eEuq]
2026-09-30 12:43:48  <--  [200] POST http://127.0.0.1:8000/webhooks/stripe [evt_1ULKxlF3gJzrtdpO6zY3eEuq]
```

A subsequent read-only check of the app's configured SQLite database returned:

```text
Update event: [('evt_1ULKxlF3gJzrtdpO6zY3eEuq', '2026-09-30T10:43:48.369461+00:00')]
Tenant: ('pro', 'active')
```

This confirms the real sandbox update was recorded as processed and the mapped tenant remained Pro/active, matching Stripe. This metadata-only update does not demonstrate a real Stripe transition from active to another status.

### Local subscription status tests

Command run in the Capstone workspace:

```powershell
.\venv\Scripts\python.exe -m pytest -q
```

Actual result:

```text
13 passed, 3 warnings in 3.15s
```

The suite includes the two existing metering/quota tests plus 11 tests in `test_subscription_updates.py`. The subscription tests use temporary databases and locally signed webhook payloads; they do not contact Stripe.

- `test_update_preserves_status_and_enforces_access`: eight cases covering active, past_due, unpaid, paused, trialing, incomplete, canceled, and incomplete_expired. Only active permits generation under the current strict-access policy; other statuses return 402.
- `test_recovery_and_duplicate_do_not_restore_old_status`: recovery to active and replay of an already processed past-due event.
- `test_missing_status_rolls_back_event`: invalid status rejected without recording the event or changing tenant status.
- `test_checkout_attaches_tenant_to_subscription`: mocked Checkout creation includes tenant metadata on both the session and subscription.

The warnings concern deprecated FastAPI startup hooks and the Starlette/AnyIO test-client alias.

### Cancellation: observed tenant state

After the sandbox cancellation workflow, the developer supplied this usage result. Formatting was normalized from chat; the exact cancellation event time and delivery log have not yet been supplied.

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/usage/demo-free" | ConvertTo-Json -Depth 5
```

```json
{
  "tenant_id": "demo-free",
  "plan": "free",
  "subscription_status": "canceled",
  "api_calls": {"used": 1, "limit": 1000},
  "ai_tokens": {"used": 125, "limit": 100000},
  "cost_microcents": 1400,
  "pricing": {
    "api_call_microcents": 1000,
    "input_token_microcents": 3,
    "cached_input_token_microcents": 1,
    "output_token_microcents": 12,
    "reasoning_token_microcents": 12
  }
}
```

The API shows Free/canceled with Free quotas restored, while usage and cost remain unchanged. This records the resulting tenant state; it does not independently establish the cancellation webhook's event ID or delivery response.

### Remaining webhook checks

- Pending: attach the `customer.subscription.deleted` event ID and listener delivery/status lines to complete the cancellation delivery evidence.
- Other status transitions have local test coverage above, but have not been exercised end-to-end against Stripe.

## Monthly usage periods

UTC calendar-month quotas and costs are filtered by `created_at >= period_start AND created_at < period_end`. Historical events and tenant-wide idempotency records remain intact. No reset job or schema change is required; the existing tenant/timestamp index supports the query.

Verification command:

```powershell
.\venv\Scripts\python.exe -m pytest -q
```

Actual result after the monthly-period change:

```text
19 passed, 3 warnings in 5.35s
```

Six cases in `test_monthly_usage.py` use temporary databases and controlled clocks: September-to-October quota/cost rollover with historical retention and cross-month replay; December-to-January, leap February, and non-leap February boundaries; next-month exclusion; and a single metering timestamp across midnight. The other 13 tests also passed. No Stripe calls are made by these tests.

### Observed October usage response

On 2026-10-01, the developer ran the following against the running local API and supplied the output below (chat formatting normalized):

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/usage/demo-free" | ConvertTo-Json -Depth 5
```

```json
{
  "tenant_id": "demo-free",
  "plan": "free",
  "subscription_status": "canceled",
  "period": {
    "start": "2026-10-01T00:00:00+00:00",
    "end": "2026-11-01T00:00:00+00:00",
    "timezone": "UTC"
  },
  "api_calls": {
    "used": 0,
    "limit": 1000
  },
  "ai_tokens": {
    "used": 0,
    "limit": 100000
  },
  "cost_microcents": 0,
  "pricing": {
    "api_call_microcents": 1000,
    "input_token_microcents": 3,
    "cached_input_token_microcents": 1,
    "output_token_microcents": 12,
    "reasoning_token_microcents": 12
  }
}
```

The response reports the October UTC calendar month with zero usage and cost, while retaining the Free plan, canceled subscription status, and Free quotas. September's previously observed usage is excluded from these current-month totals. Historical record retention is covered by the local rollover test above; this API response alone does not inspect stored history.

## Pricing math

One request with 100 cached-input tokens and 25 reasoning tokens returned `cost_microcents: 1400`:

```text
1000 (API call) + 100 × 1 (cached input) + 25 × 12 (reasoning/output) = 1400
```

## Background rollup job

`POST /jobs/rollup` followed by `GET /jobs/4970aa44-7cbd-4e53-a525-99caee2b64ea` returned `status: completed` and `attempts: 1`.
