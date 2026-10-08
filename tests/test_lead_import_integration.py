"""API tests against the explicitly configured disposable PostgreSQL database."""
import asyncio
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4
from fastapi import Request
from app.auth.dependencies import organization_access
from app.db.models.identity import Organization
ORG = ""

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from app.config import Settings
from app.db.session import Database
from app.db.models import Lead, ImportBatch
from app.api.leads import session_dependency
from app.main import app

pytestmark = pytest.mark.skipif(not (os.getenv('TEST_DATABASE_URL') and os.getenv('TEST_MIGRATION_DATABASE_URL')), reason='Needs disposable local PostgreSQL')


@pytest.fixture(scope='module', autouse=True)
def migrated():
    if not os.getenv('TEST_MIGRATION_DATABASE_URL'): return
    env = {**os.environ, 'MIGRATION_DATABASE_URL': os.environ['TEST_MIGRATION_DATABASE_URL'], 'DATABASE_PASSWORD': '', 'DB_SSLMODE': 'disable', 'DB_SSLROOTCERT': ''}
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], cwd=Path(__file__).resolve().parents[1], env=env, check=True, capture_output=True)


@pytest.fixture
async def client():
    db = Database(Settings(_env_file=None, DATABASE_URL=os.environ['TEST_DATABASE_URL'], DATABASE_PASSWORD='', DB_SSLMODE='disable'))
    global ORG
    ORG = str(uuid4())
    async with db.transaction() as value:
        value.add(Organization(id=__import__('uuid').UUID(ORG), name='Import test', slug=ORG, status='active'))
    async def authorized(request: Request):
        request.state.organization_id = __import__('uuid').UUID(ORG)
        request.state.permissions = {'leads.read','leads.import'}
    app.dependency_overrides[organization_access] = authorized
    async def session():
        db.engine
        async with db._sessions() as value:
            value.info["organization_id"] = __import__("uuid").UUID(ORG)
            yield value
    app.dependency_overrides[session_dependency] = session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            yield client
    finally:
        app.dependency_overrides.pop(session_dependency, None)
        app.dependency_overrides.pop(organization_access, None)
        await db.dispose()


def form():
    return {'mapping': json.dumps({'name': 'Name', 'phone': 'Phone', 'email': 'Email', 'source': 'Source'}), 'country': 'IN'}


async def test_preview_commit_retry_search_and_detail(client):
    marker = 'Test-' + uuid4().hex[:10]
    # Valid US number keeps +1 regardless of the default country.
    data = f'Name,Phone,Email,Source,City\n{marker},+12025550123,lead@example.com,Website,Kochi\nDuplicate,+12025550123,,Website,Kochi\nBad,abc,,Website,Kochi\n'.encode()
    files = {'file': ('leads.csv', data)}
    preview = await client.post(f'/api/orgs/{ORG}/imports/preview', files=files, data=form())
    assert preview.status_code == 200, preview.text
    summary = preview.json()
    assert summary['imported_rows'] == 1 and summary['duplicate_rows'] == 1 and summary['invalid_rows'] == 1
    before = await client.get(f'/api/orgs/{ORG}/leads', params={'q': marker})
    assert before.json()['total'] == 0
    result = await client.post(f'/api/orgs/{ORG}/imports', files=files, data=form())
    assert result.status_code == 201, result.text
    assert result.json()['imported_rows'] == 1
    again = await client.post(f'/api/orgs/{ORG}/imports', files=files, data=form())
    assert again.json()['id'] == result.json()['id'] and again.json()['already_imported']
    listed = (await client.get(f'/api/orgs/{ORG}/leads', params={'q': marker, 'status': 'new', 'page_size': 1})).json()
    assert listed['total'] == 1 and len(listed['items']) == 1
    detail = (await client.get(f'/api/orgs/{ORG}/leads/' + listed['items'][0]['id'])).json()
    assert detail['extra_fields'] == {'City': 'Kochi'}
    assert detail['contacts'][0]['suppressed'] is True
    assert detail['import']['filename'] == 'leads.csv'
    filtered = (await client.get(f'/api/orgs/{ORG}/leads', params={'q': marker, 'status': 'qualified'})).json()
    assert filtered['total'] == 0
    assert (await client.get(f'/api/orgs/{ORG}/leads/' + str(uuid4()))).status_code == 404
    assert (await client.get(f'/api/orgs/{ORG}/leads', params={'page': 0})).status_code == 422
    assert (await client.get(f'/api/orgs/{ORG}/leads', params={'q': marker, 'page': 2, 'page_size': 1})).json()['items'] == []


async def test_concurrent_imports_skip_existing_without_overwrite(client):
    marker = uuid4().hex[:8]
    one = f'Name,Phone,Email,Source\nFirst-{marker},+12025550124,,Sheet A\n'.encode()
    two = f'Name,Phone,Email,Source\nSecond-{marker},+12025550124,,Sheet B\n'.encode()
    responses = await asyncio.gather(*[client.post(f'/api/orgs/{ORG}/imports', files={'file': ('leads.csv', content)}, data=form()) for content in (one, two)])
    assert all(r.status_code == 201 for r in responses)
    assert sorted(r.json()['imported_rows'] for r in responses) == [0, 1]
    assert sorted(r.json()['duplicate_rows'] for r in responses) == [0, 1]
    result = (await client.get(f'/api/orgs/{ORG}/leads', params={'q': '+12025550124'})).json()
    assert result['total'] == 1


async def test_reject_mapping_before_import(client):
    result = await client.post(f'/api/orgs/{ORG}/imports', files={'file': ('a.csv', b'Name,Phone\nNo,9876543210')}, data={'mapping': '{}', 'country': 'IN'})
    assert result.status_code == 422
    assert (await client.get(f'/api/orgs/{ORG}/imports')).status_code == 200
