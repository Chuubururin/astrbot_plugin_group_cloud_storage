"""distribution 纯逻辑件的直接单测（自 DistributorService 抽出后钉住公开面）。

行为级覆盖仍走服务接缝（test_distributor.py / test_distributor_render_bounds.py）；
这里只测模块本身，防止再被绕回私有方法。
"""

from __future__ import annotations

from core.application.distribution import media_spec


def _entry(name, url):
    return {"image": {"name": name, "photoUrls": [{"url": {"url": url}}]}}


def test_select_media_entry_matches_stem_when_ext_dropped():
    media = [_entry("other.png", "http://cdn/other"), _entry("logo", "http://cdn/logo")]
    picked = media_spec.select_media_entry(media, "logo.png")
    assert media_spec.extract_media_url(picked) == "http://cdn/logo"


def test_select_media_entry_no_substring_false_positive():
    # BUG-16: "a" must not match "data" via containment — with no real match
    # the fallback is the FIRST entry, not the containment hit.
    media = [_entry("other.png", "http://cdn/other"), _entry("data.png", "http://cdn/data")]
    picked = media_spec.select_media_entry(media, "a")
    assert media_spec.extract_media_url(picked) == "http://cdn/other"


def test_select_media_url_prefers_largest_spec():
    media = [
        {
            "name": "照片.png",
            "image": {
                "photoUrls": [
                    {"url": {"url": "http://cdn/photo/400?w5=400&h5=400"}},
                    {"url": {"url": "http://cdn/photo/0?w5=800&h5=800"}},
                ]
            },
        }
    ]
    assert media_spec.select_media_url(media, "照片.png") == "http://cdn/photo/0?w5=800&h5=800"


def test_select_media_url_falls_back_to_any_entry_with_url():
    media = [{"name": "broken.png", "image": {}}, _entry("ok.png", "http://cdn/ok")]
    assert media_spec.select_media_url(media, "broken.png") == "http://cdn/ok"


def test_select_media_url_raises_when_no_url_anywhere():
    import pytest

    with pytest.raises(ValueError, match="album media url unavailable"):
        media_spec.select_media_url([{"name": "x.png"}], "x.png")
