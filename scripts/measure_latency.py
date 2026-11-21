"""Read-only DB/HTTP probes; optional Gemini greeting, never places a phone call.

Run from be: .venv/bin/python -m scripts.measure_latency [--gemini]
Only timings and aggregate query-plan metrics are printed, never credentials/data.
"""
import argparse
import asyncio
import json
from time import perf_counter

import httpx
from sqlalchemy import text

from app.config import settings
from app.db.session import database


def report(**values):
    print(json.dumps(values), flush=True)


async def main(args):
    async with httpx.AsyncClient(timeout=15) as client:
        for _ in range(args.samples):
            started = perf_counter()
            response = await client.get(args.base_url.rstrip('/') + '/health')
            report(probe='health', ms=round((perf_counter() - started) * 1000, 1),
                   status=response.status_code, server_timing=response.headers.get('server-timing'))
    try:
        for sample in range(args.samples):
            started = perf_counter()
            async with database.engine.connect() as connection:
                acquire = (perf_counter() - started) * 1000
                started = perf_counter()
                await connection.execute(text('SELECT 1'))
                first = (perf_counter() - started) * 1000
                started = perf_counter()
                await connection.execute(text('SELECT 1'))
                warm = (perf_counter() - started) * 1000
                started = perf_counter()
                await connection.rollback()
                rollback = (perf_counter() - started) * 1000
            report(probe='database', sample=sample, acquire_ms=round(acquire, 1),
                   first_sql_in_transaction_ms=round(first, 1), warm_sql_ms=round(warm, 1),
                   rollback_ms=round(rollback, 1))
        if settings.PLIVO_ORGANIZATION_ID:
            # Identical read-only predicates, comparing sequential vs consolidated round trips.
            params = {'org': settings.PLIVO_ORGANIZATION_ID,
                      'interval': 1 / settings.TELEPHONY_DIALS_PER_SECOND}
            predicates = ["created_at > now() - interval '1 hour'",
                "provider = 'plivo' AND (state NOT IN ('completed', 'failed') OR cleanup_needed)",
                "created_at > now() - make_interval(secs => :interval)"]
            source = ' FROM app.outbound_authorizations WHERE organization_id = CAST(:org AS uuid)'
            async with database.engine.connect() as connection:
                await connection.execute(text('SET TRANSACTION READ ONLY'))
                for sample in range(args.samples):
                    started = perf_counter()
                    old = [await connection.scalar(text('SELECT count(*)' + source + ' AND (' + predicate + ')'), params)
                           for predicate in predicates]
                    old_ms = (perf_counter() - started) * 1000
                    started = perf_counter()
                    new = (await connection.execute(text('SELECT ' + ', '.join(
                        'count(*) FILTER (WHERE ' + predicate + ')' for predicate in predicates) + source), params)).one()
                    report(probe='dial_limit_queries', sample=sample, before_ms=round(old_ms, 1),
                           after_ms=round((perf_counter() - started) * 1000, 1), same_result=list(new) == old)
                plan = await connection.scalar(text('EXPLAIN (ANALYZE, FORMAT JSON) SELECT ' + ', '.join(
                    'count(*) FILTER (WHERE ' + predicate + ')' for predicate in predicates) + source), params)
                report(probe='dial_limit_plan', execution_ms=plan[0]['Execution Time'], planning_ms=plan[0]['Planning Time'])
                await connection.rollback()
    finally:
        await database.dispose()
    if args.gemini:
        from app.gemini.live_session import GeminiLiveManager
        manager = GeminiLiveManager(settings.GEMINI_API_KEY, settings.GEMINI_LIVE_MODEL,
                                    settings.SYSTEM_INSTRUCTION, settings.VOICE_NAME)
        started = perf_counter()
        try:
            async with asyncio.timeout(35):
                async with manager.connect() as session:
                    connected = perf_counter()
                    await manager.send_greeting(session, settings.INITIAL_GREETING)
                    sent = perf_counter()
                    while True:
                        async for message in session.receive():
                            turn = getattr(getattr(message, 'server_content', None), 'model_turn', None)
                            if any(getattr(getattr(part, 'inline_data', None), 'data', None)
                                   for part in (getattr(turn, 'parts', None) or [])):
                                report(probe='gemini', connect_ms=round((connected-started)*1000, 1),
                                       greeting_ms=round((perf_counter()-sent)*1000, 1),
                                       total_ms=round((perf_counter()-started)*1000, 1))
                                return
        finally:
            if manager._client:
                await manager._client.aio.aclose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--samples', type=int, choices=range(1, 11), default=3)
    parser.add_argument('--gemini', action='store_true')
    try:
        asyncio.run(main(parser.parse_args()))
    except Exception as exc:
        report(error_type=type(exc).__name__)
        raise SystemExit(1) from None
