/**
 * Album domain commands - create / gallery / detail on album rows.
 *
 * Album rows are albums (not media); media-level actions live inside the
 * gallery opened by album-gallery (double-click opens the same path).
 *
 * @module features/command-defs-albums
 */

import { registerCommand } from './commands.js';
import { rowGroupFor } from '../utils/group.js';
import { API, apiGet, apiPost } from '../api.js';
import { detailEx, showFormModal } from '../components/modal.js';
import { toast } from '../components/toast.js';
import { openPreview } from './preview.js';
import { refresh } from '../store.js';

/** Register the album domain commands. */
export function registerAllAlbumCommands() {
  registerCommand({
    id: 'album-create',
    label: '创建相册',
    icon: 'PLUS',
    allowNoSelection: true,
    async run(ctx) {
      const group = ctx.state.albumGroup || ctx.rows[0]?.group_id || ctx.state.currentGroup || '';
      if (!group) { toast('请先在上方选择群', 'warn'); return; }
      const res = await showFormModal('创建群相册', [
        { name: 'album_name', label: '相册名称', required: true, placeholder: '如 AstrBot云盘' },
        { name: 'album_desc', label: '相册描述（可选）' },
      ]);
      if (!res?.album_name?.trim()) return false;
      try {
        await apiPost(API.ALBUMS.CREATE, {
          group, album_name: res.album_name.trim(), album_desc: res.album_desc || '',
        });
        toast('相册创建成功', 'success');
        refresh('albums');
      } catch (e) {
        toast(`创建失败: ${e.message || e}`, 'error');
      }
    },
    refresh: ['albums'],
  });

  registerCommand({
    id: 'album-gallery',
    label: '查看媒体',
    icon: 'IMAGE',
    needsSingle: true,
    async run(ctx) {
      await openPreview(ctx.rows[0], 'album');
    },
    keepSelection: true,
  });

  registerCommand({
    id: 'album-detail',
    label: '相册详情',
    icon: 'INFO',
    needsSingle: true,
    async run(ctx) {
      const row = ctx.rows[0] || {};
      // Live detail merges the stored meta with the current cloud album
      // entry (accurate media count, self-healed album ID); when the call
      // fails the modal degrades to the fields carried by the listing row.
      let d = {};
      try {
        d = await apiGet(API.ALBUMS.DETAIL, {
          id: row.id, group: rowGroupFor(ctx.state, row, 'album'),
        });
      } catch (e) { toast(`详情获取失败: ${e.message || e}`, 'warn'); }
      await detailEx('相册详情', [
        { label: '名称', value: d.name || row.name || '-' },
        { label: '描述', value: d.desc || '-' },
        { label: '媒体数', value: String(d.media_count ?? '-') },
        { label: '所属群', value: d.group_id || row.group_id || ctx.state.albumGroup || '-' },
        { label: '相册 ID', value: d.album_id || row.album_id || '-' },
        ...(d.cover_url ? [{ label: '封面', value: d.cover_url }] : []),
      ]);
    },
    keepSelection: true,
  });
}
