"""Domain: SSE events handler — handlers extracted from webapi.py."""

from __future__ import annotations

import asyncio
import json

from astrbot.api import logger
from astrbot.api.web import request, stream_response

from commands.handlers import Services
from .webapi_base import SSE_HEARTBEAT_SEC


async def api_queue_events(s: Services):
    """SSE: OpQueue event stream (queued / started / retried / done / failed).

    Robustness contract (I5):
    - Heartbeat every SSE_HEARTBEAT_SEC so clients and intermediaries can
      detect a dead stream instead of idling silently.
    - Disconnect detection: watch the ASGI ``http.disconnect`` message while
      waiting for events. A vanished client (tab closed, proxy reaped the
      connection, laptop sleep) must release its listener queue promptly —
      otherwise leaked listeners accumulate on every page load and the
      browser's per-host connection budget stays occupied, which is what
      freezes other tabs' data loads after a long idle session.
    - Cancellation-safe: the response task is cancelled by the server on
      disconnect; ``finally`` cancels the pending iterator task and closes
      the subscription generator, so the listener is always released.

    Context contract (坏链#25): ``astrbot.api.web.request`` is a ContextVar
    proxy that is only bound while the handler function itself runs, but
    StreamingResponse iterates the ``events()`` generator AFTER the handler
    returned. Touching ``request`` inside the generator raised RuntimeError
    and every connection died right after its first heartbeat (live
    2026-09-12: the catch-all masked it as a heartbeat and the browser's
    EventSource auto-reconnect hid the dead stream entirely). The ASGI
    ``receive`` callable is therefore captured here, inside the handler
    context, and passed into the generator as a plain value; if the proxy
    refuses, the stream degrades to heartbeat-only disconnect detection
    (server task cancellation still releases the listener on disconnect).
    """
    try:
        receive = getattr(request, "receive", None) if request is not None else None
    except Exception as e:
        logger.warning(f"[webapi] SSE: request proxy unavailable ({e}); "
                       "disconnect falls back to server cancellation")
        receive = None

    async def events():
        agen = s.queue.subscribe()
        # Drive the subscription iterator from a persistent task so the
        # heartbeat timeout does NOT cancel it: cancelling ``__anext__()``
        # would inject CancelledError into the subscription generator and
        # close it (listener discarded — stream goes deaf after one
        # heartbeat window; reproduced in-container 2026-09-12).
        get_task = asyncio.ensure_future(agen.__anext__())
        recv_task = asyncio.ensure_future(receive()) if receive is not None else None
        try:
            while True:
                wait_set = {get_task}
                if recv_task is not None:
                    wait_set.add(recv_task)
                done, _pending = await asyncio.wait(
                    wait_set,
                    timeout=SSE_HEARTBEAT_SEC,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                emitted = False
                if recv_task is not None and recv_task in done:
                    msg = None
                    try:
                        msg = recv_task.result()
                    except Exception:
                        msg = None
                    if msg and msg.get("type") == "http.disconnect":
                        logger.debug("[webapi] SSE client disconnected; releasing listener")
                        return
                    if msg and msg.get("type") == "http.request" and msg.get("more_body"):
                        # Body bytes are irrelevant for this endpoint; keep
                        # watching for the eventual disconnect message.
                        recv_task = asyncio.ensure_future(receive())
                    else:
                        recv_task = None
                if get_task in done:
                    try:
                        ev = get_task.result()
                    except StopAsyncIteration:
                        return
                    except asyncio.CancelledError:
                        raise
                    get_task = asyncio.ensure_future(agen.__anext__())
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                    emitted = True
                if not emitted:
                    yield 'data: {"type":"heartbeat"}\n\n'
        finally:
            get_task.cancel()
            if recv_task is not None:
                recv_task.cancel()
            # Let the cancelled iterator task unwind before closing the
            # generator, otherwise aclose() hits "already running".
            try:
                await get_task
            except (asyncio.CancelledError, StopAsyncIteration):
                pass
            except Exception as e:
                logger.warning(f"[webapi] SSE: iterator task ended with {e!r}")
            await agen.aclose()

    return stream_response(events())
