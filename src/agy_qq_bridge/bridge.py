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
import uuid
import logging
import glob
from typing import Optional, Dict, Any
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
BRAIN_DIR = Path(os.environ.get("BRAIN_DIR", str(Path.home() / ".gemini/antigravity-cli/brain")))
LOG_DIR = Path(os.environ.get("LOG_DIR", str(Path.home() / ".agy-qq-bridge")))

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

    async def log_listener(self):
        """纯异步增量日志广播协程：无脑在后台读取最新修改日志的增量并推送到 QQ。"""
        # 启动时，先扫描并绑定目前最新的日志（以当前 24 小时前为基线）
        init_log = self.find_latest_transcript(time.time() - 86400.0)
        if init_log:
            self.current_log_path = init_log
            try:
                self.last_log_size = init_log.stat().st_size
                # 扫描已有的历史日志，提取最新一条回复的时间戳，进行时间锁死防止历史刷屏
                with open(init_log, 'r', encoding='utf-8', errors='replace') as f:
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
            logger.info(f"[Listener] Bound to existing active log: {self.current_log_path} (size={self.last_log_size}, last_ts={self.last_sent_timestamp})")

        while self.running:
            await asyncio.sleep(0.5)

            # 1. 动态探测是否有新修改的文件诞生（比如重置会话拉起新 UUID 目录）
            try:
                latest_log = self.find_latest_transcript(time.time() - 86400.0)
                if latest_log and (not self.current_log_path or latest_log != self.current_log_path):
                    self.current_log_path = latest_log
                    self.last_log_size = 0  # 绑定全新文件，从头读起
                    logger.info(f"[Listener] Switched to newer active log: {self.current_log_path}")
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
        if content.strip().lower() in ["/new", "/reset", "/清空", "/新对话", "new", "reset"]:
            logger.info("[Recv] New session command received")
            self.group_chat_buffer.clear()

            # 1. 强杀 tmux s0
            proc_kill = await asyncio.create_subprocess_shell(f"tmux kill-session -t {self.tmux_session} 2>/dev/null || true")
            await proc_kill.communicate()
            await asyncio.sleep(0.5)

            # 2. 强建 tmux s0
            proc_new = await asyncio.create_subprocess_exec("tmux", "new-session", "-d", "-s", self.tmux_session)
            await proc_new.communicate()
            await asyncio.sleep(2.0)

            # 3. 启动 AGY
            proc_start = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", f"{self.tmux_session}:", self.agy_start_cmd, "Enter"
            )
            await proc_start.communicate()

            # 4. 确认信任提示
            await asyncio.sleep(4.0)
            proc_enter = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", f"{self.tmux_session}:", "Enter", ""
            )
            await proc_enter.communicate()

            reply = "✅ 已强杀并重建 tmux 会话，重新拉起全新 AGY。上下文已完全重置。"
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
            reply = await get_local_git_status(Path.cwd())
            await self.send_message_rest(user_openid, reply)
            return

        if content.strip().lower() in ["/help", "/帮助", "帮助", "help"]:
            reply = (
                "🤖 **AGY-QQ-Bridge 控制中心**\n\n"
                "• `/new` 或 `/清空`：重置后台 tmux 会话，开启全新无上下文会话\n"
                "• `/stop` 或 `/停止`：向后台发送 Ctrl+C 中断信号终止当前任务\n"
                "• `/status` 或 `/状态`：查看当前工作区与会话绑定状态\n"
                "• 直接发送文本：自动输入给后台 Google Antigravity CLI\n"
                "• 发送图片/文件：原生直链由 AGY 视觉与多模态解析"
            )
            await self.send_message_rest(user_openid, reply)
            return

        if content.strip().lower() in ["/status", "/状态", "status"]:
            conv_name = self.current_log_path.parent.name if self.current_log_path else "暂未绑定（等待首条消息）"
            reply = (
                "📊 **AGY-QQ-Bridge 运行状态**\n\n"
                f"• **tmux 会话**: `{self.tmux_session}`\n"
                f"• **当前会话**: `{conv_name}`\n"
                f"• **启动命令**: `{self.agy_start_cmd}`\n"
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

        if is_master and clean_content.lower() in ["/new", "/reset", "/清空", "/新对话", "new", "reset"]:
            logger.info("[Group Recv] Reset command received")
            proc_kill = await asyncio.create_subprocess_shell(f"tmux kill-session -t {self.tmux_session} 2>/dev/null || true")
            await proc_kill.communicate()
            await asyncio.sleep(0.5)
            proc_new = await asyncio.create_subprocess_exec("tmux", "new-session", "-d", "-s", self.tmux_session)
            await proc_new.communicate()
            await asyncio.sleep(2.0)
            proc_start = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", f"{self.tmux_session}:", self.agy_start_cmd, "Enter"
            )
            await proc_start.communicate()
            await asyncio.sleep(4.0)
            proc_enter = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", f"{self.tmux_session}:", "Enter", ""
            )
            await proc_enter.communicate()

            self.group_chat_buffer.clear()
            reply = "✅ 已强杀并重建 tmux 会话，重新拉起全新 AGY。上下文与群聊缓存已完全重置。"
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
            reply = await get_local_git_status(Path.cwd())
            await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
            return

        if clean_content.lower() in ["/help", "/帮助", "帮助", "help"]:
            reply = (
                "🤖 **AGY-QQ-Bridge 群聊指令说明**\n\n"
                "• `@机器人 /new`：(管理员) 重置会话与群聊讨论缓存\n"
                "• `@机器人 /stop`：(管理员) 发送中断信号停止当前任务\n"
                "• `@机器人 /status`：查看当前运行状态与工作区\n"
                "• `@机器人 [问题]`：将群聊最近上下文与提问汇总送交 AGY CLI"
            )
            await self.send_group_message_rest(group_openid, reply, reply_to=msg_id)
            return

        if clean_content.lower() in ["/status", "/状态", "status"]:
            conv_name = self.current_log_path.parent.name if self.current_log_path else "暂未绑定"
            reply = (
                "📊 **AGY-QQ-Bridge 运行状态**\n\n"
                f"• **tmux 会话**: `{self.tmux_session}`\n"
                f"• **当前会话**: `{conv_name}`\n"
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
                            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                        elif t in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}:
                            task = asyncio.create_task(self.handle_group_message(d, t))
                            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
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