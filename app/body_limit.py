"""Refuse request bodies over a size limit as they arrive, before anything reads them.

FastAPI reads a request's whole body before it runs the endpoint's dependencies, login
included. Without this, anyone could make the server write an upload of any size to disk, or
read a JSON body of any size into memory, and only get a 401 afterwards.
"""

from fastapi import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DETAIL = "The request is too large"


class BodySizeLimit:
    """ASGI middleware that answers 413 to any request body over max_bytes."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Most clients say how big the body is, so a body that's too big is turned away
        # before any of it is read.
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > self.max_bytes:
            await JSONResponse({"detail": DETAIL}, status_code=413)(scope, receive, send)
            return

        # A chunked body doesn't say its size, so count the bytes as they arrive too.
        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # FastAPI passes an HTTPException raised while reading the body straight
                    # on, so this becomes a normal 413 response. Any other error would be a 400.
                    raise HTTPException(status_code=413, detail=DETAIL)
            return message

        await self.app(scope, counting_receive, send)
