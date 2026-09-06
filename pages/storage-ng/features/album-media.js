/**
 * Album media item normalization for the gallery viewer.
 *
 * The QQ NT album service returns camelCase media entries; legacy
 * NapCat-style adapters return snake_case variants. Both are accepted so
 * the gallery and the keyframe GIF flow work against either backend.
 *
 * @module features/album-media
 */

/** Pick the larger {url:{url,width,height}} variant by pixel area. */
function byAreaDesc(a, b) {
  const wa = (a.url && a.url.width) || 0;
  const ha = (a.url && a.url.height) || 0;
  const wb = (b.url && b.url.width) || 0;
  const hb = (b.url && b.url.height) || 0;
  return wb * hb - wa * ha;
}

/**
 * Map raw album media entries to gallery items.
 *
 * Image items carry a multi-spec photoUrls list plus a defaultUrl fallback;
 * video items carry a multi-spec videoUrl list plus a flat playback URL.
 * Images locate by lloc, videos by their id.
 *
 * @param {Array<Object>} media - raw media list from the albums/media API
 * @returns {Array<{url: string, is_video: boolean, name: string, lloc: string}>}
 */
export function normalizeMedia(media) {
  return (media || []).map((m) => {
    const img = m.image || {};
    const photos = (img.photoUrls || img.photo_url || []).slice().sort(byAreaDesc);
    let url = photos.length ? (photos[0].url && photos[0].url.url) : '';
    if (!url && img.defaultUrl && img.defaultUrl.url) url = img.defaultUrl.url;
    if (!url && m.url && typeof m.url === 'object' && m.url.url) url = m.url.url;
    if (!url && typeof m.url === 'string') url = m.url;
    if (!url && typeof m.file === 'string') url = m.file;
    // Videos render as a cover thumbnail with a keyframe-GIF button instead
    // of the direct link; the URL is still kept for copy/download actions.
    const vid = m.video || null;
    if (!url && vid) {
      const specs = (vid.videoUrl || vid.video_url || []).slice().sort(byAreaDesc);
      url = (specs.length && specs[0].url && specs[0].url.url) || (typeof vid.url === 'string' ? vid.url : '');
    }
    let poster = '';
    if (vid) {
      const covers = ((vid.cover && (vid.cover.photoUrls || vid.cover.photo_url)) || []).slice().sort(byAreaDesc);
      poster =
        (covers.length && covers[0].url && covers[0].url.url) ||
        (vid.cover && vid.cover.defaultUrl && vid.cover.defaultUrl.url) ||
        '';
    }
    return {
      url,
      is_video: Boolean(vid),
      poster,
      name: img.name || m.desc || m.name || (vid && vid.id) || m.filename || '(未命名)',
      lloc: img.lloc || m.lloc || (vid && vid.id) || m.media_id || '',
    };
  });
}
