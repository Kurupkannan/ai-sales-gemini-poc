"""Limit multipart upload bodies even when Content-Length is omitted."""
from fastapi import HTTPException
from starlette.responses import JSONResponse
from app.services.lead_sheets import MAX_BYTES


class UploadSizeLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or not scope["path"].startswith("/api/imports"):
            return await self.app(scope, receive, send)
        limit = MAX_BYTES + 64 * 1024  # Allow multipart headers and column mapping.
        received = 0

        async def limited_receive():
            nonlocal received
            event = await receive()
            received += len(event.get("body", b""))
            if received > limit:
                raise HTTPException(413, "Upload exceeds 5 MB. Choose a smaller file.")
            return event

        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            length = 0
        if length > limit:
            return await JSONResponse({"detail": "Upload exceeds 5 MB. Choose a smaller file."}, status_code=413)(scope, receive, send)
        await self.app(scope, limited_receive, send)
