"""QQ 相册媒体条目的名称匹配与直链择优——纯函数（自 DistributorService 抽出）。

条目形状随适配端而变：QQ NT 返回 camelCase（image.photoUrls[].url.url、
image.defaultUrl.url、video.videoUrl[]），legacy NapCat 返回 snake_case
（image.photo_url、video.video_url[]）。规格列表按最大像素面积择优，与画廊
前端归一化语义一致（album-media.js byAreaDesc）。
"""

from __future__ import annotations

# 相册入口类型校验用扩展名集合（与 cloud_ingest 对齐）
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".wmv"}


def pick_largest_spec(specs: list) -> dict:
    """Pick the best spec (front-end byAreaDesc semantics, hardened).

    QQ NT album photoUrls/videoUrl lists order small thumbnails first;
    taking the first entry silently degrades distributed media to the
    lowest-resolution variant. Field-proven ranking (live 2026-09-11:
    the same photo is served as two lloc variants — a hi-res one whose
    /800 tail returns 95KB and a lo-res twin whose every tail returns
    2659B; declared width/height was stale on the lo-res variant):
    1. w5*h5 from the URL query — QQ's real pixel size of that variant
    2. declared width*height
    3. URL tail spec — "/0" is QQ's original-image spec (highest),
       then descending numeric tails ("800" > "400" > "200")
    """
    from urllib.parse import parse_qs

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    def rank(p):
        u = (p or {}).get("url") or {}
        if not isinstance(u, dict):
            return (0, 0, 0)
        url = u.get("url") or ""
        real_px = tail_spec = 0
        if isinstance(url, str) and url:
            path, _, query = url.partition("?")
            qs = parse_qs(query.split("#", 1)[0])
            w5 = _int((qs.get("w5") or ["0"])[0])
            h5 = _int((qs.get("h5") or ["0"])[0])
            real_px = w5 * h5
            tail = path.rstrip("/").rsplit("/", 1)[-1]
            try:
                tail_num = int(tail)
            except ValueError:
                tail_num = -1
            tail_spec = float("inf") if tail_num == 0 else max(tail_num, 0)
        return (
            real_px,
            tail_spec,
            _int(u.get("width")) * _int(u.get("height")),
        )

    return max(specs or [], key=rank) if specs else {}


def first_spec_url(specs: list) -> str:
    """URL of the largest spec; accept both {url:{url}} and flat url."""
    p = pick_largest_spec(specs)
    u = (p or {}).get("url")
    if isinstance(u, dict) and u.get("url"):
        return str(u["url"])
    if isinstance(u, str) and u:
        return u
    return ""


def extract_media_url(m: dict) -> str:
    """Support both shapes: flat {url} and the nested QQ album image.

    Spec lists resolve to the largest pixel area via pick_largest_spec.
    """
    if not isinstance(m, dict):
        return ""
    flat = m.get("url") or m.get("file") or ""
    if isinstance(flat, str) and flat:
        return flat
    image = m.get("image") or {}
    for key in ("photoUrls", "photo_url"):
        url = first_spec_url(image.get(key) or [])
        if url:
            return url
    default_url = image.get("defaultUrl")
    if isinstance(default_url, dict) and default_url.get("url"):
        return str(default_url["url"])
    video = m.get("video") or {}
    if isinstance(video.get("url"), str) and video["url"]:
        return video["url"]
    for key in ("videoUrl", "video_url"):
        url = first_spec_url(video.get(key) or [])
        if url:
            return url
    cover = video.get("cover") or {}
    for key in ("photoUrls", "photo_url"):
        url = first_spec_url(cover.get(key) or [])
        if url:
            return url
    return ""


def _entry_display_name(m: dict) -> str:
    image = m.get("image") if isinstance(m.get("image"), dict) else {}
    video = m.get("video") if isinstance(m.get("video"), dict) else {}
    # QQ NT entries carry the display name inside image.name; legacy shapes
    # keep it at the top level.
    return str(
        m.get("name")
        or image.get("name")
        or m.get("desc")
        or m.get("filename")
        or video.get("name")
        or ""
    ).strip().lower()


def select_media_entry(media: list, name: str) -> dict:
    """Match an album media entry by filename; fall back to the first entry.

    QQ album entries often drop the file extension in image.name (live
    2026-09-11: "logo.png" stored as "logo"); compare both forms so the
    requested file is actually found instead of silently falling back to
    the first entry (a different photo). BUG-16: exact match or
    filename-prefix match only — no substring containment (avoids "a"
    matching "data").
    """
    picked = media[0]
    want = str(name or "").strip().lower()
    if not want:
        return picked
    want_stem = want.rsplit(".", 1)[0] if "." in want else want

    def _forms(value: str) -> tuple:
        v = value.strip().lower()
        stem = v.rsplit(".", 1)[0] if "." in v else v
        return (v, stem)

    for m in media:
        if not isinstance(m, dict):
            continue
        cand = _entry_display_name(m)
        if not cand:
            continue
        hit = cand == want or cand == want_stem
        if not hit:
            for c in _forms(cand):
                if c and (c.startswith(want_stem) or want_stem.startswith(c)):
                    hit = True
                    break
            if not hit and "." in want:
                hit = cand.startswith(want) or want.startswith(cand)
        if hit:
            picked = m
            break
    return picked


def select_media_url(media: list, name: str) -> str:
    """Direct link of the entry matching ``name``; falls back to the first
    entry, then to any entry that carries a link. Raises ValueError when no
    entry has a usable url."""
    picked = select_media_entry(media, name)
    url = extract_media_url(picked)
    if not url:
        for m in media:
            url = extract_media_url(m)
            if url:
                break
    if not url:
        raise ValueError("album media url unavailable")
    return str(url)
