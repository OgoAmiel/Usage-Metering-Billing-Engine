"""Local signed-webhook tests; no Stripe calls or real credentials."""
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "billing.db"))
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "local-test-signing-secret")
    with TestClient(main.app) as client:
        with main.db(write=True) as conn:
            conn.execute("INSERT INTO tenants (id,name,plan_id) VALUES ('t1','Test','pro')")
            key = main.issue_tenant_key(conn, 't1')
        client.headers['Authorization'] = f'Bearer {key}'
        yield client


def send_update(client, status, event_id="evt_local_update"):
    payload = json.dumps({
        "id": event_id, "object": "event", "type": "customer.subscription.updated",
        "data": {"object": {"id": "sub_local", "object": "subscription",
                            "metadata": {"tenant_id": "t1"}, "status": status}},
    })
    timestamp = int(time.time())
    signature = hmac.new(b"local-test-signing-secret", f"{timestamp}.{payload}".encode(), hashlib.sha256).hexdigest()
    return client.post("/webhooks/stripe", content=payload,
                       headers={"Stripe-Signature": f"t={timestamp},v1={signature}"})


@pytest.mark.parametrize("status", ["active", "past_due", "unpaid", "paused", "trialing", "incomplete", "canceled", "incomplete_expired"])
def test_update_preserves_status_and_enforces_access(client, status):
    response = send_update(client, status)
    assert response.status_code == 200
    assert response.json() == {"processed": True}
    usage = client.get("/usage/t1").json()
    assert usage["subscription_status"] == status
    assert usage["plan"] == ("free" if status in {"canceled", "incomplete_expired"} else "pro")
    action = client.post("/generate", json={"tenant_id": "t1"}, headers={"Idempotency-Key": "test-action"})
    assert action.status_code == (200 if status == "active" else 402)


def test_recovery_and_duplicate_do_not_restore_old_status(client):
    assert send_update(client, "past_due", "evt_past_due").status_code == 200
    assert send_update(client, "active", "evt_recovery").status_code == 200
    assert send_update(client, "past_due", "evt_past_due").json() == {"processed": False}
    assert client.get("/usage/t1").json()["subscription_status"] == "active"


def test_missing_status_rolls_back_event(client):
    assert send_update(client, None).status_code == 422
    with main.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM processed_webhook_events").fetchone()[0] == 0
    assert client.get("/usage/t1").json()["subscription_status"] == "active"


def test_checkout_attaches_tenant_to_subscription(client, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_local_dummy")
    captured = {}
    def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(url="https://example.invalid/checkout", id="cs_test_local")
    monkeypatch.setattr(main.stripe.checkout.Session, "create", create)
    assert client.post("/checkout/t1").status_code == 200
    assert captured["metadata"] == {"tenant_id": "t1"}
    assert captured["subscription_data"]["metadata"] == {"tenant_id": "t1"}
