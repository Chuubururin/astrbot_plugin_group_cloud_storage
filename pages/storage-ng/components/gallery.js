/**
 * Gallery - album media viewer (F13/FE-15) + video keyframe GIFs (F14).
 *
 * Images mount lazily in batches via IntersectionObserver (media only
 * loads when scrolled into view). Video items have no cloud preview
 * link, so a button generates a keyframe GIF through the backend
 * (albums/video-preview) and swaps the placeholder for the GIF frame.
 *
 * @module components/gallery
 */

import { API, apiPost } from '../api.js';
import { escapeHtml } from '../utils/helpers.js';
import { toast } from './toast.js';

const LAZY_BATCH = 12;   // items eagerly mounted before scrolling takes over

let overlay = null;
let observer = null;

function ensure() {
  if (overlay) return;
  overlay = document.createElement('div');
  overlay.className = 'modal-overlay hidden';
  overlay.innerHTML = `
    <div class="modal-box modal-wide">
      <div class="modal-title"></div>
      <div class="gallery-grid"></div>
      <div class="modal-actions">
        <button class="modal-ok primary">关闭</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  const close = () => {
    overlay.classList.add('hidden');
    if (observer) { observer.disconnect(); observer = null; }
  };
  overlay.querySelector('.modal-ok').addEventListener('click', close);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
}

/** F14: request a keyframe GIF from the backend and show it. */
async function generateKeyframe(holder, item, ctx) {
  const btn = holder.querySelector('.gallery-vid-btn');
  if (btn) { btn.disabled = true; btn.textContent = '生成中...'; }
  try {
    const r = await apiPost(API.ALBUMS.VIDEO_PREVIEW, {
      group: ctx.group,
      album_id: ctx.albumId,
      name: item.name,
    });
    if (r && r.gif_base64) {
      const img = document.createElement('img');
      img.className = 'gallery-img';
      img.src = 'data:image/gif;base64,' + r.gif_base64;
      img.alt = item.name || '';
      holder.replaceChildren(img);
    } else {
      fail();
      toast('关键帧生成失败', 'error');
    }
  } catch (e) {
    fail();
    toast('关键帧生成失败', 'error');
  }
  function fail() {
    if (btn) { btn.disabled = false; btn.textContent = '生成关键帧预览'; }
  }
}

function mountItem(holder, item) {
  if (item && item.url) {
    const img = document.createElement('img');
    img.className = 'gallery-img';
    img.src = item.url;
    img.alt = item.name || '';
    img.loading = 'lazy';
    holder.replaceChildren(img);
  } else {
    holder.textContent = item ? (item.name || '(无预览)') : '';
  }
}

/** Mount items lazily: first batch eagerly, the rest on scroll (FE-15). */
function mountLazily(items, grid, ctx) {
  observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      mountItem(entry.target, entry.target.__item);
      observer.unobserve(entry.target);
    }
  }, { root: grid, rootMargin: '200px' });

  const holders = [];
  for (const item of items) {
    const holder = document.createElement('div');
    holder.className = 'gallery-item';
    holder.__item = item;
    if (item.is_video) {
      holder.innerHTML =
        `<span class="gallery-hint">[视频] ${escapeHtml(item.name || '')}</span>` +
        `<button class="gallery-vid-btn">生成关键帧预览</button>`;
      holder.querySelector('.gallery-vid-btn')?.addEventListener('click', (e) => {
        e.stopPropagation();
        generateKeyframe(holder, item, ctx || {});
      });
    } else {
      holder.innerHTML = `<span class="gallery-hint">${escapeHtml(item.name || '')}</span>`;
    }
    grid.appendChild(holder);
    holders.push(holder);
  }
  holders.slice(0, LAZY_BATCH).forEach((h) => {
    mountItem(h, h.__item);
    observer.unobserve(h);
  });
  holders.slice(LAZY_BATCH).forEach((h) => observer.observe(h));
}

/**
 * Open the album media gallery.
 * @param {string} name - album/resource name
 * @param {Array<{url?: string, name?: string, is_video?: boolean}>} items
 * @param {{group: string, albumId: string}} [ctx] - context for GIF generation
 */
export function showGallery(name, items, ctx) {
  ensure();
  overlay.querySelector('.modal-title').textContent =
    `相册媒体: ${name} (${(items || []).length})`;
  const grid = overlay.querySelector('.gallery-grid');
  grid.innerHTML = '';
  if (!items || items.length === 0) {
    grid.innerHTML = '<div class="empty-hint">无媒体</div>';
    overlay.classList.remove('hidden');
    return;
  }
  mountLazily(items, grid, ctx);
  overlay.classList.remove('hidden');
}