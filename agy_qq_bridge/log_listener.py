"""
日志监听与流式增量捕获模块
异步监控 AGY transcript.jsonl，实现增量偏移寻址（seek）、截断自愈与大模型最终回复广播
"""
import os
import glob
import json
import time
import asyncio
from pathlib import Path
from typing import Optional, Set, Callable, Awaitable

from .config import (
    BRAIN_DIR,
    TARGET_CONV_ID,
    EXCLUDE_CONV_IDS,
    LAST_CONV_FILE,
    setup_logger,
)
from .session_manager import get_workspace_conv_id, transcript_has_prompt
from .terminal.base import BaseTerminalManager

logger = setup_logger("agy_qq_bridge.log_listener")


class LogListener:
    """AGY 脑部结构化日志 (transcript.jsonl) 增量监听器"""

    def __init__(
        self,
        terminal_manager: BaseTerminalManager,
        brain_dir: Path = BRAIN_DIR,
        target_conv_id: str = TARGET_CONV_ID,
        exclude_conv_ids: Optional[Set[str]] = None,
        last_conv_file: Path = LAST_CONV_FILE,
    ):
        self.terminal_manager = terminal_manager
        self.brain_dir = brain_dir
        self.target_conv_id = target_conv_id
        self.exclude_conv_ids = exclude_conv_ids or EXCLUDE_CONV_IDS
        self.last_conv_file = last_conv_file

        self.current_log_path: Optional[Path] = None
        self.last_log_size: int = 0
        self.last_sent_timestamp: str = ""
        self.running: bool = False

    def find_latest_transcript(
        self,
        min_mtime: float,
        require_prompt: Optional[str] = None
    ) -> Optional[Path]:
        """获取与当前 AGY 进程关联的最新 transcript.jsonl 日志文件"""
        # 1. 显式指定的目标会话 ID（最高优先级）
        if self.target_conv_id:
            target_path = self.brain_dir / self.target_conv_id / ".system_generated" / "logs" / "transcript.jsonl"
            if target_path.exists():
                return target_path

        # 2. 扫描所有候选 transcript.jsonl
        pattern = str(self.brain_dir / "*" / ".system_generated" / "logs" / "transcript.jsonl")
        paths = glob.glob(pattern.replace('\\', '/'))
        if not paths:
            return None

        paths_with_mtime = []
        for p in paths:
            p_obj = Path(p)
            conv_id = p_obj.parts[-4] if len(p_obj.parts) >= 4 else ""
            if conv_id in self.exclude_conv_ids:
                continue
            try:
                mtime = os.path.getmtime(p)
                if mtime >= min_mtime:
                    paths_with_mtime.append((p_obj, mtime))
            except OSError:
                continue

        if not paths_with_mtime:
            return None

        paths_with_mtime.sort(key=lambda x: x[1], reverse=True)

        # 3. 如果需要匹配 prompt（例如向新会话发送了首条指令），优先指纹校验
        if require_prompt:
            for p_obj, mtime in paths_with_mtime:
                if transcript_has_prompt(p_obj, require_prompt):
                    return p_obj

        # 4. 根据当前工作区从 last_conversations.json 辅助匹配
        ws_conv_id = get_workspace_conv_id(self.terminal_manager.current_workspace, self.last_conv_file)
        if ws_conv_id and ws_conv_id not in self.exclude_conv_ids:
            for p_obj, mtime in paths_with_mtime:
                if p_obj.parts[-4] == ws_conv_id:
                    return p_obj

        return paths_with_mtime[0][0]

    def bind_log(self, log_path: Path, skip_history: bool = False) -> None:
        """绑定目标日志并设定增量偏移指针"""
        self.current_log_path = log_path
        if skip_history:
            # 冷启动或恢复历史会话：跳过已有历史内容，防止回灌既往回复
            try:
                self.last_log_size = log_path.stat().st_size
                with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.read().splitlines()
                for line in reversed(lines):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if obj.get("type") == "PLANNER_RESPONSE" and obj.get("source") == "MODEL":
                            ts = obj.get("created_at")
                            if ts:
                                self.last_sent_timestamp = ts
                                break
                    except Exception:
                        continue
            except OSError:
                self.last_log_size = 0
        else:
            # 运行时新建会话：从第 0 字节开始监听，确保第一条新回复不漏发
            self.last_log_size = 0
            self.last_sent_timestamp = ""

        logger.info(
            f"[Log Listener] 已绑定日志: {self.current_log_path} "
            f"(size={self.last_log_size}, last_ts={self.last_sent_timestamp}, skip_history={skip_history})"
        )

    def get_current_conv_id(self) -> Optional[str]:
        """获取当前绑定的会话 ID"""
        if self.current_log_path and len(self.current_log_path.parts) >= 4:
            return self.current_log_path.parts[-4]
        if self.target_conv_id:
            return self.target_conv_id
        return get_workspace_conv_id(self.terminal_manager.current_workspace, self.last_conv_file)

    async def start_listening(self, on_reply: Callable[[str], Awaitable[None]]) -> None:
        """纯异步增量日志监听协程"""
        self.running = True

        # 冷启动时优先绑定最新日志并跳过既往历史
        init_log = self.find_latest_transcript(time.time() - 86400.0)
        if init_log:
            self.bind_log(init_log, skip_history=True)

        while self.running:
            await asyncio.sleep(0.5)

            # 1. 尚未绑定日志，或者终端管理器显式触发重置 (/new, /reset) 时探测新日志
            if not self.current_log_path or self.terminal_manager.needs_rebind:
                try:
                    min_mtime = (
                        self.terminal_manager.fresh_time
                        if self.terminal_manager.fresh_time > 0
                        else (time.time() - 86400.0)
                    )
                    latest_log = self.find_latest_transcript(
                        min_mtime, require_prompt=self.terminal_manager.last_sent_prompt
                    )
                    if latest_log:
                        if not self.current_log_path or latest_log != self.current_log_path:
                            self.bind_log(latest_log, skip_history=False)
                        self.terminal_manager.needs_rebind = False
                        self.terminal_manager.fresh_time = 0.0
                except Exception as e:
                    logger.error(f"[Log Listener] 扫描新日志异常: {e}")

            if not self.current_log_path:
                continue

            # 2. 检测文件大小变动
            try:
                curr_size = self.current_log_path.stat().st_size
            except FileNotFoundError:
                self.current_log_path = None
                continue

            # 针对日志文件被截断/缩减导致的 log rotation 现象进行安全水位重置
            if curr_size < self.last_log_size:
                logger.info(
                    f"[Log Listener] 日志文件截断变小 (由 {self.last_log_size} 变为 {curr_size})，重置偏移指针为 0"
                )
                self.last_log_size = 0

            if curr_size <= self.last_log_size:
                continue

            # 3. 增量读取新行
            try:
                with open(self.current_log_path, 'r', encoding='utf-8', errors='replace') as f:
                    f.seek(self.last_log_size)
                    new_lines = f.read().splitlines()
            except OSError:
                continue

            # 更新指针
            self.last_log_size = curr_size

            # 4. 解析增量行
            for line in new_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                # 只捕获模型返回的最终回复内容
                if obj.get("type") == "PLANNER_RESPONSE" and obj.get("source") == "MODEL":
                    ts = obj.get("created_at")
                    if ts and self.last_sent_timestamp and ts <= self.last_sent_timestamp:
                        continue

                    content = obj.get("content", "")
                    if isinstance(content, list):
                        text = "\n".join(
                            item.get("text", "") for item in content
                            if isinstance(item, dict) and item.get("type") == "text"
                        )
                    else:
                        text = str(content)
                    text = text.strip()
                    if text:
                        logger.info(f"[Log Listener -> QQ] 捕获模型回复 (ts={ts}): {text[:100]}")
                        if ts:
                            self.last_sent_timestamp = ts
                        self.terminal_manager.is_busy = False
                        await on_reply(text)

        logger.info("[Log Listener] 监听协程已退出")
