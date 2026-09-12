"""
Windows 平台 ConPTY 虚拟终端管理器
通过 pywinpty 的 PtyProcess 实现原生 ConPTY 桥接、终端握手自动应答与进程自愈
"""
import time
import shlex
import asyncio
from pathlib import Path
from typing import Optional

from .base import BaseTerminalManager
from ..config import setup_logger

logger = setup_logger("agy_qq_bridge.terminal.winpty")

try:
    from winpty import PtyProcess
except ImportError:
    PtyProcess = None


class WinptyTerminalManager(BaseTerminalManager):
    """基于 Windows ConPTY 的终端管理器"""

    def __init__(
        self,
        start_cmd: str = "C:\\Users\\Administrator\\AppData\\Local\\agy\\bin\\agy.exe --dangerously-skip-permissions",
        workspace: Optional[Path] = None,
    ):
        super().__init__(workspace=workspace)
        self.start_cmd = start_cmd
        self.proc: Optional[PtyProcess] = None
        self.write_lock = asyncio.Lock()
        self.loop = None

    async def start(
        self,
        fresh: bool = False,
        conversation_id: Optional[str] = None,
        cwd: Optional[Path] = None,
    ) -> None:
        """在后台虚拟终端中拉起并常驻运行 AGY CLI"""
        if PtyProcess is None:
            raise RuntimeError("pywinpty 未安装，请在 Windows 环境下执行: pip install pywinpty")

        self.loop = asyncio.get_running_loop()
        self.is_busy = False
        target_cwd = cwd if (cwd and cwd.exists()) else self.current_workspace

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

        if self.proc:
            self.terminate()

        logger.info("[AGY WinPTY] 正在 Windows ConPTY 中启动常驻 AI 进程...")
        try:
            cmd_parts = [p.strip('"\'') for p in shlex.split(self.start_cmd, posix=False)]
            if conversation_id:
                cmd_parts.extend(["--conversation", conversation_id])
            elif not fresh:
                cmd_parts.append("-c")

            logger.info(f"[AGY WinPTY] 启动命令: {cmd_parts} (工作区: {target_cwd})")
            self.proc = PtyProcess.spawn(cmd_parts, cwd=str(target_cwd), dimensions=(40, 160))
            if cwd and cwd.exists():
                self.current_workspace = target_cwd.resolve()

            # 启动后台异步读取，清空 PTY 缓冲区并自动应答 TUI 设备属性探测
            asyncio.create_task(self._pty_stdout_drainer())
            logger.info(f"[AGY WinPTY] AI 进程 (PID={self.proc.pid}) 拉起成功，已在后台保持常驻。")
        except Exception as e:
            logger.error(f"[AGY WinPTY] ConPTY 启动失败: {e}")

    async def _pty_stdout_drainer(self) -> None:
        """持续清空 PTY 输出缓冲区并响应终端握手"""
        while self.proc and self.proc.isalive():
            try:
                data = await self.loop.run_in_executor(None, self.proc.read, 4096)
                if not data:
                    await asyncio.sleep(0.05)
                    continue
                # 关键修复：agy 启动时会发送 \x1b[c 探测终端能力，必须应答 \x1b[?1;2c 才能打破启动挂起
                if "\x1b[c" in data:
                    logger.info("[AGY WinPTY] 捕获到终端握手请求 (\\x1b[c)，已自动应答。")
                    self.proc.write("\x1b[?1;2c")
            except EOFError:
                break
            except Exception:
                break

    def terminate(self) -> None:
        """强行关闭当前常驻终端"""
        logger.info("[AGY WinPTY] 正在关闭常驻 AI 进程...")
        if self.proc:
            try:
                self.proc.terminate(force=True)
            except Exception:
                pass
            self.proc = None

    async def send_message(self, msg: str) -> None:
        """模拟物理键盘输入将消息送给 AI 进程"""
        if not self.proc or not self.proc.isalive():
            logger.warning("[AGY WinPTY] AI 进程未启动，正在重新拉起...")
            await self.start(fresh=False)
            await asyncio.sleep(2.0)

        async with self.write_lock:
            try:
                logger.info(f"[Bridge -> AGY WinPTY] 写入消息: {msg[:100]}")
                self.last_sent_prompt = msg.strip()
                self.last_sent_time = time.time()
                self.is_busy = True
                # 发送 Escape 强退可能卡在 TUI 或 PAGER 的状态 (Windows下对应 \x1b)
                self.proc.write("\x1b")
                await asyncio.sleep(0.3)
                # 写入消息并敲回车 \r\n
                self.proc.write(f"{msg}\r\n")
            except Exception as e:
                logger.error(f"[AGY WinPTY] 写入虚拟终端失败: {e}")

    async def send_ctrl_c(self) -> None:
        """向常驻进程安全发送中断信号，打断正在执行的任务而不杀死空闲终端"""
        if self.proc and self.proc.isalive():
            async with self.write_lock:
                try:
                    logger.info("[AGY WinPTY] 正在发送中断信号 (Ctrl+C)...")
                    # 单次 Ctrl+C 打断当前正在执行的子命令或生成
                    self.proc.write("\x03")
                    await asyncio.sleep(0.2)
                    # 发送 Escape 确保退出所有残留交互，干净退回到提示符
                    self.proc.write("\x1b")
                except Exception as e:
                    logger.error(f"[AGY WinPTY] 发送中断信号失败: {e}")
            # 检查进程是否因极端异常退出了，若是则立刻在后台无缝拉起保活
            await asyncio.sleep(0.3)
            if not self.proc or not self.proc.isalive():
                logger.warning("[AGY WinPTY] 终端进程在中断后退出，正在自动拉起恢复...")
                await self.start(fresh=False)
        self.is_busy = False

    def is_alive(self) -> bool:
        return bool(self.proc and self.proc.isalive())

    def get_info(self) -> str:
        pid_str = f"PID: {self.proc.pid}" if (self.proc and self.proc.isalive()) else "已停止"
        return f"Windows ConPTY ({pid_str})"
