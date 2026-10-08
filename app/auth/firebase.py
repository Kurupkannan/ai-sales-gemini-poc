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
    credential = (
        credentials.Certificate(settings.GOOGLE_APPLICATION_CREDENTIALS)
        if settings.GOOGLE_APPLICATION_CREDENTIALS else None
    )
    return firebase_admin.initialize_app(credential, options={'projectId': settings.FIREBASE_PROJECT_ID}, name='relay')

def verify_token(token):
    return auth.verify_id_token(token, app=firebase_app(), check_revoked=True)
