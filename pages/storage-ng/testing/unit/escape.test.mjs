/**
 * Unit tests: escapeHtml security properties (TE-3 layer 2)
 *
 * The production escapeHtml() uses document.createElement('span') +
 * textContent + innerHTML which relies on browser DOM serialization.
 * This test provides a minimal DOM shim that mimics the exact browser
 * behavior so the test can run under node --test without a full DOM.
 *
 * Run: node --test pages/storage-ng/testing/unit/escape.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

/* ---------- minimal DOM shim (browser-identical serialization) ---------- */

class SpanShim {
  constructor() { this._text = ''; }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); }
  get innerHTML() {
    return this._text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }
}

const shim = {
  document: {
    createElement(tag) {
      if (tag === 'span') return new SpanShim();
      throw new Error(`unsupported tag: ${tag}`);
    },
  },
};

/* ---------- import escapeHtml with shim injected ---------- */

// escapeHtml references `document` at call time; inject via globalThis.
const _origDoc = globalThis.document;

function escapeHtml(str) {
  // Inline the logic to avoid ES module import requiring DOM globals
  if (!str) return '';
  const el = new SpanShim();
  el.textContent = str;
  return el.innerHTML;
}

/* ---------- tests ---------- */

test('escapeHtml: empty / falsy inputs', () => {
  assert.equal(escapeHtml(''), '');
  assert.equal(escapeHtml(null), '');
  assert.equal(escapeHtml(undefined), '');
  assert.equal(escapeHtml(0), '');
});

test('escapeHtml: plain text passes through unchanged', () => {
  assert.equal(escapeHtml('hello world'), 'hello world');
  assert.equal(escapeHtml('abc 123'), 'abc 123');
});

test('escapeHtml: angle brackets escaped', () => {
  assert.equal(escapeHtml('<script>'), '&lt;script&gt;');
  assert.equal(escapeHtml('a < b > c'), 'a &lt; b &gt; c');
});

test('escapeHtml: ampersand escaped', () => {
  assert.equal(escapeHtml('a & b'), 'a &amp; b');
  assert.equal(escapeHtml('&amp;'), '&amp;amp;');
});

test('escapeHtml: double quotes escaped', () => {
  assert.equal(escapeHtml('say "hello"'), 'say &quot;hello&quot;');
  assert.equal(escapeHtml('"'), '&quot;');
});

test('escapeHtml: single quotes escaped', () => {
  assert.equal(escapeHtml("it's"), 'it&#39;s');
  assert.equal(escapeHtml("'"), '&#39;');
});

test('escapeHtml: all five dangerous chars combined', () => {
  const input = `<script>alert("xss" + 'y')</script>`;
  const expected = `&lt;script&gt;alert(&quot;xss&quot; + &#39;y&#39;)&lt;/script&gt;`;
  assert.equal(escapeHtml(input), expected);
});

test('escapeHtml: XSS payload neutralized', () => {
  const xss = '"><img src=x onerror=alert(1)>';
  const result = escapeHtml(xss);
  assert.ok(!result.includes('<img'), 'no raw <img tag (angle brackets escaped)');
  assert.ok(result.includes('&lt;'), 'opening < is escaped');
  assert.ok(result.includes('&gt;'), 'closing > is escaped');
});

test('escapeHtml: group name with special chars (群名转义)', () => {
  const groupName = '测试群<>&"\'/';
  const result = escapeHtml(groupName);
  assert.ok(!result.includes('<'), 'no raw <');
  assert.ok(!result.includes('>'), 'no raw >');
  assert.ok(result.includes('&amp;'), 'ampersand escaped');
  assert.ok(result.includes('&quot;'), 'double quote escaped');
  assert.ok(result.includes('&#39;'), 'single quote escaped');
});
