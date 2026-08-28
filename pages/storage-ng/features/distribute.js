/**
 * Distribute commands - W2-A target distribution (files/netdisk/album/essence).
 *
 * The four "distribute" commands share one shape: pick a target from the
 * whitelisted options, submit the module-specific payload, and surface
 * direct links (local) or full text (copy). They differ only in target
 * options and payload extraction, so one factory builds all four.
 *
 * SMB links are explicitly unsupported (no SMB channel, zero new ports);
 * the target list honestly omits them.
 *
 * @module features/distribute
 */

import { registerCommand, rowGroup } from './commands.js';
import { API, apiPost } from '../api.js';
import { showFormModal } from '../components/modal.js';
import { toast } from '../components/toast.js';
import { copyToClipboard } from '../utils/helpers.js';

const TARGET_OPTIONS = [
  { value: 'local', label: '下载到本地（直链）' },
  { value: 'netdisk', label: '下载到网盘' },
  { value: 'album', label: '下载到相册' },
  { value: 'essence', label: '下载到精华' },
  { value: 'group', label: '下载到群文件' },
  { value: 'copy', label: '浏览器复制（文本）' },
];

/**
 * Distribution command factory.
 * @param {Object} spec - {id, contextLabel, targets, endpoint, payload, refresh}
 */
function makeDistribute(spec) {
  registerCommand({
    id: spec.id,
    label: '分发',
    icon: 'SHARE',
    needsSingle: true,
    async run(ctx) {
      const options = TARGET_OPTIONS.filter((t) => spec.targets.includes(t.value));
      const res = await showFormModal(`${spec.contextLabel}：目标分发`, [
        { name: 'target', label: '分发目标', type: 'select', value: 'local', options },
      ]);
      const target = res ? res.target : '';
      if (!target) { toast('已取消', 'info'); return; }
      try {
        const out = await apiPost(spec.endpoint, spec.payload(ctx, target));
        if (out.target === 'local' || out.target === 'copy') {
          await copyToClipboard(out.http_url || out.text || '');
          toast(out.target === 'copy' ? '全文已复制' : '直链已复制（HTTP）', 'success');
        } else {
          toast('分发任务已提交', 'success');
        }
      } catch (e) {
        toast(`分发失败: ${e.message || ''}`, 'error');
      }
    },
    refresh: spec.refresh || ['files', 'bridge', 'tasks'],
  });
}

/** Register the four module distribution commands. */
export function registerDistributeCommands() {
  makeDistribute({
    id: 'files-distribute',
    contextLabel: '文件下载',
    targets: ['local', 'netdisk', 'album', 'essence'],
    endpoint: API.FILES.DISTRIBUTE,
    payload: (ctx, target) => ({
      id: Number(ctx.keys[0]), group: rowGroup(ctx.state, ctx.rows[0]), target,
    }),
  });

  makeDistribute({
    id: 'netdisk-distribute',
    contextLabel: '网盘下载',
    targets: ['local', 'group', 'album', 'essence'],
    endpoint: API.BRIDGE.NETDISK_DISTRIBUTE,
    payload: (ctx, target) => ({
      path: ctx.keys[0], target,
      group: ctx.state.currentGroup || '',
      name: ctx.rows[0]?.name || '',
    }),
  });

  makeDistribute({
    id: 'album-distribute',
    contextLabel: '相册下载',
    targets: ['local', 'netdisk', 'group', 'essence'],
    endpoint: API.ALBUMS.DISTRIBUTE,
    payload: (ctx, target) => {
      const row = ctx.rows[0] || {};
      const albumId = row.album_id || (row.meta && row.meta.album_id) || '';
      if (!albumId) throw new Error('缺少相册 ID');
      return {
        album_id: albumId, name: row.name || '', target,
        group: row.group_id || ctx.state.albumGroup || ctx.state.currentGroup || '',
      };
    },
  });

  makeDistribute({
    id: 'essence-distribute',
    contextLabel: '精华下载',
    targets: ['local', 'copy', 'netdisk', 'group', 'album'],
    endpoint: API.ESSENCE.DISTRIBUTE,
    refresh: ['essence', 'bridge', 'files', 'tasks'],
    payload: (ctx, target) => {
      const row = ctx.rows[0] || {};
      return {
        id: Number(ctx.keys[0]),
        group: row.group_id || ctx.state.essenceGroup || ctx.state.currentGroup || '',
        target,
      };
    },
  });
}