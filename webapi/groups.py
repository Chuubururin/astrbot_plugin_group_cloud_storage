"""Domain: Group management."""

from __future__ import annotations

from astrbot.api.web import error_response, json_response

from commands.handlers import Services
from core.api_validate import json_body, pick
from .webapi_base import (
    _TAG_RE,
    _account_scope_for,
    _group_item,
    _group_open_error,
    _param,
)


async def api_groups(s: Services) -> dict:
    """Group list (Page-manageable: whitelist union owned) + scan status + queue status.

    Only groups with managed=1 are listed; groups of offline accounts are
    excluded by list_page_groups' wither filter (data kept, the list recovers
    on reconnect). User-removed groups (managed=0, not whitelisted) stay hidden.
    """
    groups = await s.scan.list_page_groups(s.config.get("managed_groups", []))
    capacity = s.planner.capacity_stats(groups) if s.planner else {}
    if capacity:
        capacity["album_count"] = sum((g.album_count or 0) for g in groups)
        capacity["essence_count"] = sum((g.essence_count or 0) for g in groups)
    return json_response(
        {
            "capacity": capacity,
            "groups": [_group_item(g) for g in groups],
            "scan": (s.scan.last_result.as_dict() if s.scan.last_result else None),
            "queue": await s.queue.status(),
            "managed_groups": s.config.get("managed_groups", []),
        }
    )


async def api_scan(s: Services) -> dict:
    """Scan, two modes:
    - all (default): every group (group info + capacity + new-group detection)
    - range: body {mode:"range", group_ids?:[...]}; when no groups are given, pick
      the first group in sort order with unknown used capacity plus up to 2 groups
      ranked above it (default_range_ids).
    """
    payload = await json_body()
    mode = pick(
        payload, "mode", default="incremental", enum=("all", "incremental", "range")
    )  # incremental-first principle
    if payload and payload.get("scope") in ("albums", "essence"):
        mode = "all"
        group_ids = payload.get("group_ids")
        if isinstance(group_ids, list) and group_ids:
            return json_response({
                "task_id": await s.queue.submit(
                    "scan", target="*", payload={"mode": "all", "group_ids": group_ids}
                ),
                "mode": "all", "groups": len(group_ids),
            })
    if mode in ("all", "incremental"):
        task_id = await s.queue.submit("scan", target="*", payload={"mode": mode})
        return json_response({"task_id": task_id, "mode": mode})
    ids = (payload or {}).get("group_ids")
    if not isinstance(ids, list) or not ids:
        ids = await s.scan.default_range_ids()
        if not ids:
            return json_response(
                {
                    "task_id": "",
                    "mode": "range",
                    "groups": 0,
                    "note": "所有群容量已知，无需范围扫描",
                }
            )
    # Semantics: range = in-group file scan (file_scan); group info has no manual range
    task_id = await s.queue.submit(
        "file_scan", target="*", payload={"mode": "range", "groups": ids}
    )
    return json_response({"task_id": task_id, "mode": "range", "groups": len(ids)})


async def api_groups_removed(s: Services) -> dict:
    """Groups removed from management: only removed groups of currently online accounts."""
    online_ids = s.get_online_account_ids() if s.get_online_account_ids else set()
    rows = [
        g
        for g in await s.store.list_groups()
        if getattr(g, "managed", 1) == 0
        and (not online_ids or g.account_id in online_ids)
    ]
    return json_response(
        {
            "groups": [_group_item(g) for g in rows],
            "managed_groups": s.config.get("managed_groups", []),
        }
    )


async def api_groups_restore(s: Services) -> dict:
    """Restore management: managed=0 -> 1, back into the managed list."""
    payload = await json_body()
    ids = pick(payload, "group_ids", cast=list, required=True)
    if not ids or len(ids) > 500:
        return error_response("group_ids required (1..500)", status_code=400)
    gids = [str(x) for x in ids if str(x)]
    await s.store.mark_groups_removed(gids, 0)
    await s.store.set_groups_managed(gids, 1)
    return json_response({"restored": len(gids)})


async def api_accounts(s: Services) -> dict:
    """Account list: unified/individual management - only online accounts and their group counts."""
    accounts = await s.store.list_accounts()
    # Collect the set of online account IDs
    online_ids = s.get_online_account_ids() if s.get_online_account_ids else set()
    # Keep only online accounts (groups of offline accounts are already marked managed=0)
    online_accounts = []
    for a in accounts:
        if a["account_id"] in online_ids:
            a["online"] = True
            online_accounts.append(a)
    total_groups = sum(a["groups"] for a in online_accounts)
    return json_response(
        {
            "accounts": online_accounts,
            "total_groups": total_groups,
            "online_count": len(online_accounts),
        }
    )


async def api_group_info(s: Services) -> dict:
    group = await _param("group", "")
    if not group:
        return error_response("group required", status_code=400)
    no_cache = (await _param("no_cache", "false")).lower() in ("1", "true", "yes", "on")
    # Open gate: the group detail view may be deep-linked (offline owner /
    # dissolved group must fail closed) and the OneBot call must run under
    # the owning account.
    if err := await _group_open_error(s, group):
        return err
    async with _account_scope_for(s, group):
        info = await s.api.get_group_info(group, no_cache=no_cache)
    return json_response(info)


async def api_group_members(s: Services) -> dict:
    group = await _param("group", "")
    if not group:
        return error_response("group required", status_code=400)
    no_cache = (await _param("no_cache", "false")).lower() in ("1", "true", "yes", "on")
    if err := await _group_open_error(s, group):
        return err
    async with _account_scope_for(s, group):
        members = await s.api.list_group_members(group, no_cache=no_cache)
    return json_response({"members": [m.__dict__ for m in members]})


async def api_group_honor(s: Services) -> dict:
    group = await _param("group", "")
    if not group:
        return error_response("group required", status_code=400)
    honor_type = await _param("type", "")
    if err := await _group_open_error(s, group):
        return err
    async with _account_scope_for(s, group):
        honor = await s.api.get_group_honor_info(group, honor_type or None)
    return json_response(honor)


async def api_group_system_msg(s: Services) -> dict:
    group = await _param("group", "")
    only_pending = (await _param("only_pending", "false")).lower() in ("1", "true", "yes", "on")
    count = int(await _param("count", "50") or 50)
    if err := await _group_open_error(s, group):
        return err
    async with _account_scope_for(s, group):
        msgs = await s.api.get_group_system_msg(group, only_pending, max(1, min(count, 200)))
    return json_response(msgs)


async def api_groups_open_state(s: Services) -> dict:
    """Pre-flight group accessibility check (used by the frontend before
    navigating to the files tab).

    Returns {managed, account_online, seen, reason?} so the frontend can
    decide whether to allow or block navigation without a full page load.
    """
    group = await _param("group", "")
    if not group:
        return error_response("group required", status_code=400)
    state = await s.scan.group_open_state(group, s.config.get("managed_groups", []))
    reason = None
    if not state["managed"]:
        reason = "group not managed"
    elif not state["account_online"]:
        reason = "群归属账号离线，暂不可操作"
    elif not state["seen"]:
        reason = "群已解散或不可访问"
    return json_response({**state, "reason": reason})


async def api_groups_batch_actions(s: Services) -> dict:
    """Batch group actions: rename / join option / remark - queued, one real call per group."""
    payload = await json_body()
    group_ids = payload.get("group_ids")
    action = str(payload.get("action") or "")
    value = payload.get("value")
    if not isinstance(group_ids, list) or not group_ids:
        return error_response("group_ids required", status_code=400)
    if action not in ("rename", "add_option", "remark"):
        return error_response(
            "action must be rename|add_option|remark", status_code=400
        )
    if action == "rename":
        if not isinstance(value, str) or not (0 < len(value) <= 60):
            return error_response("rename value length 1..60", status_code=400)
    if action == "remark":
        if not isinstance(value, str) or not (0 < len(value) <= 60):
            return error_response("remark value length 1..60", status_code=400)
    if action == "add_option":
        if not isinstance(value, int) or value not in (1, 2, 3, 4, 5):
            return error_response("add_option value must be int 1..5", status_code=400)
    for gid in group_ids:
        if err := await _group_open_error(s, str(gid)):
            return err
    task_id = await s.queue.submit(
        "batch_groups",
        target="*",
        payload={
            "action": action,
            "value": value,
            "group_ids": [str(g) for g in group_ids],
        },
    )
    return json_response({"task_id": task_id, "groups": len(group_ids)})


async def api_groups_batch_update(s: Services) -> dict:
    """Batch rename/label: body = [{group_id, display_name?, label?, set_remote?}]."""
    payload = await json_body()
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        return error_response("items required", status_code=400)
    applied, queued = 0, 0
    for it in items:
        gid = str(it.get("group_id") or "")
        if err := await _group_open_error(s, gid):
            return err
        display = it.get("display_name")
        label = it.get("label")
        if display is not None and not (0 < len(str(display)) <= 80):
            return error_response("display_name length 1..80", status_code=400)
        if label is not None and not _TAG_RE.match(str(label)):
            return error_response("label invalid", status_code=400)
        # set_remote defaults to true: the real rename (calling set_group_name) is the
        # primary action; local display_name/label are backfilled only after
        # verification passes (group name matches) - never filled unverified;
        # set_remote=false: pure local fields are written immediately.
        if it.get("set_remote", True) and display:
            await s.queue.submit(
                "rename",
                target=gid,
                payload={
                    "name": str(display),
                    "display_name": str(display),
                    "label": it.get("label"),
                },
            )
            queued += 1
        elif display is not None or label is not None:
            fields = {}
            if display is not None:
                fields["display_name"] = str(display)
            if label is not None:
                fields["label"] = str(label)
            await s.store.update_group_fields(gid, **fields)
            applied += 1
    return json_response({"applied": applied, "queued": queued})


async def api_groups_order(s: Services) -> dict:
    """Persist ordering: body = {ordered_ids: [...]}."""
    payload = await json_body()
    ordered = payload.get("ordered_ids")
    if not isinstance(ordered, list):
        return error_response("ordered_ids required", status_code=400)
    ids = [str(x) for x in ordered if str(x)]
    await s.store.reorder_groups(ids)
    return json_response({"ok": True, "count": len(ids)})


async def api_groups_remove(s: Services) -> dict:
    """Remove managed entries (managed=0: hidden from the list and not revived by
    scans; the real group is not deleted).
    """
    payload = await json_body()
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return error_response("items required", status_code=400)
    ids = [str(x) for x in items if str(x)]
    await s.store.set_groups_managed(ids, 0)
    # removed=1 keeps user removals distinct from offline auto-hiding, so
    # account restore / liveness sweeps never resurrect them.
    await s.store.mark_groups_removed(ids, 1)
    return json_response({"removed": len(ids)})
