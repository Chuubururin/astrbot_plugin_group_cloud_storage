/**
 * Config view (T-7) - grouped configuration center (D-7, 归纳分类、便利优先).
 *
 * Fetches config/get (grouped schema + current values), renders by
 * category with a search filter, marks reload-required keys, tracks
 * dirty items, and saves the changed subset via config/save. The 13-class
 * type-extension table (type_ext_overrides, N-01) is editable right here
 * as a normal config item - changing the table changes classification
 * without code changes (CT-9).
 *
 * @module views/config
 */

import { set, subscribe } from '../store.js';
import { API, apiGet, apiPost } from '../api.js';
import { getIcon } from '../icons.js';
import { escapeHtml, debounce } from '../utils/helpers.js';
import { toast } from '../components/toast.js';
import { confirmEx } from '../components/modal.js';

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
    <div id="config-status" class="config-status"></div>
  `;

  loadConfig();

  const unsubRefresh = subscribe('refresh:config', loadConfig);

  // Convenience search filter (D-7): group name or item key match.
  const search = container.querySelector('#config-search');
  search?.addEventListener('input', debounce(() => {
    const q = (search.value || '').toLowerCase();
    container.querySelectorAll('.config-group').forEach((g) => {
      const name = g.dataset.groupName || '';
      let visible = !q || name.toLowerCase().includes(q);
      if (!visible) visible = !!g.querySelector(`.config-item[data-key*="${q}"]`);
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
  el.innerHTML = `
    <div class="config-item-head">
      <span class="config-key">${escapeHtml(item.key)}</span>
      ${item.reload_required ? '<span class="badge warn">需重载</span>' : ''}
    </div>
    <div class="config-item-desc">${escapeHtml(item.description || '')}</div>
    <div class="config-item-input"></div>
  `;
  const input = fieldFor(item);
  el.querySelector('.config-item-input').appendChild(input);
  input.addEventListener('change', () => {
    el.classList.add('dirty');
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

function valueOf(item, input) {
  if (item.type === 'bool') return input.checked;
  if (item.type === 'int') {
    const n = parseInt(String(input.value), 10);
    return Number.isNaN(n) ? null : n;
  }
  if (item.type === 'float') {
    const n = parseFloat(String(input.value));
    return Number.isNaN(n) ? null : n;
  }
  if (item.type === 'list') {
    try { return JSON.parse(input.value || '[]'); } catch (e) { return null; }
  }
  if (item.type === 'dict') {
    try { return JSON.parse(input.value || '{}'); } catch (e) { return null; }
  }
  return String(input.value);
}

async function saveConfig() {
  const values = {};
  document.querySelectorAll('.config-item.dirty').forEach((el) => {
    if (typeof el.__getValue === 'function') {
      const v = el.__getValue();
      if (v !== null) {
        // Masked fields: empty string = no change (留空则不修改)
        if (el.classList.contains('masked') && (v === '' || v === '***')) return;
        values[el.dataset.key] = v;
      }
    }
  });
  if (Object.keys(values).length === 0) {
    toast('没有变更的配置项', 'warn');
    return;
  }
  try {
    const r = await apiPost(API.CONFIG_SAVE, { values });
    toast(`已保存 ${(r.saved || []).length} 项配置`, 'success');
    if (r.reload_required && r.reload_required.length) {
      setTimeout(() => confirmEx('重载提示',
        `以下配置需要插件重载生效：\n${r.reload_required.join('、')}\n\n是否立即重载？（将在 AstrBot 插件页重载）`), 300);
    }
    loadConfig();
  } catch (e) {
    toast(`保存失败: ${e.message || e}`, 'error');
  }
}