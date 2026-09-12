"""
终端层导出与自适应工厂函数
"""
import sys
from typing import Optional
from pathlib import Path

from .base import BaseTerminalManager
from .tmux import TmuxTerminalManager

try:
    from .winpty import WinptyTerminalManager
except ImportError:
    WinptyTerminalManager = None

__all__ = [
    "BaseTerminalManager",
    "TmuxTerminalManager",
    "WinptyTerminalManager",
    "create_terminal_manager",
]


def create_terminal_manager(
    backend: Optional[str] = None,
    start_cmd: Optional[str] = None,
    workspace: Optional[Path] = None,
    tmux_session: str = "0",
) -> BaseTerminalManager:
    """根据运行平台或显式指定的 backend 创建对应的终端管理器"""
    chosen = backend
    if not chosen:
        chosen = "winpty" if sys.platform == "win32" else "tmux"

    chosen = chosen.lower()
    if chosen in ["winpty", "windows", "conpty"]:
        if WinptyTerminalManager is None:
            raise RuntimeError("WinptyTerminalManager 不可用，请确保在 Windows 环境下并安装了 pywinpty。")
        kwargs = {}
        if start_cmd:
            kwargs["start_cmd"] = start_cmd
        if workspace:
            kwargs["workspace"] = workspace
        return WinptyTerminalManager(**kwargs)
    elif chosen in ["tmux", "linux"]:
        kwargs = {"session_name": tmux_session}
        if start_cmd:
            kwargs["start_cmd"] = start_cmd
        if workspace:
            kwargs["workspace"] = workspace
        return TmuxTerminalManager(**kwargs)
    else:
        raise ValueError(f"未知的终端后端类型: {backend}，仅支持 'tmux' 或 'winpty'。")
