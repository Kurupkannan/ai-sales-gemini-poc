"""Real PostgreSQL tenant/RBAC tests; only an explicitly disposable auth DB is used.
Set TEST_AUTH_DATABASE_URL to the restricted runtime role, and
TEST_AUTH_ADMIN_URL to its disposable administrative connection. Migrate first.
"""
import asyncio
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4, UUID
from unittest.mock import Mock
import pytest
from cryptography.fernet import Fernet
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from app.config import Settings
from app.db.session import Database
from app.db.models import Lead, ImportBatch, LeadContact
from app.db.models.identity import User, Organization, Membership, Invitation, EmailOutbox
from app.auth import dependencies, worker, calls
from app.api import identity, leads, whatsapp
from app.main import app

pytestmark = pytest.mark.skipif(not os.getenv('TEST_AUTH_DATABASE_URL'), reason='Requires disposable auth database')

def config(url): return Settings(_env_file=None, DATABASE_URL=url, DATABASE_PASSWORD='', DB_SSLMODE='disable')

@pytest.fixture
async def context(monkeypatch):
    db = Database(config(os.environ['TEST_AUTH_DATABASE_URL']))
    admin = Database(config(os.environ['TEST_AUTH_ADMIN_URL']))
    for module in [dependencies, identity, worker, calls]: monkeypatch.setattr(module,'database',db)
    import app.db.session as sessions
    monkeypatch.setattr(sessions,'database',db)
    key=Fernet.generate_key().decode()
    monkeypatch.setattr(identity.settings,'AUTH_OUTBOX_KEY',__import__('pydantic').SecretStr(key))
    monkeypatch.setattr(identity.settings,'SMTP_HOST','smtp.invalid')
    monkeypatch.setattr(identity.settings,'SMTP_FROM','relay@example.com')
    monkeypatch.setattr(identity.settings,'AUTH_REQUIRE_ADMIN_MFA',False)
    uid='platform-'+str(uuid4())
    async with admin.transaction() as s:
        user=User(firebase_uid=uid,email=uid+'@example.com',platform_admin=True)
        s.add(user); await s.flush()
    def verify(token):
        return {'uid':token, 'email':token+'@example.com', 'email_verified':True,'auth_time':time.time(),
                'platform_role':'admin', 'permissions':['organizations.manage']}  # Deliberately overprivileged/stale claims.
    monkeypatch.setattr(dependencies,'verify_token',verify)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test',headers={'Authorization':'Bearer '+uid}) as client:
        yield client,db,admin,key,uid
    await db.dispose(); await admin.dispose()

async def create_org(client):
    slug='org-'+uuid4().hex[:12]
    r=await client.post('/api/platform/organizations',json={'name':slug,'slug':slug})
    assert r.status_code==201,r.text
    return r.json()['id']

async def invite_token(client,admin,key,org,uid,role='org_admin'):
    r=await client.post(f'/api/orgs/{org}/invitations',json={'email':uid+'@example.com','role':role})
    assert r.status_code==201,r.text
    async with admin.transaction() as s:
        mail=await s.scalar(select(EmailOutbox).where(EmailOutbox.invitation_id==UUID(r.json()['id'])))
        token=Fernet(key.encode()).decrypt(mail.payload.encode()).decode()
    return token,r.json()['id']

async def accept(client,uid,token):
    return await client.post('/api/invitations/accept',json={'token':token},headers={'Authorization':'Bearer '+uid})

async def test_org_invites_roles_last_admin_and_database_authority(context):
    client,db,admin,key,platform=context
    org=await create_org(client); uid='admin-'+uuid4().hex
    token,invite=await invite_token(client,admin,key,org,uid)
    assert (await accept(client,'wrong-'+uuid4().hex,token)).status_code==403
    assert (await accept(client,uid,token)).status_code==200
    assert (await accept(client,uid,token)).status_code==200  # Idempotent replay by recipient.
    headers={'Authorization':'Bearer '+uid}
    assert (await client.get('/api/platform/organizations',headers=headers)).status_code==403  # Claims cannot escalate.
    rows=(await client.get(f'/api/orgs/{org}/members',headers=headers)).json()
    m=rows[0]
    denied=await client.patch(f'/api/orgs/{org}/members/{m["id"]}',headers=headers,json={'role':'viewer','status':'active'})
    assert denied.status_code==409
    member='member-'+uuid4().hex
    token,_=await invite_token(client,admin,key,org,member,'member')
    assert (await accept(client,member,token)).status_code==200
    mh={'Authorization':'Bearer '+member}
    assert (await client.get(f'/api/orgs/{org}/members',headers=mh)).status_code==403
    assert (await client.get(f'/api/orgs/{org}/leads',headers=mh)).status_code==200
    rows=(await client.get(f'/api/orgs/{org}/members')).json(); m=next(m for m in rows if m['email']==member+'@example.com')
    assert (await client.patch(f'/api/orgs/{org}/members/{m["id"]}',json={'role':'viewer','status':'active'})).status_code==200
    assert (await client.post(f'/api/orgs/{org}/imports/inspect',headers=mh,files={'file':('a.csv',b'phone\n9876543210')})).status_code==403
    assert (await client.patch(f'/api/platform/organizations/{org}',json={'status':'suspended'})).status_code==200
    assert (await client.get(f'/api/orgs/{org}/leads',headers=headers)).status_code==403

async def test_cross_org_imports_queries_and_rls(context):
    client,db,admin,key,platform=context
    orgs=[await create_org(client),await create_org(client)]
    users=['a-'+uuid4().hex,'b-'+uuid4().hex]
    for org,uid in zip(orgs,users):
        token,_=await invite_token(client,admin,key,org,uid)
        assert (await accept(client,uid,token)).status_code==200
    content=b'name,phone\nExample,9876543210\n'
    async def upload(org,uid):
        return await client.post(f'/api/orgs/{org}/imports',headers={'Authorization':'Bearer '+uid},
            files={'file':('leads.csv',content)},data={'mapping':json.dumps({'name':'name','phone':'phone'}),'country':'IN'})
    results=[await upload(o,u) for o,u in zip(orgs,users)]
    for r in results: assert r.status_code==201 and r.json()['imported_rows']==1,r.text
    assert (await upload(orgs[0],users[0])).json()['already_imported']
    ah={'Authorization':'Bearer '+users[0]}
    a=(await client.get(f'/api/orgs/{orgs[0]}/leads',headers=ah)).json()
    assert a['total']==a['stats']['total']==1
    lead_id=a['items'][0]['id']
    assert (await client.get(f'/api/orgs/{orgs[0]}/leads',headers={'Authorization':'Bearer '+users[1]})).status_code==404
    assert (await client.get(f'/api/orgs/{orgs[1]}/leads/{lead_id}',headers={'Authorization':'Bearer '+users[1]})).status_code==404
    async with db.transaction() as s:
        assert list((await s.scalars(select(Lead))).all())==[]
    async with db.transaction() as s:
        s.info['organization_id']=UUID(orgs[0])
        assert len((await s.scalars(select(Lead))).all())==1
        with pytest.raises(DBAPIError):
            async with s.begin_nested():
                s.add(Lead(organization_id=UUID(orgs[1]),name='unauthorized'))
                await s.flush()
    async with db.transaction() as s:
        assert list((await s.scalars(select(Lead))).all())==[]  # Pool context reset.
    async with admin.transaction() as s:
        with pytest.raises(IntegrityError):
            async with s.begin_nested():
                s.add(LeadContact(organization_id=UUID(orgs[1]),lead_id=UUID(lead_id),phone_number='+919876543211',channel='voice'))
                await s.flush()

async def test_invitation_resend_expiry_and_concurrent_last_admin(context):
    client,db,admin,key,platform=context
    org=await create_org(client); a='a-'+uuid4().hex; b='b-'+uuid4().hex
    token,ident=await invite_token(client,admin,key,org,a)
    assert (await client.post(f'/api/orgs/{org}/invitations/{ident}/resend')).status_code==200
    assert (await accept(client,a,token)).status_code==400
    token,_=await invite_token(client,admin,key,org,a); assert (await accept(client,a,token)).status_code==200
    token,_=await invite_token(client,admin,key,org,b); assert (await accept(client,b,token)).status_code==200
    members=(await client.get(f'/api/orgs/{org}/members')).json()
    results=await asyncio.gather(*[client.patch(f'/api/orgs/{org}/members/{m["id"]}',json={'role':'viewer','status':'active'}) for m in members])
    assert sorted(r.status_code for r in results)==[200,409]
    token,ident=await invite_token(client,admin,key,org,'expire-'+uuid4().hex,'member')
    async with admin.transaction() as s:
        invite=await s.get(Invitation,UUID(ident)); invite.expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)
        email=invite.email
    assert (await accept(client,email.split('@')[0],token)).status_code==400

async def test_worker_mail_and_claim_sync(context,monkeypatch):
    client,db,admin,key,platform=context
    org=await create_org(client); uid='new-'+uuid4().hex
    token,ident=await invite_token(client,admin,key,org,uid)
    # Mark unrelated rows complete so this test can run with a retained disposable DB.
    async with admin.transaction() as s:
        await s.execute(text("UPDATE app.users SET claims_synced_version=permissions_version WHERE firebase_uid <> :uid"),{'uid':platform})
        await s.execute(text("UPDATE app.email_outbox SET status='cancelled',payload=NULL WHERE invitation_id <> :id"),{'id':UUID(ident)})
    send=Mock(); claims=Mock()
    monkeypatch.setattr(worker,'send_mail',send)
    monkeypatch.setattr(worker,'firebase_app',lambda:None)
    monkeypatch.setattr(worker.auth,'set_custom_user_claims',claims)
    account_update=Mock()
    monkeypatch.setattr(worker.auth,'update_user',account_update)
    await worker.tick()
    account_update.assert_not_called()  # Claims sync cannot re-enable disabled Firebase identities.
    assert send.call_args.args[0]==uid+'@example.com'
    assert send.call_args.args[2]==token
    assert claims.call_args.args[1]['permissions']==['organizations.manage']
    async with admin.transaction() as s:
        mail=await s.scalar(select(EmailOutbox).where(EmailOutbox.invitation_id==UUID(ident)))
        assert mail.status=='sent' and mail.payload is None


async def test_bootstrap_cli_idempotent_claims_and_suspension(context, monkeypatch):
    from types import SimpleNamespace
    from scripts import bootstrap_admin as bootstrap
    client, db, admin, key, platform = context
    uid = 'bootstrap-' + uuid4().hex
    selected = SimpleNamespace(uid=uid, email=uid+'@example.com', display_name='Main admin',
        disabled=False, email_verified=False)
    monkeypatch.setattr(bootstrap, 'database', db)
    monkeypatch.setattr(bootstrap, 'firebase_app', lambda: SimpleNamespace(project_id='test-project'))
    monkeypatch.setattr(bootstrap.auth, 'get_user', Mock(return_value=selected))
    claims = Mock()
    monkeypatch.setattr(bootstrap.auth, 'set_custom_user_claims', claims)
    args = bootstrap.arguments(['--uid', uid])
    assert await bootstrap.run(args) == 0
    async with admin.transaction() as s:
        u = await s.scalar(select(User).where(User.firebase_uid == uid))
        version = u.permissions_version
        assert u.platform_admin and u.active and u.claims_synced_version == version
    assert claims.call_args.args[1]['permissions'] == ['organizations.manage']
    assert await bootstrap.run(args) == 0
    async with admin.transaction() as s:
        u = await s.scalar(select(User).where(User.firebase_uid == uid))
        assert u.permissions_version == version
        count = await s.scalar(text("SELECT count(*) FROM app.audit_events WHERE target=:target AND action='platform_admin.bootstrapped'"), {'target': str(u.id)})
        assert count == 1
    # A partial Firebase failure is recoverable without repeating the database grant.
    claims.side_effect = RuntimeError('unavailable')
    assert await bootstrap.run(args) == 2
    async with admin.transaction() as s:
        u = await s.scalar(select(User).where(User.firebase_uid == uid))
        assert u.platform_admin and u.claims_synced_version == 0
        u.active = False
    with pytest.raises(bootstrap.BootstrapError, match='suspended'):
        await bootstrap.run(args)
