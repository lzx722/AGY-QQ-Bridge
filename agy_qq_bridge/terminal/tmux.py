"""
Linux 平台 Tmux 虚拟终端管理器
通过 tmux session 实现后台常驻与 send-keys 物理按键模拟
"""
import asyncio
import time
import subprocess
from pathlib import Path
from typing import Optional

from .base import BaseTerminalManager
from ..config import setup_logger

logger = setup_logger("agy_qq_bridge.terminal.tmux")


class TmuxTerminalManager(BaseTerminalManager):
    """基于 Linux tmux 的终端管理器"""

    def __init__(
        self,
        session_name: str = "0",
        start_cmd: str = "cd ~ && agy --dangerously-skip-permissions",
        workspace: Optional[Path] = None,
    ):
        super().__init__(workspace=workspace)
        self.session_name = session_name
        self.start_cmd = start_cmd

    async def start(
        self,
        fresh: bool = False,
        conversation_id: Optional[str] = None,
        cwd: Optional[Path] = None,
    ) -> None:
        """重启 tmux session 并拉起 AGY"""
        target_cwd = cwd if (cwd and cwd.exists()) else self.current_workspace
        self.is_busy = False

        if conversation_id:
            self.fresh_time = 0.0
            self.needs_rebind = False
        elif fresh:
            self.fresh_time = time.time()
            self.last_sent_prompt = ""
            self.needs_rebind = True
        else:
            self.fresh_time = 0.0
            self.needs_rebind = True

        logger.info(f"[Tmux Run] 正在重启 tmux session [{self.session_name}] (cwd={target_cwd})...")

        # 1. 强杀现有 tmux session
        proc_kill = await asyncio.create_subprocess_shell(
            f"tmux kill-session -t {self.session_name} 2>/dev/null || true"
        )
        await proc_kill.communicate()
        await asyncio.sleep(0.5)

        # 2. 强建 tmux session 并指定工作目录
        proc_new = await asyncio.create_subprocess_exec(
            "tmux", "new-session", "-d", "-s", self.session_name, "-c", str(target_cwd)
        )
        await proc_new.communicate()
        if cwd and cwd.exists():
            self.current_workspace = target_cwd.resolve()
        await asyncio.sleep(1.5)

        # 3. 启动 AGY
        if conversation_id:
            cmd = f"agy --dangerously-skip-permissions --conversation {conversation_id}"
        elif fresh:
            # 全新启动，不续接
            cmd = self.start_cmd
        else:
            # 尝试续接（如果命令未指定 -c 则补充）
            cmd = self.start_cmd if "-c" in self.start_cmd else f"{self.start_cmd} -c"

        logger.info(f"[Tmux Run] 发送启动命令: {cmd}")
        proc_start = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", f"{self.session_name}:", cmd, "Enter"
        )
        await proc_start.communicate()
        logger.info(f"[Tmux Run] Tmux session [{self.session_name}] 启动就绪。")

    async def send_message(self, msg: str) -> None:
        """模拟按键发送消息到 tmux"""
        self.is_busy = True
        self.last_sent_time = time.time()
        self.last_sent_prompt = msg.strip()

        logger.info(f"[Tmux -> AGY] 写入消息至 session {self.session_name}: {msg[:100]}")
        # 模拟按 Escape 强退可能卡在 TUI 或 PAGER 的状态
        proc_esc = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", f"{self.session_name}:", "Escape", ""
        )
        await proc_esc.communicate()
        await asyncio.sleep(0.3)

        # 写入消息并敲回车
        proc_msg = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", f"{self.session_name}:", msg, ""
        )
        await proc_msg.communicate()
        await asyncio.sleep(0.1)

        proc_enter = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", f"{self.session_name}:", "Enter", ""
        )
        await proc_enter.communicate()

    async def send_ctrl_c(self) -> None:
        """智能安全发送中断信号"""
        if not self.is_busy and (time.time() - self.last_sent_time > 60 or self.last_sent_time == 0):
            # 空闲状态防误退，仅发 Escape 清理残余输入
            logger.info("[Tmux] 当前空闲，发送 Escape 防误退")
            proc = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", f"{self.session_name}:", "Escape", ""
            )
            await proc.communicate()
        else:
            logger.info("[Tmux] 正在执行任务，发送 C-c 与 Escape 中断")
            for key in ["C-c", "Escape"]:
                proc = await asyncio.create_subprocess_exec(
                    "tmux", "send-keys", "-t", f"{self.session_name}:", key, ""
                )
                await proc.communicate()
                await asyncio.sleep(0.2)
        self.is_busy = False

    def terminate(self) -> None:
        """关闭 tmux session"""
        try:
            subprocess.run(["tmux", "kill-session", "-t", self.session_name], check=False)
        except Exception:
            pass

    def is_alive(self) -> bool:
        """检查 tmux session 是否存活"""
        try:
            res = subprocess.run(
                ["tmux", "has-session", "-t", self.session_name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return res.returncode == 0
        except Exception:
            return False

    def get_info(self) -> str:
        return f"tmux (session: `{self.session_name}`)"
