/**
 * Security tests: format* output purity (TE-3 layer 2).
 *
 * formatSize/formatRate/formatDuration outputs are interpolated into
 * innerHTML when table rows render — any metacharacter in their output
 * would be an XSS vector. The backend (core/units.py) is covered by
 * tests/security/test_units_security.py; this file pins the same contract
 * on the actual front-end module shipped to browsers.
 *
 * Run: node --test pages/storage-ng/testing/unit/format-security.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { formatSize, formatRate, formatDuration } from '../../utils/helpers.js';

const HOSTILE_INPUTS = [
  0, 1, -1, -1000, 0.5, 512, 1023, 1024, 95e6, 2e9, 2e12, 1e18, 1e308,
  Number.MAX_SAFE_INTEGER, Number.MAX_VALUE,
  Infinity, -Infinity, NaN,
  null, undefined, true, false,
  '1GB', '<script>alert(1)</script>', '<img src=x onerror=alert(1)>',
  'javascript:alert(1)', '${1}', '{{1}}', '"', "'", '`', '\\',
  [], {}, () => {}, new Date(0), Symbol('x'),
];

// Glyphs legitimately used by the formatters: digits, dot, space, sign-free
// units, and the CJK duration units.
const ALLOWED = new Set('0123456789. MBG/sT时秒分'.split(''));
const METACHARS = ['<', '>', '&', '"', "'", '`', '\\', '{', '}', '$', ';', '(', ')'];

for (const [name, fn] of [
  ['formatSize', formatSize],
  ['formatRate', formatRate],
  ['formatDuration', formatDuration],
]) {
  test(`${name}: output stays metacharacter-free for every hostile input`, () => {
    for (const input of HOSTILE_INPUTS) {
      const out = fn(input);
      assert.equal(typeof out, 'string', `${name}(${String(input)}) type`);
      for (const mc of METACHARS) {
        assert.ok(!out.includes(mc), `${name}(${String(input)}) -> ${JSON.stringify(out)} leaks ${mc}`);
      }
    }
  });

  test(`${name}: output uses only unit glyphs (no XSS payload can be formed)`, () => {
    for (const input of HOSTILE_INPUTS) {
      const out = fn(input);
      for (const ch of out) {
        assert.ok(
          ALLOWED.has(ch),
          `${name}(${String(input)}) -> unexpected char ${ch} in ${out}`,
        );
      }
    }
  });

  test(`${name}: unit vocabulary obeys the project measure rules`, () => {
    // Storage base 1000, MB floor: no byte/KB magnitudes ever appear.
    assert.equal(formatSize(0), '0 MB');
    assert.equal(formatSize(999), '0.1 MB');
    assert.ok(!formatSize(1023).includes('B') || formatSize(1023).includes('MB'));
    // Bandwidth base 1024, MB/s floor: no KB/s.
    assert.ok(!formatRate(100).includes('KB'));
    assert.equal(formatRate(0), '0 MB/s');
    // Durations: 时/分/秒 only — no ms anywhere.
    assert.equal(formatDuration(45), '45秒');
    assert.equal(formatDuration(0), '0秒');
    for (const input of HOSTILE_INPUTS) {
      assert.ok(!formatDuration(input).includes('ms'), `ms leaked for ${String(input)}`);
    }
  });
}
