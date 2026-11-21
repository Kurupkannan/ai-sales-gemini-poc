import os
from functools import lru_cache
import firebase_admin
from firebase_admin import auth, credentials
from app.config import settings

@lru_cache
def firebase_app():
    if not settings.FIREBASE_PROJECT_ID:
        raise RuntimeError('FIREBASE_PROJECT_ID is not configured')
    if os.getenv('FIREBASE_AUTH_EMULATOR_HOST') and settings.APP_ENV != 'development':
        raise RuntimeError('Firebase emulator is prohibited outside development')
    if settings.NETWORK_IPV4_ONLY:
        import urllib3.util.connection
        urllib3.util.connection.HAS_IPV6 = False  # requests/firebase_admin connect over IPv4 only
    credential = (
        credentials.Certificate(settings.GOOGLE_APPLICATION_CREDENTIALS)
        if settings.GOOGLE_APPLICATION_CREDENTIALS else None
    )
    return firebase_admin.initialize_app(credential, options={
        'projectId': settings.FIREBASE_PROJECT_ID,
        'httpTimeout': settings.AUTH_HTTP_TIMEOUT_SECONDS,
        **({'databaseURL': settings.FIREBASE_DATABASE_URL} if settings.FIREBASE_DATABASE_URL else {}),
    }, name='relay')

def verify_token(token):
    return auth.verify_id_token(token, app=firebase_app(), check_revoked=True)
