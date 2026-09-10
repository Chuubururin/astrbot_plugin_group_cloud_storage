/**
 * Command definitions - group-file and distribute domains.
 *
 * Every download form routes through features/download.js downloadItems()
 * (one executor, internal target subdivision; 转存网盘 is download's
 * special form — target=netdisk — so there is no separate bridge-out
 * command). All operations are OneBot11-API composites exposed by the
 * backend (see webapi/); the frontend only declares the UX contract.
 *
 * @module features/command-defs
 */

import { registerCommand, rowGroup } from './commands.js';
import { runEachWithFailures } from '../utils/helpers.js';
import { API, apiGet, apiPost } from '../api.js';
import { confirmEx, promptEx, detailEx } from '../components/modal.js';
import { toast } from '../components/toast.js';
import { formatSize } from '../utils/helpers.js';
import { pickFolder } from './folder-picker.js';
import { openPreview } from './preview.js';
import { downloadItems, promptDownloadTarget, showDownloadAddress } from './download.js';

/** Volume reassembly confirmation (shared by local download entries). */
const confirmVolumes = (n) => confirmEx('分卷下载（重组校验）',
  `选中含 ${n} 个分卷文件。下载将自动：拉取全部分卷 / 逐卷 SHA-256 校验 / 按序合并重组。`,
  { okText: '开始下载' });

/** Per-target success text for the unified executor summary. */
const TARGET_SUCCESS = {
  local: '下载已开始',
  link: 'CDN 直链已复制',
  address: '本机服务地址已就绪',
  netdisk: '转存到网盘任务已提交，可在任务页查看进度',
  album: '转存到相册任务已提交，可在任务页查看进度',
  essence: '转存到精华任务已提交，可在任务页查看进度',
  group: '转存到群文件任务已提交，可在任务页查看进度',
  copy: '全文已复制',
};

/** Toast one executor result (failures summarized, first error inline). */
function summarize(label, r) {
  if (r.failed.length) {
    toast(`${label}完成 ${r.done}，失败 ${r.failed.length}（${String(r.failed[0]).slice(0, 80)}）`, 'warn');
  } else {
    toast(TARGET_SUCCESS[label] || `${label}完成`, 'success');
  }
}

/** Register every command of the files and distribute domains. */
export function registerAllCommands() {
  registerCommand({
    id: 'clear',
    label: '清空',
    icon: 'X',
    async run(ctx) {
      ctx.source?.selection.clear();
    },
    keepSelection: true,
  });

  registerCommand({
    id: 'download',
    label: '下载',
    icon: 'DOWNLOAD',
    async run(ctx) {
      const r = await downloadItems(ctx, 'local', { confirmVolumes });
      if (r.cancelled) return false; // 取消: 命令层保持现状
      summarize('local', r);
    },
    refresh: 'files',
  });

  registerCommand({
    id: 'link',
    label: '直链',
    icon: 'LINK',
    async run(ctx) {
      summarize('link', await downloadItems(ctx, 'link'));
    },
    keepSelection: true,
  });

  registerCommand({
    id: 'address',
    label: '下载地址',
    icon: 'LINK',
    needsSingle: true,
    async run(ctx) {
      const r = await downloadItems(ctx, 'address');
      if (r.failed.length) { toast(r.failed[0], 'warn'); return; }
      const copied = await showDownloadAddress(r.address);
      if (copied) toast('HTTP 地址已复制', 'success');
    },
    keepSelection: true,
  });

  registerCommand({
    id: 'files-distribute',
    label: '下载/转存',
    icon: 'SHARE',
    async run(ctx) {
      // One entry, all download forms: CDN 直链 / 本机服务地址 / 转存到
      // 网盘/相册/精华 all hang off the same unified executor.
      // The offered targets must be the subset downloadItems() implements
      // (group/copy belong to the cross-module distribute commands).
      const target = await promptDownloadTarget(
        ['local', 'link', 'address', 'netdisk', 'album', 'essence']);
      if (!target) { toast('已取消', 'info'); return false; }
      const r = await downloadItems(ctx, target, { confirmVolumes });
      if (r.cancelled) return false;
      if (r.address) {
        const copied = await showDownloadAddress(r.address);
        if (copied) toast('HTTP 地址已复制', 'success');
        return;
      }
      summarize(target, r);
    },
    refresh: ['files', 'bridge', 'tasks'],
  });

  registerCommand({
    id: 'move',
    label: '移动',
    icon: 'MOVE',
    needsGroup: true,
    async run(ctx) {
      const target = await pickFolder(rowGroup(ctx.state, ctx.rows[0]));
      if (!target) return false; // 取消: 命令层保持现状
      // Batch endpoint carries per-item groups (aggregated view safe).
      await apiPost(API.FILES.BATCH_MOVE, {
        items: ctx.rows.map((f) => ({ id: Number(f.id), group: rowGroup(ctx.state, f) })),
        folder_id: target.id || '/',
      });
      toast('移动已入队', 'success');
    },
    refresh: ['files'],
  });

  registerCommand({
    id: 'rename',
    label: '改名',
    icon: 'EDIT',
    needsSingle: true,
    async run(ctx) {
      const name = await promptEx('改名重传', `当前: ${ctx.rows[0].name}`, { value: ctx.rows[0].name });
      if (name === null || name === ctx.rows[0].name) return false;
      // OneBot has no rename action: rename = replace upload with the new name.
      // Backend contract: {id, group, new_name} (files/replace_name).
      await apiPost(API.FILES.REPLACE_NAME, {
        id: Number(ctx.keys[0]), group: rowGroup(ctx.state, ctx.rows[0]), new_name: name,
      });
      toast('改名重传已入队', 'success');
    },
    refresh: ['files'],
  });

  registerCommand({
    id: 'convert',
    label: '分卷',
    icon: 'COPY',
    needsSingle: true,
    async run(ctx) {
      // Manual volume split of an existing cloud file (never automatic —
      // the file-scan auto sweep was removed). Backend rejects files below
      // the threshold or already composite.
      await apiPost(API.FILES.CONVERT_VOLUMES, {
        id: Number(ctx.keys[0]), group: rowGroup(ctx.state, ctx.rows[0]),
      });
      toast('分卷任务已入队，可在任务页查看进度', 'success');
    },
    refresh: ['files', 'tasks'],
  });

  registerCommand({
    id: 'tags',
    label: '标签',
    icon: 'CHECK',
    async run(ctx) {
      const res = await promptEx('设置标签', '输入标签（逗号分隔，最多 10 个）');
      if (res === null) return false;
      const tags = res.split(',').map((t) => t.trim()).filter(Boolean).slice(0, 10);
      // Backend contract: items=[{id,group}] + tags (batch-tags).
      await apiPost(API.FILES.BATCH_TAGS, {
        items: ctx.rows.map((f) => ({ id: Number(f.id), group: rowGroup(ctx.state, f) })),
        tags,
      });
      toast('标签设置成功', 'success');
    },
    refresh: ['files'],
  });

  registerCommand({
    id: 'detail',
    label: '详情',
    icon: 'INFO',
    needsSingle: true,
    async run(ctx) {
      const data = await apiGet(API.FILES.DETAIL, { id: Number(ctx.keys[0]), group: rowGroup(ctx.state, ctx.rows[0]) });
      await detailEx('文件详情', [
        { label: '名称', value: data.name || '-' },
        { label: '类型', value: data.type || '-' },
        { label: '大小', value: formatSize(data.size) },
        { label: '上传者', value: data.uploader || '-' },
        { label: '目录', value: data.folder || '/' },
        { label: '标签', value: (data.tags || []).join(', ') || '-' },
        { label: 'URI', value: data.uri || '-' },
      ]);
    },
    keepSelection: true,
  });

  registerCommand({
    id: 'delete',
    label: '删除',
    icon: 'DELETE',
    danger: true,
    confirm: (count) => `确定删除 ${count} 个文件？此操作不可撤销。`,
    async run(ctx) {
      await apiPost(API.FILES.BATCH_DELETE, {
        items: ctx.rows.map((f) => ({ id: Number(f.id), group: rowGroup(ctx.state, f) })),
      });
      toast('删除成功', 'success');
    },
    refresh: ['files'],
  });

  registerCommand({
    id: 'essence-view',
    label: '查看',
    icon: 'INFO',
    needsSingle: true,
    async run(ctx) {
      await openPreview(ctx.rows[0], 'essence');
    },
    keepSelection: true,
  });

  registerCommand({
    id: 'essence-delete',
    label: '删除',
    icon: 'DELETE',
    danger: true,
    needsGroup: true,
    confirm: (count) => `确定删除 ${count} 条精华消息？此操作不可撤销。`,
    async run(ctx) {
      const { done, failed } = await runEachWithFailures(ctx.rows, (f) =>
        apiPost(API.ESSENCE.DELETE, { group: rowGroup(ctx.state, f), id: Number(f.id) }));
      if (failed.length) {
        throw new Error(`成功 ${done}，失败 ${failed.length}：${String(failed[0]).slice(0, 80)}`);
      }
      toast('精华消息删除已排队', 'success');
    },
    refresh: ['files', 'essence'],
  });
}
