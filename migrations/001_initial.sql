-- Initial persistence schema. main.py applies the equivalent idempotent schema at startup for local convenience.
CREATE TABLE plans (id TEXT PRIMARY KEY, api_call_limit INTEGER NOT NULL, token_limit INTEGER NOT NULL);
CREATE TABLE tenants (id TEXT PRIMARY KEY, name TEXT NOT NULL, plan_id TEXT NOT NULL REFERENCES plans(id), subscription_status TEXT NOT NULL DEFAULT 'active');
CREATE TABLE usage_events (
  id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id), idempotency_key TEXT NOT NULL,
  api_calls INTEGER NOT NULL, input_tokens INTEGER NOT NULL, cached_input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL, reasoning_tokens INTEGER NOT NULL, cost_microcents INTEGER NOT NULL,
  created_at TEXT NOT NULL, UNIQUE(tenant_id, idempotency_key)
);
CREATE INDEX idx_usage_events_tenant_created ON usage_events(tenant_id, created_at);
CREATE TABLE idempotency_responses (tenant_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, status_code INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(tenant_id, idempotency_key));
CREATE TABLE processed_webhook_events (stripe_event_id TEXT PRIMARY KEY, processed_at TEXT NOT NULL);
CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL, error TEXT, created_at TEXT NOT NULL, finished_at TEXT);
