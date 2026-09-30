import os
from pathlib import Path

os.environ["DATABASE_PATH"] = str(Path(__file__).with_name("test_billing.db"))
from fastapi.testclient import TestClient
from main import app, init_db, db


def reset():
    Path(os.environ["DATABASE_PATH"]).unlink(missing_ok=True)
    init_db()
    with db(write=True) as c:
        c.execute("INSERT INTO tenants (id,name,plan_id) VALUES ('t1','Tenant One','free')")


def test_retry_creates_one_event():
    reset()
    with TestClient(app) as client:
        headers = {"Idempotency-Key": "same-request"}
        first = client.post("/generate", json={"tenant_id":"t1", "output_tokens":10}, headers=headers)
        second = client.post("/generate", json={"tenant_id":"t1", "output_tokens":10}, headers=headers)
        assert first.status_code == second.status_code == 200
        assert first.json()["event_id"] == second.json()["event_id"]
        assert client.get("/usage/t1").json()["api_calls"]["used"] == 1


def test_exact_quota_is_allowed_next_is_rejected():
    reset()
    with db(write=True) as c:
        c.execute("UPDATE plans SET api_call_limit=1 WHERE id='free'")
    with TestClient(app) as client:
        assert client.post("/generate", json={"tenant_id":"t1"}, headers={"Idempotency-Key":"one"}).status_code == 200
        blocked = client.post("/generate", json={"tenant_id":"t1"}, headers={"Idempotency-Key":"two"})
        assert blocked.status_code == 429
