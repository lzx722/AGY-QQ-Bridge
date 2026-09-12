"""
会话管理与持久化核心模块
提供会话历史检视、多层持久化主题修改（Triple Persistence）、
参数防误输校验与 90 秒 / 3 次跳转控制
"""
import os
import re
import json
import time
import sqlite3
import datetime
import urllib.parse
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple, Set

from .config import (
    CLI_HOME,
    BRAIN_DIR,
    HISTORY_FILE,
    CONVERSATIONS_DIR,
    CONV_SUMMARIES_DB,
    LAST_CONV_FILE,
    EXCLUDE_CONV_IDS,
    setup_logger,
)

logger = setup_logger("agy_qq_bridge.session_manager")


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


def extract_prompt_from_transcript(cid: str, brain_dir: Path = BRAIN_DIR) -> str:
    """尝试从 transcript.jsonl 中提取首个用户请求的内容作为备选标题"""
    transcript_file = brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
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


def get_conversation_title(
    cid: str,
    cli_home: Path = CLI_HOME,
    conv_summaries_db: Path = CONV_SUMMARIES_DB,
    brain_dir: Path = BRAIN_DIR
) -> str:
    """获取指定会话在持久层存储中的当前主题名称"""
    # 1. 优先读取 AGY 官方原生持久化注解文件 annotations/<cid>.pbtxt
    ann_file = cli_home / "annotations" / f"{cid}.pbtxt"
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
    if conv_summaries_db.exists():
        try:
            conn = sqlite3.connect(str(conv_summaries_db))
            c = conn.cursor()
            c.execute("SELECT title, preview FROM conversation_summaries WHERE conversation_id = ?", (cid,))
            row = c.fetchone()
            conn.close()
            if row and (row[0] or row[1]):
                return row[0] or row[1]
        except Exception:
            pass

    return extract_prompt_from_transcript(cid, brain_dir) or "未命名会话"


def rename_conversation(
    cid: str,
    new_title: str,
    cli_home: Path = CLI_HOME,
    conv_summaries_db: Path = CONV_SUMMARIES_DB,
    brain_dir: Path = BRAIN_DIR
) -> Tuple[bool, str]:
    """
    重命名指定会话的主题。
    持久化同步更新（Triple Persistence）：
    1. ~/.gemini/antigravity-cli/annotations/<cid>.pbtxt (AGY 顶层权威主题文件)
    2. ~/.gemini/antigravity-cli/cache/conversation_metadata.json (元数据快照缓存)
    3. ~/.gemini/antigravity-cli/conversation_summaries.db (SQLite 历史库)
    返回 (是否成功, 旧主题名称)
    """
    old_title = get_conversation_title(cid, cli_home, conv_summaries_db, brain_dir)
    is_valid, _ = validate_rename_title(new_title)
    if not is_valid:
        return False, old_title
    success = False

    # 1. 写入 AGY 原生 annotations/<cid>.pbtxt
    try:
        ann_dir = cli_home / "annotations"
        ann_dir.mkdir(parents=True, exist_ok=True)
        ann_file = ann_dir / f"{cid}.pbtxt"
        escaped_title = new_title.replace("\\", "\\\\").replace('"', '\\"')
        ann_file.write_text(f'title:"{escaped_title}"\n', encoding="utf-8")
        success = True
    except Exception as e:
        logger.warning(f"写入 annotations pbtxt 失败: {e}")

    # 2. 同步更新 cache/conversation_metadata.json (若存在)
    try:
        meta_file = cli_home / "cache" / "conversation_metadata.json"
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
    if conv_summaries_db.exists():
        try:
            conn = sqlite3.connect(str(conv_summaries_db))
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


def get_history_conversations(
    conversations_dir: Path = CONVERSATIONS_DIR,
    conv_summaries_db: Path = CONV_SUMMARIES_DB,
    cli_home: Path = CLI_HOME,
    history_file: Path = HISTORY_FILE,
    brain_dir: Path = BRAIN_DIR,
    exclude_conv_ids: Set[str] = EXCLUDE_CONV_IDS
) -> List[Dict[str, Any]]:
    """获取所有有效历史会话并按最后活动时间倒序排序。
    与 AGY 原生 /resume 选择器保持 100% 规则对齐：
    1. 优先扫描 conversations/ 目录与 conversation_summaries.db。
    2. 过滤掉无步骤（steps == 0，即初始空库 48KB）的无效/空会话。
    3. 提取官方格式化标题或预览摘要、原始工作区与最后修改时间。
    4. 若上述存储不存在则优雅降级读取 history.jsonl。
    """
    valid_cids = {}
    if conversations_dir.exists():
        try:
            with os.scandir(conversations_dir) as entries:
                for entry in entries:
                    name = entry.name
                    if name.endswith(".pb"):
                        cid = name[:-3]
                        if cid not in exclude_conv_ids:
                            valid_cids[cid] = entry.stat().st_mtime
                    elif name.endswith(".db"):
                        # SQLite 初始空数据库大小严格为 49152 字节 (48KB)
                        # 有实际对话 steps 的数据库大小均 >= 216KB
                        if entry.stat().st_size > 49152:
                            cid = name[:-3]
                            if cid not in exclude_conv_ids:
                                valid_cids[cid] = entry.stat().st_mtime
        except Exception as e:
            logger.debug(f"扫描 conversations 目录异常: {e}")

    # 若成功识别到有效会话，优先从 conversation_summaries.db 提取元数据
    if valid_cids and conv_summaries_db.exists():
        try:
            conn = sqlite3.connect(str(conv_summaries_db))
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
                    ann_file = cli_home / "annotations" / f"{cid}.pbtxt"
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
                        final_title = extract_prompt_from_transcript(cid, brain_dir) or "无标题会话"

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
                    "final_title": extract_prompt_from_transcript(cid, brain_dir) or "历史会话",
                    "workspace": "",
                    "last_timestamp": valid_cids[cid]
                })

            convs.sort(key=lambda x: parse_timestamp(x.get("last_timestamp")), reverse=True)
            return convs
        except Exception as e:
            logger.error(f"读取 conversation_summaries.db 异常: {e}")

    # 降级兜底方案：从 history.jsonl 解析
    convs = {}
    if history_file.exists():
        try:
            with open(history_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        cid = d.get("conversationId")
                        if not cid or cid in exclude_conv_ids:
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
            t_from_file = extract_prompt_from_transcript(c["cid"], brain_dir)
            if t_from_file:
                title = t_from_file
                c["real_prompt"] = True

        if not c["real_prompt"] and title in ["/new", "/resume"]:
            continue

        c["final_title"] = title
        valid_convs.append(c)

    valid_convs.sort(key=lambda x: parse_timestamp(x.get("last_timestamp")), reverse=True)
    return valid_convs


def get_workspace_conv_id(workspace: Path, last_conv_file: Path = LAST_CONV_FILE) -> Optional[str]:
    """从 last_conversations.json 读取指定工作区的最近会话 ID"""
    if not last_conv_file.exists():
        return None
    try:
        with open(last_conv_file, "r", encoding="utf-8") as f:
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


def transcript_has_prompt(path: Path, prompt: str) -> bool:
    """检查 transcript 日志前几行是否包含发送的 prompt 指纹"""
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


class SessionHistoryTracker:
    """
    跟踪与校验 /history 查询缓存及 /resume 切换的生命周期
    严格限制：
    1. 90 秒时效窗口
    2. 单轮列表最多 3 次跳转
    """
    def __init__(self, ttl_seconds: float = 90.0, max_jumps: int = 3):
        self.ttl_seconds = ttl_seconds
        self.max_jumps = max_jumps
        self.cached_history_list: List[Dict[str, Any]] = []
        self.last_history_time: float = 0.0
        self.history_resume_count: int = 0

    def record_history(self, convs: List[Dict[str, Any]]) -> None:
        """记录新的 /history 列表，重置倒计时与跳转计数器"""
        self.cached_history_list = list(convs)
        self.last_history_time = time.time()
        self.history_resume_count = 0

    def reset(self) -> None:
        """清空缓存（如 /new 重置会话时）"""
        self.cached_history_list = []
        self.last_history_time = 0.0
        self.history_resume_count = 0

    def can_resume(self, target_idx: int) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """
        校验当前是否允许根据序号执行 /resume 跳转
        返回 (allowed, error_msg, target_conv_item)
        """
        if not self.cached_history_list or self.last_history_time == 0:
            return False, (
                "⚠️ 当前尚未查询过历史会话列表，或列表已失效。\n\n"
                "👉 请先发送 `/history` 获取当前会话列表与对应编号。"
            ), None

        elapsed = time.time() - self.last_history_time
        if elapsed > self.ttl_seconds:
            return False, (
                f"⚠️ 上次历史会话查询已超过 {int(self.ttl_seconds)} 秒有效时限（已过 {int(elapsed)} 秒）。\n\n"
                "👉 请重新发送 `/history` 刷新列表获取最新序号后再进行切换。"
            ), None

        if self.history_resume_count >= self.max_jumps:
            return False, (
                f"⚠️ 当前轮次已达到 {self.max_jumps} 次跳转上限。\n\n"
                "👉 为避免会话状态混乱，请重新发送 `/history` 刷新列表后再进行切换。"
            ), None

        if target_idx <= 0:
            return False, (
                f"⚠️ 会话编号必须从 1 开始（输入为 {target_idx}）。当前有效编号范围为 1 ~ {len(self.cached_history_list)}。"
            ), None

        if target_idx > len(self.cached_history_list):
            return False, (
                f"⚠️ 请求的会话编号 [{target_idx}] 超出当前列表总数（共 {len(self.cached_history_list)} 个）。"
                f"当前有效编号范围为 1 ~ {len(self.cached_history_list)}。可发送 `/history` 重新查看。"
            ), None

        return True, None, self.cached_history_list[target_idx - 1]

    def record_resume_jump(self) -> Tuple[int, int]:
        """
        记录一次成功的 resume 跳转，返回 (remaining_jumps, remaining_time_seconds)
        """
        self.history_resume_count += 1
        remaining_jumps = max(0, self.max_jumps - self.history_resume_count)
        remaining_time = max(1, int(self.ttl_seconds - (time.time() - self.last_history_time)))
        return remaining_jumps, remaining_time

    def update_title(self, cid: str, new_title: str) -> None:
        """同步更新当前缓存列表中指定会话的主题"""
        if self.cached_history_list:
            for item in self.cached_history_list:
                if item.get("cid") == cid:
                    item["final_title"] = new_title
                    break
