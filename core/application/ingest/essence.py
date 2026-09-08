from __future__ import annotations

import asyncio
import time
import uuid

from core.domain.enums import ResourceType
from core.domain.resource import Resource
from core.log import logger
from core.application.composition.spec import encode_composition
from core.application.composition.splitter import (
    effective_chunk_limit,
    split_text,
)

CLOUD_CALL_TIMEOUT = 12.0
_PART_MARK = "[云盘|{title}|{seq}/{total}]"


class EssenceMixin:
    async def submit_essence_save(self, group_id: str, title: str, text: str) -> str:
        """Register an essence text save (long text is split by the char
        limit; each part is sent and set as an essence message)."""
        if not (0 < len(title) <= 80):
            raise ValueError("title length 1..80")
        if not text.strip():
            raise ValueError("text empty")
        return await self.queue.submit(
            "essence_save",
            target=group_id,
            payload={"title": title, "text": text},
        )

    @staticmethod
    def _marker(title: str, seq: int, total: int) -> str:
        return _PART_MARK.format(title=title, seq=seq, total=total)

    async def _part_essence_set(
        self, group_id: str, title: str, seq: int, total: int, chunk: str
    ) -> str:
        """Send one part and set it as essence, with cloud read-back
        verification (QQ occasionally drops the essence flag -> resend and
        re-set, up to 3 attempts).

        The trailing chunk marker is a semantic composition identifier placed
        at the **end** of the text (the QQ essence list preview shows the
        beginning, so a trailing marker does not cover the body). Returns the
        final message_id; on resend the old message automatically becomes a
        normal message (no side effects).
        """
        marker = self._marker(title, seq, total)
        msg = f"{chunk}\n{marker}"
        for attempt in range(3):
            r = await self.api.send_group_msg(
                group_id, [{"type": "text", "data": {"text": msg}}]
            )
            mid = str((r or {}).get("message_id") or "")
            if not mid:
                raise ValueError("send_group_msg returned no message_id")
            await self.api.set_essence_msg(mid)
            try:
                timeout = CLOUD_CALL_TIMEOUT
                essences = await asyncio.wait_for(
                    self.api.get_essence_msg_list(group_id),
                    timeout=timeout,
                )
            except Exception:
                essences = []
            if any(self._has_marker(self._extract_text(e), marker) for e in essences):
                return mid
            logger.warning(
                f"[ingest] essence part {seq}/{total} not confirmed, retry {attempt + 1}"
            )
            await asyncio.sleep(1.5)
        raise ValueError(f"essence part {seq}/{total} not confirmed after 3 attempts")

    @staticmethod
    def _has_marker(text: str, marker: str) -> bool:
        """Marker hit test: matches a trailing or a leading marker
        (compatibility with existing stored data)."""
        t = (text or "").strip()
        return t.endswith(marker) or t.startswith(marker)

    @staticmethod
    def _strip_marker(text: str, marker: str) -> str | None:
        """Strip the chunk marker to recover the body: trailing marker first,
        leading marker as fallback."""
        t = (text or "").strip()
        if t.endswith(marker):
            return t[: -len(marker)].rstrip("\n").rstrip()
        if t.startswith(marker):
            return t[len(marker) :].lstrip("\n").lstrip()
        return None

    async def _do_essence_save(self, op) -> None:
        title, text = op.payload["title"], op.payload["text"]
        # BUG-10 fix: converge the chunk limit with the actual total count.
        # The marker size depends on the total part count, which in turn
        # depends on the chunk limit. Iterate until stable. The base is fixed
        # so limit only shrinks; total only grows; the loop terminates because
        # marker overhead is bounded and limit has a floor (100 from
        # effective_chunk_limit).
        base = self.essence_chunk_chars
        limit = base
        total = len(split_text(text, limit))
        prev_total = -1
        while total != prev_total and limit > 100:
            prev_total = total
            limit = effective_chunk_limit(title, total, base)
            total = len(split_text(text, limit))
        chunks = split_text(text, limit)
        total = len(chunks)
        parts: list[dict] = []
        for seq, chunk in enumerate(chunks, 1):
            mid = await self._part_essence_set(op.target, title, seq, total, chunk)
            # Part texts are stored locally as redundancy: the full text can
            # be rebuilt offline if the cloud is unavailable
            parts.append(
                {"seq": seq, "message_id": mid, "chars": len(chunk), "text": chunk}
            )
            self.queue.publish(
                {
                    "type": "progress",
                    "kind": "essence_save",
                    "target": op.target,
                    "i": seq,
                    "n": total,
                    "part": f"{seq}/{total}",
                }
            )
        await self.store.upsert_resources(
            [
                Resource(
                    group_id=op.target,
                    type=ResourceType.ESSENCE,
                    name=title,
                    source_ref=f"text:{uuid.uuid4().hex[:10]}",
                    size=len(text),
                    created_at=int(time.time()),
                    meta={
                        "kind": "text_split",
                        "parts": parts,
                        # Summary covers the head of the text: FTS content
                        # search window + fallback when the cloud is missing
                        # (20k chars keeps long documents searchable)
                        "summary": text[:20000].replace("\n", " "),
                        "composition": encode_composition(
                            "text_split", total, "marker"
                        ),
                    },
                )
            ]
        )
        logger.info(f"[ingest] essence saved: {title} -> {total} parts in {op.target}")

    @staticmethod
    def _extract_text(e: dict) -> str:
        content = e.get("content")
        if isinstance(content, list):
            return " ".join(
                str((s.get("data") or {}).get("text") or "")
                for s in content
                if isinstance(s, dict) and s.get("type") == "text"
            ).strip()
        return str(content or e.get("text") or "").strip()

    async def essence_full_text(self, group_id: str, id: int) -> tuple[str, list[int]]:
        """Rebuild the full sharded text from the cloud essence list; returns
        (full text, missing part seqs).

        The essence list is eventually consistent (a short delay after the
        set-essence call is possible); missing parts are retried 3 times at
        1s intervals.
        """
        row = await self.store.get_resource_any(id)
        if not row or str(row.get("group_id")) != str(group_id):
            raise ValueError(f"resource {id} not found in group {group_id}")
        meta = row.get("meta") or {}
        parts = list(meta.get("parts") or [])
        if not parts:
            # Non-split essence: prefer fetching the full entry content from
            # the cloud (source_ref = essence entry message_id)
            try:
                timeout = CLOUD_CALL_TIMEOUT
                essences = await asyncio.wait_for(
                    self.api.get_essence_msg_list(group_id),
                    timeout=timeout,
                )
            except Exception:
                essences = []
            for e in essences:
                if str(e.get("message_id") or "") == str(row.get("source_ref") or ""):
                    text = self._extract_text(e)
                    if text:
                        return text, []
            # Cloud miss -> local summary fallback
            return (meta.get("summary") or row.get("name") or ""), []
        total = len(parts)
        # Local cache fast path: part texts were stored redundantly at save
        # time -> no immediate cloud rebuild needed
        if parts and all(p.get("text") for p in parts):
            ordered = [p["text"] for p in sorted(parts, key=lambda x: x["seq"])]
            return "\n".join(ordered), []
        by_prefix: dict[int, str] = {}
        cloud_timeout = False
        cloud_ok = False
        for attempt in range(4):
            try:
                timeout = CLOUD_CALL_TIMEOUT
                essences = await asyncio.wait_for(
                    self.api.get_essence_msg_list(group_id),
                    timeout=timeout,
                )
                cloud_ok = True
            except asyncio.TimeoutError:
                cloud_timeout = True
                essences = []
            except Exception:
                essences = []
            for e in essences:
                text = self._extract_text(e)
                for p in parts:
                    # Marker at the end (current format) or start (existing
                    # stored data) are both accepted
                    body = self._strip_marker(text, self._marker(row["name"], p["seq"], total))
                    if body is not None:
                        by_prefix[p["seq"]] = body
            missing = sorted(p["seq"] for p in parts if p["seq"] not in by_prefix)
            if not missing or attempt == 3:
                break
            logger.debug(
                f"[ingest] essence parts missing {missing}, retry {attempt + 1}"
            )
            await asyncio.sleep(1.0)
        if missing:
            import json as _json

            raw_sample = [
                {
                    k: (str(v)[:80])
                    for k, v in (e or {}).items()
                    if k in ("message_id", "msg_seq", "content", "text")
                }
                for e in essences[:3]
            ]
            logger.warning(
                f"[ingest] essence rebuild incomplete: {missing} of {total} "
                f"(raw: {_json.dumps(raw_sample, ensure_ascii=False)})"
            )
        if not by_prefix and not cloud_ok:
            # Cloud fully unavailable: raise a clear error (network/session
            # degradation) instead of returning an empty modal
            if cloud_timeout:
                raise TimeoutError(
                    "云端精华列表拉取超时（QQ 会话退化或网络波动），请稍后重试"
                )
            raise ValueError("云端精华列表不可用，且本地无缓存分片")
        ordered = [
            by_prefix[p["seq"]]
            for p in sorted(parts, key=lambda x: x["seq"])
            if p["seq"] in by_prefix
        ]
        return "\n".join(ordered), missing

    async def submit_essence_delete(self, group_id: str, id: int) -> str:
        """Delete an essence resource: remove each part from essence, then
        soft-delete the resource (cloud is the source of truth)."""
        row = await self.store.get_resource_any(id)
        if not row or str(row.get("group_id")) != str(group_id):
            raise ValueError(f"resource {id} not found in group {group_id}")
        if row.get("type") != "essence":
            raise ValueError("essence delete only accepts essence resources")
        return await self.queue.submit(
            "essence_delete",
            target=group_id,
            payload={
                "id": id,
                "parts": list((row.get("meta") or {}).get("parts") or []),
            },
        )

    async def _do_essence_delete(self, op) -> None:
        parts = op.payload.get("parts") or []
        failed = 0
        for p in parts:
            try:
                await self.api.delete_essence_msg(str(p.get("message_id") or ""))
            except Exception as e:
                failed += 1
                logger.warning(f"[ingest] essence part delete failed: {e}")
        # The cloud may have already lost this essence (session handle /
        # manual removal) -> local soft-delete keeps consistency
        await self.store.update_resource_fields(op.payload["id"], status="deleted")
        logger.info(
            f"[ingest] essence deleted: {op.payload['id']} "
            f"({len(parts)} parts, {failed} cloud-miss)"
        )
