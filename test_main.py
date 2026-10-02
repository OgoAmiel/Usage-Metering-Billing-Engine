import pytest
import main
from fastapi.testclient import TestClient
from main import app, init_db, db, issue_tenant_key


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'metering.db'))


def reset():
    init_db()
    with db(write=True) as c:
        c.execute("INSERT INTO tenants (id,name,plan_id) VALUES ('t1','Tenant One','free')")
        return issue_tenant_key(c, 't1')


def test_retry_creates_one_event():
    key = reset()
    with TestClient(app, headers={'Authorization': f'Bearer {key}'}) as client:
        headers = {"Idempotency-Key": "same-request"}
        first = client.post("/generate", json={"tenant_id":"t1", "output_tokens":10}, headers=headers)
        second = client.post("/generate", json={"tenant_id":"t1", "output_tokens":10}, headers=headers)
        assert first.status_code == second.status_code == 200
        assert first.json()["event_id"] == second.json()["event_id"]
        assert client.get("/usage/t1").json()["api_calls"]["used"] == 1


def test_exact_quota_is_allowed_next_is_rejected():
    key = reset()
    with db(write=True) as c:
        c.execute("UPDATE plans SET api_call_limit=1 WHERE id='free'")
    with TestClient(app, headers={'Authorization': f'Bearer {key}'}) as client:
        assert client.post("/generate", json={"tenant_id":"t1"}, headers={"Idempotency-Key":"one"}).status_code == 200
        blocked = client.post("/generate", json={"tenant_id":"t1"}, headers={"Idempotency-Key":"two"})
        assert blocked.status_code == 429
