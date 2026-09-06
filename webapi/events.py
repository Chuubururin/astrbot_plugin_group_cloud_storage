"""Domain: SSE events handler — handlers extracted from webapi.py."""

from __future__ import annotations

import asyncio
import json

from astrbot.api.web import stream_response

from commands.handlers import Services
from .webapi_base import SSE_HEARTBEAT_SEC


async def api_queue_events(s: Services):
    """SSE: OpQueue event stream (queued / started / retried / done / failed)."""

    async def events():
        agen = s.queue.subscribe()
        try:
            while True:
                try:
                    # Heartbeat keepalive: emit a heartbeat periodically during
                    # idle periods so the frontend can detect and recover from
                    # dropped connections.
                    ev = await asyncio.wait_for(
                        agen.__anext__(), timeout=SSE_HEARTBEAT_SEC
                    )
                except asyncio.TimeoutError:
                    yield 'data: {"type":"heartbeat"}\n\n'
                    continue
                except StopAsyncIteration:
                    return
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception:
            # SSE stability: a single error must not kill the session (the
            # browser can reconnect).
            yield 'data: {"type":"heartbeat"}\n\n'
        finally:
            await agen.aclose()

    return stream_response(events())
