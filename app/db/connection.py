"""Bound network waits, including pool pings, independently of server timeouts."""
import asyncio
import psycopg


class BoundedConnection(psycopg.AsyncConnection):
    io_timeout = 20

    async def wait(self, gen, interval=0.1, **kwargs):
        # A server-side statement_timeout cannot help when packets stop arriving.
        operation = asyncio.create_task(super().wait(gen, interval=interval, **kwargs))
        try:
            done, _ = await asyncio.wait({operation}, timeout=self.io_timeout)
            if done:
                return operation.result()
            # Close first: cancellation must not wait for a dead server to acknowledge it.
            await self.close()
            raise psycopg.OperationalError('Database network operation timed out; connection closed.')
        except asyncio.CancelledError:
            await self.close()
            raise
        finally:
            if not operation.done():
                operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
