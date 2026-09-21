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

import { registerCommand, rowGroup, rowGroupFor } from './commands.js';
import { runEachWithFailures } from '../utils/helpers.js';
import { API, apiGet, apiPost } from '../api.js';
import { MAX_BATCH_ITEMS } from '../constants.js';
import { getState } from '../store.js';
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

/** Domain lookups: DataSource id -> refresh topic, and view id -> DataSource
 * id (the fallback when the command context carries no source). */
const SOURCE_TOPIC = { group: 'files', album: 'albums', essence: 'essence', netdisk: 'netdisk' };
const VIEW_SOURCE = { files: 'group', albums: 'album', album: 'album', essence: 'essence', netdisk: 'netdisk' };
/** DataSource id of the command context (module-scoped commands such as tags
 * resolve both group and refresh topic from the owning domain). */
const sourceIdOf = (ctx) => ctx?.source?.id || VIEW_SOURCE[getState().currentView] || 'group';
/** Refresh topic of the command context's domain. */
const topicOf = (ctx) => SOURCE_TOPIC[sourceIdOf(ctx)] || 'files';

/** Front-end pre-check for the batch endpoints (backend cap MAX_BATCH_ITEMS:
 * an over-limit batch is rejected as a whole with 400). */
function overBatchLimit(ctx) {
  if (ctx.rows.length <= MAX_BATCH_ITEMS) return false;
  toast(`单次最多 ${MAX_BATCH_ITEMS} 项，请分批操作`, 'warn');
  return true;
}

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
  } else if (!r.ok) {
    // Nothing succeeded and nothing failed (e.g. an empty link batch): no
    // usable result was produced, so never report success.
    toast(`${label}未完成任何项`, 'warn');
  } else {
    toast(TARGET_SUCCESS[label] || `${label}完成`, 'success');
  }
}

/** Register every command of the files and distribute domains. */
export function registerFilesCommands() {
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
      if (r.failed.length) { toast(r.failed[0], 'warn'); return false; }
      const copied = await showDownloadAddress(r.address);
      if (!copied) { toast('未复制地址（已取消）', 'info'); return false; }
      toast('HTTP 地址已复制', 'success');
    },
    keepSelection: true,
  });

  registerCommand({
    id: 'files-distribute',
    label: '下载/转存',
    icon: 'SHARE',
    async run(ctx) {
      // One entry, all download forms: CDN 直链 / 本机服务地址 / 转存到
      // 网盘/相册/精华 all hang off the same unified executor. The offered
      // targets must be the subset downloadItems() implements (group/copy
      // belong to the cross-module distribute commands).
      const target = await promptDownloadTarget(
        ['local', 'link', 'address', 'netdisk', 'album', 'essence']);
      if (!target) { toast('已取消', 'info'); return false; }
      const r = await downloadItems(ctx, target, { confirmVolumes });
      if (r.cancelled) return false;
      if (r.address) {
        const copied = await showDownloadAddress(r.address);
        toast(copied ? 'HTTP 地址已复制' : '未复制地址（已取消）', copied ? 'success' : 'info');
        return copied;
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
      // 聚合视图跨群选择时目录树只来自第一行的群，选出的 folder_id 对其他
      // 群的行不存在（QQ 无跨群转移能力）：整体拒绝而不是提交注定部分失败的批次。
      const groups = [...new Set(ctx.rows.map((f) => rowGroup(ctx.state, f)))];
      if (groups.length > 1) {
        toast('移动仅支持群内操作：选中的文件来自多个群，请分群分别移动', 'warn');
        return false;
      }
      if (overBatchLimit(ctx)) return false;
      const target = await pickFolder(groups[0]);
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
      const current = ctx.rows[0].name || '';
      const name = await promptEx('改名重传', `当前: ${current}`, { value: current });
      if (name === null) return false; // 取消
      const next = name.trim();
      if (!next) { toast('名称不能为空', 'warn'); return false; }
      if (next === current) return false; // 未修改
      // OneBot has no rename action: rename = replace upload (files/replace_name).
      await apiPost(API.FILES.REPLACE_NAME, {
        id: Number(ctx.keys[0]), group: rowGroup(ctx.state, ctx.rows[0]), new_name: next,
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
      // Manual volume split (never automatic: the file-scan auto sweep was
      // removed); the backend rejects sub-threshold or already-split files.
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
      if (overBatchLimit(ctx)) return false;
      // One command serves both tabs: row group and refresh topic follow the
      // owning domain (the album table only subscribes refresh:albums).
      const sourceId = sourceIdOf(ctx);
      const cur = ctx.rows[0]?.tags;
      const res = await promptEx('设置标签', '输入标签（逗号分隔，最多 10 个）',
        { value: Array.isArray(cur) ? cur.join(',') : (cur || '') });
      if (res === null) return false;
      const tags = res.split(',').map((t) => t.trim()).filter(Boolean);
      if (tags.length > 10) { toast('标签最多 10 个，请精简后重试', 'warn'); return false; }
      // Backend contract: items=[{id,group}] + tags (batch-tags).
      await apiPost(API.FILES.BATCH_TAGS, {
        items: ctx.rows.map((f) => ({ id: Number(f.id), group: rowGroupFor(ctx.state, f, sourceId) })),
        tags,
      });
      toast('标签设置成功', 'success');
    },
    refresh: topicOf,
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
      if (overBatchLimit(ctx)) return false;
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
