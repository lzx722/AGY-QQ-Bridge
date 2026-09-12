"""
配置管理与运行时基础环境
提供 .env 解析、跨平台路径推断、网络常量及日志初始化
"""
import os
import sys
import logging
from pathlib import Path
from typing import Set

import aiohttp
import aiohttp.connector
import aiohttp.resolver

# 修复 Windows Proactor 事件循环下的 aiohttp 异步 DNS 解析问题
try:
    aiohttp.connector.DefaultResolver = aiohttp.resolver.ThreadedResolver
    aiohttp.connector.AsyncResolver = aiohttp.resolver.ThreadedResolver
except Exception:
    pass


def load_env(env_path: str = ".env") -> None:
    """本地 .env 解析函数，避免强依赖外部第三方库"""
    candidates = [
        Path(env_path),
        Path(__file__).parent / env_path,
        Path(__file__).parent.parent.parent / env_path,
        Path.cwd() / env_path,
        Path.home() / ".env",
    ]
    for p in candidates:
        if p.exists() and p.is_file():
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


# 立即执行环境配置加载
load_env()

# ================= QQ 官方网关与 OpenAPI 常量 =================
API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
GATEWAY_URL_PATH = "/gateway"

CONNECT_TIMEOUT = 20
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
MAX_RECONNECT_ATTEMPTS = 100
HEARTBEAT_INTERVAL = 15.0

# ================= 路径配置 =================
USER_PROFILE = os.environ.get("USERPROFILE", str(Path.home()))
CLI_HOME = Path(os.environ.get("CLI_HOME", str(Path(USER_PROFILE) / ".gemini" / "antigravity-cli")))
BRAIN_DIR = Path(os.environ.get("BRAIN_DIR", str(CLI_HOME / "brain")))
LOG_DIR = Path(os.environ.get("LOG_DIR", str(Path(USER_PROFILE) / ".agy-qq-bridge")))
HISTORY_FILE = CLI_HOME / "history.jsonl"
CONVERSATIONS_DIR = CLI_HOME / "conversations"
CONV_SUMMARIES_DB = CLI_HOME / "conversation_summaries.db"
LAST_CONV_FILE = CLI_HOME / "cache" / "last_conversations.json"

# 确保日志目录存在
os.makedirs(LOG_DIR, exist_ok=True)

# ================= 凭证与进阶运行参数 =================
APP_ID = os.environ.get("APP_ID", "")
CLIENT_SECRET = os.environ.get("CLIENT_SECRET", "")
MASTER_OPENID = os.environ.get("MASTER_OPENID", "")

TARGET_CONV_ID = os.environ.get("TARGET_CONV_ID", "").strip()
EXCLUDE_CONV_IDS: Set[str] = set(
    cid.strip() for cid in os.environ.get("EXCLUDE_CONV_IDS", "").split(",") if cid.strip()
)

# 默认启动命令与工作区
if sys.platform == "win32":
    DEFAULT_AGY_CMD = "C:\\Users\\Administrator\\AppData\\Local\\agy\\bin\\agy.exe --dangerously-skip-permissions"
    DEFAULT_WORKSPACE = USER_PROFILE
else:
    DEFAULT_AGY_CMD = "cd ~ && agy --dangerously-skip-permissions"
    DEFAULT_WORKSPACE = str(Path.home())

AGY_CMD = os.environ.get("AGY_START_CMD", DEFAULT_AGY_CMD)
AGY_WORKSPACE = Path(os.environ.get("AGY_WORKSPACE", DEFAULT_WORKSPACE)).resolve()
TMUX_SESSION = os.environ.get("TMUX_SESSION", "0")


def setup_logger(name: str = "agy_qq_bridge") -> logging.Logger:
    """初始化双端（终端标准输出 + 文件记录）日志记录器"""
    logger = logging.getLogger(name)
    logger.propagate = False
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

        file_handler = logging.FileHandler(LOG_DIR / "agy-qq-bridge.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
    return logger
