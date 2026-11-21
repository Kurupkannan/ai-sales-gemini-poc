"""Small durable reconciliation loop, also available as a one-shot CLI.

Workers lease rows in PostgreSQL; no external queue or scheduler is required.
"""
import asyncio
import logging
from uuid import UUID
from datetime import timedelta
from sqlalchemy import select
from app.auth import calls
from app.config import settings
from app.db.models.identity import OutboundAuthorization
from app.plivo import client
from app.telephony.service import terminate
from app.telephony import runtime

logger = logging.getLogger(__name__)


async def tick():
    async with calls.database.transaction() as s:
        rows = list((await s.scalars(select(OutboundAuthorization).where(
            OutboundAuthorization.provider == 'plivo',
            OutboundAuthorization.provider_account_id == settings.PLIVO_AUTH_ID,
            OutboundAuthorization.organization_id == UUID(settings.PLIVO_ORGANIZATION_ID),
            OutboundAuthorization.reconcile_after <= calls.now()
        ).order_by(OutboundAuthorization.reconcile_after).limit(20).with_for_update(skip_locked=True))).all())
        for row in rows:
            row.reconcile_after = calls.now() + timedelta(seconds=60)
    for row in rows:
        if runtime.get(row.id):
            continue  # Final status belongs to the in-memory call until persisted.
        try:
            if row.cleanup_needed:
                await terminate(row.id, row.end_reason or 'cleanup_retry')
                continue
            if row.provider_call_id:
                try:
                    record = await client.get_call(row.provider_call_id)
                except client.ProviderError:
                    record = None  # A CDR outage must not prevent timeout cleanup.
                if (record and record.get('call_uuid') == row.provider_call_id
                        and record.get('end_time') not in (None, '', 'NA')):
                    await calls.callback(row.id, 'hangup', {
                        'CallUUID': row.provider_call_id, 'Direction': 'outbound',
                        'From': row.from_number, 'To': row.to_number,
                        'AnswerTime': record.get('answer_time') if record.get('answer_time') not in ('NA', None) else '',
                        'HangupCauseName': record.get('hangup_cause_name', 'reconciled'),
                        'BillDuration': str(record.get('bill_duration', '')),
                    })
                    continue
            age = (calls.now() - row.created_at).total_seconds()
            if row.state in calls.TERMINAL:
                # Fetch a final CDR after REST cleanup, but do not poll forever.
                if age > settings.CALL_MAX_DURATION_SECONDS + 300:
                    await calls.cleanup_complete(row.id, reconcile=False)
                continue
            if row.state == 'active':
                stale = (row.used and (not row.heartbeat_at or (calls.now()-row.heartbeat_at).total_seconds() > 45))
                no_media = not row.used and row.answered_at and (calls.now()-row.answered_at).total_seconds() > settings.MEDIA_START_TIMEOUT_SECONDS + 20
                if stale or no_media or age > settings.CALL_MAX_DURATION_SECONDS + settings.CALL_ANSWER_TIMEOUT_SECONDS + 30:
                    await terminate(row.id, 'session_timeout')
            elif row.expires_at <= calls.now():
                if row.provider_call_id or row.provider_request_id:
                    await terminate(row.id, 'answer_timeout')
                else:
                    # No Plivo ID was saved (e.g. the server stopped mid-dial). Live calls exist only in
                    # the process that placed them, so if Plivo did connect one, its answer webhook was
                    # refused and it hung up. Release the caller number instead of holding it forever.
                    await calls.release_unconfirmed(row.id)
                    logger.warning('Released unconfirmed dial attempt=%s; number freed, not redialled', row.id)
            if row.token_ciphertext and row.expires_at <= calls.now():
                async with calls.database.transaction() as s:
                    current = await calls.locked_attempt(s, row.id)
                    current.token_ciphertext = None
        except Exception:
            # Provider responses/DB exception parameters may contain sensitive data.
            logger.error('Reconciliation failed attempt=%s; will retry', row.id)


async def run():
    while True:
        try:
            await runtime.reconcile()
            # Live attempts are skipped inside tick(), so it is safe alongside active calls.
            await tick()
        except Exception:
            logger.error('Telephony reconciliation unavailable; check database and configuration')
        await asyncio.sleep(15)


if __name__ == '__main__':
    async def main():
        settings.validate_plivo()
        try:
            await tick()
        finally:
            await calls.database.dispose()
    asyncio.run(main())
