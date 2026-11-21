"""Check the connection or backfill once. Never dials or edits application leads."""
import argparse
import asyncio
import json

import httpx

from app.config import settings
from app.db.session import database
from app.services.google_sheets import Exporter, SheetsError


async def main(check_only=False):
    try:
        async with httpx.AsyncClient(timeout=45, transport=httpx.AsyncHTTPTransport(
                local_address='0.0.0.0' if settings.NETWORK_IPV4_ONLY else None)) as client:
            exporter = Exporter(client)
            await exporter.request('ping')
            print('Google Sheets connection: OK (script version 3)')
            if not check_only:
                print('Export:', json.dumps(await exporter.sync_once(), sort_keys=True))
    except SheetsError as exc:
        print('Google Sheets check failed:', str(exc))
        return 1
    finally:
        await database.dispose()
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Authenticate without exporting rows')
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.check)))
