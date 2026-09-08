/**
 * Unit tests: token-expiry resilience (view preloading + expired hint).
 *
 * The dashboard embeds this page in a sandboxed iframe with an asset_token
 * that expires after ~60s; module fetches after that window 401 and the old
 * recover-by-reload strategy replaced the page with a bare JSON error. These
 * tests pin the two guards:
 *  1. main.js preloads every VIEW_IMPORTS entry's first import at init time
 *     (all view modules enter the module cache during the fresh-token window);
 *  2. router.js reads the asset_token exp claim locally and, when expired,
 *     shows the re-login hint instead of reloading (a reload can only 401).
 *
 * Run: node --test pages/storage-ng/testing/unit/token-expiry.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const mainSrc = readFileSync(join(here, '../../main.js'), 'utf8');
const routerSrc = readFileSync(join(here, '../../router.js'), 'utf8');

test('main.js preloads every view module at init (fresh-token window)', () => {
  // The preload loop must exist and read each VIEW_IMPORTS value's import slot
  // (value[1] = first import thunk; exportName sits at value[0]).
  assert.match(
    mainSrc,
    /for\s*\(const\s+value\s+of\s+Object\.values\(VIEW_IMPORTS\)\)/,
    'preload loop over VIEW_IMPORTS values missing',
  );
  assert.match(mainSrc, /value\[1\]/, 'preload must call the first import thunk (value[1])');
  // The preload must not run the ?retry=N fallbacks eagerly (only loadView does).
  assert.doesNotMatch(
    mainSrc,
    /for\s*\(const\s+attempt\s+of\s+imports\)/,
    'preload must not fire every retry thunk eagerly',
  );
  // Preload happens before initRouter so the fresh window covers navigation.
  const preloadAt = mainSrc.indexOf('Object.values(VIEW_IMPORTS)');
  const routerAt = mainSrc.indexOf('initRouter()');
  assert.ok(preloadAt !== -1 && routerAt !== -1 && preloadAt < routerAt,
    'preload must run before initRouter()');
});

test('router.js detects expired asset_token from the exp claim (no signature check)', () => {
  assert.match(routerSrc, /assetTokenExpired/, 'assetTokenExpired helper missing');
  // base64url decode of the JWT payload segment, exp compared against Date.now()
  assert.match(routerSrc, /replace\(/, 'base64url char restoration missing');
  assert.match(routerSrc, /payload\.exp\s*\*\s*1000\s*<=\s*Date\.now\(\)/,
    'exp comparison against Date.now() missing');
  // Malformed/absent token must be treated as NOT expired (fail open to the
  // reload path; the server enforces auth regardless).
  assert.match(routerSrc, /catch\s*\(e\)\s*\{\s*return false;/s,
    'parse failure must return false');
});

test('router.js shows the re-login hint instead of reloading when expired', () => {
  // reloadAfterImportPoison checks expiry BEFORE any reload navigation
  const checkAt = routerSrc.indexOf('if (assetTokenExpired())');
  const reloadAt = routerSrc.indexOf('window.location.reload()');
  assert.ok(checkAt !== -1 && reloadAt > checkAt,
    'expiry check must precede window.location.reload()');
  // Hint is actionable: explains the cause and offers a manual retry button.
  assert.match(routerSrc, /登录状态已过期/, 'expired-state explanation missing');
  assert.match(routerSrc, /刷新宿主页面/, 'actionable instruction missing');
  assert.match(routerSrc, /view-reload-retry/, 'retry button id missing');
  // The retry button re-runs the view load attempt chain.
  const hintAt = routerSrc.indexOf('function showExpiredHint');
  const retryAt = routerSrc.indexOf("attemptViewLoad(0)", hintAt);
  assert.ok(hintAt !== -1 && retryAt !== -1, 'hint retry must re-attempt view load');
});

test('jwt payload exp decode works on a real-shaped token', () => {
  // Mirror of assetTokenExpired's decode logic against a synthetic token.
  const payload = { username: 'u', exp: 1700000000 };
  const b64 = Buffer.from(JSON.stringify(payload)).toString('base64url');
  const token = `hdr.${b64}.sig`;
  const seg = token.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
  const decoded = JSON.parse(Buffer.from(seg, 'base64').toString('utf8'));
  assert.equal(decoded.exp, 1700000000);
  assert.equal(decoded.exp * 1000 <= Date.now(), true, 'old token must read as expired');
});
