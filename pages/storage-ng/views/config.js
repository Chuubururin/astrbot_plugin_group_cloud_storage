/**
 * Config view  - grouped configuration center (categorized, convenience first).
 *
 * Fetches config/get (grouped schema + current values), renders by
 * category with a search filter, marks reload-required keys, tracks
 * dirty items, and saves the changed subset via config/save. The 13-class
 * type-extension table (type_ext_overrides) is editable right here
 * as a normal config item - changing the table changes classification
 * without code changes .
 *
 * @module views/config
 */

import { set, subscribe } from '../store.js';
import { API, apiGet, apiPost } from '../api.js';
import { getIcon } from '../icons.js';
import { escapeHtml, debounce } from '../utils/helpers.js';
import { toast } from '../components/toast.js';
import { confirmEx } from '../components/modal.js';
import { invalidatePolicyCache } from '../features/preview.js';
import { initDatabaseAdmin } from '../features/database-admin.js';

/**
 * 危险项/凭据项：保存前必须二次确认（需求简报「配置」：便利优先——搜索、
 * 变更即时生效提示、**危险项二次确认**）。
 *
 * 来源：后端 `config/get`（webapi/config.py）目前只下发
 * reload_required / masked，**没有** danger 标记，因此这里维护显式集合，
 * 同时兼容后端将来下发 `item.danger === true`（见 buildItem）。
 * 集合口径 = 放开出站安全边界（SSRF 白名单）或持有凭据的键。
 */
const DANGER_KEYS = new Set([
  'fetch_allow_private_address',
  'openlist_allow_private_address',
  'database_admin_token',
  'download_token',
  'managed_groups',
]);

/** 解析失败哨兵：区别于「无值」（null 已被 JSON 的 null 值占用）。 */
const INVALID = Symbol('invalid-config-value');

/** 标红 / 取消标红一个配置项（styles/ 不可改，用内联描边保证可见）。 */
function markInvalid(el, on) {
  el.classList.toggle('invalid', on);
  el.style.borderColor = on ? 'var(--danger)' : '';
  el.style.boxShadow = on ? '0 0 0 1px var(--danger)' : '';
}

/**
 * Initialize the config view.
 * @param {HTMLElement} container
 * @returns {function} cleanup
 */
export function initConfigView(container) {
  container.innerHTML = `
    <div class="config-toolbar toolbar">
      <div class="toolbar-left">
        <input type="search" id="config-search" placeholder="搜索配置项..." />
        <button id="config-refresh" class="icon-btn" title="刷新">${getIcon('REFRESH', 14)}</button>
      </div>
      <div class="toolbar-right">
        <button id="config-save" class="primary">保存配置</button>
      </div>
    </div>
    <div id="config-groups" class="config-groups"></div>
    <div id="database-admin" class="database-admin"></div>
    <div id="config-status" class="config-status"></div>
  `;

  loadConfig();

  const unsubRefresh = subscribe('refresh:config', loadConfig);

  // Database administration section (database/* endpoints; token-gated).
  initDatabaseAdmin(container.querySelector('#database-admin'));

  // Convenience search filter : group name or item key match.
  const search = container.querySelector('#config-search');
  search?.addEventListener('input', debounce(() => {
    const q = (search.value || '').toLowerCase();
    // The key match goes into an attribute selector: strip quote/backslash
    // so a keystroke like '"' cannot throw a SyntaxError mid-filter.
    const safe = q.replace(/["\\]/g, '');
    container.querySelectorAll('.config-group').forEach((g) => {
      const name = g.dataset.groupName || '';
      let visible = !q || name.toLowerCase().includes(q);
      if (!visible && safe) visible = !!g.querySelector(`.config-item[data-key*="${safe}"]`);
      g.classList.toggle('hidden', !visible);
    });
  }, 150));

  container.querySelector('#config-refresh')?.addEventListener('click', loadConfig);
  container.querySelector('#config-save')?.addEventListener('click', saveConfig);

  return () => { unsubRefresh(); };
}

async function loadConfig() {
  set('loading', true);
  try {
    const data = await apiGet(API.CONFIG_GET);
    set('configGroups', data.groups || []);
    set('configReloadRequired', data.reload_required || []);
    renderGroups(data.groups || []);
  } catch (e) {
    console.error('[config] load failed:', e);
    toast('加载配置失败', 'error');
  } finally {
    set('loading', false);
  }
}

function renderGroups(groups) {
  const root = document.getElementById('config-groups');
  if (!root) return;
  root.innerHTML = '';
  for (const g of groups) {
    const section = document.createElement('section');
    section.className = 'config-group';
    section.dataset.groupName = g.name;
    section.innerHTML = `<h3 class="config-group-title">${escapeHtml(g.name)}</h3>`;
    const items = document.createElement('div');
    items.className = 'config-items';
    for (const item of g.items || []) items.appendChild(buildItem(item));
    section.appendChild(items);
    root.appendChild(section);
  }
}

function buildItem(item) {
  const el = document.createElement('div');
  el.className = item.masked ? 'config-item masked' : 'config-item';
  el.dataset.key = item.key;
  // 危险项来源：后端 danger 标记优先，缺失时回退到前端显式集合。
  if (item.danger === true || DANGER_KEYS.has(item.key)) el.dataset.danger = '1';
  el.innerHTML = `
    <div class="config-item-head">
      <span class="config-key">${escapeHtml(item.key)}</span>
      ${item.reload_required ? '<span class="badge warn">需重载</span>' : ''}
      ${el.dataset.danger ? '<span class="badge danger">危险项</span>' : ''}
    </div>
    <div class="config-item-desc">${escapeHtml(item.description || '')}</div>
    <div class="config-item-input"></div>
  `;
  const input = fieldFor(item);
  el.querySelector('.config-item-input').appendChild(input);
  input.addEventListener('change', () => {
    el.classList.add('dirty');
    markInvalid(el, false);
    el.__getValue = () => valueOf(item, input);
  });
  return el;
}

function fieldFor(item) {
  if (item.type === 'bool') {
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = Boolean(item.value);
    box.className = 'config-input';
    return box;
  }
  if (item.type === 'list' || item.type === 'dict') {
    const area = document.createElement('textarea');
    area.className = 'config-input config-textarea';
    area.value = JSON.stringify(item.value, null, 2);
    area.placeholder = item.type === 'list' ? '["a","b"]' : '{"k":"v"}';
    return area;
  }
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'config-input';
  input.value = item.value == null ? '' : String(item.value);
  if (item.masked) input.placeholder = '***（留空则不修改）';
  return input;
}

/**
 * Read one field's typed value.
 * @returns {*} the value, or the INVALID sentinel when the text does not
 *   parse (never null — null is a legitimate parsed JSON value and would
 *   otherwise make "bad input" indistinguishable from "no value").
 */
function valueOf(item, input) {
  if (item.type === 'bool') return input.checked;
  if (item.type === 'int') {
    const n = parseInt(String(input.value), 10);
    return Number.isNaN(n) ? INVALID : n;
  }
  if (item.type === 'float') {
    const n = parseFloat(String(input.value));
    return Number.isNaN(n) ? INVALID : n;
  }
  if (item.type === 'list') {
    try { return JSON.parse(input.value || '[]'); } catch (e) { return INVALID; }
  }
  if (item.type === 'dict') {
    try { return JSON.parse(input.value || '{}'); } catch (e) { return INVALID; }
  }
  return String(input.value);
}

/**
 * Collect the dirty items' typed values.
 * @returns {{values: Object, invalid: string[], danger: string[]}}
 */
function collectDirty() {
  const values = {};
  const invalid = [];
  const danger = [];
  document.querySelectorAll('.config-item.dirty').forEach((el) => {
    if (typeof el.__getValue !== 'function') return;
    const v = el.__getValue();
    if (v === INVALID) {
      // 解析失败必须标红并阻止保存：静默丢弃会让用户以为已生效。
      markInvalid(el, true);
      invalid.push(el.dataset.key);
      return;
    }
    markInvalid(el, false);
    // Masked fields: empty string = no change
    if (el.classList.contains('masked') && (v === '' || v === '***')) return;
    if (el.dataset.danger === '1') danger.push(el.dataset.key);
    values[el.dataset.key] = v;
  });
  return { values, invalid, danger };
}

async function saveConfig() {
  const { values, invalid, danger } = collectDirty();
  if (invalid.length) {
    toast(`以下配置项格式非法，已阻止保存：${invalid.join('、')}`, 'error');
    return;
  }
  if (Object.keys(values).length === 0) {
    toast('没有变更的配置项', 'warn');
    return;
  }
  if (danger.length) {
    const yes = await confirmEx('危险配置项二次确认',
      `以下配置项属于危险项或凭据项，保存后立即影响运行中的行为：\n${danger.join('、')}\n\n确认保存？`,
      { danger: true, okText: '确认保存', cancelText: '取消' });
    if (!yes) return;
  }
  try {
    const r = await apiPost(API.CONFIG_SAVE, { values });
    toast(`已保存 ${(r.saved || []).length} 项配置`, 'success');
    // preview_policy 等键后端即时生效，前端策略缓存必须同步失效，
    // 否则保存后预览仍按旧模式路由（直到刷新页面）。
    invalidatePolicyCache();
    if (r.reload_required && r.reload_required.length) {
      setTimeout(async () => {
        const yes = await confirmEx('应用配置',
          `以下配置需重载插件后生效：\n${r.reload_required.join('、')}\n\n立即热重载？（与 AstrBot 插件页重载等效，约需 2 秒）`,
          { okText: '立即热重载', cancelText: '稍后手动重载' });
        if (!yes) return;
        try {
          await apiPost(API.CONFIG_RELOAD, {});
          toast('插件正在热重载，完成后自动刷新…', 'success');
          // Reload rebuilds the runtime and reconnects SSE; refetch the
          // config afterwards to show the latest state.
          setTimeout(() => { loadConfig(); }, 2500);
        } catch (e2) {
          toast(`热重载失败: ${e2.message || e2}`, 'error');
        }
      }, 300);
    }
    loadConfig();
  } catch (e) {
    toast(`保存失败: ${e.message || e}`, 'error');
  }
}