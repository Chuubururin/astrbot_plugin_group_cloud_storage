/**
 * Unit tests: FE-18 data sources (capabilities, sourceFor mapping,
 * selection isolation, local filter/sort).
 *
 * Run: node --test pages/storage-ng/testing/unit/
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  GROUP_SOURCE, NETDISK_SOURCE, ALBUM_SOURCE, ESSENCE_SOURCE,
  applyLocalFilterSort, sourceFor, netdiskExtType, netdiskTypeMap,
} from '../../features/data-sources.js';
import { getState, set } from '../../store.js';
import { FILE_TYPE_EXT } from './ext-table-fixture.mjs';

test('capability matrix: group full set, netdisk trimmed (N4c)', () => {
  // 2026-09-06 下载整合：download/link/address 并入 files-distribute 单入口
  assert.ok(GROUP_SOURCE.capabilities.includes('files-distribute'));
  assert.ok(!GROUP_SOURCE.capabilities.includes('download'));
  assert.ok(!GROUP_SOURCE.capabilities.includes('link'));
  assert.ok(!GROUP_SOURCE.capabilities.includes('address'));
  assert.ok(NETDISK_SOURCE.capabilities.includes('netdisk-download'));
  assert.ok(GROUP_SOURCE.capabilities.includes('delete'));
  assert.ok(NETDISK_SOURCE.capabilities.includes('netdisk-delete'));
  assert.ok(GROUP_SOURCE.capabilities.includes('clear'));
  assert.ok(NETDISK_SOURCE.capabilities.includes('clear'));
  // 2026-09-06 整改：bridge-out 移除（转存网盘 = download target=netdisk）
  assert.ok(!GROUP_SOURCE.capabilities.includes('bridge-out'));
  assert.ok(!NETDISK_SOURCE.capabilities.includes('bridge-out'));
  // 幽灵能力清理：volumes/verify（命令已删）与 essence-detail（未注册）不再被引用
  assert.ok(!GROUP_SOURCE.capabilities.includes('volumes'));
  assert.ok(!GROUP_SOURCE.capabilities.includes('verify'));
  assert.ok(!ESSENCE_SOURCE.capabilities.includes('essence-detail'));
  assert.ok(GROUP_SOURCE.capabilities.includes('files-distribute'));
  // 2026-09-03 整改（S2）：transfer-in / netdisk-index 移除（重复/无意义）
  assert.ok(!NETDISK_SOURCE.capabilities.includes('transfer-in'));
  assert.ok(!NETDISK_SOURCE.capabilities.includes('netdisk-index'));
  assert.ok(!GROUP_SOURCE.capabilities.includes('transfer-in'));
  assert.ok(NETDISK_SOURCE.capabilities.includes('netdisk-distribute'));
});

test('sourceFor: view maps to adapter (FE-18)', () => {
  assert.equal(sourceFor('files').id, 'group');
  assert.equal(sourceFor('netdisk').id, 'netdisk');
  assert.equal(sourceFor('albums').id, 'album');
  assert.equal(sourceFor('essence').id, 'essence');
});

test('capability matrix: albums carry album ops, essence carries distribute (W3-A)', () => {
  // 2026-09-05：相册行动作改为 创建相册/查看媒体/相册详情（原 album-distribute
  // 语义错位——相册行不是媒体，恒取首个媒体下载，已从动作栏移除）
  assert.ok(ALBUM_SOURCE.capabilities.includes('album-create'));
  assert.ok(ALBUM_SOURCE.capabilities.includes('album-gallery'));
  assert.ok(ALBUM_SOURCE.capabilities.includes('album-detail'));
  assert.ok(!ALBUM_SOURCE.capabilities.includes('album-distribute'));
  assert.ok(ESSENCE_SOURCE.capabilities.includes('essence-distribute'));
  assert.ok(!ALBUM_SOURCE.capabilities.includes('files-distribute'));
  assert.ok(!ESSENCE_SOURCE.capabilities.includes('netdisk-distribute'));
});

test('selection isolation: album writes albumSelected, never fileSelected (W3-A)', () => {
  set('fileSelected', new Set());
  set('albumSelected', new Set());
  ALBUM_SOURCE.selection.toggle('11', true);
  ALBUM_SOURCE.selection.toggle('12', true);
  ALBUM_SOURCE.selection.toggle('12', false);
  assert.deepEqual([...getState().albumSelected], ['11']);
  assert.equal(getState().fileSelected.size, 0);
  ALBUM_SOURCE.selection.setMany(['3', 4]);
  assert.deepEqual([...getState().albumSelected], ['3', '4']);
  ALBUM_SOURCE.selection.clear();
  assert.equal(getState().albumSelected.size, 0);
});

test('selection isolation: essence writes essenceSelected (W3-A)', () => {
  set('fileSelected', new Set());
  set('essenceSelected', new Set());
  ESSENCE_SOURCE.selection.toggle('21', true);
  assert.deepEqual([...getState().essenceSelected], ['21']);
  assert.equal(getState().fileSelected.size, 0);
});

test('applyLocalFilterSort: ext table filter (N4a, CT-9)', () => {
  const extTypes = new Map(Object.entries(FILE_TYPE_EXT).flatMap(
    ([t, exts]) => (exts || []).map((e) => [e, t])));
  const items = [
    { name: 'a.mp4', type: 'video', size: 3 },
    { name: 'b.txt', type: 'document', size: 1 },
    { name: 'c.xyz', size: 2 },
  ];
  const video = applyLocalFilterSort(items, { type: 'video', sort_by: '', sort_dir: 'asc' }, extTypes);
  assert.deepEqual(video.map((x) => x.name), ['a.mp4']);
  const unknown = applyLocalFilterSort(items, { type: 'data', sort_by: '', sort_dir: 'asc' }, extTypes);
  assert.deepEqual(unknown, []);
});

test('applyLocalFilterSort: local sort by size desc / name asc', () => {
  const items = [
    { name: 'b.mp4', size: 3 },
    { name: 'a.mp4', size: 9 },
  ];
  const sorted = applyLocalFilterSort(items, { type: '', sort_by: 'size', sort_dir: 'desc' }, null);
  assert.equal(sorted[0].size, 9);
  const byName = applyLocalFilterSort(items, { type: '', sort_by: 'name', sort_dir: 'asc' }, null);
  assert.equal(byName[0].name, 'a.mp4');
});
test('netdisk classification: independent 4-type mapping (2026-09-03)', () => {
  assert.equal(netdiskExtType('a.txt'), 'text');
  assert.equal(netdiskExtType('b.md'), 'text');
  assert.equal(netdiskExtType('c.mp3'), 'audio');
  assert.equal(netdiskExtType('d.mp4'), 'video');
  assert.equal(netdiskExtType('e.png'), 'image');
  assert.equal(netdiskExtType('f.xyz'), 'other');
  const m = netdiskTypeMap();
  assert.equal(m.get('.mkv'), 'video');
  assert.equal(m.get('.json'), 'text');
  assert.ok(!m.has('.zip'), 'zip 不在网盘 4 类中（群文件专属分类不复用）');
});

test('netdisk source filter uses independent map (2026-09-03)', () => {
  const items = [
    { name: 'a.txt', size: 1 },
    { name: 'b.mp4', size: 2 },
    { name: 'c.zip', size: 3 },
  ];
  const video = applyLocalFilterSort(items, { type: 'video', sort_by: '', sort_dir: 'asc' }, netdiskTypeMap());
  assert.deepEqual(video.map((x) => x.name), ['b.mp4']);
  const text = applyLocalFilterSort(items, { type: 'text', sort_by: '', sort_dir: 'asc' }, netdiskTypeMap());
  assert.deepEqual(text.map((x) => x.name), ['a.txt']);
  const other = applyLocalFilterSort(items, { type: 'other', sort_by: '', sort_dir: 'asc' }, netdiskTypeMap());
  assert.deepEqual(other.map((x) => x.name), ['c.zip']);
});
