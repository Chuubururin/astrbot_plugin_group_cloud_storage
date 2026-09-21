"""OneBot mixin 空响应兜底回归：data: null 不得对 None 调 .get()。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.onebot.napcat import NapCatApiAdapter  # noqa: E402
from core.domain.resource import FileSystemInfo, GroupFileList  # noqa: E402


def _api(payload):
    async def impl(action, **params):
        return payload

    return NapCatApiAdapter(impl, interval=0)


@pytest.mark.asyncio
async def test_list_group_root_tolerates_null_data():
    """`data: null` 是合法的 OneBot 回复；_parse_file_list 会调 data.get()。"""
    fl = await _api(None).list_group_root("g1")
    assert isinstance(fl, GroupFileList)
    assert fl.files == [] and fl.folders == []


@pytest.mark.asyncio
async def test_list_group_folder_tolerates_null_data():
    fl = await _api(None).list_group_folder("g1", folder_id="d1")
    assert isinstance(fl, GroupFileList)
    assert fl.files == []


@pytest.mark.asyncio
async def test_get_group_fs_info_tolerates_null_data():
    info = await _api(None).get_group_fs_info("g1")
    assert isinstance(info, FileSystemInfo)
    assert info.file_count == 0 and info.limit_count == 0


@pytest.mark.asyncio
async def test_null_data_does_not_mask_real_payload():
    """回归边界：有数据时仍正常解析。"""
    api = _api({"files": [{"file_id": "f1", "file_name": "a.zip", "file_size": 7}],
                "folders": [{"folder_id": "d1", "folder_name": "Docs"}]})
    fl = await api.list_group_root("g1")
    assert [f.name for f in fl.files] == ["a.zip"]
    assert [d.name for d in fl.folders] == ["Docs"]
