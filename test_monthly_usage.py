import pytest
from fastapi.testclient import TestClient
import main


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "monthly.db"))
    clock = {"now": "2026-09-30T23:59:59+00:00"}
    monkeypatch.setattr(main, "utcnow", lambda: clock["now"])
    with TestClient(main.app) as client:
        with main.db(write=True) as db:
            db.execute("INSERT INTO tenants (id,name,plan_id) VALUES ('t','Test','free')")
            db.execute("UPDATE plans SET api_call_limit=1,token_limit=10 WHERE id='free'")
            key = main.issue_tenant_key(db, 't')
        client.headers['Authorization'] = f'Bearer {key}'
        yield client, clock


def send(client, key, tokens=10):
    return client.post('/generate', json={'tenant_id':'t','input_tokens':tokens}, headers={'Idempotency-Key':key})


def test_rollover_resets_calls_tokens_cost_preserves_history_and_replay(setup):
    client, clock = setup
    original = send(client, 'september')
    assert original.status_code == 200
    assert send(client, 'blocked').status_code == 429
    clock['now'] = '2026-10-01T00:00:00+00:00'
    usage = client.get('/usage/t').json()
    assert usage['api_calls']['used'] == usage['ai_tokens']['used'] == usage['cost_microcents'] == 0
    assert usage['period'] == {'start':'2026-10-01T00:00:00+00:00', 'end':'2026-11-01T00:00:00+00:00','timezone':'UTC'}
    assert send(client, 'september').json() == original.json()
    assert send(client, 'october').status_code == 200
    assert send(client, 'over').status_code == 429
    usage = client.get('/usage/t').json()
    assert (usage['api_calls']['used'],usage['ai_tokens']['used'],usage['cost_microcents']) == (1,10,1030)
    with main.db() as db:
        assert db.execute('SELECT COUNT(*) FROM usage_events').fetchone()[0] == 2


@pytest.mark.parametrize('at,start,end', [
    ('2026-12-31T23:59:59+00:00','2026-12-01T00:00:00+00:00','2027-01-01T00:00:00+00:00'),
    ('2028-02-29T12:00:00+00:00','2028-02-01T00:00:00+00:00','2028-03-01T00:00:00+00:00'),
    ('2026-02-28T12:00:00+00:00','2026-02-01T00:00:00+00:00','2026-03-01T00:00:00+00:00'),
])
def test_calendar_boundaries(at,start,end):
    assert main.monthly_period(at) == (start,end)


def test_next_month_event_excluded_from_previous_month(setup):
    client, clock = setup
    clock['now'] = '2026-10-01T00:00:00+00:00'
    assert send(client,'october').status_code == 200
    clock['now'] = '2026-09-30T23:59:59+00:00'
    assert client.get('/usage/t').json()['api_calls']['used'] == 0


def test_metering_uses_one_timestamp(setup, monkeypatch):
    client, clock = setup
    instants = iter(['2026-09-30T23:59:59+00:00','2026-10-01T00:00:00+00:00'])
    monkeypatch.setattr(main,'utcnow',lambda: next(instants))
    assert send(client,'midnight').status_code == 200
    with main.db() as db:
        assert db.execute('SELECT created_at FROM usage_events').fetchone()[0] == '2026-09-30T23:59:59+00:00'
    assert next(instants) == '2026-10-01T00:00:00+00:00'
