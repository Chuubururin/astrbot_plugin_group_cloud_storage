/**
 * Download executor - the single code path behind every download form.
 *
 * User model: 下载 is one user operation with internal target subdivision
 * (本地 / 直链 / 本机下载服务地址 / 网盘 / 相册 / 精华); 转存到网盘 is
 * download's special form (target=netdisk -> batched bridge/transfer), not
 * a parallel pipeline. Mirrors the uploadOnce contract: IO is injectable
 * ({apiPost, apiGet, download, copy}) so unit tests drive it with stubs;
 * the executor returns a structured result and never toasts - the command
 * layer owns toasts/refresh/selection.
 *
 * @module features/download
 */

import { API, apiGet as _apiGet, apiPost as _apiPost, download as sdkDownload } from '../api.js';
import { copyToClipboard, runEachWithFailures } from '../utils/helpers.js';
import { rowGroup } from '../utils/group.js';
import { showFormModal } from '../components/modal.js';
import { targetOptions as _targetOptions } from './download-targets.js';

/** Target options (canonical table, order = modal order). */
export const DOWNLOAD_TARGET_OPTIONS = _targetOptions();

/**
 * Target-select modal (default 本地). Resolves '' on cancel.
 * @param {string[]} [allowed] - subset of target values (default all)
 * @returns {Promise<string>}
 */
export async function promptDownloadTarget(allowed) {
  const options = _targetOptions(allowed);
  const res = await showFormModal('下载：选择目标', [
    { name: 'target', label: '目标', type: 'select', value: 'local', options },
  ]);
  return res ? res.target : '';
}

/**
 * Download-service address modal (HTTP/SFTP/SMB lines + copy HTTP on OK).
 * @param {Object} d - files/address response ({http_url, sftp?, note?})
 */
export async function showDownloadAddress(d) {
  const lines = [
    `HTTP：${d.http_url || '-'}`,
    d.sftp
      ? `SFTP：sftp://${d.sftp.user}:${d.sftp.password}@${d.sftp.host}:${d.sftp.port}${d.sftp.path}`
      : 'SFTP：未开启',
    d.smb
      ? `SMB：${d.smb.unc || ''}`
      : (d.smb_notice || 'SMB：未开启'),
    d.note || '',
  ].join('\n');
  const res = await showFormModal('本机下载服务地址', [
    { name: 'addr', label: '地址', type: 'textarea', rows: 5, value: lines },
  ], { okText: '复制 HTTP 地址' });
  if (res) {
    await copyToClipboard(d.http_url || '');
    return true;
  }
  return false;
}

/**
 * Execute a download for the selection.
 *
 * @param {Object} ctx - command context {rows, state}
 * @param {string} target - one of DOWNLOAD_TARGET_OPTIONS values
 * @param {Object} [opts]
 * @param {Object} [opts.deps] - injectable IO {apiPost, apiGet, download, copy}
 * @param {(count: number) => Promise<boolean>} [opts.confirmVolumes] -
 *   volume confirmation for local downloads
 * @returns {Promise<{ok: boolean, done: number, failed: string[],
 *   cancelled?: boolean, copied?: string|null, address?: Object}>}
 *   Transport errors propagate (throws) like uploadOnce.
 */
export async function downloadItems(ctx, target, opts = {}) {
  const deps = opts.deps || {};
  const apiPost = deps.apiPost || _apiPost;
  const apiGet = deps.apiGet || _apiGet;
  const download = deps.download || sdkDownload;
  const copy = deps.copy || copyToClipboard;
  const rows = ctx.rows || [];
  const state = ctx.state || {};

  switch (target) {
    case 'local': {
      // 分卷：一次确认，重组校验由后端内置完成；不完整分卷单独确认，
      // 走 allow_incomplete（跳过总哈希，重组可用分片）。
      const volumes = rows.filter((f) => f.is_volume);
      const incomplete = rows.filter(
        (f) => f.volume_total && !f.volume_complete,
      );
      const completeVolumes = volumes.length - incomplete.length;
      if (completeVolumes > 0 && opts.confirmVolumes) {
        const ok = await opts.confirmVolumes(completeVolumes);
        if (!ok) return { ok: false, done: 0, failed: [], cancelled: true };
      }
      const r = await runEachWithFailures(rows, (f) => {
        const params = { id: f.id, group: rowGroup(state, f) };
        if (f.volume_total && !f.volume_complete) {
          params.allow_incomplete = 1;
        }
        return download(
          API.FILES.DOWNLOAD, params, f.name || 'download');
      });
      return { ok: r.failed.length === 0, ...r };
    }

    case 'link': {
      if (rows.length > 20) return { ok: false, done: 0, failed: ['直链单次最多 20 项'] };
      const items = rows.map((f) => ({ id: Number(f.id), group: rowGroup(state, f) }));
      const data = await apiPost(API.FILES.LINKS, { items });
      const links = (data && data.links) || [];
      const errors = (data && data.errors) || [];
      const text = links.map((l) => (l && l.url) || l).join('\n');
      if (text) await copy(text);
      return { ok: links.length > 0, done: links.length, failed: errors, copied: text || null };
    }

    case 'address': {
      if (rows.length !== 1) return { ok: false, done: 0, failed: ['下载地址需选择单个文件'] };
      const d = await apiGet(API.FILES.ADDRESS, {
        group: rowGroup(state, rows[0]), id: rows[0].id,
      });
      return { ok: true, done: 1, failed: [], address: d };
    }

    case 'netdisk': {
      // 转存 = 下载的特殊形式：按群归并，每组一次批量提交
      const byGroup = new Map();
      for (const f of rows) {
        const g = rowGroup(state, f);
        if (!byGroup.has(g)) byGroup.set(g, []);
        byGroup.get(g).push(Number(f.id));
      }
      const results = [];
      const failed = [];
      for (const [g, ids] of byGroup) {
        try {
          const r = await apiPost(API.BRIDGE.TRANSFER, { group: g, resource_ids: ids });
          for (const it of (r && r.results) || []) results.push(it);
          for (const e of (r && r.errors) || []) failed.push(String(e));
        } catch (e) {
          failed.push(`群 ${g}: ${(e && e.message) || e}`);
        }
      }
      return { ok: results.length > 0 && failed.length === 0, done: results.length, failed };
    }

    case 'album':
    case 'essence': {
      // 分卷资源没有单一源文件：显式失败，指引「下载」（自动重组）
      const r = await runEachWithFailures(rows, async (f) => {
        if (f.is_volume) throw new Error('分卷资源请使用「下载」（自动重组）');
        await apiPost(API.FILES.DISTRIBUTE, {
          id: Number(f.id), group: rowGroup(state, f), target,
        });
      });
      return { ok: r.failed.length === 0 && r.done > 0, ...r };
    }

    default:
      throw new Error(`unknown download target: ${target}`);
  }
}
