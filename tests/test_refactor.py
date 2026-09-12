"""
自动化单元测试套件
验证重构后各模块的逻辑正确性、异常防护及向后兼容性
"""
import sys
import time
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agy_qq_bridge.session_manager import (
    validate_rename_title,
    parse_history_range,
    parse_resume_arg,
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

    # 非法输入：负数（禁止被错误识别为 resume 2）
    for neg in ["-2", "-10", "#-2"]:
        ok, err = validate_rename_title(neg)
        assert not ok, f"Expected negative '{neg}' to be rejected"
        assert "负数" in err, f"Expected error to mention 负数, got: {err}"
        assert "/resume 2" not in err, f"Should NOT suggest resume 2 for negative {neg}"

    # 非法输入：0
    for zero in ["0", "#0", "[0]"]:
        ok, err = validate_rename_title(zero)
        assert not ok, f"Expected zero '{zero}' to be rejected"
        assert "0" in err, f"Expected error to mention 0, got: {err}"

    # 非法输入：小数 / 浮点数
    for dec in ["2.5", "0.5", "-1.5", "1.0", ".5", "#2.5"]:
        ok, err = validate_rename_title(dec)
        assert not ok, f"Expected decimal '{dec}' to be rejected"
        assert "小数" in err, f"Expected error to mention 小数, got: {err}"

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
    assert not ok, "Expected UUID to be rejected"

    print("  -> Passed!")


def test_shorten_workspace():
    print("[Test] shorten_workspace...")
    assert shorten_workspace("") == "默认"
    assert shorten_workspace(str(Path.home())) == "~"
    assert shorten_workspace("E:/Git/AGY-QQ-Bridge") in ["AGY-QQ-Bridge", "E:\\Git\\AGY-QQ-Bridge", "E:/Git/AGY-QQ-Bridge"]
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

    # 异常格式报错：0、负数与小数
    s, e, err = parse_history_range(["/history", "0"], total)
    assert err is not None and "大于 0" in err, f"Expected 0 error, got: {err}"

    s, e, err = parse_history_range(["/history", "-5"], total)
    assert err is not None and "负数" in err, f"Expected negative error, got: {err}"

    s, e, err = parse_history_range(["/history", "2.5"], total)
    assert err is not None and "小数" in err, f"Expected decimal error, got: {err}"

    s, e, err = parse_history_range(["/history", "1.5-3.5"], total)
    assert err is not None and "小数" in err, f"Expected decimal range error, got: {err}"

    s, e, err = parse_history_range(["/history", "-1-5"], total)
    assert err is not None and ("负数" in err or "1 开始" in err), f"Expected negative range error, got: {err}"

    s, e, err = parse_history_range(["/history", "0-5"], total)
    assert err is not None and ("0" in err or "1 开始" in err), f"Expected zero range error, got: {err}"

    s, e, err = parse_history_range(["/history", "1.5", "3.5"], total)
    assert err is not None and "小数" in err, f"Expected decimal error, got: {err}"

    s, e, err = parse_history_range(["/history", "-1", "5"], total)
    assert err is not None and ("负数" in err or "1 开始" in err), f"Expected negative error, got: {err}"

    s, e, err = parse_history_range(["/history", "0", "5"], total)
    assert err is not None and ("0" in err or "1 开始" in err), f"Expected zero error, got: {err}"

    s, e, err = parse_history_range(["/history", "p2.5"], total)
    assert err is not None and "小数" in err, f"Expected decimal error, got: {err}"

    s, e, err = parse_history_range(["/history", "page", "-2"], total)
    assert err is not None and "大于等于 1" in err, f"Expected page error, got: {err}"

    s, e, err = parse_history_range(["/history", "abc"], total)
    assert err is not None, "Expected error on abc"

    print("  -> Passed!")


def test_parse_resume_arg():
    print("[Test] parse_resume_arg...")
    # 合法单正整数输入
    idx, err = parse_resume_arg(["/resume", "1"])
    assert (idx, err) == (1, None), f"Got {(idx, err)}"

    idx, err = parse_resume_arg(["/resume", "#2"])
    assert (idx, err) == (2, None), f"Got {(idx, err)}"

    idx, err = parse_resume_arg(["/切换", "15"])
    assert (idx, err) == (15, None), f"Got {(idx, err)}"

    idx, err = parse_resume_arg(["/resume", "[3]"])
    assert (idx, err) == (3, None), f"Got {(idx, err)}"

    # 无参数
    idx, err = parse_resume_arg(["/resume"])
    assert idx is None and "请提供要恢复的会话编号" in err

    # 负数参数拦截（核心防护：绝不能误解析为正数）
    for neg in ["-2", "#-2", "-10"]:
        idx, err = parse_resume_arg(["/resume", neg])
        assert idx is None, f"Expected negative '{neg}' to be rejected"
        assert "负数" in err, f"Expected error to mention 负数, got: {err}"
        assert idx != 2, "Negative number must NOT be stripped to 2!"

    # 0 拦截
    for zero in ["0", "#0", "[0]"]:
        idx, err = parse_resume_arg(["/resume", zero])
        assert idx is None, f"Expected zero '{zero}' to be rejected"
        assert "1 开始" in err or "0" in err, f"Expected error to mention 1 开始, got: {err}"

    # 小数 / 浮点数拦截
    for dec in ["2.5", "0.5", "-1.5", "#2.5"]:
        idx, err = parse_resume_arg(["/resume", dec])
        assert idx is None, f"Expected decimal '{dec}' to be rejected"
        assert "小数" in err, f"Expected error to mention 小数, got: {err}"

    # 多参数及两数范围拦截
    idx, err = parse_resume_arg(["/resume", "1", "2"])
    assert idx is None and "两数范围" in err

    idx, err = parse_resume_arg(["/resume", "page", "4"])
    assert idx is None and "分页参数" in err

    # 连字符范围拦截及引导
    idx, err = parse_resume_arg(["/resume", "31-40"])
    assert idx is None and "/history 31-40" in err

    idx, err = parse_resume_arg(["/resume", "1.5-3.5"])
    assert idx is None and "小数" in err

    # 分页拦截及引导
    idx, err = parse_resume_arg(["/resume", "p4"])
    assert idx is None and "/history p4" in err

    idx, err = parse_resume_arg(["/resume", "p1.5"])
    assert idx is None and "小数" in err

    # 非法字符串
    idx, err = parse_resume_arg(["/resume", "abc"])
    assert idx is None and "格式无效" in err

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


def test_bridge_resume_command():
    print("[Test] BridgeApp /resume command execution...")
    import asyncio
    sent_replies = []

    class MockQQClient:
        def __init__(self):
            self.bot_openid = "bot_1"
            self.master_openid = "test_user"

        def is_duplicate(self, msg_id):
            return False

        async def send_c2c_message(self, openid, content):
            sent_replies.append(content)
            return True

    tmux_mgr = TmuxTerminalManager(session_name="dummy_0")
    app = BridgeApp(terminal_manager=tmux_mgr, master_openid="test_user", app_id="test_id", client_secret="test_sec")
    app.qq_client = MockQQClient()

    # 模拟用户发送 /resume 2，确保不会触发 NameError: name 're' is not defined
    async def run_test():
        await app.handle_c2c_message({
            "id": "msg_test_1",
            "content": "/resume 2",
            "author": {"user_openid": "test_user"}
        })

    asyncio.run(run_test())
    assert len(sent_replies) == 1
    # 此时因为没有先查 history，会提示请先发送 /history
    assert "尚未查询过历史会话列表" in sent_replies[0] or "未找到" in sent_replies[0]
    print("  -> Passed!")


def test_menu_panel_defaults():
    print("[Test] menu_panel default configurations...")
    from agy_qq_bridge.menu_panel import (
        DEFAULT_CUSTOM_MENU,
        DEFAULT_C2C_PANEL,
        DEFAULT_GROUP_PANEL,
        _make_headers,
    )

    # 1. 验证菜单格式与长度约束
    menu_items = DEFAULT_CUSTOM_MENU.get("menu", {}).get("items", [])
    assert len(menu_items) > 0
    for item in menu_items:
        if item.get("type") == "menu":
            sub_items = item.get("sub_menu_items", [])
            assert len(sub_items) <= 5, "QQ开放平台约束：二级菜单最多 5 项"
            for sub in sub_items:
                assert len(sub.get("name", "")) <= 14, "QQ开放平台约束：二级菜单名称 <= 14 字符"

    # 2. 验证面板格式与长度约束 (严防 40030013 超出数量限制)
    for p_cfg in [DEFAULT_C2C_PANEL, DEFAULT_GROUP_PANEL]:
        items = p_cfg.get("panel", {}).get("items", [])
        assert len(items) <= 20, "QQ开放平台约束：面板项 <= 20"
        for item in items:
            desc = item.get("desc", "")
            assert len(desc) <= 15, f"QQ开放平台严重约束：desc '{desc}' 超过 15 字符会导致 40030013 错误！"

    # 3. 验证鉴权请求头格式
    headers = _make_headers("dummy_token")
    assert headers["Authorization"] == "QQBot dummy_token"
    print("  -> Passed!")


if __name__ == "__main__":
    test_validate_rename_title()
    test_shorten_workspace()
    test_parse_history_range()
    test_parse_resume_arg()
    test_session_history_tracker()
    test_terminal_factory()
    test_bridge_instantiation()
    test_bridge_resume_command()
    test_menu_panel_defaults()
    print("\n[SUCCESS] ALL TESTS PASSED SUCCESSFULLY!")
