"""Release an unknown dial reservation only after an operator verifies no call was placed.

Never run this speculatively to get around a concurrency limit. This command does
not originate, retry, or hang up calls. Provider calls with known IDs must be
resolved through signed callbacks/CDR reconciliation instead.
"""
import argparse
import asyncio
from uuid import UUID
from app.auth import calls
from app.config import settings
from app.db.models.identity import AuditEvent


async def resolve(attempt_id, reason):
    if not 10 <= len(reason.strip()) <= 500:
        raise ValueError('Provide a 10–500 character explanation of the provider-log verification.')
    async with calls.database.transaction() as s:
        row = await calls.locked_attempt(s, attempt_id)
        if row.state != 'unknown' or row.provider_call_id or row.provider_request_id or row.used:
            raise ValueError('Only an unknown attempt without provider identifiers or a claimed stream can be released.')
        await calls.transition(s, row, 'failed', 'operator_verified_not_placed')
        row.cleanup_needed = False
        row.reconcile_after = None
        s.add(AuditEvent(organization_id=row.organization_id, actor_id=None,
            action='call.unknown_resolved', target=str(row.id), details={'verification':reason.strip()}))


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--attempt-id', required=True, type=UUID)
    parser.add_argument('--confirm-not-placed', action='store_true', required=True)
    parser.add_argument('--reason', required=True)
    args = parser.parse_args()
    try:
        settings.validate_plivo()
        await resolve(args.attempt_id, args.reason)
        print('Reservation released; the original idempotency key remains consumed. No call was placed.')
    finally:
        await calls.database.dispose()


if __name__ == '__main__':
    asyncio.run(main())
