#!/usr/bin/env python3
"""
agy_qq_bridge.bridge — AGY tmux 常驻进程直连 C2C 桥接 QQ
架构: QQ官方WS网关 ↔ Python asyncio ↔ tmux send-keys ↔ AGY
流程:
  QQ 消息 → 桥接脚本 → tmux send-keys -t 0 "消息" Enter
  AGY 回复 → 后台异步循环监听 AGY brain transcript.jsonl 增量推送到 QQ
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

# ================= 环境与配置加载 =================
def load_env(env_path: str = ".env"):
    """极简的本地 .env 解析函数，避免依赖外部 python-dotenv 库"""
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

# ================= 全局常量 =================
API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
GATEWAY_URL_PATH = "/gateway"

CONNECT_TIMEOUT = 20
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
MAX_RECONNECT_ATTEMPTS = 100
HEARTBEAT_INTERVAL = 15.0

# 路径与命令配置化
CLI_HOME = Path(os.environ.get("CLI_HOME", str(Path.home() / ".gemini/antigravity-cli")))
BRAIN_DIR = Path(os.environ.get("BRAIN_DIR", str(CLI_HOME / "brain")))
LOG_DIR = Path(os.environ.get("LOG_DIR", str(Path.home() / ".agy-qq-bridge")))
HISTORY_FILE = CLI_HOME / "history.jsonl"
CONVERSATIONS_DIR = CLI_HOME / "conversations"
CONV_SUMMARIES_DB = CLI_HOME / "conversation_summaries.db"
LAST_CONV_FILE = CLI_HOME / "cache" / "last_conversations.json"
EXCLUDE_CONV_IDS = set(
    cid.strip() for cid in os.environ.get("EXCLUDE_CONV_IDS", "").split(",") if cid.strip()
)

os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "agy-qq-bridge.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("agy_qq_bridge")


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
    r"""智能缩短工作区路径展示，如 /home/user/project -> project，主目录 -> ~"""
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

        if not c["real_prompt"] and title in ["/new", "/resume"]:
            continue

        c["final_title"] = title
        valid_convs.append(c)

    valid_convs.sort(key=lambda x: parse_timestamp(x.get("last_timestamp")), reverse=True)
    return valid_convs


def _log_task_exception(t):
    if not t.cancelled():
        exc = t.exception()
        if exc:
            logger.error(f"[Task Error] 异步任务执行异常: {exc}", exc_info=exc)


class QQBridge:
    def __init__(self):
        # 从环境变量加载配置
        self.app_id = os.environ.get("APP_ID", "")
        self.client_secret = os.environ.get("CLIENT_SECRET", "")
        self.master_openid = os.environ.get("MASTER_OPENID", "")
        self.tmux_session = os.environ.get("TMUX_SESSION", "0")
        self.agy_start_cmd = os.environ.get("AGY_START_CMD", "cd ~ && agy --dangerously-skip-permissions")

        # 核心连接状态
        self.access_token: Optional[str] = None
        self.token_expires_at: float = 0.0
        self.session_id: Optional[str] = None
        self.last_seq: Optional[int] = None
        self.ws = None
        self.http_client = None
        self.running = False
        self.last_msg_id: Optional[str] = None
        self.bot_openid: str = ""
        self.heartbeat_task = None

        # 消息去重
        self.seen_messages: Dict[str, float] = {}

        # 异步群聊缓存与动态路由
        self.group_chat_buffer = []
        # 初始化 last_message_source
        self.last_message_source = {"type": "c2c", "openid": self.master_openid, "reply_to": None}

        # 异步监听状态
        self.last_log_size = 0
        self.current_log_path = None
        self.last_sent_timestamp = ""  # 记录最后发送给 QQ 的消息时间戳，防重与防历史刷屏
        self.is_busy = False
        self.last_sent_time = 0.0
        self.cached_history_list: List[Dict[str, Any]] = []
        self.last_history_time: float = 0.0
        self.history_resume_count: int = 0
        self.agy_workspace = Path(os.environ.get("AGY_WORKSPACE", str(Path.home()))).resolve()
        self.needs_rebind = False

    def get_current_conv_id(self) -> Optional[str]:
        """获取当前已绑定的活跃会话 ID"""
        if self.current_log_path and len(self.current_log_path.parts) >= 4:
            return self.current_log_path.parts[-4]
        ws_cid = get_workspace_conv_id(self.agy_workspace)
        if ws_cid:
            return ws_cid
        return None

    def get_http_client(self):
        if self.http_client is None:
            import httpx
            self.http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        return self.http_client

    async def ensure_token(self) -> str:
        if self.access_token and time.time() < self.token_expires_at - 60:
            return self.access_token
        client = self.get_http_client()
        resp = await client.post(
            TOKEN_URL,
            json={"appId": self.app_id, "clientSecret": self.client_secret},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"Failed to get token: {data}")
        expires_in = int(data.get("expires_in", 7200))
        self.access_token = token
        self.token_expires_at = time.time() + expires_in
        logger.info(f"Token refreshed, expires in {expires_in}s")
        return token

    async def get_gateway_url(self) -> str:
        token = await self.ensure_token()
        client = self.get_http_client()
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

    async def send_identify(self, ws):
        token = await self.ensure_token()
        payload = {
            "op": 2,
            "d": {
                "token": f"QQBot {token}",
                "intents": (1 << 25) | (1 << 30) | (1 << 12) | (1 << 26),
                "shard": [0, 1],
                "properties": {"$os": "Linux", "$browser": "agy-qq-bridge", "$device": "agy-qq-bridge"},
            },
        }
        await ws.send_json(payload)
        logger.info("Identify sent")

    async def send_resume(self, ws):
        token = await self.ensure_token()
        payload = {
            "op": 6,
            "d": {"token": f"QQBot {token}", "session_id": self.session_id, "seq": self.last_seq},
        }
        await ws.send_json(payload)
        logger.info(f"Resume sent (session={self.session_id}, seq={self.last_seq})")

    def _next_msg_seq(self, msg_id: str = "default") -> int:
        time_part = int(time.time()) % 100000000
        rand = int(uuid.uuid4().hex[:4], 16)
        return (time_part ^ rand) % 65536

    def is_duplicate(self, msg_id: str) -> bool:
        now = time.time()
        if msg_id in self.seen_messages and now - self.seen_messages[msg_id] < 300:
            return True
        self.seen_messages[msg_id] = now
        if len(self.seen_messages) > 1000:
            for k in list(self.seen_messages.keys()):
                if now - self.seen_messages[k] > 600:
                    del self.seen_messages[k]
        return False

    async def send_message_rest(self, user_openid: str, content: str) -> bool:
        """给指定用户发送 C2C 消息"""
        token = await self.ensure_token()
        client = self.get_http_client()
        headers = {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
            "User-Agent": "AGY-QQ-Bridge/2.0",
        }
        msg_seq = self._next_msg_seq(user_openid)
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

    async def send_group_message_rest(self, group_openid: str, content: str, reply_to: Optional[str] = None) -> bool:
        """给指定群聊发送消息"""
        token = await self.ensure_token()
        client = self.get_http_client()
        headers = {
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
            "User-Agent": "AGY-QQ-Bridge/2.0",
        }
        msg_seq = self._next_msg_seq(group_openid)
        display_content = content[:3990] + "\n\n... (已截断)" if len(content) > 4000 else content
        body = {"markdown": {"content": display_content}, "msg_type": 2, "msg_seq": msg_seq}
        if reply_to:
            body["msg_id"] = reply_to

        try:
            resp = await client.post(
                f"{API_BASE}/v2/groups/{group_openid}/messages",
                headers=headers, json=body, timeout=30.0,
            )
            if resp.status_code >= 400:
                logger.error(f"Send group failed [{resp.status_code}]: {resp.text[:200]}")
                return False
            return True
        except Exception as e:
            logger.error(f"Send group exception: {e}")
            return False

    async def send_to_agy(self, message: str):
        """发送消息给 tmux 中的 AGY"""
        self.is_busy = True
        self.last_sent_time = time.time()
        logger.info(f"[Tmux Target] Sending keys to session: {self.tmux_session}")
        # 模拟按 Escape 强退可能卡在 TUI 或 PAGER 的状态
        proc_esc = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", self.tmux_session, "Escape", ""
        )
        await proc_esc.communicate()
        await asyncio.sleep(0.5)

        # 写入消息
        proc_msg = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", self.tmux_session, message, ""
        )
        await proc_msg.communicate()
        await asyncio.sleep(0.1)

        # 按回车执行
        proc_enter = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", self.tmux_session, "Enter", ""
        )
        await proc_enter.communicate()
        logger.info(f"[Bridge -> AGY] {message[:100]}")

    def find_latest_transcript(self, min_mtime: float) -> Optional[Path]:
        """获取在 min_mtime 之后新修改/创建的最新 transcript.jsonl 日志文件"""
        pattern = str(BRAIN_DIR / "*" / ".system_generated" / "logs" / "transcript.jsonl")
        paths = glob.glob(pattern)
        if not paths:
            return None
        paths_with_mtime = []
        for p in paths:
            try:
                mtime = os.path.getmtime(p)
                if mtime >= min_mtime:
                    paths_with_mtime.append((Path(p), mtime))
            except OSError:
                continue
        if not paths_with_mtime:
            return None
        paths_with_mtime.sort(key=lambda x: x[1], reverse=True)
        return paths_with_mtime[0][0]

    def bind_log(self, log_path: Path, skip_history: bool = False):
        """绑定目标日志并设定偏移指针"""
        self.current_log_path = log_path
        if skip_history:
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
            self.last_log_size = 0
        logger.info(f"[Listener] Bound to log: {self.current_log_path} (size={self.last_log_size}, last_ts={self.last_sent_timestamp}, skip_history={skip_history})")

    async def restart_agy(self, conversation_id: Optional[str] = None, cwd: Optional[Path] = None):
        """强杀并重新拉起 tmux session 中的 AGY 进程"""
        target_cwd = cwd if (cwd and cwd.exists()) else self.agy_workspace
        self.is_busy = False

        # 1. 强杀现有 tmux session
        proc_kill = await asyncio.create_subprocess_shell(f"tmux kill-session -t {self.tmux_session} 2>/dev/null || true")
        await proc_kill.communicate()
        await asyncio.sleep(0.5)

        # 2. 强建 tmux session 并指定工作目录
        proc_new = await asyncio.create_subprocess_exec("tmux", "new-session", "-d", "-s", self.tmux_session, "-c", str(target_cwd))
        await proc_new.communicate()
        await asyncio.sleep(2.0)

        # 3. 启动 AGY
        if conversation_id:
            cmd = f"agy --dangerously-skip-permissions --conversation {conversation_id}"
            self.needs_rebind = False
        else:
            cmd = self.agy_start_cmd
            self.needs_rebind = True

        proc_start = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", f"{self.tmux_session}:", cmd, "Enter"
        )
        await proc_start.communicate()

        # 4. 确认信任提示
        await asyncio.sleep(4.0)
        proc_enter = await asyncio.create_subprocess_exec(
            "tmux", "send-keys", "-t", f"{self.tmux_session}:", "Enter", ""
        )
        await proc_enter.communicate()

        if cwd and cwd.exists():
            self.agy_workspace = target_cwd.resolve()

        if conversation_id:
            target_log = BRAIN_DIR / conversation_id / ".system_generated" / "logs" / "transcript.jsonl"
            if target_log.exists():
                self.bind_log(target_log, skip_history=True)

    async def log_listener(self):
        """纯异步增量日志广播协程：无脑在后台读取最新修改日志的增量并推送到 QQ。"""
        # 启动时，先扫描并绑定目前最新的日志（以当前 24 小时前为基线）
        init_log = self.find_latest_transcript(time.time() - 86400.0)
        if init_log:
            self.bind_log(init_log, skip_history=True)

        while self.running:
            await asyncio.sleep(0.5)

            # 1. 尚未绑定日志，或者显式触发重置时探测新修改的文件诞生
            if not self.current_log_path or self.needs_rebind:
                try:
                    latest_log = self.find_latest_transcript(time.time() - 86400.0)
                    if latest_log and (not self.current_log_path or latest_log != self.current_log_path):
                        self.bind_log(latest_log, skip_history=False)
                        self.needs_rebind = False
                except Exception as e:
                    logger.error(f"[Listener] Scan error: {e}")

            if not self.current_log_path:
                continue

            # 2. 检测大小变动
            try:
                curr_size = self.current_log_path.stat().st_size
            except FileNotFoundError:
                self.current_log_path = None
                continue

            # 针对日志文件被 AI 客户端自动截断/收缩导致的 log rotation 现象进行安全水位重置
            if curr_size < self.last_log_size:
                logger.info(f"[Listener] Log file truncated (decreased from {self.last_log_size} to {curr_size}), resetting offset.")
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
                    # 如果当前行的时间戳不大于已发送的时间戳，说明是重读的历史记录，直接跳过
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
                        logger.info(f"[Listener -> QQ] Broadcasting response (ts={ts}): {text[:100]}")
                        if ts:
                            self.last_sent_timestamp = ts
                        self.is_busy = False
                        # 动态路由选择投递渠道
                        target = self.last_message_source
                        if target["type"] == "group":
                            await self.send_group_message_rest(target["openid"], text, reply_to=target["reply_to"])
                        else:
                            dest = target["openid"] or self.master_openid
                            if dest:
                                await self.send_message_rest(dest, text)

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


    async def handle_c2c_message(self, d: dict):
        msg_id = str(d.get("id", ""))
        if not msg_id or self.is_duplicate(msg_id):
            return

        content = str(d.get("content", "")).strip()

        # 提取附件
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

        self.last_msg_id = msg_id
        logger.info(f"[C2C Recv] openid={user_openid}: {content[:100]}")

        # 检查并自动绑定 MASTER_OPENID
        if not self.master_openid:
            self.master_openid = user_openid
            logger.info(f"[Auto-bind] First message from {user_openid} set as MASTER_OPENID")
            try:
                env_path = Path(".env")
                if env_path.exists():
                    env_text = env_path.read_text(encoding="utf-8")
                    if "MASTER_OPENID=" not in env_text:
                        env_path.write_text(
                            env_text.rstrip() + f"\nMASTER_OPENID={user_openid}\n",
                            encoding="utf-8",
                        )
            except Exception as e:
                logger.error(f"Failed to auto-bind MASTER_OPENID in .env: {e}")

        # 非主人消息直接静默丢弃
        if user_openid != self.master_openid:
            logger.info(f"[Skip] non-master openid: {user_openid}")
            return

        # 登记当前指令来自 C2C 私发
        self.last_message_source = {"type": "c2c", "openid": user_openid, "reply_to": None}

        # 命令处理
        parts = content.strip().split()
        cmd = parts[0].lower() if parts else ""

        if cmd in ["/new", "/reset", "/清空", "/新对话", "new", "reset"]:
            logger.info("[Recv] New session command received")
            self.cached_history_list = []
            self.last_history_time = 0.0
            self.history_resume_count = 0
            self.group_chat_buffer.clear()
            self.current_log_path = None
            self.last_log_size = 0
            self.last_sent_timestamp = ""
            await self.restart_agy(conversation_id=None)
            reply = "✅ 已强杀并重建 tmux 会话，重新拉起全新 AGY。上下文已完全重置。"
            await self.send_message_rest(user_openid, reply)
            return

        if cmd in ["/history", "/历史", "/sessions", "history"]:
            logger.info(f"[Recv] History list requested: {content}")
            all_convs = get_history_conversations()
            total_len = len(all_convs)

            if not all_convs:
                reply = "ℹ️ 未找到任何历史会话记录。"
                await self.send_message_rest(user_openid, reply)
                return

            start_idx, end_idx, err_msg = parse_history_range(parts, total_len)
            if err_msg:
                await self.send_message_rest(user_openid, err_msg)
                return

            if start_idx > total_len:
                reply = f"⚠️ 请求的起始序号 [{start_idx}] 超出历史会话总数（共 {total_len} 个）。当前有效范围为 1 ~ {total_len}。"
                await self.send_message_rest(user_openid, reply)
                return

            # 校验成功后才刷新 90 秒窗口与计数
            self.cached_history_list = all_convs
            self.last_history_time = time.time()
            self.history_resume_count = 0

            actual_end = min(end_idx, total_len)
            selected_convs = all_convs[start_idx - 1 : actual_end]
            if not selected_convs:
                reply = f"ℹ️ 未找到对应范围的历史会话记录（共 {total_len} 个）。当前有效范围为 1 ~ {total_len}。"
                await self.send_message_rest(user_openid, reply)
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
            await self.send_message_rest(user_openid, reply)
            return

        if cmd in ["/resume", "/切换", "/switch", "resume"]:
            logger.info(f"[Recv] Resume session requested: {content}")
            now = time.time()

            # 1. 检查是否存在有效 history 缓存以及是否在 90 秒内
            if not self.cached_history_list or self.last_history_time == 0:
                reply = (
                    "⚠️ **未找到有效的历史会话列表**\n\n"
                    "• 请先发送 `/history` 查看历史会话列表；\n"
                    "• 并在列表展示后的 **90 秒内** 使用 `/resume <编号>` 进行恢复。"
                )
                await self.send_message_rest(user_openid, reply)
                return

            elapsed = now - self.last_history_time
            if elapsed > 90:
                reply = (
                    f"⚠️ **历史会话列表已过期**（已过 {int(elapsed)} 秒，超时限制为 90 秒）。\n\n"
                    "👉 请重新发送 `/history` 获取最新会话列表后再进行恢复。"
                )
                await self.send_message_rest(user_openid, reply)
                return

            # 2. 检查 90 秒内跳转次数上限（最多 3 次）
            if self.history_resume_count >= 3:
                reply = (
                    "⚠️ **本轮历史会话的恢复跳转次数已达上限**（最多连续跳转 3 次）。\n\n"
                    "👉 如需继续切换会话，请重新发送 `/history` 刷新会话列表。"
                )
                await self.send_message_rest(user_openid, reply)
                return

            # 3. 参数检测
            if len(parts) < 2:
                remaining_time = max(1, int(90 - elapsed))
                remaining_jumps = 3 - self.history_resume_count
                reply = (
                    "ℹ️ **请指定要恢复的会话序号**\n\n"
                    f"• 示例：`/resume 1`\n"
                    f"• 状态：本轮还可跳转 {remaining_jumps} 次，列表有效期剩余 {remaining_time} 秒。\n"
                    "• 提示：可发送 `/history` 重新查看会话列表与编号。"
                )
                await self.send_message_rest(user_openid, reply)
                return

            if len(parts) > 2:
                reply = (
                    f"⚠️ **参数过多**：`/resume` 仅支持单个会话编号。\n\n"
                    f"• 正确示例：`/resume {parts[1]}`\n"
                    f"• 请勿在编号后输入多余参数。"
                )
                await self.send_message_rest(user_openid, reply)
                return

            target_arg = parts[1].strip()

            # 检查是否误输入了范围（如 31-40 或 31~40）
            if re.search(r"[-~.]", target_arg):
                reply = (
                    f"⚠️ 检测到范围格式「{target_arg}」，`/resume` 仅支持恢复单个会话编号（如 `/resume 1`）。\n\n"
                    f"👉 若要查看第 {target_arg} 项的会话列表，请使用：`/history {target_arg}`"
                )
                await self.send_message_rest(user_openid, reply)
                return

            # 检查是否误输入了页码（如 p2, page 2）
            if re.match(r"^(?:p|page|页)\d+$", target_arg, re.I):
                reply = (
                    f"⚠️ 检测到页码格式「{target_arg}」，`/resume` 仅支持具体会话编号（如 `/resume 1`）。\n\n"
                    f"👉 若要查看该页会话列表，请使用：`/history {target_arg}`"
                )
                await self.send_message_rest(user_openid, reply)
                return

            all_convs = self.cached_history_list
            clean_arg = target_arg.lstrip("#").strip("[]()")

            if not clean_arg.isdigit():
                reply = (
                    f"⚠️ 无效的参数「{target_arg}」。`/resume` 的参数必须为纯数字会话编号。\n\n"
                    f"• 正确示例：`/resume 1`\n"
                    f"• 当前列表中共有 {len(all_convs)} 个会话，可发送 `/history` 重新查看。"
                )
                await self.send_message_rest(user_openid, reply)
                return

            idx = int(clean_arg)
            if idx <= 0:
                reply = f"⚠️ 会话编号必须从 1 开始（输入为 {idx}）。当前有效编号范围为 1 ~ {len(all_convs)}。"
                await self.send_message_rest(user_openid, reply)
                return

            if idx > len(all_convs):
                reply = f"⚠️ 请求的会话编号 [{idx}] 超出当前列表总数（共 {len(all_convs)} 个）。当前有效编号范围为 1 ~ {len(all_convs)}。可发送 `/history` 重新查看。"
                await self.send_message_rest(user_openid, reply)
                return

            target_item = all_convs[idx - 1]

            target_cid = target_item["cid"]
            orig_ws_str = target_item.get("workspace", "")
            old_ws = self.agy_workspace
            target_cwd = Path(orig_ws_str) if (orig_ws_str and Path(orig_ws_str).exists()) else self.agy_workspace
            ws_changed = (target_cwd.resolve() != old_ws.resolve())

            logger.info(f"[Resume] Switching to conv_id={target_cid}, cwd={target_cwd} (ws_changed={ws_changed})")

            # 1. 重拉 tmux 会话
            await self.restart_agy(conversation_id=target_cid, cwd=target_cwd)

            self.history_resume_count += 1
            remaining_jumps = 3 - self.history_resume_count
            remaining_time = max(1, int(90 - (time.time() - self.last_history_time)))
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
            await self.send_message_rest(user_openid, reply)
            return

        if cmd in ["/rename", "/重命名", "/name", "rename"]:
            logger.info(f"[Recv] Rename session requested: {content}")
            curr_cid = self.get_current_conv_id()
            if not curr_cid:
                reply = "⚠️ 当前尚未绑定任何活动会话（可先发送一条消息开启对话后再重命名）。"
                await self.send_message_rest(user_openid, reply)
                return

            if len(parts) < 2:
                reply = (
                    "ℹ️ **请提供新的会话主题**\n\n"
                    "• 示例：`/rename 修复QQ机器人功能`"
                )
                await self.send_message_rest(user_openid, reply)
                return

            new_title = " ".join(parts[1:]).strip()
            is_valid, err_msg = validate_rename_title(new_title)
            if not is_valid:
                await self.send_message_rest(user_openid, err_msg)
                return

            success, old_title = rename_conversation(curr_cid, new_title)
            if success:
                if self.cached_history_list:
                    for c in self.cached_history_list:
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
            await self.send_message_rest(user_openid, reply)
            return

        if content.strip().lower() in ["/stop", "/停止", "/kill", "stop"]:
            logger.info("[Recv] Stop command received")
            # 智能防护：如果当前没有正在执行的任务（空闲状态），发送 Escape 清理输入状态，绝不发送多次 Ctrl+C 杀退终端
            if not self.is_busy and (time.time() - self.last_sent_time > 60 or self.last_sent_time == 0):
                proc = await asyncio.create_subprocess_exec(
                    "tmux", "send-keys", "-t", f"{self.tmux_session}:", "Escape", ""
                )
                await proc.communicate()
                reply = "ℹ️ 当前终端处于空闲就绪状态，未在执行耗时任务，请放心继续发送新消息。"
            else:
                for key in ["C-c", "Escape"]:
                    proc = await asyncio.create_subprocess_exec(
                        "tmux", "send-keys", "-t", f"{self.tmux_session}:", key, ""
                    )
                    await proc.communicate()
                    await asyncio.sleep(0.2)
                self.is_busy = False
                reply = "⛔ 已向后台发送中断信号（Ctrl+C），正在打断当前任务并恢复就绪状态。"
            await self.send_message_rest(user_openid, reply)
            return

        if content.strip().lower() in ["/git", "/git status", "git status", "/git diff"]:
            logger.info("[Recv] Local git status requested")
            reply = await get_local_git_status(self.agy_workspace)
            await self.send_message_rest(user_openid, reply)
            return

        if content.strip().lower() in ["/help", "/帮助", "帮助", "help"]:
            reply = (
                "🤖 **AGY-QQ-Bridge 控制中心**\n\n"
                "• `/new` 或 `/清空`：重置后台 tmux 会话，开启全新无上下文会话\n"
                "• `/history` 或 `/历史`：查看历史会话列表（如 `/history 10`、`/history 31-40`）\n"
                "• `/resume <编号>`：快速切换并恢复至指定历史会话继续工作\n"
                "• `/rename <新主题>`：重命名当前已绑定会话的主题名称\n"
                "• `/stop` 或 `/停止`：向后台发送 Ctrl+C 中断信号终止当前任务\n"
                "• `/status` 或 `/状态`：查看当前工作区与会话绑定状态\n"
                "• `git status`：秒级本地诊断当前工作区 Git 变动状态\n"
                "• 直接发送文本：自动输入给后台 Google Antigravity CLI\n"
                "• 发送图片/文件：原生直链由 AGY 视觉与多模态解析"
            )
            await self.send_message_rest(user_openid, reply)
            return

        if content.strip().lower() in ["/status", "/状态", "status"]:
            curr_cid = self.get_current_conv_id()
            if curr_cid:
                title = get_conversation_title(curr_cid)
                conv_desc = f"{title}"
            else:
                conv_desc = "暂未绑定（等待首条消息）"
            reply = (
                "📊 **AGY-QQ-Bridge 运行状态**\n\n"
                f"• **tmux 会话**: `{self.tmux_session}`\n"
                f"• **当前会话**: {conv_desc}\n"
                f"• **工作区**: `{self.agy_workspace}`\n"
                f"• **管理员**: `{self.master_openid[:8]}...`"
            )
            await self.send_message_rest(user_openid, reply)
            return

        logger.info(f"[QQ -> AGY] {content}")
        # 直接发送，不等待，不阻塞
        await self.send_to_agy(content)

    async def handle_group_message(self, d: dict, event_type: str):
        msg_id = str(d.get("id", ""))
        if not msg_id or self.is_duplicate(msg_id):
            return

        content = str(d.get("content", "")).strip()

        # 提取附件
        attachments = d.get("attachments") or []
        for att in attachments:
            url = att.get("url")
            if url:
                name = att.get("filename") or att.get("name") or "file"
                content += f"\n\n[附件({name}): {url}]"

        content = content.strip()
        group_openid = str(d.get("group_openid", ""))
        author = d.get("author") if isinstance(d.get("author"), dict) else {}
        member_openid = str(author.get("member_openid", ""))

        if not group_openid or not content:
            return

        sender_name = author.get("nickname") or author.get("username")
        if not sender_name:
            sender_name = f"user_{member_openid[-6:]}" if member_openid else "User"

        # 过滤 @ 机器人的前缀
        clean_content = content
        if self.bot_openid:
            clean_content = clean_content.replace(f"<@!{self.bot_openid}>", "").strip()

        msg_line = f"[{sender_name}] {clean_content}"

        is_mentioned = False
        if event_type == "GROUP_AT_MESSAGE_CREATE":
            is_mentioned = True
        else:
            mentions = d.get("mentions") or []
            for m in mentions:
                if m.get("is_you") is True:
                    is_mentioned = True
                    break
                mid = m.get("member_openid") or m.get("id") or m.get("user_openid") or ""
                if self.bot_openid and str(mid) == str(self.bot_openid):
                    is_mentioned = True
                    break

        if not is_mentioned:
            # 没被 @ 时默默记录到缓冲中
            self.group_chat_buffer.append(msg_line)
            if len(self.group_chat_buffer) > 100:
                self.group_chat_buffer.pop(0)
            logger.info(f"[Group Buffer] From {sender_name}: {clean_content[:50]}")
            return

        self.last_msg_id = msg_id
        logger.info(f"[Group Recv AT] From {sender_name}: {clean_content[:100]}")

        # 动态更新路由指向此群聊
        self.last_message_source = {"type": "group", "openid": group_openid, "reply_to": msg_id}

        # 判断发送人是否是主人授权执行命令
        is_master = (member_openid == self.master_openid)

        g_parts = clean_content.split()
        g_cmd = g_parts[0].lower() if g_parts else ""

        if is_master and g_cmd in ["/new", "/reset", "/清空", "/新对话", "new", "reset"]:
            logger.info("[Group Recv] Reset command received")
            self.cached_history_list = []
            self.last_history_time = 0.0
            self.history_resume_count = 0
            self.group_chat_buffer.clear()
            self.current_log_path = None
            self.last_log_size = 0
            self.last_sent_timestamp = ""
            await self.restart_agy(conversation_id=None)
            reply = "✅ 已强杀并重建 tmux 会话，重新拉起全新 AGY。上下文与群聊缓存已完全重置。"
            await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
            return

        if is_master and g_cmd in ["/history", "/历史", "/sessions", "history"]:
            logger.info(f"[Group Recv] History list requested: {clean_content}")
            all_convs = get_history_conversations()
            total_len = len(all_convs)

            if not all_convs:
                reply = "ℹ️ 未找到任何历史会话记录。"
                await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
                return

            start_idx, end_idx, err_msg = parse_history_range(g_parts, total_len)
            if err_msg:
                await self.send_group_message_rest(group_openid, err_msg, reply_to=msg_id)
                return

            if start_idx > total_len:
                reply = f"⚠️ 请求的起始序号 [{start_idx}] 超出历史会话总数（共 {total_len} 个）。当前有效范围为 1 ~ {total_len}。"
                await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
                return

            # 校验成功后才刷新 90 秒窗口与计数
            self.cached_history_list = all_convs
            self.last_history_time = time.time()
            self.history_resume_count = 0

            actual_end = min(end_idx, total_len)
            selected_convs = all_convs[start_idx - 1 : actual_end]
            if not selected_convs:
                reply = f"ℹ️ 未找到对应范围的历史会话记录（共 {total_len} 个）。当前有效范围为 1 ~ {total_len}。"
                await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
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

            lines.append(f"👉 **恢复会话**：90 秒内输入 `@机器人 /resume <编号>` (如 `/resume {start_idx}`) 即可切换（最多跳转 3 次）。")
            lines.append("💡 **翻页提示**：支持范围如 `/history 31-40`、页码如 `/history p4` 或指定数量如 `/history 10`。")
            reply = "\n".join(lines)
            await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
            return

        if is_master and g_cmd in ["/resume", "/切换", "/switch", "resume"]:
            logger.info(f"[Group Recv] Resume session requested: {clean_content}")
            now = time.time()

            # 1. 检查是否存在有效 history 缓存以及是否在 90 秒内
            if not self.cached_history_list or self.last_history_time == 0:
                reply = (
                    "⚠️ **未找到有效的历史会话列表**\n\n"
                    "• 请先发送 `@机器人 /history` 查看历史会话列表；\n"
                    "• 并在列表展示后的 **90 秒内** 使用 `@机器人 /resume <编号>` 进行恢复。"
                )
                await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
                return

            elapsed = now - self.last_history_time
            if elapsed > 90:
                reply = (
                    f"⚠️ **历史会话列表已过期**（已过 {int(elapsed)} 秒，超时限制为 90 秒）。\n\n"
                    "👉 请重新发送 `@机器人 /history` 获取最新会话列表后再进行恢复。"
                )
                await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
                return

            # 2. 检查 90 秒内跳转次数上限（最多 3 次）
            if self.history_resume_count >= 3:
                reply = (
                    "⚠️ **本轮历史会话的恢复跳转次数已达上限**（最多连续跳转 3 次）。\n\n"
                    "👉 如需继续切换会话，请重新发送 `@机器人 /history` 刷新会话列表。"
                )
                await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
                return

            # 3. 参数检测
            if len(g_parts) < 2:
                remaining_time = max(1, int(90 - elapsed))
                remaining_jumps = 3 - self.history_resume_count
                reply = (
                    "ℹ️ **请指定要恢复的会话序号**\n\n"
                    f"• 示例：`@机器人 /resume 1`\n"
                    f"• 状态：本轮还可跳转 {remaining_jumps} 次，列表有效期剩余 {remaining_time} 秒。\n"
                    "• 提示：可发送 `@机器人 /history` 查看最近历史会话列表。"
                )
                await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
                return

            if len(g_parts) > 2:
                reply = (
                    f"⚠️ **参数过多**：`/resume` 仅支持单个会话编号。\n\n"
                    f"• 正确示例：`@机器人 /resume {g_parts[1]}`\n"
                    f"• 请勿在编号后输入多余参数。"
                )
                await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
                return

            target_arg = g_parts[1].strip()

            # 检查是否误输入了范围（如 31-40 或 31~40）
            if re.search(r"[-~.]", target_arg):
                reply = (
                    f"⚠️ 检测到范围格式「{target_arg}」，`/resume` 仅支持恢复单个会话编号（如 `@机器人 /resume 1`）。\n\n"
                    f"👉 若要查看第 {target_arg} 项的会话列表，请使用：`@机器人 /history {target_arg}`"
                )
                await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
                return

            # 检查是否误输入了页码（如 p2, page 2）
            if re.match(r"^(?:p|page|页)\d+$", target_arg, re.I):
                reply = (
                    f"⚠️ 检测到页码格式「{target_arg}」，`/resume` 仅支持具体会话编号（如 `@机器人 /resume 1`）。\n\n"
                    f"👉 若要查看该页会话列表，请使用：`@机器人 /history {target_arg}`"
                )
                await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
                return

            all_convs = self.cached_history_list
            clean_arg = target_arg.lstrip("#").strip("[]()")

            if not clean_arg.isdigit():
                reply = (
                    f"⚠️ 无效的参数「{target_arg}」。`/resume` 的参数必须为纯数字会话编号。\n\n"
                    f"• 正确示例：`@机器人 /resume 1`\n"
                    f"• 当前列表中共有 {len(all_convs)} 个会话，可发送 `@机器人 /history` 重新查看。"
                )
                await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
                return

            idx = int(clean_arg)
            if idx <= 0:
                reply = f"⚠️ 会话编号必须从 1 开始（输入为 {idx}）。当前有效编号范围为 1 ~ {len(all_convs)}。"
                await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
                return

            if idx > len(all_convs):
                reply = f"⚠️ 请求的会话编号 [{idx}] 超出当前列表总数（共 {len(all_convs)} 个）。当前有效编号范围为 1 ~ {len(all_convs)}。可发送 `@机器人 /history` 重新查看。"
                await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
                return

            target_item = all_convs[idx - 1]

            target_cid = target_item["cid"]
            orig_ws_str = target_item.get("workspace", "")
            old_ws = self.agy_workspace
            target_cwd = Path(orig_ws_str) if (orig_ws_str and Path(orig_ws_str).exists()) else self.agy_workspace
            ws_changed = (target_cwd.resolve() != old_ws.resolve())

            await self.restart_agy(conversation_id=target_cid, cwd=target_cwd)
            self.group_chat_buffer.clear()

            self.history_resume_count += 1
            remaining_jumps = 3 - self.history_resume_count
            remaining_time = max(1, int(90 - (time.time() - self.last_history_time)))
            jump_note = (
                f"\n• **跳转限额**: 本轮剩余 {remaining_jumps} 次（有效期剩 {remaining_time} 秒）"
                if remaining_jumps > 0
                else "\n• **跳转限额**: 本轮 3 次跳转已用完，下次切换请先发送 `@机器人 /history`"
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
                f"👉 终端已在后台热重载就绪，直接 @机器人 发送消息即可在当前会话中继续工作！"
            )
            await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
            return

        if is_master and g_cmd in ["/rename", "/重命名", "/name", "rename"]:
            logger.info(f"[Group Recv] Rename session requested: {clean_content}")
            curr_cid = self.get_current_conv_id()
            if not curr_cid:
                reply = "⚠️ 当前尚未绑定任何活动会话（可先发送一条消息开启对话后再重命名）。"
                await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
                return

            if len(g_parts) < 2:
                reply = (
                    "ℹ️ **请提供新的会话主题**\n\n"
                    "• 示例：`@机器人 /rename 修复QQ机器人功能`"
                )
                await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
                return

            new_title = " ".join(g_parts[1:]).strip()
            is_valid, err_msg = validate_rename_title(new_title)
            if not is_valid:
                await self.send_group_message_rest(group_openid, err_msg, reply_to=msg_id)
                return

            success, old_title = rename_conversation(curr_cid, new_title)
            if success:
                if self.cached_history_list:
                    for c in self.cached_history_list:
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
            await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
            return

        if is_master and clean_content.lower() in ["/stop", "/停止", "/kill", "stop"]:
            logger.info("[Group Recv] Stop command received")
            if not self.is_busy and (time.time() - self.last_sent_time > 60 or self.last_sent_time == 0):
                proc = await asyncio.create_subprocess_exec(
                    "tmux", "send-keys", "-t", f"{self.tmux_session}:", "Escape", ""
                )
                await proc.communicate()
                reply = "ℹ️ 当前终端处于空闲就绪状态，未在执行耗时任务，请放心继续提问。"
            else:
                for key in ["C-c", "Escape"]:
                    proc = await asyncio.create_subprocess_exec(
                        "tmux", "send-keys", "-t", f"{self.tmux_session}:", key, ""
                    )
                    await proc.communicate()
                    await asyncio.sleep(0.2)
                self.is_busy = False
                reply = "⛔ 已向后台发送中断信号（Ctrl+C），正在打断当前任务并恢复就绪状态。"
            await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
            return

        if clean_content.lower() in ["/git", "/git status", "git status", "/git diff"]:
            logger.info("[Group Recv] Local git status requested")
            reply = await get_local_git_status(self.agy_workspace)
            await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
            return

        if clean_content.lower() in ["/help", "/帮助", "帮助", "help"]:
            reply = (
                "🤖 **AGY-QQ-Bridge 群聊指令说明**\n\n"
                "• `@机器人 /new`：(管理员) 重置会话与群聊讨论缓存\n"
                "• `@机器人 /history`：(管理员) 查看历史会话列表（如 `/history 10`、`/history 31-40`）\n"
                "• `@机器人 /resume <编号>`：(管理员) 快速切换并恢复至指定会话\n"
                "• `@机器人 /rename <新主题>`：(管理员) 重命名当前活动会话主题\n"
                "• `@机器人 /stop`：(管理员) 发送中断信号停止当前任务\n"
                "• `@机器人 /status`：查看当前运行状态与工作区\n"
                "• `git status`：秒级本地诊断当前工作区 Git 变动状态\n"
                "• `@机器人 [问题]`：将群聊最近上下文与提问汇总送交 AGY CLI"
            )
            await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
            return

        if clean_content.lower() in ["/status", "/状态", "status"]:
            curr_cid = self.get_current_conv_id()
            if curr_cid:
                title = get_conversation_title(curr_cid)
                conv_desc = f"{title}"
            else:
                conv_desc = "暂未绑定"
            reply = (
                "📊 **AGY-QQ-Bridge 运行状态**\n\n"
                f"• **tmux 会话**: `{self.tmux_session}`\n"
                f"• **当前会话**: {conv_desc}\n"
                f"• **工作区**: `{self.agy_workspace}`\n"
                f"• **群聊缓冲数**: {len(self.group_chat_buffer)} 条"
            )
            await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
            return

        # 拼接群聊历史上下文
        full_payload = ""
        if self.group_chat_buffer:
            full_payload += "以下是之前的群聊讨论上下文：\n"
            full_payload += "\n".join(self.group_chat_buffer)
            full_payload += "\n\n请针对上述讨论，回答我当前的提问：\n"

        full_payload += f"[{sender_name}] {clean_content}"

        # 消费后立即清空缓存队列，绝对不循环发送旧消息
        self.group_chat_buffer.clear()

        logger.info(f"[Group -> AGY Terminal] Sending packed payload size: {len(full_payload)}")
        await self.send_to_agy(full_payload)

    async def event_loop(self, ws):
        self.ws = ws
        self.heartbeat_task = asyncio.create_task(self._heartbeat_sender(ws, HEARTBEAT_INTERVAL))

        try:
            while self.running and ws and not ws.closed:
                msg = await ws.receive()
                if msg.type == 1:
                    try:
                        payload = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.warning(f"JSON parse error: {msg.data[:100]}")
                        continue

                    op = payload.get("op")
                    t = payload.get("t")
                    s = payload.get("s")
                    d = payload.get("d")

                    if isinstance(s, int) and (self.last_seq is None or s > self.last_seq):
                        self.last_seq = s

                    if op == 10:
                        d_data = d if isinstance(d, dict) else {}
                        interval_ms = d_data.get("heartbeat_interval", 30000)
                        heartbeat_interval = interval_ms / 1000.0 * 0.8
                        logger.info(f"Hello recv, heartbeat={heartbeat_interval:.1f}s")
                        
                        if self.heartbeat_task:
                            self.heartbeat_task.cancel()
                        self.heartbeat_task = asyncio.create_task(self._heartbeat_sender(ws, heartbeat_interval))

                        if self.session_id and self.last_seq is not None:
                            await self.send_resume(ws)
                        else:
                            await self.send_identify(ws)
                        continue

                    if op == 0 and t:
                        logger.info(f"[WS Dispatch] event_type={t}")
                        if t == "READY":
                            if isinstance(d, dict):
                                self.session_id = d.get("session_id")
                                user = d.get("user") if isinstance(d.get("user"), dict) else {}
                                self.bot_openid = str(user.get("id", ""))
                                logger.info(f"READY, session_id={self.session_id}, bot_openid={self.bot_openid}")
                        elif t == "RESUMED":
                            logger.info("Session resumed")
                        elif t == "C2C_MESSAGE_CREATE":
                            task = asyncio.create_task(self.handle_c2c_message(d))
                            task.add_done_callback(_log_task_exception)
                        elif t in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}:
                            task = asyncio.create_task(self.handle_group_message(d, t))
                            task.add_done_callback(_log_task_exception)
                        continue

                elif msg.type == 9:
                    logger.warning("WS close received")
                    break

        except Exception as e:
            logger.error(f"Event loop error: {e}")

    async def _heartbeat_sender(self, ws, interval: float):
        try:
            while self.running and ws and not ws.closed:
                await asyncio.sleep(interval)
                if ws and not ws.closed:
                    await ws.send_json({"op": 1, "d": self.last_seq})
                    logger.debug("Heartbeat sent")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.debug(f"Heartbeat error: {e}")

    async def _sync_menu_and_panels(self):
        """在后台自动同步 QQ 机器人自定义菜单与指令面板"""
        try:
            root_dir = Path(__file__).resolve().parent.parent.parent
            if str(root_dir) not in sys.path:
                sys.path.insert(0, str(root_dir))
            from manage_menu_panel import QQMenuPanelManager
            loop = asyncio.get_running_loop()
            def _do_sync():
                mgr = QQMenuPanelManager(self.app_id, self.client_secret)
                return mgr.ensure_all_defaults()
            res = await loop.run_in_executor(None, _do_sync)
            logger.info(f"QQ 菜单与面板已就绪: 菜单版本 {res.get('menu', {}).get('version')}, C2C面板: {res.get('panel_c2c', {}).get('action')}, 群面板: {res.get('panel_group', {}).get('action')}")
        except Exception as e:
            logger.warning(f"自动同步菜单面板跳过/失败: {e}")

    async def start(self):
        self.running = True

        # 启动后台异步日志监听服务
        asyncio.create_task(self.log_listener())

        # 自动同步/注册 QQ 自定义菜单与指令面板
        asyncio.create_task(self._sync_menu_and_panels())

        try:
            gateway_url = await self.get_gateway_url()
            logger.info(f"Gateway URL: {gateway_url}")
        except Exception as e:
            logger.error(f"Failed to get gateway: {e}")
            sys.exit(1)

        import aiohttp

        while self.running:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                        gateway_url,
                        timeout=aiohttp.ClientTimeout(total=CONNECT_TIMEOUT),
                        heartbeat=HEARTBEAT_INTERVAL,
                    ) as ws:
                        logger.info("WS connected")
                        await self.event_loop(ws)
            except asyncio.CancelledError:
                break
            except Exception as e:
                if self.running:
                    logger.error(f"WS connection error: {e}")
                    backoff = RECONNECT_BACKOFF[0]
                    logger.info(f"Reconnecting in {backoff}s...")
                    await asyncio.sleep(backoff)

        logger.info("Bridge stopped")


# ================= CLI 入口与 --init 交互式配置 =================

VERSION = "2.0.0"


def run_init():
    """交互式初始化配置，自动生成 .env 文件"""
    print("=" * 50)
    print("  AGY QQ Bridge — 初始化配置")
    print("=" * 50)
    print()

    app_id = input("请输入 QQ Bot APP_ID: ").strip()
    while not app_id:
        app_id = input("APP_ID 不能为空，请输入: ").strip()

    client_secret = input("请输入 QQ Bot CLIENT_SECRET: ").strip()
    while not client_secret:
        client_secret = input("CLIENT_SECRET 不能为空，请输入: ").strip()

    tmux_session = input("tmux 会话名称（默认 0，直接回车使用默认）: ").strip()
    if not tmux_session:
        tmux_session = "0"

    print()
    print("-" * 40)
    print("AGY 启动命令配置（可选）")
    print("-" * 40)
    print()
    print(f"默认值: cd ~ && agy --dangerously-skip-permissions")
    print("提示：如果你的 agy 不在 PATH 里，或需要 script -q -c 包装，请自定义。")
    print("      （大多数用户直接回车即可）")
    print()
    agy_start_cmd = input("回车使用默认: ").strip()
    if agy_start_cmd:
        agy_start_cmd_line = f"\nAGY_START_CMD={agy_start_cmd}"
    else:
        agy_start_cmd_line = ""

    env_path = Path(".env")
    content = f"""# AGY-QQ-Bridge 配置 — 由 `agy-qq-bridge --init` 自动生成
# MASTER_OPENID 将在首次收到消息时自动绑定
APP_ID={app_id}
CLIENT_SECRET={client_secret}
TMUX_SESSION={tmux_session}{agy_start_cmd_line}
"""
    env_path.write_text(content, encoding="utf-8")
    print()
    print("✅ .env 已生成！")
    print()
    print("运行以下命令启动桥接服务：")
    print()
    print("  agy-qq-bridge")
    print()
    print("或使用 PM2 保活：")
    print()
    print("  pm2 start $(which agy-qq-bridge) --name agy-qq-bridge")
    print()


def cli() -> int:
    """CLI 入口：处理 --init / --version 后运行主桥接"""
    # 检查命令行参数
    if "--init" in sys.argv or (len(sys.argv) > 1 and sys.argv[1] == "--init"):
        run_init()
        return 0

    if "--version" in sys.argv or "-V" in sys.argv:
        print(f"AGY-QQ-Bridge v{VERSION}")
        return 0

    if "--help" in sys.argv or "-h" in sys.argv:
        print("用法:")
        print("  agy-qq-bridge            启动桥接服务")
        print("  agy-qq-bridge --init     交互式配置（首次使用）")
        print("  agy-qq-bridge --version  显示版本号")
        print("  agy-qq-bridge --help     显示帮助")
        print()
        print("配置说明：")
        print("  运行 --init 后会生成 .env 文件，")
        print("  也可手动创建 .env 填入 APP_ID / CLIENT_SECRET / MASTER_OPENID")
        return 0

    # 检查 .env 是否存在
    env_found = any(
        Path(p).exists()
        for p in [".env", str(Path(__file__).parent / ".env"), str(Path.home() / ".env")]
    )
    if not env_found:
        print("⚠️  未找到 .env 配置文件！")
        print("   请先运行: agy-qq-bridge --init")
        print("   或手动创建 .env 文件（参考 .env.example）")
        return 1

    # 运行主桥接
    try:
        bridge = QQBridge()
        asyncio.run(bridge.start())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    return 0


if __name__ == "__main__":
    sys.exit(cli())