"""Latency measurements without SQL, request bodies, credentials or query strings."""
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class Timings:
    request_id: str = field(default_factory=lambda: uuid4().hex)
    durations: dict = field(default_factory=dict)
    queries: int = 0
    active: bool = True


current = ContextVar('request_timings', default=None)


@contextmanager
def measure(stage):
    started = perf_counter()
    try:
        yield
    finally:
        timing = current.get()
        if timing and timing.active:
            timing.durations[stage] = timing.durations.get(stage, 0) + (perf_counter() - started) * 1000


def install_sql_timing(engine):
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, 'before_cursor_execute')
    def before(conn, cursor, statement, parameters, context, executemany):
        context._latency_started = perf_counter()

    @event.listens_for(engine.sync_engine, 'after_cursor_execute')
    def after(conn, cursor, statement, parameters, context, executemany):
        timing = current.get()
        if timing and timing.active:
            timing.queries += 1
            timing.durations['sql'] = timing.durations.get('sql', 0) + (perf_counter() - context._latency_started) * 1000


class RequestTiming:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        timing = Timings()
        token = current.set(timing)
        started = perf_counter()
        status = 500

        async def timed_send(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
                durations = {'app': (perf_counter() - started) * 1000, **timing.durations}
                message = {**message, 'headers': [*message.get('headers', []),
                    (b'x-request-id', timing.request_id.encode()),
                    (b'server-timing', ', '.join(f'{key};dur={value:.1f}' for key, value in durations.items()).encode())]}
            await send(message)

        try:
            await self.app(scope, receive, timed_send)
        finally:
            timing.active = False  # Background tasks inherit context but must not change a finished request.
            route = getattr(scope.get('route'), 'path', 'unmatched')
            logger.info('api_timing request=%s method=%s route=%s status=%s total_ms=%.1f sql_count=%s stages_ms=%s',
                        timing.request_id, scope['method'], route, status,
                        (perf_counter() - started) * 1000, timing.queries,
                        {key: round(value, 1) for key, value in timing.durations.items()})
            current.reset(token)


def call_mark(call, stage):
    """Log each startup milestone once; monotonic timestamps remain server-only."""
    marks = getattr(call, '_timing_marks', None)
    if marks is None:
        marks = call._timing_marks = {}
    if stage in marks:
        return
    now = perf_counter()
    marks[stage] = now
    logger.info('call_timing attempt=%s stage=%s elapsed_ms=%.1f answer_elapsed_ms=%s',
                call.id, stage, (now - next(iter(marks.values()))) * 1000,
                round((now - marks['answer']) * 1000, 1) if 'answer' in marks else None)
