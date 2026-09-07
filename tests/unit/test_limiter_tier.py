"""动作分级限速测试：读类放宽、写类/未知保守。"""

from adapters.limiter.tier import READ_MULT, interval_mult


def test_read_actions_get_relaxed_interval():
    for action in (
        "get_group_root_files",
        "get_group_files_by_folder",
        "get_group_file_url",
        "get_group_fs_info",
        "get_group_member_list",
        "get_image",
        "get_login_info",
    ):
        assert interval_mult(action) == READ_MULT, action


def test_write_and_unknown_actions_keep_base_interval():
    for action in (
        "upload_group_file",
        "delete_group_file",
        "create_group_file_folder",
        "rename_group_file",
        "move_group_file",
        "send_group_msg",
        "set_group_file_forever",
    ):
        assert interval_mult(action) == 1.0, action
    # 未知动作（未来新增的写类）宁慢勿漏
    assert interval_mult("some_new_action") == 1.0
