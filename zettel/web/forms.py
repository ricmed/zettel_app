"""The one async step a handler needs, as a dependency. No package imports.

Handlers are plain ``def`` so FastAPI runs them in the threadpool (#304). A
handler that reads a free-form body declares ``form = Depends(read_form)``:
FastAPI awaits the dependency on the event loop, then calls the handler in a
thread with the parsed body.
"""

from __future__ import annotations

from fastapi import Request
from starlette.datastructures import FormData


async def read_form(request: Request) -> FormData:
    return await request.form()
