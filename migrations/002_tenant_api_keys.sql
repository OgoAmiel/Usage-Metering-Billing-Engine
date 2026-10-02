CREATE TABLE IF NOT EXISTS tenant_api_keys (
    tenant_id TEXT PRIMARY KEY REFERENCES tenants(id),
    key_hash TEXT NOT NULL UNIQUE
);
