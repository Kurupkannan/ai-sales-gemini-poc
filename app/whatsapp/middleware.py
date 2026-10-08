"""Bound request bodies and keep signup credentials out of HTTP diagnostics."""
import logging
from fastapi import HTTPException
from starlette.responses import JSONResponse


class HideWebhookQuery(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            if str(args[2]).startswith('/webhooks/whatsapp'):
                args[2] = str(args[2]).split('?', 1)[0]
                record.args = tuple(args)
        return True


logging.getLogger('uvicorn.access').addFilter(HideWebhookQuery())


class WhatsAppLimits:
    def __init__(self, app): self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or '/whatsapp' not in scope['path']:
            return await self.app(scope, receive, send)
        limit = 256 * 1024 if scope['path'].startswith('/webhooks') else 16 * 1024
        length = dict(scope['headers']).get(b'content-length', b'0')
        try: too_large = int(length) > limit
        except ValueError: too_large = True
        if too_large:
            return await JSONResponse({'detail': 'WhatsApp request is too large.'}, status_code=413)(scope, receive, send)
        received = 0
        async def limited():
            nonlocal received
            event = await receive()
            received += len(event.get('body', b''))
            if received > limit: raise HTTPException(413, 'WhatsApp request is too large.')
            return event
        async def no_cache(event):
            if event['type'] == 'http.response.start':
                event['headers'] = [(k, v) for k, v in event['headers'] if k.lower() != b'cache-control'] + [(b'cache-control', b'no-store')]
            await send(event)
        await self.app(scope, limited, no_cache)
