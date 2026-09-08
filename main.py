"""Thin AstrBot adapter for the group cloud storage manager."""
from __future__ import annotations
import os
import sys

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _PLUGIN_DIR)

# The host framework purges only "data.plugins.<name>.*" entries from
# sys.modules on plugin reload. This plugin imports its internals as
# top-level names (core.*, webapi.*, commands.*, ...), which the purge
# never touches — stale bytecode would survive every reload. Evict any
# top-level modules that resolve back into this plugin directory before
# the imports below run, so a reload always picks up fresh code.
_TOP_LEVEL_PKGS = ("commands", "core", "webapi", "adapters", "ports", "bootstrap")
for _pkg in _TOP_LEVEL_PKGS:
    for _key in [k for k in sys.modules if k == _pkg or k.startswith(_pkg + ".")]:
        _mod = sys.modules.get(_key)
        _file = getattr(_mod, "__file__", None) or ""
        if _file and os.path.realpath(os.path.dirname(_file)).startswith(
            os.path.realpath(_PLUGIN_DIR)
        ):
            del sys.modules[_key]
del _pkg, _key, _mod, _file

from astrbot.api.event import AstrMessageEvent, filter  # noqa: E402  (after sys.path bootstrap)
from astrbot.api.star import Context, Star  # noqa: E402
from commands.handlers import (handle_csarchive, handle_csbridge, handle_csfetch, handle_cssave, handle_csfile, handle_csfiles, handle_cssync, handle_cshelp)  # noqa: E402
from core.runtime.adapter import RuntimeAdapter  # noqa: E402
from core.runtime.commands import strip_command_params  # noqa: E402
from core.runtime.events import handle_aiocqhttp_event  # noqa: E402

class Main(RuntimeAdapter, Star):
    """Group cloud storage manager.

    /cssync [group_id] sync the group netdisk index + stats overview
    /csfiles [group_id] [page] file list
    /csfile <id> [group_id] file details + download direct link
    /cssave [group_id] <title> <text> save text as an essence message
    /csfetch [group_id] <URL> [filename] import an external file
    /cshelp help
    """

    def __init__(self, context: Context, config: dict):
        Star.__init__(self, context)
        self._init_runtime(context, config)

    # ---------- Command shells (parse -> authorize -> service) ----------

    @filter.command("cssync")
    async def cssync(self, event: AstrMessageEvent, group_id: str = ""):
        """同步群云存储索引并输出统计概览"""
        if not self._lifecycle._inited:
            await self._ensure_init()
        async with self._bot_scope(event):
            yield event.plain_result(
                await handle_cssync(event, self.services, group_id)
            )

    @filter.command("csfiles")
    async def csfiles(self, event: AstrMessageEvent, group_id: str = "", page: int = 1):
        """分页列出群文件"""
        if not self._lifecycle._inited:
            await self._ensure_init()
        async with self._bot_scope(event):
            yield event.plain_result(
                await handle_csfiles(event, self.services, group_id, int(page))
            )

    @filter.command("csfile")
    async def csfile(self, event: AstrMessageEvent, id: str, group_id: str = ""):
        """文件详情与下载直链"""
        if not self._lifecycle._inited:
            await self._ensure_init()
        async with self._bot_scope(event):
            try:
                fid = int(id)
            except (TypeError, ValueError):
                yield event.plain_result(
                    "❌ 参数错误：ID 必须为数字（/csfiles 列表中的编号）。"
                )
                return
            yield event.plain_result(
                await handle_csfile(event, self.services, fid, group_id)
            )

    @filter.command("csarchive")
    async def csarchive(
        self,
        event: AstrMessageEvent,
        group_id: str = "",
        file_ref: str = "",
        force: str = "",
    ):
        """Archive group file to OpenList"""
        if not self._lifecycle._inited:
            await self._ensure_init()
        async with self._bot_scope(event):
            yield event.plain_result(
                await handle_csarchive(
                    event,
                    self.services,
                    group_id,
                    file_ref,
                    force="--force" in force or force.lower() == "true",
                )
            )

    @filter.command("csbridge")
    async def csbridge(
        self, event: AstrMessageEvent, action: str = "", task_id: str = ""
    ):
        """Bridge task management"""
        if not self._lifecycle._inited:
            await self._ensure_init()
        async with self._bot_scope(event):
            yield event.plain_result(
                await handle_csbridge(event, self.services, action, task_id)
            )

    @filter.command("cshelp")
    async def cshelp(self, event: AstrMessageEvent):
        """显示指令帮助"""
        yield event.plain_result(handle_cshelp())

    @filter.command("cssave")
    async def cssave(self, event: AstrMessageEvent):
        """文本保存为精华消息（长文本自动分段）"""
        if not self._lifecycle._inited:
            await self._ensure_init()
        rest = strip_command_params(
            getattr(event, "message_str", "") or ""
        )
        group_id, title, text = "", "", ""
        tokens = rest.split(" ", 2)
        if tokens and tokens[0].isdigit() and len(tokens[0]) >= 5:
            group_id = tokens.pop(0)
        if tokens:
            title = tokens.pop(0)
        if tokens:
            text = tokens[0]
        async with self._bot_scope(event):
            yield event.plain_result(
                await handle_cssave(event, self.services, group_id, title, text)
            )

    @filter.command("csfetch")
    async def csfetch(self, event: AstrMessageEvent):
        """拉取外部文件存入群文件"""
        if not self._lifecycle._inited:
            await self._ensure_init()
        rest = strip_command_params(
            getattr(event, "message_str", "") or ""
        )
        group_id, url, name = "", "", ""
        tokens = rest.split(" ", 2)
        if tokens and tokens[0].isdigit() and len(tokens[0]) >= 5:
            group_id = tokens.pop(0)
        if tokens:
            url = tokens.pop(0)
        if tokens:
            name = tokens[0]
        async with self._bot_scope(event):
            yield event.plain_result(
                await handle_csfetch(event, self.services, group_id, url, name)
            )

    # ---------- Event indexing (group_upload) ----------

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_aiocqhttp(self, event: AstrMessageEvent):
        """Receive all aiocqhttp platform events (notices included); index group_upload."""
        await handle_aiocqhttp_event(
            event,
            config=self.config,
            resolver=self._resolver,
            platform_bots=self._platform_bots,
            queue=self.queue,
            sync_service=self.sync,
            dispatch=self._dispatch,
            maybe_submit_scan=self._lifecycle.maybe_submit_scan,
            bot_scope=self._bot_scope,
        )

    # ---------- Lifecycle ----------

    async def terminate(self):
        """Called on plugin disable/reload: cleans up auto scan, OpQueue and SQLite."""
        await self._lifecycle.terminate()
