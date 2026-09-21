/**
 * Unit tests: SSE 长任务链路契约回归（2026-09-13 前端连续操作排查）.
 *
 * Covers:
 *  - constants: EVENT_TYPES.BRIDGE 与后端 op kind 全集（20 种）在
 *    EVENT_KINDS 中齐全 —— SSE kind 比较与标签映射的原料
 *  - main.js 源码契约: progress 事件的 i/n 必须进入 activeTask（此前恒
 *    显示 0/100）、CANCELLED 清顶栏、bridge 失败事件必须 toast、
 *    queueStatus 轮询 depth→pending 归一化
 *  - tasks.js: pause/resume/interrupt 必须检查后端 ok:false（此前对终态
 *    任务假报成功）
 *  - database-admin.js: sessionStorage 访问必须守卫（opaque iframe 里
 *    getter 本身抛 SecurityError）
 *  - upload.js: prepare 拒绝原因必须透传到 r.error
 *  - bridge-panel.js: 方向切换 last-request-wins 守卫
 *  - recover.js: 重拉必须受限并发（串行最坏阻塞 20×30s）
 *  - modal.js: 单例 overlay 二次 open 必须先结束挂起的 Promise
 *
 * Run: node --test pages/storage-ng/testing/unit/
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { EVENT_TYPES, EVENT_KINDS } from '../../constants.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const src = (rel) => fs.readFileSync(path.join(here, '..', '..', rel), 'utf8');

// ---- constants: 与后端 op kind 全集对齐 ----

test('EVENT_TYPES.BRIDGE exists (bridge transfer lifecycle events)', () => {
  assert.equal(EVENT_TYPES.BRIDGE, 'bridge');
});

test('EVENT_KINDS covers the full backend op kind set (20 kinds)', () => {
  const BACKEND_KINDS = [
    'scan', 'file_scan', 'diff_file_scan', 'sync',
    'upload', 'delete', 'move_file', 'replace_name', 'convert_volumes',
    'create_folder', 'fetch',
    'essence_save', 'essence_delete',
    'video_upload', 'video_album', 'image_album',
    'netdisk_index', 'batch_groups', 'bridge_out', 'bridge_in',
  ];
  const defined = Object.values(EVENT_KINDS);
  for (const k of BACKEND_KINDS) {
    assert.ok(defined.includes(k), `EVENT_KINDS missing backend kind: ${k}`);
  }
});

// ---- main.js: SSE 事件消费契约 ----

test('main.js destructures i/n and feeds them into activeTask (not 0/100)', () => {
  const s = src('main.js');
  assert.match(s, /const \{[^}]*\bi\b[^}]*\bn\b[^}]*\} = ev;/,
    'handleSSEEvent 必须解构 i/n（OpQueue progress 的主形状）');
  assert.match(s, /i:\s*i \?\? percent \?\? 0/);
  assert.match(s, /n:\s*n \?\? 100/);
});

test('main.js clears activeTask on CANCELLED (terminal state)', () => {
  const s = src('main.js');
  assert.match(s,
    /type === EVENT_TYPES\.DONE \|\| type === EVENT_TYPES\.FAILED\s*\n?\s*\|\| type === EVENT_TYPES\.CANCELLED/,
    'CANCELLED 是终态，必须与 DONE/FAILED 一样清 activeTask');
});

test('main.js consumes bridge-type events: failed state must toast', () => {
  const s = src('main.js');
  assert.match(s, /case EVENT_TYPES\.BRIDGE:/,
    'type:"bridge" 事件不能被丢弃（转存失败唯一可见渠道）');
  assert.match(s, /taskState === 'failed'/);
});

test('main.js wires the queue indicator; it maps tasks/queue depth -> pending', () => {
  const m = src('main.js');
  assert.match(m, /startQueueIndicator\(\)/,
    'queueStatus 指示器必须接上数据来源');
  const q = src('utils/queue-indicator.js');
  assert.match(q, /API\.TASKS_QUEUE/);
  assert.match(q, /pending:\s*st\?\.depth/,
    '后端 queue.status() 的字段是 depth，组件读 pending，必须归一化');
  assert.match(q, /QUEUE_POLL_INTERVAL/, '轮询间隔必须来自常量表');
});

// ---- tasks.js: 操作反馈真实性 ----

test('tasks.js pause/resume/interrupt check backend ok:false', () => {
  const s = src('views/tasks.js');
  for (const api of ['API.TASKS_PAUSE', 'API.TASKS_RESUME', 'API.TASKS_INTERRUPT']) {
    // 精确匹配 apiPost(...) 调用点：TASKS_RESUME 是 TASKS_RESUME_PENDING 的前缀
    const callIdx = s.indexOf(`apiPost(${api},`);
    assert.ok(callIdx > 0, `${api} must be called`);
    const window = s.slice(callIdx, callIdx + 220);
    assert.match(window, /r\?\.ok/, `${api} 调用后必须检查返回的 ok 字段`);
    assert.match(window, /r\?\.reason/, '失败时必须展示后端 reason');
  }
});

// ---- database-admin.js: 沙箱安全 ----

test('database-admin.js guards every sessionStorage access', () => {
  const s = src('features/database-admin.js');
  assert.match(s, /function sessionStore\(\)/, '必须有守卫访问器');
  // 不再允许裸访问（router.js 对同一宿主约束已有守卫先例）
  assert.doesNotMatch(s, /[^.\w]sessionStorage\./,
    'sessionStorage 必须经 sessionStore() 守卫访问');
  assert.match(s, /saveDbToken/);
});

// ---- upload.js: prepare 拒绝原因透传 ----

test('upload.js uploadOnce forwards prepare refusal reason as error', () => {
  const s = src('features/upload.js');
  assert.match(s, /error:\s*prep\?\.error \|\| prep\?\.message \|\| 'prepare 被拒绝'/,
    '调用方读 r.error，prepare 的 4xx message 不能在中间层丢失');
});

// ---- bridge-panel.js: 方向切换竞态 ----

test('bridge-panel.js guards task loads with a seq counter', () => {
  const s = src('components/bridge-panel.js');
  assert.match(s, /let tasksSeq = 0;/);
  assert.match(s, /const seq = \+\+tasksSeq;/);
  assert.match(s, /if \(seq !== tasksSeq\) return;/,
    '旧方向的后到响应必须丢弃（last-request-wins）');
});

// ---- recover.js: 重拉并发度 ----

test('recover.js refetches rows with bounded concurrency (not serial)', () => {
  const s = src('utils/recover.js');
  assert.match(s, /REFETCH_CONCURRENCY = 4/);
  assert.match(s, /Promise\.all/, '必须并发执行 worker');
});

// ---- netdisk-upload 接力链: 台账轮询不受分页截断 ----

test('waitTasksDone polls with task_ids filter (pagination window cannot hide own task)', () => {
  const s = src('features/netdisk-upload.js');
  assert.match(s, /task_ids:\s*\[\.\.\.pending\]/,
    '接力链轮询必须按 task_ids 精确过滤：台账超过 limit=100 时分页窗口可能不含自己的任务');
});

// ---- modal.js: 单例嵌套不挂死 ----

test('modal.js open() resolves a still-pending previous dialog', () => {
  const s = src('components/modal.js');
  const openFn = s.slice(s.indexOf('function open()'), s.indexOf('function open()') + 400);
  assert.match(openFn, /if \(resolveCurrent\)/,
    '二次 open 必须先结束旧 Promise，否则其调用方 busy 锁泄漏');
  assert.match(openFn, /resolveCurrent\(null\)/);
});
