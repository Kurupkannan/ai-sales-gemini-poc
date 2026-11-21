"""Reproduce a live socket that silently stops returning database packets."""
import asyncio
import os
from time import perf_counter

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.exc import OperationalError

from app.config import Settings
from app.db.session import Database


@pytest.mark.skipif(not os.getenv('TEST_AUTH_DATABASE_URL'), reason='Requires disposable PostgreSQL')
@pytest.mark.parametrize('phase', ['query', 'pool_ping'])
async def test_blackholed_query_releases_pool_slot_and_next_request_recovers(phase):
    target = make_url(os.environ['TEST_AUTH_DATABASE_URL'])
    drop = False
    clients = set()
    tasks = set()
    connections = 0

    async def proxy(reader, writer):
        nonlocal connections
        connections += 1
        number = connections
        task = asyncio.current_task()
        tasks.add(task)
        upstream = None
        pumps = []
        clients.add(writer)
        try:
            source, upstream = await asyncio.open_connection(target.host, target.port or 5432)
            clients.add(upstream)
            async def pump(source, destination):
                while chunk := await source.read(65536):
                    if not (drop and number == 1):
                        destination.write(chunk)
                        await destination.drain()
            pumps = [asyncio.create_task(pump(reader, upstream)), asyncio.create_task(pump(source, writer))]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pump_task in pumps:
                pump_task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for stream in (writer, upstream):
                if stream:
                    stream.close()
                    clients.discard(stream)
            tasks.discard(task)

    server = await asyncio.start_server(proxy, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    url = target.set(host='127.0.0.1', port=port)
    db = Database(Settings(_env_file=None,
        DATABASE_URL=url.render_as_string(hide_password=False), DATABASE_PASSWORD='',
        DB_SSLMODE='disable', DB_IO_TIMEOUT_SECONDS=1, DB_POOL_SIZE=1, DB_MAX_OVERFLOW=0))
    try:
        async with db.engine.connect() as connection:
            assert await connection.scalar(text('SELECT 1')) == 1
            await connection.rollback()
            if phase == 'query':
                drop = True
                started = perf_counter()
                with pytest.raises(OperationalError) as error:
                    await connection.scalar(text('SELECT 1'))
                assert error.value.connection_invalidated
                assert perf_counter() - started < 3
        assert db.engine.pool.checkedout() == 0
        drop = True
        started = perf_counter()
        async with db.engine.connect() as connection:
            assert await connection.scalar(text('SELECT 1')) == 1
        assert perf_counter() - started < 3
        assert connections == 2
    finally:
        await db.dispose()
        server.close()
        await server.wait_closed()
        for stream in list(clients):
            stream.close()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*list(tasks), return_exceptions=True)
