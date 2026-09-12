#!/usr/bin/env python3
"""
agy_qq_bridge_win.py — AGY Windows 常驻进程直连 C2C 桥接 QQ
架构: QQ官方WS网关 ↔ Python asyncio ↔ Windows ConPTY (pywinpty) ↔ AGY
"""
import asyncio
import json
import re
import os
import sys
import time
import datetime
import uuid
import logging
import glob
import sqlite3
import urllib.parse
from typing import Optional, Dict, Any, List, Tuple
from pathlib import Path
from winpty import PtyProcess  # Windows 运行环境依赖：pip install pywinpty
import aiohttp
import aiohttp.connector
import aiohttp.resolver
try:
    aiohttp.connector.DefaultResolver = aiohttp.resolver.ThreadedResolver
    aiohttp.connector.AsyncResolver = aiohttp.resolver.ThreadedResolver
except Exception:
    pass

# ================= 环境与配置加载 =================
def load_env(env_path: str = ".env"):
    paths = [
        Path(env_path),
        Path(__file__).parent / env_path,
        Path.home() / ".env"
    ]
    for p in paths:
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if "=" in line:
                            key, val = line.split("=", 1)
                            os.environ[key.strip()] = val.strip().strip('"').strip("'")
                break
            except Exception:
                pass

# 执行配置加载
load_env()

# ================= 配置区 =================
APP_ID = os.environ.get("APP_ID", "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")
MASTER_OPENID = os.environ.get("MASTER_OPENID", "")
API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
GATEWAY_URL_PATH = "/gateway"

CONNECT_TIMEOUT = 20
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
MAX_RECONNECT_ATTEMPTS = 100
HEARTBEAT_INTERVAL = 15.0

# 路径与启动配置
USER_PROFILE = os.environ.get("USERPROFILE", str(Path.home()))
CLI_HOME = Path(os.environ.get("CLI_HOME", str(Path(USER_PROFILE) / ".gemini" / "antigravity-cli")))
BRAIN_DIR = Path(os.environ.get("BRAIN_DIR", str(CLI_HOME / "brain")))
LOG_DIR = Path(os.environ.get("LOG_DIR", str(Path(USER_PROFILE) / ".agy-qq-bridge")))
AGY_CMD = os.environ.get("AGY_START_CMD", "C:\\Users\\Administrator\\AppData\\Local\\agy\\bin\\agy.exe --dangerously-skip-permissions")
AGY_WORKSPACE = Path(os.environ.get("AGY_WORKSPACE", USER_PROFILE)).resolve()

# 会话绑定与过滤策略（可通过 .env 配置）
TARGET_CONV_ID = os.environ.get("TARGET_CONV_ID", "").strip()
EXCLUDE_CONV_IDS = set(
    cid.strip() for cid in os.environ.get("EXCLUDE_CONV_IDS", "").split(",") if cid.strip()
)
LAST_CONV_FILE = CLI_HOME / "cache" / "last_conversations.json"
HISTORY_FILE = CLI_HOME / "history.jsonl"
CONVERSATIONS_DIR = CLI_HOME / "conversations"
CONV_SUMMARIES_DB = CLI_HOME / "conversation_summaries.db"
# ==========================================

os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "agy-qq-bridge.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("agy_qq_bridge_win")

_access_token: Optional[str] = None
_token_expires_at: float = 0.0
_session_id: Optional[str] = None
_last_seq: Optional[int] = None
_ws = None
_http_client = None
_running = False
_last_msg_id: Optional[str] = None
_bot_openid: str = ""
heartbeat_task = None

# === 异步监听状态 ===
_last_log_size = 0
_current_log_path = None
_last_sent_timestamp = ""  # 记录最后发送给 QQ 的消息时间戳
_cached_history_list: List[Dict[str, Any]] = []  # 缓存最近一次 /history 查询的会话列表，用于编号快速切换
_last_history_time: float = 0.0  # 上一次 /history 请求的时间戳
_history_resume_count: int = 0   # 当前 /history 周期内已使用 /resume 的跳转次数（上限 3 次）

import shlex

# ================= Process Manager (ConPTY) =================
class AgyProcessManager:
    def __init__(self):
        self.proc = None
        self.write_lock = asyncio.Lock()
        self.loop = None
        self.needs_rebind = False
        self.fresh_time = 0.0
        self.last_sent_prompt = ""
        self.last_sent_time = 0.0
        self.is_busy = False

    def start(self, fresh: bool = False, conversation_id: Optional[str] = None, cwd: Optional[Path] = None):
        """在后台虚拟终端中拉起并常驻运行 AGY CLI"""
        global _current_log_path, _last_sent_timestamp, _last_log_size, AGY_WORKSPACE
        self.loop = asyncio.get_running_loop()
        self.is_busy = False
        
        target_cwd = cwd if (cwd and cwd.exists()) else AGY_WORKSPACE
        
        if conversation_id:
            self.fresh_time = 0.0
            self.needs_rebind = False
        elif fresh:
            self.fresh_time = time.time()
            self.last_sent_prompt = ""
            self.needs_rebind = True
            _current_log_path = None
            _last_sent_timestamp = ""
            _last_log_size = 0
        else:
            self.fresh_time = 0.0
            self.needs_rebind = True
        
        # 如果已有运行的终端，先杀掉重置
        if self.proc:
            self.terminate()
            
        logger.info("[AGY Run] 正在 Windows ConPTY 中启动常驻 AI 进程...")
        try:
            cmd_parts = [p.strip('"\'') for p in shlex.split(AGY_CMD, posix=False)]
            if conversation_id:
                cmd_parts.extend(["--conversation", conversation_id])
            elif not fresh:
                cmd_parts.append("-c")

            logger.info(f"[AGY Run] 启动命令: {cmd_parts} (工作区: {target_cwd})")
            self.proc = PtyProcess.spawn(cmd_parts, cwd=str(target_cwd), dimensions=(40, 160))
            if cwd and cwd.exists():
                AGY_WORKSPACE = target_cwd.resolve()
            
            # 启动后台异步读取，清空 PTY 缓冲区并自动应答 TUI 设备属性探测
            asyncio.create_task(self._pty_stdout_drainer())
            logger.info(f"[AGY Run] AI 进程 (PID={self.proc.pid}) 拉起成功，已在后台保持常驻。")
        except Exception as e:
            logger.error(f"[AGY Run] ConPTY 启动失败: {e}")

    async def _pty_stdout_drainer(self):
        """持续清空 PTY 输出缓冲区并响应终端握手"""
        while self.proc and self.proc.isalive():
            try:
                data = await self.loop.run_in_executor(None, self.proc.read, 4096)
                if not data:
                    await asyncio.sleep(0.05)
                    continue
                # 关键修复：agy 启动时会发送 \x1b[c 探测终端能力，必须应答 \x1b[?1;2c 才能打破启动挂起
                if "\x1b[c" in data:
                    logger.info("[AGY Run] 捕获到终端握手请求 (\\x1b[c)，已自动应答。")
                    self.proc.write("\x1b[?1;2c")
            except EOFError:
                break
            except Exception:
                break

    def terminate(self):
        """强行关闭当前常驻终端"""
        logger.info("[AGY Run] 正在关闭常驻 AI 进程...")
        if self.proc:
            try:
                self.proc.terminate(force=True)
            except Exception:
                pass
            self.proc = None

    async def send_message(self, msg: str):
        """模拟物理键盘输入将消息送给 AI 进程"""
        if not self.proc or not self.proc.isalive():
            logger.warning("[AGY Run] AI 进程未启动，正在重新拉起...")
            self.start(fresh=False)
            await asyncio.sleep(2.0)
            
        async with self.write_lock:
            try:
                logger.info(f"[Bridge -> AGY] 写入消息: {msg}")
                self.last_sent_prompt = msg.strip()
                self.last_sent_time = time.time()
                self.is_busy = True
                # 发送 Escape 强退可能卡在 TUI 或 PAGER 的状态 (Windows下对应 \x1b)
                self.proc.write("\x1b")
                await asyncio.sleep(0.3)
                # 写入消息并敲回车 \r\n
                self.proc.write(f"{msg}\r\n")
            except Exception as e:
                logger.error(f"[AGY Run] 写入虚拟终端失败: {e}")

    async def send_ctrl_c(self):
        """向常驻进程安全发送中断信号，打断正在执行的任务而不杀死空闲终端"""
        if self.proc and self.proc.isalive():
            async with self.write_lock:
                try:
                    logger.info("[AGY Run] 正在发送中断信号 (Ctrl+C)...")
                    # 单次 Ctrl+C 打断当前正在执行的子命令或生成
                    self.proc.write("\x03")
                    await asyncio.sleep(0.2)
                    # 发送 Escape 确保退出所有残留交互，干净退回到提示符
                    self.proc.write("\x1b")
                except Exception as e:
                    logger.error(f"[AGY Run] 发送中断信号失败: {e}")
            # 检查进程是否因极端异常退出了，若是则立刻在后台无缝拉起保活
            await asyncio.sleep(0.3)
            if not self.proc or not self.proc.isalive():
                logger.warning("[AGY Run] 终端进程在中断后退出，正在自动拉起恢复...")
                self.start(fresh=False)
        self.is_busy = False

# 全局进程管理器
agy_mgr = AgyProcessManager()

def get_workspace_conv_id(workspace: Path) -> Optional[str]:
    """从 last_conversations.json 读取指定工作区的最近会话 ID"""
    if not LAST_CONV_FILE.exists():
        return None
    try:
        with open(LAST_CONV_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        ws_str = str(workspace.resolve()).lower()
        for path_key, conv_id in data.items():
            try:
                if str(Path(path_key).resolve()).lower() == ws_str:
                    return conv_id
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"读取 last_conversations.json 失败: {e}")
    return None


def get_conv_id_from_history(workspace: Optional[Path] = None, prompt_match: Optional[str] = None) -> Optional[str]:
    """从 history.jsonl 末尾查找匹配工作区或 Prompt 的最新会话 ID"""
    if not HISTORY_FILE.exists():
        return None
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        ws_str = str(workspace.resolve()).lower() if workspace else None
        for line in reversed(lines[-30:]):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                cid = item.get("conversationId")
                if not cid or cid in EXCLUDE_CONV_IDS:
                    continue
                if prompt_match and prompt_match in item.get("display", ""):
                    return cid
                if ws_str and item.get("workspace"):
                    try:
                        if str(Path(item.get("workspace")).resolve()).lower() == ws_str:
                            return cid
                    except Exception:
                        pass
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"读取 history.jsonl 失败: {e}")
    return None


def transcript_has_prompt(path: Path, prompt: str) -> bool:
    """检查 transcript 日志前几行是否包含发送的 prompt"""
    if not prompt:
        return True
    search_term = prompt[:30].strip()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for _ in range(10):
                line = f.readline()
                if not line:
                    break
                if search_term in line:
                    return True
    except Exception:
        pass
    return False


def find_latest_transcript(min_mtime: float, require_prompt: Optional[str] = None) -> Optional[Path]:
    """获取与当前 AGY 进程关联的最新 transcript.jsonl 日志文件"""
    # 1. 显式指定的目标会话 ID（最高优先级）
    if TARGET_CONV_ID:
        target_path = BRAIN_DIR / TARGET_CONV_ID / ".system_generated" / "logs" / "transcript.jsonl"
        if target_path.exists():
            return target_path

    # 2. 扫描所有候选 transcript.jsonl
    pattern = str(BRAIN_DIR / "*" / ".system_generated" / "logs" / "transcript.jsonl")
    paths = glob.glob(pattern.replace('\\', '/'))
    if not paths:
        return None

    paths_with_mtime = []
    for p in paths:
        p_obj = Path(p)
        conv_id = p_obj.parts[-4] if len(p_obj.parts) >= 4 else ""
        if conv_id in EXCLUDE_CONV_IDS:
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

    # 3. 如果需要匹配 prompt（例如向新会话发送了首条指令），优先内容匹配
    if require_prompt:
        for p_obj, mtime in paths_with_mtime:
            if transcript_has_prompt(p_obj, require_prompt):
                return p_obj

    # 4. 根据工作区从 last_conversations.json 辅助匹配
    ws_conv_id = get_workspace_conv_id(AGY_WORKSPACE)
    if ws_conv_id and ws_conv_id not in EXCLUDE_CONV_IDS:
        for p_obj, mtime in paths_with_mtime:
            if p_obj.parts[-4] == ws_conv_id:
                return p_obj

    return paths_with_mtime[0][0]


def extract_prompt_from_transcript(cid: str) -> str:
    """尝试从 transcript.jsonl 中提取首个用户请求的内容作为标题"""
    transcript_file = BRAIN_DIR / cid / ".system_generated" / "logs" / "transcript.jsonl"
    if not transcript_file.exists():
        return ""
    try:
        with open(transcript_file, "r", encoding="utf-8", errors="replace") as f:
            for _ in range(10):
                line = f.readline()
                if not line:
                    break
                obj = json.loads(line)
                if obj.get("type") == "USER_INPUT":
                    content = obj.get("content", "")
                    m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", content, re.DOTALL)
                    raw = m.group(1).strip() if m else content.strip()
                    if raw and not raw.startswith("/"):
                        return raw
    except Exception:
        pass
    return ""


def parse_timestamp(ts: Any) -> float:
    """统一将各类格式的 timestamp (ISO 字符串、毫秒数值、秒浮点数) 解析为 Unix 秒时间戳 (float)"""
    if not ts:
        return 0.0
    if isinstance(ts, (int, float)):
        return ts / 1000.0 if ts > 1e11 else float(ts)
    if isinstance(ts, str):
        ts_str = ts.strip()
        try:
            val = float(ts_str)
            return val / 1000.0 if val > 1e11 else val
        except ValueError:
            pass
        try:
            clean_ts = re.sub(r"(\.\d{6})\d+", r"\1", ts_str)
            dt = datetime.datetime.fromisoformat(clean_ts)
            return dt.timestamp()
        except Exception:
            pass
    return 0.0


def shorten_workspace(ws: str) -> str:
    r"""智能缩短工作区路径展示，如 E:\Git\AGY-QQ-Bridge -> AGY-QQ-Bridge，主目录 -> ~"""
    if not ws:
        return "默认"
    try:
        p = Path(ws)
        home = Path.home()
        if p.resolve() == home.resolve():
            return "~"
        name = p.name or str(p)
        return name
    except Exception:
        return ws


def parse_history_range(
    parts: List[str],
    total_count: int,
    default_limit: int = 5,
    max_page_size: int = 15
) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    """
    解析 /history 后的参数，返回 1-indexed (start_idx, end_idx, err_msg)。
    若参数格式非法或超出限制，start_idx 与 end_idx 为 None，返回明确的错误提示。
    支持合法格式:
      /history              -> 默认前 5 条 (1~5)
      /history 10           -> 前 10 条 (1~10)
      /history 31-40        -> 连字符范围 (31~40)
      /history 30~40        -> 波浪号范围 (30~40)
      /history 31 40        -> 两数范围 (31~40)
      /history p4 / page 4  -> 分页模式第4页 (31~40，每页10条)
      /history 页 4 / 页4   -> 中文分页模式 (31~40)
      /history 10 p4        -> 自定义每页10条第4页 (31~40)
    """
    args = parts[1:] if len(parts) > 1 else []
    if not args:
        return 1, min(default_limit, total_count), None

    # 参数过多检查
    if len(args) > 2:
        return None, None, (
            f"⚠️ **参数过多**（输入了 {len(args)} 个参数）。\n\n"
            "• `/history` 支持的常用格式：\n"
            "  - 查看前 N 条：`/history 10`（单次上限 15 条）\n"
            "  - 查看指定范围：`/history 31-40` 或 `/history 31 40`\n"
            "  - 查看指定页码：`/history p4` 或 `/history page 4`"
        )

    # 单参数情形
    if len(args) == 1:
        raw_arg = args[0].strip()

        # 格式1: 单参数内含范围 31-40, 31~40, 31..40
        m_range = re.match(r"^(\d+)[-~.]{1,2}(\d+)$", raw_arg)
        if m_range:
            n1, n2 = int(m_range.group(1)), int(m_range.group(2))
            if n1 <= 0 or n2 <= 0:
                return None, None, "⚠️ 会话序号从 1 开始，不能包含 0 或负数。有效范围示例：`/history 1-10`"
            start = min(n1, n2)
            end = max(n1, n2)
            if end - start + 1 > max_page_size:
                end = start + max_page_size - 1
            return start, end, None

        # 格式2: 单参数页码 p4, page4, 页4
        m_page = re.match(r"^(?:p|page|页)(\d+)$", raw_arg, re.I)
        if m_page:
            page = int(m_page.group(1))
            if page <= 0:
                return None, None, "⚠️ 页码必须大于等于 1（示例：`/history p1`）。"
            page_size = 10
            start = (page - 1) * page_size + 1
            end = start + page_size - 1
            return start, end, None

        # 格式3: 单纯数字，如 /history 10
        if raw_arg.isdigit():
            val = int(raw_arg)
            if val <= 0:
                return None, None, "⚠️ 查询数量必须大于 0（示例：`/history 10`）。"
            limit = min(val, max_page_size)
            return 1, limit, None

        # 单参数未能识别
        return None, None, (
            f"⚠️ 无法识别的参数格式「{raw_arg}」。\n\n"
            "• 支持的格式：\n"
            "  - 数量模式：`/history 10`（查看前 10 条，单次上限 15 条）\n"
            "  - 范围模式：`/history 31-40` 或 `/history 31~40`\n"
            "  - 分页模式：`/history p4` 或 `/history page 4`"
        )

    # 双参数情形 len(args) == 2
    arg1, arg2 = args[0].strip(), args[1].strip()

    # 格式 A: p 4 / page 4 / 页 4
    if arg1.lower() in ["p", "page", "页"]:
        if arg2.isdigit():
            page = int(arg2)
            if page <= 0:
                return None, None, "⚠️ 页码必须大于等于 1（示例：`/history page 1`）。"
            page_size = 10
            start = (page - 1) * page_size + 1
            end = start + page_size - 1
            return start, end, None
        else:
            return None, None, f"⚠️ 页码「{arg2}」无效，请输入正整数页码（示例：`/history page 2`）。"

    # 格式 B: 10 p4 / 31 40
    if arg1.isdigit():
        v1 = int(arg1)
        if v1 <= 0:
            return None, None, "⚠️ 起始序号或每页数量必须大于 0。"

        # 10 p4
        if arg2.lower().startswith(("p", "page", "页")):
            m_p2 = re.match(r"^(?:p|page|页)(\d+)$", arg2, re.I)
            if m_p2:
                v2 = int(m_p2.group(1))
                if v2 <= 0:
                    return None, None, "⚠️ 页码必须大于等于 1。"
                page_size = min(v1, max_page_size)
                start = (v2 - 1) * page_size + 1
                end = start + page_size - 1
                return start, end, None
            else:
                return None, None, f"⚠️ 页码「{arg2}」无效，示例：`/history 10 p4`。"

        # 31 40 (两数范围)
        if arg2.isdigit():
            v2 = int(arg2)
            if v2 <= 0:
                return None, None, "⚠️ 结束序号必须大于 0。"
            start = min(v1, v2)
            end = max(v1, v2)
            if end - start + 1 > max_page_size:
                end = start + max_page_size - 1
            return start, end, None

        return None, None, f"⚠️ 无法识别的范围结束参数「{arg2}」，两数范围示例：`/history 31 40`。"

    return None, None, (
        f"⚠️ 无法识别的双参数组合「{arg1} {arg2}」。\n\n"
        "• 支持的双参数格式：\n"
        "  - 范围查询：`/history 31 40`\n"
        "  - 分页查询：`/history page 4` 或 `/history 10 p4`"
    )


def get_current_conv_id() -> Optional[str]:
    """获取当前已绑定的活跃会话 ID"""
    if _current_log_path and len(_current_log_path.parts) >= 4:
        return _current_log_path.parts[-4]
    if TARGET_CONV_ID:
        return TARGET_CONV_ID
    ws_cid = get_workspace_conv_id(AGY_WORKSPACE)
    if ws_cid:
        return ws_cid
    return None


def get_conversation_title(cid: str) -> str:
    """获取指定会话在持久层存储中的当前主题名称"""
    # 1. 优先读取 AGY 官方原生持久化注解文件 annotations/<cid>.pbtxt
    ann_file = CLI_HOME / "annotations" / f"{cid}.pbtxt"
    if ann_file.exists():
        try:
            txt = ann_file.read_text(encoding="utf-8", errors="replace").strip()
            m = re.search(r'title\s*:\s*"(.*)"', txt)
            if m:
                val = m.group(1).replace('\\"', '"').replace('\\\\', '\\')
                if val:
                    return val
        except Exception:
            pass

    # 2. 尝试从 conversation_summaries.db 读取
    if CONV_SUMMARIES_DB.exists():
        try:
            conn = sqlite3.connect(str(CONV_SUMMARIES_DB))
            c = conn.cursor()
            c.execute("SELECT title, preview FROM conversation_summaries WHERE conversation_id = ?", (cid,))
            row = c.fetchone()
            conn.close()
            if row and (row[0] or row[1]):
                return row[0] or row[1]
        except Exception:
            pass

    return extract_prompt_from_transcript(cid) or "未命名会话"


def validate_rename_title(new_title: str) -> Tuple[bool, str]:
    """
    校验 /rename 的参数，防止误将 /resume 或 /history 的参数/指令当作主题写入。
    返回 (is_valid, error_message)
    """
    t = new_title.strip()
    if not t:
        return False, "⚠️ 会话新主题不能为空。"

    lower_t = t.lower()
    parts_t = t.split()

    # 1. 检查是否误输入为系统控制指令（如 /status, /help, /new, /stop 等）
    if lower_t in ["status", "/status", "状态", "/状态", "help", "/help", "帮助", "/帮助", "new", "/new", "reset", "/reset", "清空", "/清空", "stop", "/stop", "停止", "/停止"]:
        cmd_name = t if t.startswith('/') else '/' + t
        return False, (
            f"⚠️ 输入的参数为机器人控制指令「{t}」。\n\n"
            f"👉 若要执行该指令，请直接发送：`{cmd_name}`\n"
            f"• 若要重命名当前会话，请输入具体的描述文本（例如：`/rename 优化登录逻辑`）"
        )

    # 2. 检查是否误输入为 resume/switch 相关指令或带参指令
    if lower_t in ["resume", "/resume", "switch", "/switch", "切换", "/切换"]:
        return False, (
            "⚠️ 输入的参数为恢复会话指令。若要恢复会话，请直接使用：`/resume <编号>`。"
        )
    if lower_t.startswith(("/resume", "/switch", "/切换")):
        sub = t.split(None, 1)[1].strip() if len(parts_t) > 1 else ""
        return False, (
            f"⚠️ 检测到参数包含恢复会话指令「{t}」！\n\n"
            f"👉 若要恢复会话，请直接使用：`/resume {sub}`\n"
            f"• 若要重命名当前会话，请输入具体的描述文本（例如：`/rename 优化登录逻辑`）"
        )
    if parts_t[0].lower() in ["resume", "switch", "切换"] and len(parts_t) > 1:
        sub = " ".join(parts_t[1:])
        return False, (
            f"⚠️ 检测到参数疑似想要恢复会话「{t}」！\n\n"
            f"👉 请直接使用指令：`/resume {sub}`\n"
            f"• 若要重命名当前会话，请输入具体的描述文本（例如：`/rename 优化登录逻辑`）"
        )

    # 3. 检查是否误输入为 history/sessions 相关指令或带参指令
    if lower_t in ["history", "/history", "sessions", "/sessions", "历史", "/历史"]:
        return False, (
            "⚠️ 输入的参数为历史会话查询指令。若要查看历史列表，请直接使用：`/history`。"
        )
    if lower_t.startswith(("/history", "/sessions", "/历史")):
        sub = t.split(None, 1)[1].strip() if len(parts_t) > 1 else ""
        return False, (
            f"⚠️ 检测到参数包含历史查询指令「{t}」！\n\n"
            f"👉 若要查看历史会话，请直接使用：`/history {sub}`\n"
            f"• 若要重命名当前会话，请输入具体的描述文本（例如：`/rename 优化登录逻辑`）"
        )
    if parts_t[0].lower() in ["history", "sessions", "历史"] and len(parts_t) > 1:
        sub = " ".join(parts_t[1:])
        if (
            parts_t[1].isdigit()
            or re.match(r"^\d+[-~.]{1,2}\d+$", parts_t[1])
            or re.match(r"^(?:p|page|页)\d*$", parts_t[1], re.I)
        ):
            return False, (
                f"⚠️ 检测到参数疑似想要查看历史列表「{t}」！\n\n"
                f"👉 请直接使用指令：`/history {sub}`\n"
                f"• 若要重命名当前会话，请输入具体的描述文本（例如：`/rename 优化登录逻辑`）"
            )

    # 4. 检查是否为单编号（纯数字、带#号、括号等，如 1, #1, [1], 12）
    clean_num = t.lstrip("#-").strip("[]()").rstrip(".、")
    if clean_num.isdigit():
        return False, (
            f"⚠️ 检测到参数「{t}」为纯数字编号，不能作为会话主题！\n\n"
            f"• 若您想切换至该会话，请使用：`/resume {clean_num}`\n"
            f"• 若您想查看前 {clean_num} 个会话，请使用：`/history {clean_num}`\n"
            f"• 若您想重命名当前会话，请输入具体的描述文本（例如：`/rename 优化登录逻辑`）"
        )

    # 5. 检查是否为 history 范围参数（如 31-40, 31~40, 31..40 或两数 31 40）
    m_range = re.match(r"^(\d+)[-~.]{1,2}(\d+)$", t)
    if m_range:
        return False, (
            f"⚠️ 检测到范围参数「{t}」，疑似想要查看历史会话！\n\n"
            f"👉 若要查看第 {t} 项会话列表，请使用：`/history {t}`\n"
            f"• 若要重命名当前会话，请输入具体的描述文本（例如：`/rename 优化登录逻辑`）"
        )
    if len(parts_t) == 2 and parts_t[0].isdigit() and parts_t[1].isdigit():
        return False, (
            f"⚠️ 检测到两数范围参数「{t}」，疑似想要查看历史会话！\n\n"
            f"👉 若要查看第 {t} 项会话列表，请使用：`/history {t}`\n"
            f"• 若要重命名当前会话，请输入具体的描述文本（例如：`/rename 优化登录逻辑`）"
        )

    # 6. 检查是否为 history 分页参数（如 p4, page 4, 页 4, 10 p4, 10 page 4）
    if re.match(r"^(?:p|page|页)\s*\d+$", t, re.I):
        return False, (
            f"⚠️ 检测到页码参数「{t}」，疑似想要翻页查看历史会话！\n\n"
            f"👉 若要查看该页会话列表，请使用：`/history {t}`\n"
            f"• 若要重命名当前会话，请输入具体的描述文本（例如：`/rename 优化登录逻辑`）"
        )
    if (len(parts_t) == 2 and parts_t[0].isdigit() and re.match(r"^(?:p|page|页)\d+$", parts_t[1], re.I)) or \
       (len(parts_t) == 3 and parts_t[0].isdigit() and parts_t[1].lower() in ["p", "page", "页"] and parts_t[2].isdigit()):
        return False, (
            f"⚠️ 检测到分页参数「{t}」，疑似想要翻页查看历史会话！\n\n"
            f"👉 若要查看该页会话列表，请使用：`/history {t}`\n"
            f"• 若要重命名当前会话，请输入具体的描述文本（例如：`/rename 优化登录逻辑`）"
        )

    # 7. 检查是否为内部会话 UUID 格式
    uuid_pattern = r"^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$"
    if re.match(uuid_pattern, t):
        return False, (
            f"⚠️ 检测到参数为内部会话 UUID，不能作为会话主题！\n\n"
            f"• 如需修改会话主题，请输入易于识别的文本（例如：`/rename 项目代码重构`）"
        )

    # 8. 长度限制
    if len(t) > 100:
        return False, "⚠️ 会话新主题长度过长（建议控制在 50 字以内）。"

    return True, ""


def rename_conversation(cid: str, new_title: str) -> Tuple[bool, str]:
    """重命名指定会话的主题。
    持久化同步更新：
    1. ~/.gemini/antigravity-cli/annotations/<cid>.pbtxt (AGY 原生持久化主题文件，防止重启被覆盖)
    2. ~/.gemini/antigravity-cli/cache/conversation_metadata.json (元数据缓存)
    3. ~/.gemini/antigravity-cli/conversation_summaries.db (SQLite 历史快照库)
    返回 (是否成功, 旧主题名称)
    """
    old_title = get_conversation_title(cid)
    is_valid, _ = validate_rename_title(new_title)
    if not is_valid:
        return False, old_title
    success = False

    # 1. 写入 AGY 原生 annotations/<cid>.pbtxt
    try:
        ann_dir = CLI_HOME / "annotations"
        ann_dir.mkdir(parents=True, exist_ok=True)
        ann_file = ann_dir / f"{cid}.pbtxt"
        escaped_title = new_title.replace("\\", "\\\\").replace('"', '\\"')
        ann_file.write_text(f'title:"{escaped_title}"\n', encoding="utf-8")
        success = True
    except Exception as e:
        logger.warning(f"写入 annotations pbtxt 失败: {e}")

    # 2. 同步更新 cache/conversation_metadata.json (若存在)
    try:
        meta_file = CLI_HOME / "cache" / "conversation_metadata.json"
        if meta_file.exists():
            data = json.loads(meta_file.read_text(encoding="utf-8"))
            conv_meta = data.get("conversations", {}).get(cid)
            if conv_meta and isinstance(conv_meta, dict):
                summary = conv_meta.get("summary")
                if summary and isinstance(summary, dict):
                    summary["Title"] = new_title
                    meta_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug(f"更新 conversation_metadata.json 失败: {e}")

    # 3. 更新 SQLite conversation_summaries.db
    if CONV_SUMMARIES_DB.exists():
        try:
            conn = sqlite3.connect(str(CONV_SUMMARIES_DB))
            c = conn.cursor()
            c.execute("UPDATE conversation_summaries SET title = ? WHERE conversation_id = ?", (new_title, cid))
            rows = c.rowcount
            if rows == 0:
                c.execute(
                    "INSERT OR REPLACE INTO conversation_summaries (conversation_id, title, preview, last_modified_time) VALUES (?, ?, ?, ?)",
                    (cid, new_title, new_title, time.strftime("%Y-%m-%d %H:%M:%S+00:00"))
                )
            conn.commit()
            conn.close()
            success = True
        except Exception as e:
            logger.error(f"重命名会话更新 SQLite 失败: {e}")

    return success, old_title


def get_history_conversations() -> List[Dict[str, Any]]:
    """获取所有有效历史会话并按最后活动时间倒序排序。
    与 AGY 原生 /resume 选择器保持 100% 规则对齐：
    1. 优先扫描 conversations/ 目录与 conversation_summaries.db。
    2. 过滤掉无步骤（steps == 0，即初始空库 48KB）的无效/空会话。
    3. 提取官方格式化标题或预览摘要、原始工作区与最后修改时间。
    4. 若上述存储不存在则优雅降级读取 history.jsonl。
    """
    valid_cids = {}
    if CONVERSATIONS_DIR.exists():
        try:
            with os.scandir(CONVERSATIONS_DIR) as entries:
                for entry in entries:
                    name = entry.name
                    if name.endswith(".pb"):
                        cid = name[:-3]
                        if cid not in EXCLUDE_CONV_IDS:
                            valid_cids[cid] = entry.stat().st_mtime
                    elif name.endswith(".db"):
                        # SQLite 初始空数据库大小严格为 49152 字节 (48KB)
                        # 有实际对话 steps 的数据库大小均 >= 216KB
                        if entry.stat().st_size > 49152:
                            cid = name[:-3]
                            if cid not in EXCLUDE_CONV_IDS:
                                valid_cids[cid] = entry.stat().st_mtime
        except Exception as e:
            logger.debug(f"扫描 conversations 目录异常: {e}")

    # 若成功识别到有效会话，优先从 conversation_summaries.db 提取元数据
    if valid_cids and CONV_SUMMARIES_DB.exists():
        try:
            conn = sqlite3.connect(str(CONV_SUMMARIES_DB))
            c = conn.cursor()
            c.execute("SELECT conversation_id, title, preview, workspace_uris, last_modified_time FROM conversation_summaries")
            convs = []
            seen_cids = set()
            for cid, title, preview, ws_uris, mtime in c.fetchall():
                if cid in valid_cids:
                    seen_cids.add(cid)
                    ws = ""
                    if ws_uris:
                        try:
                            uris = json.loads(ws_uris)
                            if uris and isinstance(uris, list):
                                raw_path = uris[0]
                                if raw_path.startswith("file:///"):
                                    raw_path = raw_path[8:]
                                elif raw_path.startswith("file://"):
                                    raw_path = raw_path[7:]
                                ws = urllib.parse.unquote(raw_path)
                        except Exception:
                            pass

                    # 优先读取 annotations/<cid>.pbtxt 中的用户自定义标题
                    ann_title = ""
                    ann_file = CLI_HOME / "annotations" / f"{cid}.pbtxt"
                    if ann_file.exists():
                        try:
                            txt = ann_file.read_text(encoding="utf-8", errors="replace").strip()
                            m = re.search(r'title\s*:\s*"(.*)"', txt)
                            if m:
                                ann_title = m.group(1).replace('\\"', '"').replace('\\\\', '\\')
                        except Exception:
                            pass

                    final_title = ann_title or title or preview or ""
                    if not final_title:
                        final_title = extract_prompt_from_transcript(cid) or "无标题会话"

                    ts_val = parse_timestamp(mtime) if mtime else valid_cids.get(cid, 0.0)
                    convs.append({
                        "cid": cid,
                        "final_title": final_title,
                        "workspace": ws,
                        "last_timestamp": ts_val
                    })
            conn.close()

            # 补充极少数不在 summaries.db 中的有效会话
            for cid in set(valid_cids.keys()) - seen_cids:
                convs.append({
                    "cid": cid,
                    "final_title": extract_prompt_from_transcript(cid) or "历史会话",
                    "workspace": "",
                    "last_timestamp": valid_cids[cid]
                })

            convs.sort(key=lambda x: parse_timestamp(x.get("last_timestamp")), reverse=True)
            return convs
        except Exception as e:
            logger.error(f"读取 conversation_summaries.db 异常: {e}")

    # 降级兜底方案：从 history.jsonl 解析
    convs = {}
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        cid = d.get("conversationId")
                        if not cid or cid in EXCLUDE_CONV_IDS:
                            continue
                        display = d.get("display", "").strip()
                        ws = d.get("workspace", "")
                        ts = d.get("timestamp", 0)

                        if cid not in convs:
                            convs[cid] = {
                                "cid": cid,
                                "title": display,
                                "workspace": ws,
                                "last_timestamp": ts,
                                "prompts_count": 1,
                                "real_prompt": not display.startswith("/"),
                            }
                        else:
                            convs[cid]["prompts_count"] += 1
                            if ts > convs[cid]["last_timestamp"]:
                                convs[cid]["last_timestamp"] = ts
                            if not display.startswith("/"):
                                convs[cid]["real_prompt"] = True
                                if convs[cid]["title"].startswith("/") or not convs[cid]["title"]:
                                    convs[cid]["title"] = display
                            if ws and not convs[cid]["workspace"]:
                                convs[cid]["workspace"] = ws
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"读取 history.jsonl 失败: {e}")

    valid_convs = []
    for c in convs.values():
        title = c["title"]
        if not c["real_prompt"] or title.startswith("/"):
            t_from_file = extract_prompt_from_transcript(c["cid"])
            if t_from_file:
                title = t_from_file
                c["real_prompt"] = True

        # 过滤掉仅有 /new 或 /resume 且无实际内容的空会话
        if not c["real_prompt"] and title in ["/new", "/resume"]:
            continue

        c["final_title"] = title
        valid_convs.append(c)

    valid_convs.sort(key=lambda x: x["last_timestamp"], reverse=True)
    return valid_convs


def bind_log(log_path: Path, skip_history: bool = False):
    """绑定目标日志并设定偏移指针"""
    global _current_log_path, _last_log_size, _last_sent_timestamp
    _current_log_path = log_path
    if skip_history:
        # 冷启动或恢复历史会话时，跳过历史已有内容，防止启动时把历史回复全部回灌 QQ
        try:
            _last_log_size = log_path.stat().st_size
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
                            _last_sent_timestamp = ts
                            break
                except Exception:
                    continue
        except OSError:
            _last_log_size = 0
    else:
        # 运行中新建会话：从 0 读取，确保新生成的回复不被漏发
        _last_log_size = 0

    logger.info(f"[Listener] Bound to log: {_current_log_path} (size={_last_log_size}, last_ts={_last_sent_timestamp}, skip_history={skip_history})")


async def log_listener():
    """纯异步增量日志广播协程：绑定会话日志的增量并推送到 QQ。"""
    global _current_log_path, _last_log_size, _last_sent_timestamp

    # 启动时，先扫描并绑定目前最新的日志（以当前 24 小时前为基线，冷启动跳过旧历史）
    init_log = find_latest_transcript(time.time() - 86400.0)
    if init_log:
        bind_log(init_log, skip_history=True)

    while _running:
        await asyncio.sleep(0.5)

        # 1. 尚未绑定日志，或者进程管理器显式触发重置 (/new, /reset) 时探测新日志
        if not _current_log_path or agy_mgr.needs_rebind:
            try:
                min_mtime = agy_mgr.fresh_time if agy_mgr.fresh_time > 0 else (time.time() - 86400.0)
                latest_log = find_latest_transcript(min_mtime, require_prompt=agy_mgr.last_sent_prompt)
                if latest_log:
                    if not _current_log_path or latest_log != _current_log_path:
                        bind_log(latest_log, skip_history=False)
                    agy_mgr.needs_rebind = False
                    agy_mgr.fresh_time = 0.0
            except Exception as e:
                logger.error(f"[Listener] Scan error: {e}")

        if not _current_log_path:
            continue

        # 2. 检测大小变动
        try:
            curr_size = _current_log_path.stat().st_size
        except FileNotFoundError:
            _current_log_path = None
            continue

        # log rotation 重置
        if curr_size < _last_log_size:
            logger.info(f"[Listener] Log file truncated (decreased from {_last_log_size} to {curr_size}), resetting offset.")
            _last_log_size = 0

        if curr_size <= _last_log_size:
            continue

        # 3. 增量读取新行
        try:
            with open(_current_log_path, 'r', encoding='utf-8', errors='replace') as f:
                f.seek(_last_log_size)
                new_lines = f.read().splitlines()
        except OSError:
            continue

        # 更新指针
        _last_log_size = curr_size

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
                if ts and _last_sent_timestamp and ts <= _last_sent_timestamp:
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
                    logger.info(f"[Listener -> QQ] Broadcasting response (ts={ts}): {text[:100]}")
                    if ts:
                        _last_sent_timestamp = ts
                    agy_mgr.is_busy = False
                    await send_message_rest(MASTER_OPENID, text)


async def send_message_rest(user_openid: str, content: str) -> bool:
    """给指定用户发送 C2C 消息"""
    token = await ensure_token()
    client = get_http_client()
    headers = {
        "Authorization": f"QQBot {token}",
        "Content-Type": "application/json",
        "User-Agent": "AGY-QQ-Bridge/2.0",
    }
    msg_seq = _next_msg_seq(user_openid)
    display_content = content[:3990] + "\n\n... (已截断)" if len(content) > 4000 else content
    body = {"markdown": {"content": display_content}, "msg_type": 2, "msg_seq": msg_seq}

    try:
        resp = await client.post(
            f"{API_BASE}/v2/users/{user_openid}/messages",
            headers=headers, json=body, timeout=30.0,
        )
        if resp.status_code >= 400:
            logger.error(f"Send failed [{resp.status_code}]: {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        logger.error(f"Send exception: {e}")
        return False


def get_http_client():
    global _http_client
    if _http_client is None:
        import httpx
        _http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
    return _http_client


async def ensure_token() -> str:
    global _access_token, _token_expires_at
    if _access_token and time.time() < _token_expires_at - 60:
        return _access_token
    client = get_http_client()
    resp = await client.post(
        TOKEN_URL,
        json={"appId": APP_ID, "clientSecret": CLIENT_SECRET},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"Failed to get token: {data}")
    expires_in = int(data.get("expires_in", 7200))
    _access_token = token
    _token_expires_at = time.time() + expires_in
    logger.info(f"Token refreshed, expires in {expires_in}s")
    return token


async def get_gateway_url() -> str:
    token = await ensure_token()
    client = get_http_client()
    resp = await client.get(
        f"{API_BASE}{GATEWAY_URL_PATH}",
        headers={"Authorization": f"QQBot {token}", "User-Agent": "AGY-QQ-Bridge/1.0"},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    url = data.get("url")
    if not url:
        raise RuntimeError(f"Failed to get gateway URL: {data}")
    return url


async def send_identify(ws):
    token = await ensure_token()
    payload = {
        "op": 2,
        "d": {
            "token": f"QQBot {token}",
            "intents": (1 << 25) | (1 << 30) | (1 << 12) | (1 << 26),
            "shard": [0, 1],
            "properties": {"$os": "Windows", "$browser": "agy-qq-bridge-win", "$device": "agy-qq-bridge-win"},
        },
    }
    await ws.send_json(payload)
    logger.info("Identify sent")


async def send_resume(ws):
    token = await ensure_token()
    payload = {
        "op": 6,
        "d": {"token": f"QQBot {token}", "session_id": _session_id, "seq": _last_seq},
    }
    await ws.send_json(payload)
    logger.info(f"Resume sent (session={_session_id}, seq={_last_seq})")


def _next_msg_seq(msg_id: str = "default") -> int:
    time_part = int(time.time()) % 100000000
    rand = int(uuid.uuid4().hex[:4], 16)
    return (time_part ^ rand) % 65536


_seen_messages: Dict[str, float] = {}


def is_duplicate(msg_id: str) -> bool:
    now = time.time()
    if msg_id in _seen_messages and now - _seen_messages[msg_id] < 300:
        return True
    _seen_messages[msg_id] = now
    if len(_seen_messages) > 1000:
        for k in list(_seen_messages.keys()):
            if now - _seen_messages[k] > 600:
                del _seen_messages[k]
    return False


async def get_local_git_status(workspace: Path) -> str:
    """本地直接执行 git status，毫秒级诊断返回，零 Token 消耗"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(workspace), "status", "-s",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", errors="replace").strip()
            return f"⚠️ 执行 `git status` 失败: {err_msg}"

        status_text = stdout.decode("utf-8", errors="replace").strip()

        proc_b = await asyncio.create_subprocess_exec(
            "git", "-C", str(workspace), "branch", "--show-current",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout_b, _ = await proc_b.communicate()
        branch = stdout_b.decode("utf-8", errors="replace").strip() or "HEAD"

        if not status_text:
            return f"📁 **Git 工作区状态**\n\n• **工作区**: `{workspace}`\n• **当前分支**: `{branch}`\n\n✅ **工作区很干净，无任何未提交的代码变更。**"

        lines = [l for l in status_text.splitlines() if l.strip()]
        display_lines = "\n".join(lines[:25])
        if len(lines) > 25:
            display_lines += f"\n... (其余 {len(lines) - 25} 项已折叠)"

        return (
            f"📁 **Git 工作区状态**\n\n"
            f"• **工作区**: `{workspace}`\n"
            f"• **当前分支**: `{branch}` ({len(lines)} 项变动)\n\n"
            f"```text\n{display_lines}\n```\n"
            f"💡 *提示：若需让 AI 审查或提交代码，可直接输入对话「帮我提交以上变动」*"
        )
    except Exception as e:
        return f"⚠️ 检查工作区失败: {e}"


def _log_task_exception(t):
    if not t.cancelled():
        exc = t.exception()
        if exc:
            logger.error(f"[Task Error] 异步任务执行异常: {exc}", exc_info=exc)


async def handle_c2c_message(d: dict):
    global _last_msg_id, _bot_openid

    msg_id = str(d.get("id", ""))
    if not msg_id or is_duplicate(msg_id):
        return

    content = str(d.get("content", "")).strip()
    
    # 支持多模态附件识别（图片、文件、语音等），零过滤透传大模型
    attachments = d.get("attachments") or []
    for att in attachments:
        url = att.get("url")
        if url:
            name = att.get("filename") or att.get("name") or "file"
            content += f"\n\n[附件({name}): {url}]"

    content = content.strip()
    author = d.get("author") if isinstance(d.get("author"), dict) else {}
    user_openid = str(author.get("user_openid", ""))

    if not user_openid or not content:
        return

    _last_msg_id = msg_id
    logger.info(f"[Recv] openid={user_openid}: {content[:100]}")

    global MASTER_OPENID
    if not MASTER_OPENID:
        MASTER_OPENID = user_openid
        logger.info(f"[Auto-bind] First message from {user_openid} set as MASTER_OPENID")
        try:
            env_path = Path(__file__).parent / ".env"
            if env_path.exists():
                env_text = env_path.read_text(encoding="utf-8")
                if "MASTER_OPENID=" not in env_text:
                    env_path.write_text(
                        env_text.rstrip() + f"\nMASTER_OPENID={user_openid}\n",
                        encoding="utf-8",
                    )
        except Exception as e:
            logger.error(f"Failed to auto-bind MASTER_OPENID in .env: {e}")

    if user_openid != MASTER_OPENID:
        logger.info(f"[Skip] non-master openid: {user_openid}")
        return

    # 🛠️ 交互指令处理 (重构版)
    global _cached_history_list, _last_history_time, _history_resume_count
    parts = content.strip().split()
    cmd = parts[0].lower() if parts else ""

    if cmd in ["/new", "/reset", "/清空", "/新对话", "new", "reset"]:
        logger.info("[Recv] New session command received")
        _cached_history_list = []
        _last_history_time = 0.0
        _history_resume_count = 0
        # 强杀并重启一个不带 -c 参数的新会话
        agy_mgr.start(fresh=True)
        reply = "✅ 已重置后台 ConPTY 会话，拉起全新 AGY 进程。上下文已完全清空。"
        await send_message_rest(user_openid, reply)
        return

    if cmd in ["/history", "/历史", "/sessions", "history"]:
        logger.info(f"[Recv] History list requested: {content}")
        all_convs = get_history_conversations()
        total_len = len(all_convs)

        if not all_convs:
            reply = "ℹ️ 未找到任何历史会话记录。"
            await send_message_rest(user_openid, reply)
            return

        start_idx, end_idx, err_msg = parse_history_range(parts, total_len)
        if err_msg:
            await send_message_rest(user_openid, err_msg)
            return

        if start_idx > total_len:
            reply = f"⚠️ 请求的起始序号 [{start_idx}] 超出历史会话总数（共 {total_len} 个）。当前有效范围为 1 ~ {total_len}。"
            await send_message_rest(user_openid, reply)
            return

        # 校验成功后才刷新 90 秒窗口与计数
        _cached_history_list = all_convs
        _last_history_time = time.time()
        _history_resume_count = 0

        actual_end = min(end_idx, total_len)
        selected_convs = all_convs[start_idx - 1 : actual_end]
        if not selected_convs:
            reply = f"ℹ️ 未找到对应范围的历史会话记录（共 {total_len} 个）。当前有效范围为 1 ~ {total_len}。"
            await send_message_rest(user_openid, reply)
            return

        lines = [f"📜 **AGY 历史会话列表**（共 {total_len} 个，展示第 {start_idx} ~ {actual_end} 个）：\n"]
        for idx, item in enumerate(selected_convs, start_idx):
            ts_sec = parse_timestamp(item.get("last_timestamp"))
            time_str = time.strftime("%m-%d %H:%M", time.localtime(ts_sec)) if ts_sec > 0 else "未知时间"
            ws_short = shorten_workspace(item["workspace"])
            title = item["final_title"].replace("\n", " ").replace("\r", " ")
            if len(title) > 32:
                title = title[:32] + "..."
            if not title:
                title = "（新会话/无主题）"

            lines.append(f"**[{idx}]** 💬 {title}\n📁 `{ws_short}` | 🕒 {time_str}\n")

        lines.append(f"👉 **恢复会话**：90 秒内输入 `/resume <编号>` (如 `/resume {start_idx}`) 即可切换（最多跳转 3 次）。")
        lines.append("💡 **翻页提示**：支持范围如 `/history 31-40`、页码如 `/history p4` 或指定数量如 `/history 10`。")
        reply = "\n".join(lines)
        await send_message_rest(user_openid, reply)
        return

    if cmd in ["/resume", "/切换", "/switch", "resume"]:
        logger.info(f"[Recv] Resume session requested: {content}")
        now = time.time()

        # 1. 检查是否存在有效 history 缓存以及是否在 90 秒内
        if not _cached_history_list or _last_history_time == 0:
            reply = (
                "⚠️ **未找到有效的历史会话列表**\n\n"
                "• 请先发送 `/history` 查看历史会话列表；\n"
                "• 并在列表展示后的 **90 秒内** 使用 `/resume <编号>` 进行恢复。"
            )
            await send_message_rest(user_openid, reply)
            return

        elapsed = now - _last_history_time
        if elapsed > 90:
            reply = (
                f"⚠️ **历史会话列表已过期**（已过 {int(elapsed)} 秒，超时限制为 90 秒）。\n\n"
                "👉 请重新发送 `/history` 获取最新会话列表后再进行恢复。"
            )
            await send_message_rest(user_openid, reply)
            return

        # 2. 检查 90 秒内跳转次数上限（最多 3 次）
        if _history_resume_count >= 3:
            reply = (
                "⚠️ **本轮历史会话的恢复跳转次数已达上限**（最多连续跳转 3 次）。\n\n"
                "👉 如需继续切换会话，请重新发送 `/history` 刷新会话列表。"
            )
            await send_message_rest(user_openid, reply)
            return

        # 3. 参数检测
        if len(parts) < 2:
            remaining_time = max(1, int(90 - elapsed))
            remaining_jumps = 3 - _history_resume_count
            reply = (
                "ℹ️ **请指定要恢复的会话序号**\n\n"
                f"• 示例：`/resume 1`\n"
                f"• 状态：本轮还可跳转 {remaining_jumps} 次，列表有效期剩余 {remaining_time} 秒。\n"
                "• 提示：可发送 `/history` 重新查看会话列表与编号。"
            )
            await send_message_rest(user_openid, reply)
            return

        if len(parts) > 2:
            reply = (
                f"⚠️ **参数过多**：`/resume` 仅支持单个会话编号。\n\n"
                f"• 正确示例：`/resume {parts[1]}`\n"
                f"• 请勿在编号后输入多余参数。"
            )
            await send_message_rest(user_openid, reply)
            return

        target_arg = parts[1].strip()

        # 检查是否误输入了范围（如 31-40 或 31~40）
        if re.search(r"[-~.]", target_arg):
            reply = (
                f"⚠️ 检测到范围格式「{target_arg}」，`/resume` 仅支持恢复单个会话编号（如 `/resume 1`）。\n\n"
                f"👉 若要查看第 {target_arg} 项的会话列表，请使用：`/history {target_arg}`"
            )
            await send_message_rest(user_openid, reply)
            return

        # 检查是否误输入了页码（如 p2, page 2）
        if re.match(r"^(?:p|page|页)\d+$", target_arg, re.I):
            reply = (
                f"⚠️ 检测到页码格式「{target_arg}」，`/resume` 仅支持具体会话编号（如 `/resume 1`）。\n\n"
                f"👉 若要查看该页会话列表，请使用：`/history {target_arg}`"
            )
            await send_message_rest(user_openid, reply)
            return

        all_convs = _cached_history_list
        clean_arg = target_arg.lstrip("#").strip("[]()")

        if not clean_arg.isdigit():
            reply = (
                f"⚠️ 无效的参数「{target_arg}」。`/resume` 的参数必须为纯数字会话编号。\n\n"
                f"• 正确示例：`/resume 1`\n"
                f"• 当前列表中共有 {len(all_convs)} 个会话，可发送 `/history` 重新查看。"
            )
            await send_message_rest(user_openid, reply)
            return

        idx = int(clean_arg)
        if idx <= 0:
            reply = f"⚠️ 会话编号必须从 1 开始（输入为 {idx}）。当前有效编号范围为 1 ~ {len(all_convs)}。"
            await send_message_rest(user_openid, reply)
            return

        if idx > len(all_convs):
            reply = f"⚠️ 请求的会话编号 [{idx}] 超出当前列表总数（共 {len(all_convs)} 个）。当前有效编号范围为 1 ~ {len(all_convs)}。可发送 `/history` 重新查看。"
            await send_message_rest(user_openid, reply)
            return

        target_item = all_convs[idx - 1]

        target_cid = target_item["cid"]
        orig_ws_str = target_item.get("workspace", "")
        old_ws = AGY_WORKSPACE
        target_cwd = Path(orig_ws_str) if (orig_ws_str and Path(orig_ws_str).exists()) else AGY_WORKSPACE
        ws_changed = (target_cwd.resolve() != old_ws.resolve())

        logger.info(f"[Resume] Switching to conv_id={target_cid}, cwd={target_cwd} (ws_changed={ws_changed})")

        # 1. 重拉 AGY CLI 进程
        agy_mgr.start(conversation_id=target_cid, cwd=target_cwd)

        # 2. 绑定对应的 transcript.jsonl 日志，并跳过既往历史防止回灌 QQ
        target_log = BRAIN_DIR / target_cid / ".system_generated" / "logs" / "transcript.jsonl"
        if target_log.exists():
            bind_log(target_log, skip_history=True)

        _history_resume_count += 1
        remaining_jumps = 3 - _history_resume_count
        remaining_time = max(1, int(90 - (time.time() - _last_history_time)))
        jump_note = (
            f"\n• **跳转限额**: 本轮剩余 {remaining_jumps} 次（有效期剩 {remaining_time} 秒）"
            if remaining_jumps > 0
            else "\n• **跳转限额**: 本轮 3 次跳转已用完，下次切换请先发送 `/history`"
        )

        ws_short = shorten_workspace(str(target_cwd))
        title_snippet = target_item.get("final_title", "")[:35]
        if len(target_item.get("final_title", "")) > 35:
            title_snippet += "..."

        change_note = f"\n• **工作区切换**: 目录已同步切换至 `{target_cwd}`" if ws_changed else f"\n• **工作目录**: `{ws_short}`"

        reply = (
            f"🔄 **已成功恢复历史会话**\n\n"
            f"• **会话主题**: {title_snippet}"
            f"{change_note}"
            f"{jump_note}\n\n"
            f"👉 终端已在后台热重载就绪，直接发送消息即可在当前会话中继续工作！"
        )
        await send_message_rest(user_openid, reply)
        return

    if cmd in ["/rename", "/重命名", "/name", "rename"]:
        logger.info(f"[Recv] Rename session requested: {content}")
        curr_cid = get_current_conv_id()
        if not curr_cid:
            reply = "⚠️ 当前尚未绑定任何活动会话（可先发送一条消息开启对话后再重命名）。"
            await send_message_rest(user_openid, reply)
            return

        if len(parts) < 2:
            reply = (
                "ℹ️ **请提供新的会话主题**\n\n"
                "• 示例：`/rename 修复QQ机器人功能`"
            )
            await send_message_rest(user_openid, reply)
            return

        new_title = " ".join(parts[1:]).strip()
        is_valid, err_msg = validate_rename_title(new_title)
        if not is_valid:
            await send_message_rest(user_openid, err_msg)
            return

        success, old_title = rename_conversation(curr_cid, new_title)
        if success:
            if _cached_history_list:
                for c in _cached_history_list:
                    if c["cid"] == curr_cid:
                        c["final_title"] = new_title
                        break
            reply = (
                f"🏷️ **会话主题修改成功**\n\n"
                f"• 会话主题已从「{old_title}」改为「{new_title}」\n\n"
                f"👉 修改已即时生效，发送 `/history` 即可在列表中查看更新后的名称。"
            )
        else:
            reply = "❌ 修改会话主题失败，请检查会话持久化存储状态。"
        await send_message_rest(user_openid, reply)
        return


    if content.strip().lower() in ["/stop", "/停止", "/kill", "stop"]:
        logger.info("[Recv] Stop command received")
        # 智能防护：如果当前没有正在执行的任务（空闲状态），发送 Escape 清理输入状态，绝不发送多次 Ctrl+C 杀退终端
        if not agy_mgr.is_busy and (time.time() - agy_mgr.last_sent_time > 60 or agy_mgr.last_sent_time == 0):
            if agy_mgr.proc and agy_mgr.proc.isalive():
                async with agy_mgr.write_lock:
                    agy_mgr.proc.write("\x1b")
            reply = "ℹ️ 当前终端处于空闲就绪状态，未在执行耗时任务，请放心继续发送新消息。"
        else:
            await agy_mgr.send_ctrl_c()
            reply = "⛔ 已向后台发送中断信号（Ctrl+C），正在打断当前任务并恢复就绪状态。"
        await send_message_rest(user_openid, reply)
        return

    if content.strip().lower() in ["/git", "/git status", "git status", "/git diff"]:
        logger.info("[Recv] Local git status requested")
        reply = await get_local_git_status(AGY_WORKSPACE)
        await send_message_rest(user_openid, reply)
        return

    if content.strip().lower() in ["/help", "/帮助", "帮助", "help"]:
        reply = (
            "🤖 **AGY-QQ-Bridge 控制中心**\n\n"
            "• `/new` 或 `/清空`：重置后台终端，开启全新无上下文会话\n"
            "• `/history` 或 `/历史`：查看历史会话列表（如 `/history 10`、`/history 31-40`）\n"
            "• `/resume <编号>`：快速切换并恢复至指定历史会话继续工作\n"
            "• `/rename <新主题>`：重命名当前已绑定会话的主题名称\n"
            "• `/stop` 或 `/停止`：向后台发送 Ctrl+C 中断信号终止当前任务\n"
            "• `/status` 或 `/状态`：查看当前工作区与会话绑定状态\n"
            "• `git status`：秒级本地诊断当前工作区 Git 变动状态\n"
            "• 直接发送文本：自动输入给后台 Google Antigravity CLI\n"
            "• 发送图片/文件：原生直链由 AGY 视觉与多模态解析"
        )
        await send_message_rest(user_openid, reply)
        return

    if content.strip().lower() in ["/status", "/状态", "status"]:
        curr_cid = get_current_conv_id()
        if curr_cid:
            title = get_conversation_title(curr_cid)
            conv_desc = f"{title}"
        else:
            conv_desc = "暂未绑定（等待首条消息）"
        is_proc_alive = bool(agy_mgr.proc and getattr(agy_mgr.proc, "isalive", lambda: True)())
        reply = (
            "📊 **AGY-QQ-Bridge 运行状态**\n\n"
            f"• **工作区**: `{AGY_WORKSPACE}`\n"
            f"• **当前会话**: {conv_desc}\n"
            f"• **终端状态**: {'运行中' if is_proc_alive else '未就绪'}\n"
            f"• **管理员**: `{MASTER_OPENID[:8]}...`"
        )
        await send_message_rest(user_openid, reply)
        return

    logger.info(f"[QQ -> AGY] {content}")
    await agy_mgr.send_message(content)


async def event_loop(ws):
    global _session_id, _last_seq, _running, _ws, heartbeat_task, _last_heartbeat_ack_time
    _ws = ws
    _last_heartbeat_ack_time = time.time()
    heartbeat_interval = HEARTBEAT_INTERVAL
    heartbeat_task = asyncio.create_task(_heartbeat_sender(ws, heartbeat_interval))

    try:
        while _running and ws and not ws.closed:
            msg = await ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except json.JSONDecodeError:
                    logger.warning(f"JSON parse error: {msg.data[:100]}")
                    continue

                op = payload.get("op")
                t = payload.get("t")
                s = payload.get("s")
                d = payload.get("d")

                if isinstance(s, int) and (_last_seq is None or s > _last_seq):
                    _last_seq = s

                # op 10 Hello
                if op == 10:
                    d_data = d if isinstance(d, dict) else {}
                    interval_ms = d_data.get("heartbeat_interval", 30000)
                    heartbeat_interval = interval_ms / 1000.0 * 0.8
                    logger.info(f"Hello recv, heartbeat={heartbeat_interval:.1f}s")
                    if _session_id and _last_seq is not None:
                        await send_resume(ws)
                    else:
                        await send_identify(ws)
                    continue

                # op 11 Heartbeat ACK
                if op == 11:
                    _last_heartbeat_ack_time = time.time()
                    logger.debug("Heartbeat ACK received")
                    continue

                # op 7 Server Reconnect
                if op == 7:
                    logger.info("Server requested reconnect (op 7)")
                    if ws and not ws.closed:
                        await ws.close()
                    break

                # op 9 Invalid Session
                if op == 9:
                    resumable = bool(d) if d is not None else False
                    if not resumable:
                        logger.info("Invalid session (op 9, not resumable), clearing session")
                        _session_id = None
                        _last_seq = None
                    else:
                        logger.info("Invalid session (op 9, resumable)")
                    if ws and not ws.closed:
                        await ws.close()
                    break

                # op 0 Dispatch
                if op == 0 and t:
                    logger.info(f"[WS Dispatch] event_type={t}")
                    if t == "READY":
                        if isinstance(d, dict):
                            global _bot_openid
                            _session_id = d.get("session_id")
                            user = d.get("user") if isinstance(d.get("user"), dict) else {}
                            _bot_openid = str(user.get("id", ""))
                            logger.info(f"READY, session_id={_session_id}, bot_openid={_bot_openid}")
                    elif t == "RESUMED":
                        logger.info("Session resumed")
                    elif t == "C2C_MESSAGE_CREATE":
                        task = asyncio.create_task(handle_c2c_message(d))
                        task.add_done_callback(_log_task_exception)
                    continue

            elif msg.type == aiohttp.WSMsgType.CLOSE:
                logger.warning("WS close received")
                break
            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                raise RuntimeError("WebSocket closed abnormally")

    except Exception as e:
        logger.error(f"Event loop error: {e}")


async def _heartbeat_sender(ws, interval: float):
    global _last_heartbeat_ack_time
    try:
        while _running and ws and not ws.closed:
            await asyncio.sleep(interval)
            
            # 僵尸连接检测看门狗
            now = time.time()
            if now - _last_heartbeat_ack_time > interval * 2.5:
                logger.warning(f"Heartbeat ACK timeout ({now - _last_heartbeat_ack_time:.1f}s ago). Force closing socket...")
                if ws and not ws.closed:
                    await ws.close()
                break

            if ws and not ws.closed:
                await ws.send_json({"op": 1, "d": _last_seq})
                logger.debug("Heartbeat sent")
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.debug(f"Heartbeat error: {e}")


async def _sync_menu_and_panels():
    """在后台自动同步 QQ 机器人自定义菜单与指令面板"""
    try:
        root_dir = Path(__file__).parent.parent
        if str(root_dir) not in sys.path:
            sys.path.insert(0, str(root_dir))
        from manage_menu_panel import QQMenuPanelManager
        loop = asyncio.get_running_loop()
        def _do_sync():
            mgr = QQMenuPanelManager(APP_ID, CLIENT_SECRET)
            return mgr.ensure_all_defaults()
        res = await loop.run_in_executor(None, _do_sync)
        logger.info(f"QQ 菜单与面板已就绪: 菜单版本 {res.get('menu', {}).get('version')}, C2C面板: {res.get('panel_c2c', {}).get('action')}, 群面板: {res.get('panel_group', {}).get('action')}")
    except Exception as e:
        logger.warning(f"自动同步菜单面板跳过/失败: {e}")


async def main():
    global _running
    _running = True

    # 启动后台异步日志监听服务
    asyncio.create_task(log_listener())

    # 自动同步/注册 QQ 自定义菜单与指令面板
    asyncio.create_task(_sync_menu_and_panels())

    # 首次启动拉起本地保活终端
    agy_mgr.start(fresh=False)

    try:
        gateway_url = await get_gateway_url()
        logger.info(f"Gateway URL: {gateway_url}")
    except Exception as e:
        logger.error(f"Failed to get gateway: {e}")
        sys.exit(1)

    while _running:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    gateway_url,
                    timeout=aiohttp.ClientTimeout(total=CONNECT_TIMEOUT),
                    heartbeat=HEARTBEAT_INTERVAL,
                ) as ws:
                    logger.info("WS connected")
                    await event_loop(ws)
        except asyncio.CancelledError:
            break
        except Exception as e:
            if _running:
                logger.error(f"WS connection error: {e}")
                backoff = RECONNECT_BACKOFF[0]
                logger.info(f"Reconnecting in {backoff}s...")
                await asyncio.sleep(backoff)

    agy_mgr.terminate()
    logger.info("Bridge stopped")



import socket

_lock_socket = None

def acquire_single_instance_lock():
    global _lock_socket
    try:
        _lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _lock_socket.bind(("127.0.0.1", 28712))
        return True
    except socket.error:
        return False


if __name__ == "__main__":
    if not acquire_single_instance_lock():
        logger.error("Another instance of agy_qq_bridge_win.py is already running. Exiting.")
        sys.exit(0)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
