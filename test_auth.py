import pytest
from fastapi.testclient import TestClient
import main


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'auth.db'))
    monkeypatch.setenv('ADMIN_API_KEY', 'test-admin-' + 'x' * 40)
    with TestClient(main.app) as client:
        admin = {'Authorization': 'Bearer test-admin-' + 'x' * 40}
        keys = {}
        for tenant in ('a', 'b'):
            r = client.post('/tenants', json={'id':tenant,'name':tenant,'plan_id':'free'}, headers=admin)
            assert r.status_code == 201
            keys[tenant] = r.json()['api_key']
        yield client, admin, keys


def auth(key):
    return {'Authorization': f'Bearer {key}'}


def test_cross_tenant_read_write_checkout_and_replay(context, monkeypatch):
    client, admin, keys = context
    monkeypatch.setattr(main.stripe.checkout.Session, 'create', lambda **kw: pytest.fail('Unauthorized Stripe call'))
    for tenant in ('a','b'):
        r=client.post('/generate',json={'tenant_id':tenant},headers={**auth(keys[tenant]),'Idempotency-Key':'same'})
        assert r.status_code == 200
    assert client.get('/usage/a',headers=auth(keys['a'])).json()['api_calls']['used'] == 1
    assert client.get('/usage/b',headers=auth(keys['a'])).status_code == 403
    assert client.post('/checkout/b',headers=auth(keys['a'])).status_code == 403
    assert client.post('/generate',json={'tenant_id':'b'},headers={**auth(keys['a']),'Idempotency-Key':'same'}).status_code == 403
    with main.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM usage_events').fetchone()[0] == 2


@pytest.mark.parametrize('method,path,body', [('GET','/usage/a',None),('POST','/generate',{'tenant_id':'a'}),('POST','/checkout/a',None),('POST','/tenants',{'id':'c','name':'C','plan_id':'pro'}),('POST','/tenants/a/api-key',None),('POST','/jobs/rollup',None),('GET','/jobs/missing',None)])
def test_missing_auth_rejected(context,method,path,body):
    client,_,_=context
    assert client.request(method,path,json=body).status_code == 401


def test_admin_boundaries_and_key_rotation(context):
    client,admin,keys=context
    tenant_headers=auth(keys['a'])
    for path in ('/tenants/a/api-key','/jobs/rollup'):
        assert client.post(path,headers=tenant_headers).status_code == 403
    assert client.get('/jobs/missing',headers=tenant_headers).status_code == 403
    assert client.post('/tenants',headers=tenant_headers,json={'id':'c','name':'C','plan_id':'pro'}).status_code == 403
    assert client.get('/usage/a',headers=admin).status_code == 401
    assert client.get('/usage/a',headers=auth('wrong')).status_code == 401
    r=client.post('/tenants/a/api-key',headers=admin)
    assert r.headers['Cache-Control'] == 'no-store'
    new_key=r.json()['api_key']
    assert client.get('/usage/a',headers=tenant_headers).status_code == 401
    assert client.get('/usage/a',headers=auth(new_key)).status_code == 200
    with main.db() as conn:
        stored=conn.execute("SELECT key_hash FROM tenant_api_keys WHERE tenant_id='a'").fetchone()[0]
        assert stored == main.key_hash(new_key) and stored != new_key
    job=client.post('/jobs/rollup',headers=admin)
    assert job.status_code == 202
    assert client.get('/jobs/'+job.json()['job_id'],headers=admin).status_code == 200


def test_missing_admin_config_and_webhook_separate_auth(context,monkeypatch):
    client,admin,_=context
    monkeypatch.delenv('ADMIN_API_KEY')
    assert client.post('/tenants/a/api-key',headers=admin).status_code == 503
    monkeypatch.setenv('STRIPE_WEBHOOK_SECRET','local-test-secret')
    assert client.post('/webhooks/stripe',content='{}',headers={'Stripe-Signature':'invalid'}).status_code == 400


def test_migration_repeat_preserves_tenant_and_key(context):
    client,_,keys=context
    main.init_db()
    assert client.get('/usage/a',headers=auth(keys['a'])).status_code == 200
