/**
 * Unit tests: album media normalization (features/album-media.js).
 *
 * Fixtures mirror the two backend shapes: the QQ NT camelCase entries
 * returned by the SnowLuma album service and the legacy snake_case
 * NapCat-style entries.
 *
 * Run: node --test pages/storage-ng/testing/unit/
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { normalizeMedia } from '../../features/album-media.js';

const URL_SMALL = 'https://media.example/small.jpg';
const URL_LARGE = 'https://media.example/large.jpg';
const URL_DEFAULT = 'https://media.example/default.jpg';

test('QQ NT image item: picks the largest spec and reads camelCase fields', () => {
  const [out] = normalizeMedia([
    {
      type: 0,
      image: {
        name: 'photo.png',
        sloc: 's-loc-value',
        lloc: 'l-loc-value',
        photoUrls: [
          { spec: 1, url: { url: URL_SMALL, width: 415, height: 415 } },
          { spec: 5, url: { url: URL_LARGE, width: 1280, height: 960 } },
        ],
      },
    },
  ]);
  assert.equal(out.url, URL_LARGE);
  assert.equal(out.is_video, false);
  assert.equal(out.name, 'photo.png');
  assert.equal(out.lloc, 'l-loc-value');
});

test('legacy snake_case image item: photo_url list still parses', () => {
  const [out] = normalizeMedia([
    { image: { name: 'old.jpg', lloc: 'old-lloc', photo_url: [{ url: { url: URL_SMALL, width: 10, height: 10 } }] } },
  ]);
  assert.equal(out.url, URL_SMALL);
  assert.equal(out.lloc, 'old-lloc');
});

test('image item without specs falls back to defaultUrl', () => {
  const [out] = normalizeMedia([
    { image: { name: 'd.png', lloc: 'L', photoUrls: [], defaultUrl: { url: URL_DEFAULT, width: 1, height: 1 } } },
  ]);
  assert.equal(out.url, URL_DEFAULT);
});

test('QQ NT video item: id locates the media and multi-spec videoUrl resolves', () => {
  const [out] = normalizeMedia([
    {
      type: 1,
      video: {
        id: 'vid-001',
        url: 'https://media.example/plain.mp4',
        cover: { name: 'cover.jpg', lloc: 'cover-lloc', photoUrls: [{ spec: 1, url: { url: URL_SMALL, width: 100, height: 100 } }] },
        width: 1920,
        height: 1080,
        videoTime: 8500,
        videoUrl: [
          { spec: 1, url: { url: URL_SMALL, width: 480, height: 270 } },
          { spec: 5, url: { url: URL_LARGE, width: 1920, height: 1080 } },
        ],
      },
    },
  ]);
  assert.equal(out.is_video, true);
  assert.equal(out.url, URL_LARGE);
  assert.equal(out.poster, URL_SMALL);
  assert.equal(out.lloc, 'vid-001');
  assert.equal(out.name, 'vid-001');
});

test('video item without specs falls back to the flat playback URL', () => {
  const [out] = normalizeMedia([{ video: { id: 'vid-002', url: 'https://media.example/plain.mp4' } }]);
  assert.equal(out.is_video, true);
  assert.equal(out.url, 'https://media.example/plain.mp4');
  assert.equal(out.poster, '');
  assert.equal(out.lloc, 'vid-002');
});

test('legacy top-level fields and placeholder naming still apply', () => {
  const [out] = normalizeMedia([{ desc: 'caption', lloc: 'top-lloc', url: { url: URL_SMALL } }]);
  assert.equal(out.url, URL_SMALL);
  assert.equal(out.name, 'caption');
  assert.equal(out.lloc, 'top-lloc');
  const [anon] = normalizeMedia([{ image: { photoUrls: [{ spec: 1, url: { url: URL_SMALL, width: 1, height: 1 } }] } }]);
  assert.equal(anon.name, '(未命名)');
});

test('empty and null input normalize to an empty list', () => {
  assert.deepEqual(normalizeMedia([]), []);
  assert.deepEqual(normalizeMedia(null), []);
});
