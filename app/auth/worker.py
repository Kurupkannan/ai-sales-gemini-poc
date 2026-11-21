"""Durable mail delivery and DB-authoritative Firebase claims synchronization.
Run `python -m app.auth.worker` as a separate process (or --once for a scheduled job).
"""
import asyncio
import argparse
import logging
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from cryptography.fernet import Fernet
from sqlalchemy import select
from firebase_admin import auth
from starlette.concurrency import run_in_threadpool
from app.auth.firebase import firebase_app
from app.auth.permissions import claims_for_user
from app.auth.store import store
from app.config import settings
from app.db.models.identity import EmailOutbox
from app.db.session import database

log = logging.getLogger(__name__)
def now(): return datetime.now(timezone.utc)
def send_mail(email, org, token):
    message = EmailMessage()
    message['From'] = settings.SMTP_FROM; message['To'] = email
    message['Subject'] = f'You are invited to {org}'
    message.set_content(f'You have been invited to {org}. Sign in with {email} to accept.\n\n'
        f'{settings.APP_FRONTEND_URL.rstrip("/")}/#invite={token}\n\nThis link expires in 72 hours. If unexpected, ignore this email.')
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as smtp:
        if settings.SMTP_STARTTLS: smtp.starttls()
        if settings.SMTP_USERNAME: smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD.get_secret_value())
        smtp.send_message(message)

async def sync_claims():
    # Users live in Realtime Database. One worker process; a crash simply retries.
    for user in await store.all_users():
        if user.permissions_version == user.claims_synced_version:
            continue
        if user.claims_retry_at and user.claims_retry_at > now():
            continue
        try:
            # Claims sync must never re-enable an account disabled in Firebase.
            if not user.active:
                await run_in_threadpool(auth.update_user, user.firebase_uid, disabled=True, app=firebase_app())
                await run_in_threadpool(auth.revoke_refresh_tokens, user.firebase_uid, app=firebase_app())
            await run_in_threadpool(auth.set_custom_user_claims, user.firebase_uid, claims_for_user(user), app=firebase_app())
            user.claims_synced_version = user.permissions_version
            user.claims_retry_at = None
        except Exception:
            user.claims_retry_at = now()+timedelta(minutes=1)
            log.warning('Claims synchronization failed; retry pending for user %s', user.id)
        await store.save_user(user)


async def tick():
    # A crash retries; duplicate email delivery is harmless because invitation consumption is single use.
    await sync_claims()
    async with database.transaction() as s:
        mail = await s.scalar(select(EmailOutbox).where(EmailOutbox.status == 'pending', EmailOutbox.next_attempt_at <= now())
            .order_by(EmailOutbox.next_attempt_at).with_for_update(skip_locked=True).limit(1))
        if not mail: return
        invite = await store.invitation_by_id(mail.invitation_id)
        org = await store.get_org(invite.organization_id) if invite else None
        if not invite or not org or invite.status != 'pending' or invite.expires_at <= now() or org.status not in ('pending','active'):
            mail.status = 'cancelled'; mail.payload = None; return
        try:
            token = Fernet(settings.AUTH_OUTBOX_KEY.get_secret_value().encode()).decrypt(mail.payload.encode()).decode()
            await run_in_threadpool(send_mail, invite.email, org.name, token)
            mail.status = 'sent'; mail.payload = None
        except Exception:
            mail.attempts += 1
            mail.next_attempt_at = now()+timedelta(seconds=min(3600, 30 * 2**min(mail.attempts, 7)))
            if mail.attempts >= 8: mail.status = 'failed'; mail.payload = None
            log.warning('Invitation delivery failed; attempt %s for outbox %s', mail.attempts, mail.id)

async def main(once):
    try:
        while True:
            try: await tick()
            except Exception: log.warning('Auth worker cycle failed; retrying')
            if once: break
            await asyncio.sleep(5)
    finally: await database.dispose()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--once', action='store_true')
    asyncio.run(main(parser.parse_args().once))
