"""Usage metering and billing engine: deliberately small, transaction-safe, and testable."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import stripe
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

DB_PATH = os.getenv("DATABASE_PATH", "./billing.db")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")

# All amounts are integer microcents. Never use floating-point money.
PRICING = {
    "api_call_microcents": 1000,
    "input_token_microcents": 3,
    "cached_input_token_microcents": 1,
    "output_token_microcents": 12,
    "reasoning_token_microcents": 12,  # reasoning is billed as output
}
PLANS = {
    "free": {"api_call_limit": 1_000, "token_limit": 100_000},
    "pro": {"api_call_limit": 10_000, "token_limit": 1_000_000},
}

app = FastAPI(title="Usage Metering & Billing Engine", version="1.0.0")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def monthly_period(at: str) -> tuple[str, str]:
    """UTC calendar month: inclusive start, exclusive next-month start."""
    moment = datetime.fromisoformat(at).astimezone(timezone.utc)
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start.isoformat(), end.isoformat()


@contextmanager
def db(write: bool = False):
    connection = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        if write:
            connection.execute("BEGIN IMMEDIATE")  # serializes quota decisions
        yield connection
        if write:
            connection.commit()
    except Exception:
        if write:
            connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    with db(write=True) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS plans (
          id TEXT PRIMARY KEY, api_call_limit INTEGER NOT NULL, token_limit INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tenants (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, plan_id TEXT NOT NULL REFERENCES plans(id),
          subscription_status TEXT NOT NULL DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS usage_events (
          id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
          idempotency_key TEXT NOT NULL, api_calls INTEGER NOT NULL, input_tokens INTEGER NOT NULL,
          cached_input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
          reasoning_tokens INTEGER NOT NULL, cost_microcents INTEGER NOT NULL, created_at TEXT NOT NULL,
          UNIQUE(tenant_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_usage_events_tenant_created ON usage_events(tenant_id, created_at);
        CREATE TABLE IF NOT EXISTS idempotency_responses (
          tenant_id TEXT NOT NULL, idempotency_key TEXT NOT NULL, status_code INTEGER NOT NULL,
          payload TEXT NOT NULL, PRIMARY KEY(tenant_id, idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS processed_webhook_events (
          stripe_event_id TEXT PRIMARY KEY, processed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL,
          error TEXT, created_at TEXT NOT NULL, finished_at TEXT
        );
        """)
        for plan_id, plan in PLANS.items():
            conn.execute("INSERT OR IGNORE INTO plans VALUES (?, ?, ?)",
                         (plan_id, plan["api_call_limit"], plan["token_limit"]))


@app.on_event("startup")
def startup() -> None:
    init_db()


class GenerateRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=100)
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)


class TenantRequest(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    plan_id: str


def token_total(body: GenerateRequest) -> int:
    return body.input_tokens + body.cached_input_tokens + body.output_tokens + body.reasoning_tokens


def calculate_cost(body: GenerateRequest) -> int:
    return (PRICING["api_call_microcents"]
            + body.input_tokens * PRICING["input_token_microcents"]
            + body.cached_input_tokens * PRICING["cached_input_token_microcents"]
            + body.output_tokens * PRICING["output_token_microcents"]
            + body.reasoning_tokens * PRICING["reasoning_token_microcents"])


def usage_row(conn: sqlite3.Connection, tenant_id: str, at: str | None = None) -> sqlite3.Row:
    start, end = monthly_period(at or utcnow())
    row = conn.execute("""SELECT t.id, t.plan_id, t.subscription_status, p.api_call_limit, p.token_limit,
      COALESCE(SUM(e.api_calls), 0) api_calls, COALESCE(SUM(e.input_tokens + e.cached_input_tokens + e.output_tokens + e.reasoning_tokens), 0) tokens,
      COALESCE(SUM(e.cost_microcents), 0) cost_microcents
      FROM tenants t JOIN plans p ON p.id=t.plan_id LEFT JOIN usage_events e ON e.tenant_id=t.id
      AND e.created_at >= ? AND e.created_at < ?
      WHERE t.id=? GROUP BY t.id""", (start, end, tenant_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="tenant not found")
    return row


@app.post("/tenants", status_code=201)
def create_tenant(body: TenantRequest):
    if body.plan_id not in PLANS:
        raise HTTPException(422, "plan_id must be free or pro")
    with db(write=True) as conn:
        try:
            conn.execute("INSERT INTO tenants (id,name,plan_id) VALUES (?, ?, ?)", (body.id, body.name, body.plan_id))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "tenant already exists")
    return {"id": body.id, "plan_id": body.plan_id}


@app.post("/generate")
def generate(body: GenerateRequest, idempotency_key: str | None = Header(default=None)):
    if not idempotency_key:
        raise HTTPException(400, "Idempotency-Key header is required")
    if len(idempotency_key) > 255:
        raise HTTPException(422, "Idempotency-Key is too long")
    with db(write=True) as conn:
        saved = conn.execute("SELECT status_code,payload FROM idempotency_responses WHERE tenant_id=? AND idempotency_key=?",
                             (body.tenant_id, idempotency_key)).fetchone()
        if saved:
            return json.loads(saved["payload"])
        # Capture once after acquiring the write lock so checking and recording
        # use the same month even when the request crosses midnight.
        recorded_at = utcnow()
        summary = usage_row(conn, body.tenant_id, recorded_at)
        if summary["subscription_status"] != "active":
            raise HTTPException(status_code=402, detail="subscription is not active; upgrade or update payment")
        next_calls = summary["api_calls"] + 1
        next_tokens = summary["tokens"] + token_total(body)
        if next_calls > summary["api_call_limit"] or next_tokens > summary["token_limit"]:
            raise HTTPException(status_code=429, detail={"message": "usage quota exceeded", "used_api_calls": summary["api_calls"],
                "api_call_limit": summary["api_call_limit"], "used_tokens": summary["tokens"], "token_limit": summary["token_limit"]})
        cost = calculate_cost(body)
        event_id = str(uuid.uuid4())
        conn.execute("""INSERT INTO usage_events VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)""",
                     (event_id, body.tenant_id, idempotency_key, body.input_tokens, body.cached_input_tokens,
                      body.output_tokens, body.reasoning_tokens, cost, recorded_at))
        response = {"event_id": event_id, "accepted": True, "cost_microcents": cost, "idempotent_replay": False}
        conn.execute("INSERT INTO idempotency_responses VALUES (?, ?, ?, ?)",
                     (body.tenant_id, idempotency_key, 200, json.dumps(response)))
        return response


@app.get("/usage/{tenant_id}")
def get_usage(tenant_id: str):
    at = utcnow()
    start, end = monthly_period(at)
    with db() as conn:
        row = usage_row(conn, tenant_id, at)
    return {"tenant_id": tenant_id, "plan": row["plan_id"], "subscription_status": row["subscription_status"],
            "period": {"start": start, "end": end, "timezone": "UTC"},
            "api_calls": {"used": row["api_calls"], "limit": row["api_call_limit"]},
            "ai_tokens": {"used": row["tokens"], "limit": row["token_limit"]},
            "cost_microcents": row["cost_microcents"], "pricing": PRICING}


@app.post("/checkout/{tenant_id}")
def checkout(tenant_id: str):
    with db() as conn:
        usage_row(conn, tenant_id)
    key = os.getenv("STRIPE_SECRET_KEY")
    if not key or "replace_me" in key:
        raise HTTPException(503, "Stripe test key is not configured")
    stripe.api_key = key
    session = stripe.checkout.Session.create(
        mode="subscription",
        success_url=f"{APP_BASE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{APP_BASE_URL}/cancel",
        metadata={"tenant_id": tenant_id},
        subscription_data={
            "metadata": {"tenant_id": tenant_id}
        },
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {"name": "Pro plan"},
                "unit_amount": 1000,
                "recurring": {"interval": "month"},
            },
            "quantity": 1,
        }],
    )
    return {"checkout_url": session.url, "session_id": session.id}


def process_stripe_event(event: dict[str, Any]) -> bool:
    """Returns False for a replay; only verified callers reach this function."""
    event_id = event["id"]
    with db(write=True) as conn:
        if conn.execute("SELECT 1 FROM processed_webhook_events WHERE stripe_event_id=?", (event_id,)).fetchone():
            return False
        obj = event["data"]["object"]
        tenant_id = obj.get("metadata", {}).get("tenant_id")
        if tenant_id:
            if event["type"] == "checkout.session.completed":
                conn.execute("UPDATE tenants SET plan_id='pro', subscription_status='active' WHERE id=?", (tenant_id,))
            elif event["type"] == "customer.subscription.updated":
                subscription_status = obj.get("status")
                if subscription_status not in {
                    "incomplete", "incomplete_expired", "trialing", "active",
                    "past_due", "canceled", "unpaid", "paused",
                }:
                    raise HTTPException(422, "missing or unsupported subscription status")
                # Retain Pro limits during payment problems; generate() blocks
                # non-active statuses under our current strict-access policy.
                plan_id = "free" if subscription_status in {"canceled", "incomplete_expired"} else "pro"
                conn.execute(
                    "UPDATE tenants SET plan_id=?, subscription_status=? WHERE id=?",
                    (plan_id, subscription_status, tenant_id),
                )
            elif event["type"] == "customer.subscription.deleted":
                conn.execute("UPDATE tenants SET plan_id='free', subscription_status='canceled' WHERE id=?", (tenant_id,))
        conn.execute("INSERT INTO processed_webhook_events VALUES (?, ?)", (event_id, utcnow()))
    return True


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    signature = request.headers.get("stripe-signature")
    secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    if not secret or "replace_me" in secret:
        raise HTTPException(503, "Stripe webhook secret is not configured")
    try:
        event = stripe.Webhook.construct_event(payload, signature, secret)
    except Exception:
        raise HTTPException(400, "invalid Stripe signature")
    return {"processed": process_stripe_event(event)}


def run_rollup_job(job_id: str) -> None:
    try:
        with db(write=True) as conn:
            conn.execute("UPDATE jobs SET status='completed',attempts=attempts+1,finished_at=? WHERE id=?", (utcnow(), job_id))
    except Exception as exc:
        with db(write=True) as conn:
            conn.execute("UPDATE jobs SET status='failed',attempts=attempts+1,error=?,finished_at=? WHERE id=?", (str(exc), utcnow(), job_id))


@app.post("/jobs/rollup", status_code=202)
def enqueue_rollup(background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    with db(write=True) as conn:
        conn.execute("INSERT INTO jobs VALUES (?, 'monthly_rollup', 'queued', 0, NULL, ?, NULL)", (job_id, utcnow()))
    background_tasks.add_task(run_rollup_job, job_id)
    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with db() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        raise HTTPException(404, "job not found")
    return dict(job)
