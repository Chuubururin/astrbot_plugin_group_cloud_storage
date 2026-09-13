/**
 * Database administration panel - wires the previously unreachable
 * database/* endpoints (health / integrity / backups / backup / restore)
 * into the config center.
 *
 * Auth: the backend is deliberately fail-closed — without
 * database_admin_token configured every request is refused (restore is
 * destructive; a single layer of panel trust is not enough). The token
 * therefore lives only in this tab's sessionStorage and travels in the
 * POST body, never in URLs or logs.
 *
 * @module features/database-admin
 */

import { API, apiPost } from '../api.js';
import { getIcon } from '../icons.js';
import { escapeHtml } from '../utils/helpers.js';
import { toast } from '../components/toast.js';
import { confirmEx } from '../components/modal.js';

const TOKEN_KEY = 'gcs_db_admin_token';

/** Database admin token lives per-tab only; never persisted to disk. */
function dbToken() {
  return sessionStorage.getItem(TOKEN_KEY) || '';
}

function withToken(extra) {
  return { token: dbToken(), ...(extra || {}) };
}

/** Run a db-admin POST; on auth failure guide to the token field. */
async function call(path, body) {
  try {
    return await apiPost(path, withToken(body));
  } catch (e) {
    if (String(e.message || e).includes('unauthorized')) {
      throw new Error('管理令牌缺失或不匹配（见「管理令牌」输入框）');
    }
    throw e;
  }
}

function fmtSize(n) {
  if (!Number.isFinite(n)) return '';
  if (n > 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  if (n > 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${n} B`;
}

/**
 * Mount the database admin section into `container`.
 * @param {HTMLElement} container
 * @returns {function} cleanup
 */
export function initDatabaseAdmin(container) {
  container.innerHTML = `
    <section class="config-group" id="db-admin">
      <h3 class="config-group-title">数据管理（SQLite 维护）</h3>
      <div class="config-items">
        <div class="config-item">
          <div class="config-item-head"><span class="config-key">管理令牌</span></div>
          <div class="config-item-desc">对应配置 database_admin_token；后端未配置该令牌时所有操作被拒绝。令牌仅保存在当前标签页会话。</div>
          <input id="db-token" type="password" class="config-input" placeholder="database_admin_token" autocomplete="off" />
        </div>
        <div class="config-item">
          <div class="config-item-head"><span class="config-key">维护操作</span></div>
          <div class="config-item-desc">健康/完整性为只读检查；备份写入 data 目录 backups/ 下；恢复将整库替换为所选备份（强确认）。</div>
          <div class="btn-group">
            <button id="db-health" class="btn-act">${getIcon('REFRESH', 12)} 健康检查</button>
            <button id="db-integrity" class="btn-act">完整性检查</button>
            <button id="db-backup" class="btn-act primary">立即备份</button>
            <button id="db-backups" class="btn-act">备份列表</button>
          </div>
        </div>
        <div class="config-item" id="db-backup-list-wrap">
          <div class="config-item-head"><span class="config-key">备份列表</span></div>
          <div id="db-backup-list" class="config-item-desc">尚未加载。</div>
        </div>
      </div>
    </section>`;

  const out = (text, cls) => {
    const el = container.querySelector('#db-backup-list');
    if (el) el.innerHTML = `<span class="${cls || ''}">${escapeHtml(text)}</span>`;
  };

  container.querySelector('#db-token').value = dbToken();
  container.querySelector('#db-token').addEventListener('change', (e) => {
    sessionStorage.setItem(TOKEN_KEY, e.target.value.trim());
  });

  container.querySelector('#db-health').addEventListener('click', async () => {
    try {
      const r = await call(API.DB_HEALTH, {});
      toast(`数据库健康: ${r.ok ? '正常' : '异常'}（${r.backend || 'sqlite'}）`, r.ok ? 'success' : 'error');
    } catch (e) { toast(`健康检查失败: ${e.message || e}`, 'error'); }
  });

  container.querySelector('#db-integrity').addEventListener('click', async () => {
    try {
      const r = await call(API.DB_INTEGRITY, {});
      toast(`完整性: ${r.ok === false ? '异常' : (r.result || '正常')}`, r.ok === false ? 'error' : 'success');
    } catch (e) { toast(`完整性检查失败: ${e.message || e}`, 'error'); }
  });

  container.querySelector('#db-backup').addEventListener('click', async () => {
    try {
      const r = await call(API.DB_BACKUP, {});
      toast(`备份完成: ${r.path || r.destination || '已写入 backups/'}`, 'success');
    } catch (e) { toast(`备份失败: ${e.message || e}`, 'error'); }
  });

  const loadBackups = async () => {
    try {
      const r = await call(API.DB_BACKUPS, {});
      const list = r.backups || [];
      const el = container.querySelector('#db-backup-list');
      if (!el) return;
      if (!list.length) { el.textContent = '暂无备份。'; return; }
      el.innerHTML = '';
      for (const b of list) {
        const row = document.createElement('div');
        row.className = 'config-item-desc';
        row.innerHTML = `<span>${escapeHtml(b.name)}（${fmtSize(b.size)}）</span> `;
        const btn = document.createElement('button');
        btn.className = 'btn-act';
        btn.textContent = '恢复';
        btn.addEventListener('click', async () => {
          const ok = await confirmEx('恢复数据库',
            `将用备份「${b.name}」整体替换当前数据库，未备份的新数据将丢失且不可恢复。确认恢复？`,
            { okText: '恢复', danger: true });
          if (!ok) return;
          try {
            await call(API.DB_RESTORE, { source: b.name });
            toast('恢复已完成，建议重载插件使内存状态与库一致', 'success');
          } catch (e2) { toast(`恢复失败: ${e2.message || e2}`, 'error'); }
        });
        row.appendChild(btn);
        el.appendChild(row);
      }
    } catch (e) { out(`备份列表加载失败: ${e.message || e}`); }
  };

  container.querySelector('#db-backups').addEventListener('click', loadBackups);

  return () => {};
}
