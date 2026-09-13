/**
 * Command definitions - netdisk domain .
 *
 * Commands: direct link, download, rename, tags, delete, distribute.
 * No transfer-in command: netdisk -> group files is covered by
 * distribute with target=group.
 * Register via registerAllNetdiskCommands() from the netdisk view.
 *
 * @module features/command-defs-netdisk
 */

import { registerCommand } from './commands.js';
import { API, apiGet, apiPost } from '../api.js';
import { getState } from '../store.js';
import { promptEx, detailEx, showFormModal } from '../components/modal.js';
import { toast } from '../components/toast.js';
import { copyToClipboard, formatSize, openExternal } from '../utils/helpers.js';
import { netdiskRename, netdiskRemovePaths, netdiskMovePaths, netdiskCopyPaths } from './netdisk-ops.js';

/** Register every netdisk command. */
export function registerAllNetdiskCommands() {

  registerCommand({
    id: 'netdisk-link',
    label: '复制直链',
    icon: 'LINK',
    needsSingle: true,
    async run(ctx) {
      const d = await apiPost(API.BRIDGE.NETDISK_LINK, { path: ctx.keys[0] });
      await copyToClipboard(d.url || '');
      toast('网盘直链已复制', 'success');
    },
    keepSelection: true,
  });

  registerCommand({
    id: 'netdisk-download',
    label: '浏览器打开直链',
    icon: 'DOWNLOAD',
    needsSingle: true,
    async run(ctx) {
      const d = await apiPost(API.BRIDGE.NETDISK_LINK, { path: ctx.keys[0] });
      await openExternal(d?.url || '');
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
      if (!name) return false;
      await netdiskRename(ctx.keys[0], name);
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
      if (res === null) return false;
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
      const { done, failed } = await netdiskRemovePaths(ctx.keys);
      if (failed.length) {
        throw new Error(`成功 ${done}，失败 ${failed.length}：${String(failed[0]).slice(0, 80)}`);
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

  /** Shared move/copy flow: ask for the destination, run grouped calls. */
  async function moveCopy(ctx, label, runPaths) {
    const { netdiskPath } = getState();
    const dst = await promptEx(label, `将 ${ctx.keys.length} 项移动/复制到目标目录`,
      { value: netdiskPath || '/', placeholder: '/smb/...' });
    if (!dst || dst === null) return false;
    const { done, failed, cross = 0 } = await runPaths(ctx.keys, dst);
    if (failed.length) {
      throw new Error(`成功 ${done}，失败 ${failed.length}：${String(failed[0]).slice(0, 80)}`);
    }
    toast(`${label}成功（${done} 项${cross ? `，含 ${cross} 组跨存储后台任务` : ''}）`, 'success');
    return true;
  }

  registerCommand({
    id: 'netdisk-move',
    label: '移动到…',
    icon: 'FOLDER',
    async run(ctx) {
      return moveCopy(ctx, '移动', netdiskMovePaths);
    },
    refresh: 'netdisk',
  });

  registerCommand({
    id: 'netdisk-copy',
    label: '复制到…',
    icon: 'COPY',
    async run(ctx) {
      return moveCopy(ctx, '复制', netdiskCopyPaths);
    },
    refresh: 'netdisk',
  });

  registerCommand({
    id: 'netdisk-rename-batch',
    label: '批量改名',
    icon: 'EDIT',
    async run(ctx) {
      const res = await showFormModal('批量改名（查找替换）', [
        { name: 'find', label: '查找字符串', required: true },
        { name: 'replace', label: '替换为（可留空）' },
      ]);
      if (!res?.find) return false;
      const renames = ctx.keys.map((p) => {
        const clean = p.replace(/\/+$/, '');
        const name = clean.split('/').pop() || '';
        return { path: clean, name: name.split(res.find).join(res.replace || '') };
      }).filter((r) => r.name && r.name !== r.path.split('/').pop());
      if (!renames.length) {
        toast('没有名称包含查找字符串的选中项', 'warn');
        return false;
      }
      const { ok, errors } = await apiPost(API.BRIDGE.RENAME_BATCH, { renames });
      if (!ok && errors?.length) {
        throw new Error(`部分失败：${String(errors[0]).slice(0, 80)}`);
      }
      toast(`批量改名完成（${renames.length} 项）`, 'success');
      return true;
    },
    refresh: 'netdisk',
  });
}