"""Domain: SSE events handler — handlers extracted from webapi.py."""

from __future__ import annotations

import asyncio
import json

from astrbot.api import logger
from astrbot.api.web import request, stream_response

from commands.handlers import Services
from .webapi_base import SSE_HEARTBEAT_SEC


def _asgi_receive():
    """The ASGI ``receive`` callable of the live request, or None.

    ``astrbot.api.web.request`` is a PluginRequestProxy: it exposes no
    ``receive`` of its own and forwards unknown attributes to the host's
    PluginRequest, whose ``_request`` is the Starlette request that owns the
    ASGI callable (``Request.receive``). Probing only ``request.receive``
    therefore always returned None and the documented disconnect detection
    never ran; the probe has to walk that one level down. Every step is
    optional - an unreachable callable degrades to heartbeat-only detection
    instead of breaking the stream.
    """
    try:
        for owner in (request, getattr(request, "_request", None)):
            if owner is None:
                continue
            receive = getattr(owner, "receive", None)
            if callable(receive):
                return receive
    except Exception as e:
        logger.warning(f"[webapi] SSE: ASGI receive unreachable ({e}); "
                       "disconnect falls back to server cancellation")
    return None


async def api_queue_events(s: Services):
    """SSE: OpQueue event stream (queued / started / retried / done / failed).

    Robustness contract (I5):
    - Heartbeat every SSE_HEARTBEAT_SEC so clients and intermediaries can
      detect a dead stream instead of idling silently.
    - Disconnect detection: watch the ASGI ``http.disconnect`` message while
      waiting for events (``_asgi_receive`` locates the callable through the
      host request proxy). A vanished client (tab closed, proxy reaped the
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
    ``receive`` callable is therefore resolved here, inside the handler
    context, and passed into the generator as a plain value. When the probe
    finds no callable the stream keeps the heartbeat and relies on Starlette's
    own ``http.disconnect`` listener, which cancels the response task.
    """
    receive = _asgi_receive()

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
                    if msg and msg.get("type") == "http.request":
                        # Body bytes are irrelevant for this endpoint; keep
                        # watching for the eventual disconnect message. EVERY
                        # http.request must re-arm, including the final one
                        # (more_body=False): uvicorn's h11 impl answers the
                        # first receive() of a body-less GET with exactly
                        # {"type": "http.request", "body": b"", "more_body":
                        # False}, so gating the re-arm on more_body treated it
                        # as terminal, stopped calling receive() and made the
                        # disconnect detection below dead code on the real
                        # path (only a first message that already was
                        # http.disconnect still worked).
                        recv_task = asyncio.ensure_future(receive())
                    else:
                        # Neither a request message nor a disconnect: nothing
                        # else is expected here, stop watching instead of
                        # spinning on a receive() that keeps returning it.
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
