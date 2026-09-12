"""
虚拟终端管理器抽象基类
定义多平台终端（Linux tmux 与 Windows ConPTY）的标准生命周期与交互接口
"""
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional


class BaseTerminalManager(ABC):
    """虚拟终端核心接口"""

    def __init__(self, workspace: Optional[Path] = None):
        self.current_workspace: Path = (workspace or Path.cwd()).resolve()
        self.is_busy: bool = False
        self.needs_rebind: bool = False
        self.fresh_time: float = 0.0
        self.last_sent_prompt: str = ""
        self.last_sent_time: float = 0.0

    @abstractmethod
    async def start(
        self,
        fresh: bool = False,
        conversation_id: Optional[str] = None,
        cwd: Optional[Path] = None
    ) -> None:
        """
        启动或重启虚拟终端与 AGY 常驻进程
        :param fresh: 是否为全新不续接会话（不带 -c）
        :param conversation_id: 是否恢复续接特定会话（--conversation <id>）
        :param cwd: 启动工作目录
        """
        pass

    @abstractmethod
    async def send_message(self, msg: str) -> None:
        """模拟物理键盘输入向终端写入消息并回车执行"""
        pass

    @abstractmethod
    async def send_ctrl_c(self) -> None:
        """
        向终端发送中断信号（Ctrl+C / Escape）打断当前执行，
        需具备空闲状态防误退与自愈保活能力
        """
        pass

    @abstractmethod
    def terminate(self) -> None:
        """终止当前终端或进程"""
        pass

    @abstractmethod
    def is_alive(self) -> bool:
        """检查终端或进程是否存活"""
        pass

    @abstractmethod
    def get_info(self) -> str:
        """返回当前终端类型与运行时诊断信息"""
        pass
