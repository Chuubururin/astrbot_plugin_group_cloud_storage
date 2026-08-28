/**
 * Command definitions - netdisk domain (T-4).
 *
 * 2026-09-03 整改（S2）：移除 transfer-in（网盘→群文件已由分发 target=group 覆盖）
 * 与 netdisk-index（深度索引无意义）。保留：直链、下载、改名、标记、删除、分发。
 * Register via registerAllNetdiskCommands() from the netdisk view.
 *
 * @module features/command-defs-netdisk
 */

import { registerCommand } from './commands.js';
import { API, apiGet, apiPost } from '../api.js';
import { getState } from '../store.js';
import { promptEx, detailEx, showFormModal } from '../components/modal.js';
import { toast } from '../components/toast.js';
import { copyToClipboard, formatSize } from '../utils/helpers.js';

/** Register every netdisk command. */
export function registerAllNetdiskCommands() {

  registerCommand({
    id: 'netdisk-link',
    label: '直链',
    icon: 'LINK',
    needsSingle: true,
    async run(ctx) {
      const d = await apiPost(API.BRIDGE.NETDISK_LINK, { path: ctx.keys[0] });
      await copyToClipboard(d.url || '');
      toast('直链已复制', 'success');
    },
    keepSelection: true,
  });

  registerCommand({
    id: 'netdisk-download',
    label: '下载',
    icon: 'DOWNLOAD',
    needsSingle: true,
    async run(ctx) {
      const d = await apiPost(API.BRIDGE.NETDISK_LINK, { path: ctx.keys[0] });
      if (d?.url) window.open(d.url, '_blank');
    },
    keepSelection: true,
  });

  registerCommand({
    id: 'netdisk-rename',
    label: '改名',
    icon: 'EDIT',
    needsSingle: true,
    async run(ctx) {
      const name = await promptEx('重命名', `当前: ${ctx.rows[0]?.name || ctx.keys[0]}`, {
        value: ctx.rows[0]?.name || '',
      });
      if (!name) return;
      await apiPost(API.BRIDGE.RENAME, {
        path: ctx.keys[0],
        name,
      });
      toast('重命名成功', 'success');
    },
    refresh: 'netdisk',
  });

  registerCommand({
    id: 'netdisk-tags',
    label: '标记',
    icon: 'CHECK',
    needsSingle: true,
    async run(ctx) {
      const res = await promptEx('设置标记', '输入标签（逗号分隔）', { value: ctx.rows[0]?.tags || '' });
      if (res === null) return;
      const tags = res.split(',').map((t) => t.trim()).filter(Boolean).slice(0, 10);
      await apiPost(API.BRIDGE.NETDISK_META, { path: ctx.keys[0], tags });
      toast('标记已保存', 'success');
    },
    refresh: 'netdisk',
  });


  registerCommand({
    id: 'netdisk-delete',
    label: '删除',
    icon: 'DELETE',
    danger: true,
    confirm: (count) => `确定删除 ${count} 个网盘文件？`,
    async run(ctx) {
      for (const key of ctx.keys) {
        const dir = key.replace(/\/[^/]+$/, '') || '/';
        await apiPost(API.BRIDGE.REMOVE, { dir, names: [key.split('/').pop()] });
      }
      toast('删除成功', 'success');
    },
    refresh: 'netdisk',
  });

  registerCommand({
    id: 'netdisk-detail',
    label: '详情',
    icon: 'INFO',
    needsSingle: true,
    async run(ctx) {
      const row = ctx.rows[0] || {};
      await detailEx('网盘文件', [
        { label: '名称', value: row.name || '-' },
        { label: '路径', value: row.remote_path || '-' },
        { label: '大小', value: formatSize(row.size) },
        { label: '类型', value: row.type || '-' },
        { label: '标记', value: row.tags || '-' },
      ]);
    },
    keepSelection: true,
  });
}