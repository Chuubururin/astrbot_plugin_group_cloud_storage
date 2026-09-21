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
      const url = d?.url || '';
      if (!url) { toast('未获取到直链', 'warn'); return false; }
      const ok = await copyToClipboard(url);
      toast(ok ? '网盘直链已复制' : '复制失败，请手动复制', ok ? 'success' : 'error');
      return ok;
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
      if (!d?.url) { toast('未获取到直链', 'warn'); return false; }
      await openExternal(d.url);
    },
    keepSelection: true,
  });

  registerCommand({
    id: 'netdisk-rename',
    label: '改名',
    icon: 'EDIT',
    needsSingle: true,
    async run(ctx) {
      const current = ctx.rows[0]?.name || '';
      const name = await promptEx('重命名', `当前: ${current || ctx.keys[0]}`, { value: current });
      if (name === null) return false; // 取消
      const next = name.trim();
      if (!next) { toast('名称不能为空', 'warn'); return false; }
      if (next === current) return false; // 未修改
      await netdiskRename(ctx.keys[0], next);
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
      const res = await promptEx('设置标记', '输入标记（逗号分隔，最多 10 个）',
        { value: ctx.rows[0]?.tags || '' });
      if (res === null) return false;
      const tags = res.split(',').map((t) => t.trim()).filter(Boolean);
      if (tags.length > 10) { toast('标记最多 10 个，请精简后重试', 'warn'); return false; }
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
    confirm: (count) => `确定删除 ${count} 个网盘文件？此操作不可撤销。`,
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
    if (dst === null) return false; // 取消
    const target = dst.trim();
    if (!target) { toast('目标目录不能为空', 'warn'); return false; }
    const { done, failed, cross = 0 } = await runPaths(ctx.keys, target);
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
    // Move is irreversible at the source path; the destination directory is
    // picked in the next step (the confirm dialog only knows the count).
    confirm: (count) => `确定移动 ${count} 项？目标目录将在下一步选择，移动后原位置不再保留。`,
    async run(ctx) {
      return moveCopy(ctx, '移动', netdiskMovePaths);
    },
    refresh: 'netdisk',
  });

  registerCommand({
    id: 'netdisk-copy',
    label: '复制到…',
    icon: 'COPY',
    confirm: (count) => `确定复制 ${count} 项？目标目录将在下一步选择，复制会额外占用网盘空间。`,
    async run(ctx) {
      return moveCopy(ctx, '复制', netdiskCopyPaths);
    },
    refresh: 'netdisk',
  });

  registerCommand({
    id: 'netdisk-rename-batch',
    label: '批量改名',
    icon: 'EDIT',
    confirm: (count) => `将对选中的 ${count} 项执行查找替换改名，确定继续？`,
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
      const r = await apiPost(API.BRIDGE.RENAME_BATCH, { renames });
      // `errors` is the authoritative failure signal: `ok` may be absent and
      // may even be true alongside errors, which used to swallow them.
      const errors = (r && r.errors) || [];
      if (errors.length) {
        const done = Math.max(renames.length - errors.length, 0);
        throw new Error(`成功 ${done}，失败 ${errors.length}：${String(errors[0]).slice(0, 80)}`);
      }
      toast(`批量改名完成（${renames.length} 项）`, 'success');
      return true;
    },
    refresh: 'netdisk',
  });
}