/**
 * Distribute commands - target distribution (files/netdisk/album/essence).
 *
 * The four "distribute" commands share one shape: pick a target from the
 * shared canonical target table (features/download-targets.js), submit the
 * module-specific payload, and surface direct links (local) or full text
 * (copy). They differ only in target options and payload extraction, so
 * one factory builds all four.
 *
 * @module features/distribute
 */

import { registerCommand } from './commands.js';
import { rowGroupFor } from '../utils/group.js';
import { API, apiPost } from '../api.js';
import { showFormModal } from '../components/modal.js';
import { toast } from '../components/toast.js';
import { copyToClipboard } from '../utils/helpers.js';
import { targetOptions, targetLabel } from './download-targets.js';
import { showDownloadAddress } from './download.js';

/**
 * Distribution command factory.
 * @param {Object} spec - {id, contextLabel, targets, endpoint, payload, refresh}
 */
function makeDistribute(spec) {
  registerCommand({
    id: spec.id,
    label: '转存',
    icon: 'SHARE',
    needsSingle: true,
    async run(ctx) {
      const options = targetOptions(spec.targets);
      const res = await showFormModal(`${spec.contextLabel}：选择目标`, [
        { name: 'target', label: '目标', type: 'select', value: 'local', options },
      ]);
      const target = res ? res.target : '';
      if (!target) { toast('已取消', 'info'); return; }
      try {
        const out = await apiPost(spec.endpoint, spec.payload(ctx, target));
        if (out.target === 'local') {
          // Local direct-link service: when SFTP/SMB lines are present show
          // the full address modal; HTTP-only results copy straight away.
          if (out.sftp || out.smb) {
            const copied = await showDownloadAddress(out);
            if (copied) toast('HTTP 地址已复制', 'success');
          } else {
            await copyToClipboard(out.http_url || '');
            toast('直链已复制（HTTP）', 'success');
          }
        } else if (out.target === 'copy') {
          await copyToClipboard(out.text || '');
          toast('全文已复制', 'success');
        } else {
          toast(`转存到${targetLabel(target).replace(/^转存到/, '')}任务已提交，可在任务页查看进度`, 'success');
        }
      } catch (e) {
        toast(`操作失败: ${e.message || ''}`, 'error');
      }
    },
    refresh: spec.refresh || ['files', 'bridge', 'tasks'],
  });
}

/** Register the cross-module distribution commands.
 *
 * files-distribute moved to features/download.js (one download executor
 * with internal target subdivision; netdisk transfer is a download form).
 * The three remaining instances are cross-domain transfers (netdisk /
 * album / essence sources) that still share this factory shape.
 */
export function registerDistributeCommands() {
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
        group: rowGroupFor(ctx.state, row, 'album') || ctx.state.currentGroup || '',
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
        group: rowGroupFor(ctx.state, row, 'essence') || ctx.state.currentGroup || '',
        target,
      };
    },
  });
}