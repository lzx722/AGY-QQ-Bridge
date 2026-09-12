"""
自动化单元测试套件
验证重构后各模块的逻辑正确性、异常防护及向后兼容性
"""
import sys
import time
from pathlib import Path

# 添加 src 到路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agy_qq_bridge.session_manager import (
    validate_rename_title,
    parse_history_range,
    shorten_workspace,
    SessionHistoryTracker,
)
from agy_qq_bridge.terminal import (
    create_terminal_manager,
    TmuxTerminalManager,
    WinptyTerminalManager,
)
from agy_qq_bridge.bridge import BridgeApp


def test_validate_rename_title():
    print("[Test] validate_rename_title...")
    # 合法输入
    valid_titles = ["优化登录逻辑", "修复QQ机器人Bug", "重构前后端接口", "A cool new feature"]
    for t in valid_titles:
        ok, err = validate_rename_title(t)
        assert ok, f"Expected '{t}' to be valid, got err: {err}"

    # 非法输入：指令
    for cmd in ["/status", "status", "/help", "help", "/new", "new", "/stop", "stop"]:
        ok, err = validate_rename_title(cmd)
        assert not ok, f"Expected '{cmd}' to be rejected"

    # 非法输入：resume 指令与参数
    for res in ["/resume", "resume", "/resume 1", "resume 2", "/switch 3"]:
        ok, err = validate_rename_title(res)
        assert not ok, f"Expected '{res}' to be rejected"

    # 非法输入：history 指令与参数
    for hist in ["/history", "history", "/history 10", "/history 31-40", "/history p4", "history page 2"]:
        ok, err = validate_rename_title(hist)
        assert not ok, f"Expected '{hist}' to be rejected"

    # 非法输入：纯数字及变体
    for num in ["1", "12", "#1", "#15", "[1]", "(2)", "3."]:
        ok, err = validate_rename_title(num)
        assert not ok, f"Expected '{num}' to be rejected"

    # 非法输入：范围参数
    for r in ["31-40", "31~40", "31..40", "31 40"]:
        ok, err = validate_rename_title(r)
        assert not ok, f"Expected range '{r}' to be rejected"

    # 非法输入：分页参数
    for p in ["p4", "page 4", "页 4", "10 p4", "10 page 4"]:
        ok, err = validate_rename_title(p)
        assert not ok, f"Expected page '{p}' to be rejected"

    # 非法输入：UUID
    uuid_str = "0ef404f5-1934-460a-aaca-c41dfe2993db"
    ok, err = validate_rename_title(uuid_str)
    assert not ok, f"Expected UUID to be rejected"

    print("  -> Passed!")


def test_parse_history_range():
    print("[Test] parse_history_range...")
    total = 50

    # 默认
    s, e, err = parse_history_range(["/history"], total)
    assert (s, e, err) == (1, 5, None), f"Got {(s, e, err)}"

    # 指定数量
    s, e, err = parse_history_range(["/history", "10"], total)
    assert (s, e, err) == (1, 10, None), f"Got {(s, e, err)}"

    # 数量超出上限 15
    s, e, err = parse_history_range(["/history", "20"], total)
    assert (s, e, err) == (1, 15, None), f"Got {(s, e, err)}"

    # 连字符范围
    s, e, err = parse_history_range(["/history", "31-40"], total)
    assert (s, e, err) == (31, 40, None), f"Got {(s, e, err)}"

    # 波浪号范围
    s, e, err = parse_history_range(["/history", "31~40"], total)
    assert (s, e, err) == (31, 40, None), f"Got {(s, e, err)}"

    # 两数空格范围
    s, e, err = parse_history_range(["/history", "31", "40"], total)
    assert (s, e, err) == (31, 40, None), f"Got {(s, e, err)}"

    # 分页 p4
    s, e, err = parse_history_range(["/history", "p4"], total)
    assert (s, e, err) == (31, 40, None), f"Got {(s, e, err)}"

    # 分页 page 4
    s, e, err = parse_history_range(["/history", "page", "4"], total)
    assert (s, e, err) == (31, 40, None), f"Got {(s, e, err)}"

    # 异常格式报错
    s, e, err = parse_history_range(["/history", "0"], total)
    assert err is not None, "Expected error on 0"

    s, e, err = parse_history_range(["/history", "-5"], total)
    assert err is not None, "Expected error on negative"

    s, e, err = parse_history_range(["/history", "abc"], total)
    assert err is not None, "Expected error on abc"

    print("  -> Passed!")


def test_session_history_tracker():
    print("[Test] SessionHistoryTracker...")
    tracker = SessionHistoryTracker(ttl_seconds=2.0, max_jumps=3)
    dummy_convs = [
        {"cid": "c1", "final_title": "Conv 1", "workspace": "ws1"},
        {"cid": "c2", "final_title": "Conv 2", "workspace": "ws2"},
        {"cid": "c3", "final_title": "Conv 3", "workspace": "ws3"},
    ]

    # 未查询前不能 resume
    allowed, err, _ = tracker.can_resume(1)
    assert not allowed and "未找到" in err or "尚未查询" in err

    # 记录列表
    tracker.record_history(dummy_convs)

    # 正常跳转 1
    allowed, err, item = tracker.can_resume(1)
    assert allowed and item["cid"] == "c1"
    rem_jumps, rem_time = tracker.record_resume_jump()
    assert rem_jumps == 2

    # 正常跳转 2
    allowed, err, item = tracker.can_resume(2)
    assert allowed and item["cid"] == "c2"
    rem_jumps, rem_time = tracker.record_resume_jump()
    assert rem_jumps == 1

    # 正常跳转 3
    allowed, err, item = tracker.can_resume(3)
    assert allowed and item["cid"] == "c3"
    rem_jumps, rem_time = tracker.record_resume_jump()
    assert rem_jumps == 0

    # 超出 3 次跳转
    allowed, err, _ = tracker.can_resume(1)
    assert not allowed and "上限" in err

    # 重置后更新
    tracker.record_history(dummy_convs)
    # 超出范围的索引
    allowed, err, _ = tracker.can_resume(4)
    assert not allowed and "超出" in err

    allowed, err, _ = tracker.can_resume(0)
    assert not allowed and "从 1 开始" in err

    # 测试超时
    time.sleep(2.1)
    allowed, err, _ = tracker.can_resume(1)
    assert not allowed and "有效时限" in err

    print("  -> Passed!")


def test_terminal_factory():
    print("[Test] Terminal Manager Factory...")
    tmux_mgr = create_terminal_manager(backend="tmux", tmux_session="test_session")
    assert isinstance(tmux_mgr, TmuxTerminalManager)
    assert tmux_mgr.session_name == "test_session"

    if sys.platform == "win32":
        win_mgr = create_terminal_manager()
        assert isinstance(win_mgr, WinptyTerminalManager)

    print("  -> Passed!")


def test_bridge_instantiation():
    print("[Test] BridgeApp instantiation...")
    tmux_mgr = TmuxTerminalManager(session_name="dummy_0")
    app = BridgeApp(terminal_manager=tmux_mgr, app_id="test_id", client_secret="test_sec")
    assert app.terminal_manager == tmux_mgr
    assert app.qq_client.app_id == "test_id"
    print("  -> Passed!")


if __name__ == "__main__":
    test_validate_rename_title()
    test_parse_history_range()
    test_session_history_tracker()
    test_terminal_factory()
    test_bridge_instantiation()
    print("\n[SUCCESS] ALL TESTS PASSED SUCCESSFULLY!")
