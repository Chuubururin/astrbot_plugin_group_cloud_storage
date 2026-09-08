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
    - Disconnect detection: poll the ASGI ``http.disconnect`` message while
      waiting for events. A vanished client (tab closed, proxy reaped the
      connection, laptop sleep) must release its listener queue promptly —
      otherwise leaked listeners accumulate on every page load and the
      browser's per-host connection budget stays occupied, which is what
      freezes other tabs' data loads after a long idle session.
    - Cancellation-safe: the generator only awaits primitives that honor
      cancellation; the listener is always discarded in ``finally``.
    """

    async def _next_or_disconnected(agen, poll_sec: float):
        """Wait for the next event, racing an ``http.disconnect`` peek.

        Returns ("event", ev) or ("disconnected", None). The disconnect
        message is only consumed when present (non-blocking peek), so a
        healthy stream is never delayed by this poll.
        """
        receive = getattr(request, "receive", None) if request is not None else None
        if receive is None:
            ev = await asyncio.wait_for(agen.__anext__(), timeout=poll_sec)
            return "event", ev
        recv_task = asyncio.ensure_future(receive())
        try:
            ev = await asyncio.wait_for(agen.__anext__(), timeout=poll_sec)
            recv_task.cancel()
            return "event", ev
        except asyncio.TimeoutError:
            # Heartbeat window elapsed: drain the receive channel without
            # blocking to spot an already-arrived disconnect message.
            if recv_task.done():
                try:
                    msg = recv_task.result()
                except Exception:
                    msg = None
                if msg and msg.get("type") == "http.disconnect":
                    return "disconnected", None
            recv_task.cancel()
            return "heartbeat", None
        except asyncio.CancelledError:
            recv_task.cancel()
            raise

    async def events():
        agen = s.queue.subscribe()
        try:
            while True:
                kind, ev = await _next_or_disconnected(agen, SSE_HEARTBEAT_SEC)
                if kind == "disconnected":
                    logger.debug("[webapi] SSE client disconnected; releasing listener")
                    return
                if kind == "heartbeat":
                    yield 'data: {"type":"heartbeat"}\n\n'
                    continue
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except (StopAsyncIteration, asyncio.CancelledError):
            return
        except GeneratorExit:
            raise
        except Exception:
            # SSE stability: a single error must not kill the session (the
            # browser can reconnect).
            yield 'data: {"type":"heartbeat"}\n\n'
        finally:
            await agen.aclose()

    return stream_response(events())
