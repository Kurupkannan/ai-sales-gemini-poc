"""Shared isolated Plivo settings. No test places a real phone call."""
import pytest
from pydantic import SecretStr
from cryptography.fernet import Fernet
from app.config import settings


@pytest.fixture
def plivo_config(monkeypatch):
    values = {
        'OUTBOUND_CALLS_ENABLED': True,
        'PUBLIC_BASE_URL': 'https://voice.example.com',
        'PLIVO_AUTH_ID': 'MA123456789', 'PLIVO_AUTH_TOKEN': SecretStr('unit-test-token'),
        'PLIVO_FROM_NUMBER': '+919876543211',
        'PLIVO_ORGANIZATION_ID': '10000000-0000-4000-8000-000000000001',
        'MEDIA_GRANT_ENCRYPTION_KEY': SecretStr(Fernet.generate_key().decode()),
        'DATABASE_URL': SecretStr('postgresql://test@localhost/test'),
        'GEMINI_API_KEY': 'test-only',
    }
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value)
    return settings


# PostgreSQL-only suites written against the former SQL identity tables. Organizations,
# users, memberships and invitations now live in Firebase Realtime Database (app.auth.store).
collect_ignore = [
    'test_auth_integration.py', 'test_lead_import_integration.py',
    'test_plivo_database.py', 'test_whatsapp_integration.py', 'test_database_integration.py',
]


@pytest.fixture
def memory_store():
    """Identity store backed by an in-process tree instead of Realtime Database."""
    from app.auth.store import MemoryBackend, store
    previous = store._backend
    store.use(MemoryBackend())
    yield store
    store.use(previous)
