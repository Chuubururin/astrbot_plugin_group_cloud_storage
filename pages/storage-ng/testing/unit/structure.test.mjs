/**
 * Unit tests: structural rules of the rewritten frontend (HL-18 style):
 *  - utils boundary: exactly the live modules (dead sse-handler and
 *    render-budget were removed during the rewrite),
 *  - every JS module stays within the <=300-line budget,
 *  - no module imports any removed legacy path,
 *  - the 8-tab IA is declared in one place (components/tabs.js).
 *
 * Run: node --test pages/storage-ng/testing/unit/
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdirSync, existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

/** All JS modules under the frontend root except testing/ (recursive). */
function walk(dir) {
  const out = [];
  for (const f of readdirSync(dir)) {
    if (f === 'testing') continue;
    const p = path.join(dir, f);
    const isDir = existsSync(p) && readdirSync(dir, { withFileTypes: true })
      .find((e) => e.name === f)?.isDirectory();
    if (isDir) out.push(...walk(p));
    else if (f.endsWith('.js')) out.push(p);
  }
  return out;
}

test('structure: utils boundary is the live set (dead modules removed)', () => {
  const utils = readdirSync(path.join(root, 'utils')).filter((f) => f.endsWith('.js'));
  const expected = ['dom-diff.js', 'helpers.js', 'sse.js'];
  for (const e of expected) assert.ok(utils.includes(e), `missing ${e}`);
  for (const gone of ['sse-handler.js', 'sse-resilient.js', 'render-budget.js', 'index.js']) {
    assert.ok(!utils.includes(gone), `${gone} should be removed`);
  }
});

test('structure: legacy split modules are gone (dedup complete)', () => {
  for (const gone of [
    'features/upload-flow.js',
    'features/command-defs-files.js',
    'features/command-defs-distribute.js',
    'components/cs-bridge.js',
    'components/cs-gallery.js',
    'components/cs-essence-viewer.js',
    'components/cs-header.js',
    'components/cs-modal.js',
    'components/cs-toast.js',
    'components/cs-tabs.js',
    'components/cs-toolbar.js',
    'components/cs-file-table.js',
    'components/cs-group-table.js',
    'components/cs-action-bar.js',
    'components/cs-menu.js',
    'components/cs-breadcrumb.js',
    'components/cs-statbar.js',
    'components/cs-status-bar.js',
    'components/cs-task-panel.js',
  ]) {
    assert.ok(!existsSync(path.join(root, gone)), `${gone} should be removed`);
  }
});

test('structure: no module imports removed legacy paths', () => {
  const files = walk(root);
  const banned = ['sse-handler', 'sse-resilient', 'render-budget', 'upload-flow',
    'command-defs-files', 'command-defs-distribute', 'utils/index'];
  const importRe = /from\s+['"]([^'"]+)['"]/g;
  for (const f of files) {
    const code = readFileSync(f, 'utf-8');
    let m;
    while ((m = importRe.exec(code)) !== null) {
      for (const b of banned) {
        assert.ok(!m[1].includes(b), `${path.basename(f)} imports banned module ${m[1]}`);
      }
    }
  }
});

test('structure: every JS module stays within the 300-line budget', () => {
  const files = walk(root).concat(
    readdirSync(path.join(root, 'testing')).filter((f) => f.endsWith('.js'))
      .map((f) => path.join(root, 'testing', f)),
  );
  for (const f of files) {
    const lines = readFileSync(f, 'utf-8').split('\n').length;
    assert.ok(lines <= 300, `${f} exceeds 300 lines (${lines})`);
  }
});

test('structure: the 7-tab IA is declared in components/tabs.js', () => {
  const tabs = readFileSync(path.join(root, 'components', 'tabs.js'), 'utf-8');
  for (const tab of ["'files'", "'albums'", "'essence'", "'netdisk'", "'tasks'", "'groups'", "'config'"]) {
    assert.ok(tabs.includes(tab), `tab ${tab} declared`);
  }
  // debug tab was removed (2026-09-01)
  assert.ok(!tabs.includes("'debug'"), 'debug tab removed');
});