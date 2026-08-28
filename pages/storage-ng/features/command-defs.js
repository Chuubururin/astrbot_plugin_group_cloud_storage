/**
 * Command definitions - group-file and distribute domains (merged from
 * the previous four split modules).
 *
 * All operations here are OneBot11-API composites exposed by the backend
 * (see webapi.py); the frontend only declares the UX contract. The four
 * "distribute" commands share one factory because they differ only in
 * target options and payload extraction (W2-A target distribution).
 *
 * @module features/command-defs
 */

import { registerCommand, rowGroup } from './commands.js';
import { API, apiGet, apiPost, download } from '../api.js';
import { confirmEx, promptEx, detailEx, showFormModal } from '../components/modal.js';
import { toast } from '../components/toast.js';
import { formatSize, copyToClipboard } from '../utils/helpers.js';
import { pickFolder } from './folder-picker.js';

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
      const group = rowGroup(ctx.state, ctx.rows[0]);
      const volumes = ctx.rows.filter((f) => f.is_volume);
      if (volumes.length > 0) {
        const ok = await confirmEx('分卷下载（重组校验）',
          `选中含 ${volumes.length} 个分卷文件。下载将自动：拉取全部分卷 / 逐卷 SHA-256 校验 / 按序合并重组。`,
          { okText: '开始下载' });
        if (!ok) return;
      }
      for (const f of ctx.rows) {
        await download(API.FILES.DOWNLOAD, { id: f.id, group }, f.name || 'download');
      }
      toast('下载已开始', 'success');
    },
    refresh: 'files',
  });

  registerCommand({
    id: 'link',
    label: '直链',
    icon: 'LINK',
    async run(ctx) {
      if (ctx.keys.length > 20) { toast('最多 20 项', 'warn'); return; }
      const data = await apiPost(API.FILES.LINKS, {
        items: ctx.rows.map((f) => ({ id: Number(f.id), group: rowGroup(ctx.state, f) })),
      });
      if (data?.links) {
        await copyToClipboard(data.links.map((l) => l.url || l).join('\n'));
        toast('直链已复制', 'success');
      }
    },
    keepSelection: true,
  });

  registerCommand({
    id: 'address',
    label: '下载地址',
    icon: 'LINK',
    needsSingle: true,
    async run(ctx) {
      const d = await apiGet(API.FILES.ADDRESS, { group: rowGroup(ctx.state, ctx.rows[0]), id: ctx.keys[0] });
      const lines = [
        `HTTP：${d.http_url || '-'}`,
        d.ftp
          ? `FTP：ftp://${d.ftp.user}:${d.ftp.password}@${d.ftp.host}:${d.ftp.port}${d.ftp.path}`
          : 'FTP：未开启',
        d.note || '',
      ].join('\n');
      const res = await showFormModal('本机下载服务地址', [
        { name: 'addr', label: '地址', type: 'textarea', rows: 5, value: lines },
      ], { okText: '复制 HTTP 地址' });
      if (res) {
        await copyToClipboard(d.http_url || '');
        toast('HTTP 地址已复制', 'success');
      }
    },
    keepSelection: true,
  });

  registerCommand({
    id: 'bridge-out',
    label: '转存网盘',
    icon: 'CLOUD',
    needsGroup: true,
    confirm: (count) => `确定将 ${count} 个文件转存到 OpenList 网盘？`,
    async run(ctx) {
      for (const f of ctx.rows) {
        await apiPost(API.BRIDGE.TRANSFER, { resource_ids: [Number(f.id)], group: rowGroup(ctx.state, f) });
      }
      toast('转存任务已提交', 'success');
    },
    refresh: 'bridge',
  });

  registerCommand({
    id: 'move',
    label: '移动',
    icon: 'MOVE',
    needsGroup: true,
    async run(ctx) {
      const target = await pickFolder(rowGroup(ctx.state, ctx.rows[0]));
      if (!target) return;
      for (const f of ctx.rows) {
        await apiPost(API.FILES.MOVE, {
          ids: [Number(f.id)], group: rowGroup(ctx.state, f), folder_id: target.id || '/',
        });
      }
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
      if (name === null || name === ctx.rows[0].name) return;
      // OneBot has no rename action: rename = replace upload with the new name.
      await apiPost(API.FILES.REPLACE_NAME, {
        id: Number(ctx.keys[0]), group: rowGroup(ctx.state, ctx.rows[0]), name,
      });
      toast('改名重传已入队', 'success');
    },
    refresh: ['files'],
  });

  registerCommand({
    id: 'volumes',
    label: '转分卷',
    icon: 'FILES',
    needsSingle: true,
    async run(ctx) {
      // 2026-09-03 C-4：可选 zip 压缩（可逆；下载重组自动解压）+ 拆分态确认
      const res = await showFormModal('转分卷', [
        { name: 'compress', label: '压缩', type: 'select', value: 'no', options: [
          { value: 'no', label: '不压缩（原样切卷）' },
          { value: 'yes', label: '压缩为 ZIP（下载重组后自动解压）' },
        ] },
      ]);
      if (!res) return;
      const ok = await confirmEx('转分卷',
        '云端大文件 → 下载 → 切分 → 逐卷上传（SHA-256 校验卷） → 删原件。' +
        (res.compress === 'yes' ? '压缩分卷将先打包 ZIP 再切卷，下载重组自动还原。' : ''),
        { okText: '开始转分卷' });
      if (!ok) return;
      await apiPost(API.FILES.CONVERT_VOLUMES, {
        id: Number(ctx.keys[0]), group: rowGroup(ctx.state, ctx.rows[0]),
        compress: res.compress === 'yes',
      });
      toast('转分卷任务已提交', 'success');
    },
    refresh: ['files', 'tasks'],
  });

  registerCommand({
    id: 'tags',
    label: '标签',
    icon: 'CHECK',
    async run(ctx) {
      const res = await promptEx('设置标签', '输入标签（逗号分隔，最多 10 个）');
      if (res === null) return;
      const tags = res.split(',').map((t) => t.trim()).filter(Boolean).slice(0, 10);
      await apiPost(API.FILES.BATCH_TAGS, {
        ids: ctx.rows.map((f) => Number(f.id)), group: rowGroup(ctx.state, ctx.rows[0]), tags,
      });
      toast('标签设置成功', 'success');
    },
    refresh: ['files'],
  });

  registerCommand({
    id: 'verify',
    label: '校验',
    icon: 'CHECK',
    needsSingle: true,
    async run(ctx) {
      const d = await apiGet(API.FILES.VERIFY, { group: rowGroup(ctx.state, ctx.rows[0]), id: ctx.keys[0] });
      await detailEx('完整性校验', [
        { label: '结果', value: d.ok || d.valid ? '校验通过' : '校验失败' },
        { label: '说明', value: d.detail || d.message || '-' },
      ]);
    },
    keepSelection: true,
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
    id: 'essence-delete',
    label: 'Delete',
    icon: 'DELETE',
    danger: true,
    needsGroup: true,
    confirm: (count) => `Delete ${count} essence message(s)?`,
    async run(ctx) {
      for (const f of ctx.rows) {
        await apiPost(API.ESSENCE.DELETE, { group: rowGroup(ctx.state, f), id: Number(f.id) });
      }
      toast('Essence delete queued', 'success');
    },
    refresh: ['files', 'essence'],
  });
}
