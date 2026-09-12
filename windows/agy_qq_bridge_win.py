"""
agy_qq_bridge_win.py — Windows 原生 ConPTY 桥接启动入口
通过 pywinpty 驱动 Windows 伪控制台，直连 QQ 开放平台 WebSocket 网关
向后完全兼容 A启动机器人.bat 与独立脚本调用
"""
import sys
import socket
import asyncio
from pathlib import Path

# 确保能加载 src 下的 agy_qq_bridge 模块
src_dir = Path(__file__).resolve().parent.parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from agy_qq_bridge.config import (
    APP_ID,
    CLIENT_SECRET,
    MASTER_OPENID,
    AGY_CMD,
    AGY_WORKSPACE,
    setup_logger,
)
from agy_qq_bridge.session_manager import (
    parse_timestamp,
    shorten_workspace,
    parse_history_range,
    parse_resume_arg,
    validate_rename_title,
    get_conversation_title,
    rename_conversation,
    get_history_conversations,
    get_workspace_conv_id,
    transcript_has_prompt,
)
from agy_qq_bridge.diagnostics import get_local_git_status
from agy_qq_bridge.terminal.winpty import WinptyTerminalManager
from agy_qq_bridge.bridge import BridgeApp

logger = setup_logger("agy_qq_bridge_win")

# 向后兼容类名与函数导出
AgyProcessManager = WinptyTerminalManager

__all__ = [
    "AgyProcessManager",
    "WinptyTerminalManager",
    "BridgeApp",
    "parse_timestamp",
    "shorten_workspace",
    "parse_history_range",
    "parse_resume_arg",
    "validate_rename_title",
    "get_conversation_title",
    "rename_conversation",
    "get_history_conversations",
    "get_workspace_conv_id",
    "transcript_has_prompt",
    "get_local_git_status",
    "acquire_single_instance_lock",
    "main",
]

_lock_socket = None


def acquire_single_instance_lock(port: int = 28712) -> bool:
    """通过本地端口绑定防止 Windows 下误重复启动多个桥接实例"""
    global _lock_socket
    try:
        _lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _lock_socket.bind(("127.0.0.1", port))
        return True
    except socket.error:
        return False


async def main() -> None:
    """Windows 运行主入口"""
    terminal_manager = WinptyTerminalManager(
        start_cmd=AGY_CMD,
        workspace=AGY_WORKSPACE,
    )
    app = BridgeApp(
        terminal_manager=terminal_manager,
        master_openid=MASTER_OPENID,
        app_id=APP_ID,
        client_secret=CLIENT_SECRET,
    )
    await app.start()


if __name__ == "__main__":
    if not acquire_single_instance_lock():
        logger.error("检测到已有 agy_qq_bridge_win 实例在后台运行，禁止重复启动。退出。")
        sys.exit(0)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("用户手动中断退出")
