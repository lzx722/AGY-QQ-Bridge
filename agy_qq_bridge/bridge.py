"""
agy_qq_bridge.bridge — AGY 核心桥接器与指令调度中心
整合终端管理器、QQ 官方客户端、脑部日志增量监听与会话生命周期管理
"""
import sys
import time
import socket
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, List

from .config import (
    APP_ID,
    CLIENT_SECRET,
    MASTER_OPENID,
    AGY_CMD,
    AGY_WORKSPACE,
    TMUX_SESSION,
    setup_logger,
)
from .diagnostics import get_local_git_status
from .session_manager import (
    parse_timestamp,
    shorten_workspace,
    parse_history_range,
    parse_resume_arg,
    validate_rename_title,
    get_conversation_title,
    rename_conversation,
    get_history_conversations,
    SessionHistoryTracker,
)
from .terminal import (
    BaseTerminalManager,
    create_terminal_manager,
)
from .qq_client import QQClient
from .log_listener import LogListener

logger = setup_logger("agy_qq_bridge")

VERSION = "2.3.0"


class BridgeApp:
    """AGY QQ 桥接服务主应用"""

    def __init__(
        self,
        terminal_manager: Optional[BaseTerminalManager] = None,
        master_openid: str = MASTER_OPENID,
        app_id: str = APP_ID,
        client_secret: str = CLIENT_SECRET,
    ):
        self.master_openid = master_openid
        self.app_id = app_id
        self.client_secret = client_secret

        # 终端抽象层（自动适配 Linux tmux 或 Windows ConPTY）
        self.terminal_manager = terminal_manager or create_terminal_manager(
            start_cmd=AGY_CMD,
            workspace=AGY_WORKSPACE,
            tmux_session=TMUX_SESSION,
        )

        # QQ 官方通信客户端
        self.qq_client = QQClient(
            app_id=self.app_id,
            client_secret=self.client_secret,
            master_openid=self.master_openid,
        )

        # 日志监听器
        self.log_listener = LogListener(terminal_manager=self.terminal_manager)

        # 会话历史追踪器 (90s / 3 次跳转控制)
        self.history_tracker = SessionHistoryTracker(ttl_seconds=90.0, max_jumps=3)

        # 群聊上下文缓存与动态路由
        self.group_chat_buffer: List[str] = []
        self.last_message_source: Dict[str, Any] = {
            "type": "c2c",
            "openid": self.master_openid,
            "reply_to": None,
        }

    async def handle_reply(self, text: str) -> None:
        """广播模型最终回复至对应渠道"""
        target = self.last_message_source
        if target.get("type") == "group":
            group_id = target.get("openid")
            reply_to = target.get("reply_to")
            if group_id:
                await self.qq_client.send_group_message(group_id, text, reply_to=reply_to)
        else:
            dest = target.get("openid") or self.master_openid
            if dest:
                await self.qq_client.send_c2c_message(dest, text)

    async def handle_event(self, event_type: str, d: dict) -> None:
        """WebSocket 接收事件分发"""
        if event_type == "C2C_MESSAGE_CREATE":
            await self.handle_c2c_message(d)
        elif event_type in ["GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"]:
            await self.handle_group_message(d, event_type)

    async def handle_c2c_message(self, d: dict) -> None:
        """处理 C2C 私聊消息与本地指令"""
        msg_id = str(d.get("id", ""))
        if not msg_id or self.qq_client.is_duplicate(msg_id):
            return

        content = str(d.get("content", "")).strip()

        # 提取多模态附件
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

        logger.info(f"[C2C Recv] openid={user_openid}: {content[:100]}")

        # 首次私聊自动绑定 MASTER_OPENID
        if not self.master_openid:
            self.master_openid = user_openid
            self.qq_client.master_openid = user_openid
            logger.info(f"[Auto-bind] 首次私聊用户 {user_openid} 已自动绑定为 MASTER_OPENID")
            try:
                for env_file in [Path(".env"), Path(__file__).parent.parent.parent / ".env"]:
                    if env_file.exists():
                        txt = env_file.read_text(encoding="utf-8")
                        if "MASTER_OPENID=" not in txt:
                            env_file.write_text(txt.rstrip() + f"\nMASTER_OPENID={user_openid}\n", encoding="utf-8")
            except Exception as e:
                logger.error(f"自动写入 .env MASTER_OPENID 失败: {e}")

        # 鉴权：非管理员静默丢弃
        if user_openid != self.master_openid:
            logger.info(f"[Skip] 非管理员私聊: {user_openid}")
            return

        # 登记动态路由指向此用户
        self.last_message_source = {"type": "c2c", "openid": user_openid, "reply_to": None}

        parts = content.strip().split()
        cmd = parts[0].lower() if parts else ""

        # 1. /new /reset
        if cmd in ["/new", "/reset", "/清空", "/新对话", "new", "reset"]:
            logger.info("[Recv] 收到重置会话指令")
            self.history_tracker.reset()
            self.group_chat_buffer.clear()
            await self.terminal_manager.start(fresh=True)
            reply = f"✅ 已重置后台常驻会话 ({self.terminal_manager.get_info()})，拉起全新 AGY。上下文已完全清空。"
            await self.qq_client.send_c2c_message(user_openid, reply)
            return

        # 2. /history
        if cmd in ["/history", "/历史", "/sessions", "history"]:
            logger.info(f"[Recv] 收到会话历史请求: {content}")
            all_convs = get_history_conversations()
            total_len = len(all_convs)

            if not all_convs:
                reply = "ℹ️ 未找到任何历史会话记录。"
                await self.qq_client.send_c2c_message(user_openid, reply)
                return

            start_idx, end_idx, err_msg = parse_history_range(parts, total_len)
            if err_msg:
                await self.qq_client.send_c2c_message(user_openid, err_msg)
                return

            if start_idx > total_len:
                reply = f"⚠️ 请求的起始序号 [{start_idx}] 超出历史会话总数（共 {total_len} 个）。当前有效范围为 1 ~ {total_len}。"
                await self.qq_client.send_c2c_message(user_openid, reply)
                return

            self.history_tracker.record_history(all_convs)

            actual_end = min(end_idx, total_len)
            selected_convs = all_convs[start_idx - 1 : actual_end]
            if not selected_convs:
                reply = f"ℹ️ 未找到对应范围的历史会话记录（共 {total_len} 个）。当前有效范围为 1 ~ {total_len}。"
                await self.qq_client.send_c2c_message(user_openid, reply)
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
            await self.qq_client.send_c2c_message(user_openid, reply)
            return

        # 3. /resume
        if cmd in ["/resume", "/切换", "/switch", "resume"]:
            logger.info(f"[Recv] 收到恢复会话请求: {content}")
            target_idx, err_msg = parse_resume_arg(parts)
            if err_msg:
                await self.qq_client.send_c2c_message(user_openid, err_msg)
                return

            allowed, err_msg, target_item = self.history_tracker.can_resume(target_idx)
            if not allowed or not target_item:
                await self.qq_client.send_c2c_message(user_openid, err_msg or "⚠️ 无法切换至该会话")
                return

            target_cid = target_item["cid"]
            orig_ws_str = target_item.get("workspace", "")
            old_ws = self.terminal_manager.current_workspace
            target_cwd = Path(orig_ws_str) if (orig_ws_str and Path(orig_ws_str).exists()) else old_ws
            ws_changed = (target_cwd.resolve() != old_ws.resolve())

            logger.info(f"[Resume] 切换至会话 conv_id={target_cid}, cwd={target_cwd} (ws_changed={ws_changed})")

            # 重新拉起对应会话终端
            await self.terminal_manager.start(fresh=False, conversation_id=target_cid, cwd=target_cwd)

            # 增量监听绑定，跳过历史回灌
            target_log = self.log_listener.find_latest_transcript(0.0)
            if target_log:
                self.log_listener.bind_log(target_log, skip_history=True)

            remaining_jumps, remaining_time = self.history_tracker.record_resume_jump()
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
            await self.qq_client.send_c2c_message(user_openid, reply)
            return

        # 4. /rename
        if cmd in ["/rename", "/重命名", "/name", "rename"]:
            logger.info(f"[Recv] 收到重命名会话请求: {content}")
            curr_cid = self.log_listener.get_current_conv_id()
            if not curr_cid:
                reply = "⚠️ 当前尚未绑定任何活动会话（可先发送一条消息开启对话后再重命名）。"
                await self.qq_client.send_c2c_message(user_openid, reply)
                return

            if len(parts) < 2:
                reply = (
                    "ℹ️ **请提供新的会话主题**\n\n"
                    "• 示例：`/rename 修复QQ机器人功能`"
                )
                await self.qq_client.send_c2c_message(user_openid, reply)
                return

            new_title = " ".join(parts[1:]).strip()
            is_valid, err_msg = validate_rename_title(new_title)
            if not is_valid:
                await self.qq_client.send_c2c_message(user_openid, err_msg)
                return

            success, old_title = rename_conversation(curr_cid, new_title)
            if success:
                self.history_tracker.update_title(curr_cid, new_title)
                reply = (
                    f"🏷️ **会话主题修改成功**\n\n"
                    f"• 会话主题已从「{old_title}」改为「{new_title}」\n\n"
                    f"👉 修改已即时生效，发送 `/history` 即可在列表中查看更新后的名称。"
                )
            else:
                reply = "❌ 修改会话主题失败，请检查会话持久化存储状态。"
            await self.qq_client.send_c2c_message(user_openid, reply)
            return

        # 5. /stop
        if content.strip().lower() in ["/stop", "/停止", "/kill", "stop"]:
            logger.info("[Recv] 收到中断指令")
            was_busy = self.terminal_manager.is_busy
            await self.terminal_manager.send_ctrl_c()
            if not was_busy:
                reply = "ℹ️ 当前终端处于空闲就绪状态，未在执行耗时任务，请放心继续发送新消息。"
            else:
                reply = "⛔ 已向后台发送中断信号（Ctrl+C），正在打断当前任务并恢复就绪状态。"
            await self.qq_client.send_c2c_message(user_openid, reply)
            return

        # 6. /git
        if content.strip().lower() in ["/git", "/git status", "git status", "/git diff"]:
            logger.info("[Recv] 收到工作区 Git 诊断请求")
            reply = await get_local_git_status(self.terminal_manager.current_workspace)
            await self.qq_client.send_c2c_message(user_openid, reply)
            return

        # 7. /help
        if content.strip().lower() in ["/help", "/帮助", "帮助", "help"]:
            reply = (
                "🤖 **AGY-QQ-Bridge 控制中心**\n\n"
                "• `/new` 或 `/清空`：重置后台会话，开启全新无上下文会话\n"
                "• `/history` 或 `/历史`：查看历史会话列表（如 `/history 10`、`/history 31-40`）\n"
                "• `/resume <编号>`：快速切换并恢复至指定历史会话继续工作\n"
                "• `/rename <新主题>`：重命名当前已绑定会话的主题名称\n"
                "• `/stop` 或 `/停止`：向后台发送 Ctrl+C 中断信号终止当前任务\n"
                "• `/status` 或 `/状态`：查看当前工作区与会话绑定状态\n"
                "• `git status`：秒级本地诊断当前工作区 Git 变动状态\n"
                "• 直接发送文本：自动输入给后台 Google Antigravity CLI\n"
                "• 发送图片/文件：原生直链由 AGY 视觉与多模态解析"
            )
            await self.qq_client.send_c2c_message(user_openid, reply)
            return

        # 8. /status
        if content.strip().lower() in ["/status", "/状态", "status"]:
            curr_cid = self.log_listener.get_current_conv_id()
            if curr_cid:
                title = get_conversation_title(curr_cid)
                conv_desc = f"{title}"
            else:
                conv_desc = "暂未绑定（等待首条消息）"
            reply = (
                "📊 **AGY-QQ-Bridge 运行状态**\n\n"
                f"• **终端状态**: `{self.terminal_manager.get_info()}`\n"
                f"• **当前会话**: {conv_desc}\n"
                f"• **工作区**: `{self.terminal_manager.current_workspace}`\n"
                f"• **管理员**: `{self.master_openid[:8]}...`"
            )
            await self.qq_client.send_c2c_message(user_openid, reply)
            return

        # 常规用户 Prompt 送交终端
        logger.info(f"[QQ -> AGY] 转发 Prompt: {content[:100]}")
        await self.terminal_manager.send_message(content)

    async def handle_group_message(self, d: dict, event_type: str) -> None:
        """处理 QQ 群聊消息与动态汇总"""
        msg_id = str(d.get("id", ""))
        if not msg_id or self.qq_client.is_duplicate(msg_id):
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

        clean_content = content
        if self.qq_client.bot_openid:
            clean_content = clean_content.replace(f"<@!{self.qq_client.bot_openid}>", "").strip()

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
                if self.qq_client.bot_openid and str(mid) == str(self.qq_client.bot_openid):
                    is_mentioned = True
                    break

        if not is_mentioned:
            self.group_chat_buffer.append(msg_line)
            if len(self.group_chat_buffer) > 100:
                self.group_chat_buffer.pop(0)
            logger.info(f"[Group Buffer] From {sender_name}: {clean_content[:50]}")
            return

        self.last_message_source = {"type": "group", "openid": group_openid, "reply_to": msg_id}
        is_master = (member_openid == self.master_openid)

        g_parts = clean_content.split()
        g_cmd = g_parts[0].lower() if g_parts else ""

        # 管理员群指令
        if is_master and g_cmd in ["/new", "/reset", "/清空", "/新对话", "new", "reset"]:
            logger.info("[Group Recv] 收到重置指令")
            self.history_tracker.reset()
            self.group_chat_buffer.clear()
            await self.terminal_manager.start(fresh=True)
            reply = f"✅ 已重置后台会话 ({self.terminal_manager.get_info()})，拉起全新 AGY。上下文与群聊缓存已清空。"
            await self.qq_client.send_group_message(group_openid, reply, reply_to=msg_id)
            return

        if is_master and g_cmd in ["/history", "/历史", "/sessions", "history"]:
            all_convs = get_history_conversations()
            total_len = len(all_convs)
            if not all_convs:
                await self.qq_client.send_group_message(group_openid, "ℹ️ 未找到任何历史会话记录。", reply_to=msg_id)
                return

            start_idx, end_idx, err_msg = parse_history_range(g_parts, total_len)
            if err_msg:
                await self.qq_client.send_group_message(group_openid, err_msg, reply_to=msg_id)
                return

            if start_idx > total_len:
                await self.qq_client.send_group_message(
                    group_openid, f"⚠️ 请求起始序号超出总数（共 {total_len} 个）。", reply_to=msg_id
                )
                return

            self.history_tracker.record_history(all_convs)
            actual_end = min(end_idx, total_len)
            selected = all_convs[start_idx - 1 : actual_end]

            lines = [f"📜 **AGY 历史会话列表**（展示第 {start_idx} ~ {actual_end} 个，共 {total_len} 个）：\n"]
            for idx, item in enumerate(selected, start_idx):
                ts_sec = parse_timestamp(item.get("last_timestamp"))
                time_str = time.strftime("%m-%d %H:%M", time.localtime(ts_sec)) if ts_sec > 0 else "未知时间"
                ws_short = shorten_workspace(item["workspace"])
                title = item["final_title"].replace("\n", " ")[:32]
                lines.append(f"**[{idx}]** 💬 {title}\n📁 `{ws_short}` | 🕒 {time_str}\n")
            lines.append("👉 恢复会话：90 秒内输入 `@机器人 /resume <编号>`。")
            await self.qq_client.send_group_message(group_openid, "\n".join(lines), reply_to=msg_id)
            return

        if is_master and g_cmd in ["/resume", "/切换", "/switch", "resume"]:
            target_idx, err_msg = parse_resume_arg(g_parts)
            if err_msg:
                await self.qq_client.send_group_message(group_openid, err_msg, reply_to=msg_id)
                return
            allowed, err_msg, target_item = self.history_tracker.can_resume(target_idx)
            if not allowed or not target_item:
                await self.qq_client.send_group_message(group_openid, err_msg or "⚠️ 无法切换", reply_to=msg_id)
                return

            target_cid = target_item["cid"]
            orig_ws = target_item.get("workspace", "")
            target_cwd = Path(orig_ws) if (orig_ws and Path(orig_ws).exists()) else self.terminal_manager.current_workspace
            await self.terminal_manager.start(fresh=False, conversation_id=target_cid, cwd=target_cwd)
            target_log = self.log_listener.find_latest_transcript(0.0)
            if target_log:
                self.log_listener.bind_log(target_log, skip_history=True)

            self.history_tracker.record_resume_jump()
            reply = f"🔄 **已成功恢复历史会话**：{target_item.get('final_title', '')[:35]}"
            await self.qq_client.send_group_message(group_openid, reply, reply_to=msg_id)
            return

        if is_master and g_cmd in ["/rename", "/重命名", "/name", "rename"]:
            curr_cid = self.log_listener.get_current_conv_id()
            if not curr_cid:
                await self.qq_client.send_group_message(group_openid, "⚠️ 当前尚未绑定任何活动会话。", reply_to=msg_id)
                return
            new_title = " ".join(g_parts[1:]).strip() if len(g_parts) > 1 else ""
            is_valid, err_msg = validate_rename_title(new_title)
            if not is_valid:
                await self.qq_client.send_group_message(group_openid, err_msg, reply_to=msg_id)
                return
            success, old_title = rename_conversation(curr_cid, new_title)
            if success:
                self.history_tracker.update_title(curr_cid, new_title)
                reply = f"🏷️ **会话主题修改成功**：已从「{old_title}」改为「{new_title}」"
            else:
                reply = "❌ 修改主题失败，请检查持久化存储。"
            await self.qq_client.send_group_message(group_openid, reply, reply_to=msg_id)
            return

        if is_master and clean_content.lower() in ["/stop", "/停止", "/kill", "stop"]:
            was_busy = self.terminal_manager.is_busy
            await self.terminal_manager.send_ctrl_c()
            reply = "⛔ 已向后台发送中断信号（Ctrl+C）。" if was_busy else "ℹ️ 当前处于空闲就绪状态，无需中断。"
            await self.qq_client.send_group_message(group_openid, reply, reply_to=msg_id)
            return

        if clean_content.lower() in ["/git", "/git status", "git status", "/git diff"]:
            reply = await get_local_git_status(self.terminal_manager.current_workspace)
            await self.qq_client.send_group_message(group_openid, reply, reply_to=msg_id)
            return

        if clean_content.lower() in ["/help", "/帮助", "帮助", "help"]:
            reply = (
                "🤖 **AGY-QQ-Bridge 群聊指令说明**\n\n"
                "• `@机器人 /new`：(管理员) 重置会话与群聊讨论缓存\n"
                "• `@机器人 /history`：(管理员) 查看历史会话列表\n"
                "• `@机器人 /resume <编号>`：(管理员) 快速切换并恢复至指定会话\n"
                "• `@机器人 /rename <新主题>`：(管理员) 重命名当前活动会话主题\n"
                "• `@机器人 /stop`：(管理员) 发送中断信号停止当前任务\n"
                "• `@机器人 /status`：查看当前运行状态与工作区\n"
                "• `git status`：秒级本地诊断当前工作区 Git 变动状态\n"
                "• `@机器人 [问题]`：将群聊最近上下文与提问汇总送交 AGY CLI"
            )
            await self.qq_client.send_group_message(group_openid, reply, reply_to=msg_id)
            return

        if clean_content.lower() in ["/status", "/状态", "status"]:
            curr_cid = self.log_listener.get_current_conv_id()
            conv_desc = get_conversation_title(curr_cid) if curr_cid else "暂未绑定"
            reply = (
                "📊 **AGY-QQ-Bridge 运行状态**\n\n"
                f"• **终端状态**: `{self.terminal_manager.get_info()}`\n"
                f"• **当前会话**: {conv_desc}\n"
                f"• **工作区**: `{self.terminal_manager.current_workspace}`\n"
                f"• **群聊缓冲数**: {len(self.group_chat_buffer)} 条"
            )
            await self.qq_client.send_group_message(group_openid, reply, reply_to=msg_id)
            return

        # 拼接群聊历史上下文并送交终端
        full_payload = ""
        if self.group_chat_buffer:
            full_payload += "以下是之前的群聊讨论上下文：\n"
            full_payload += "\n".join(self.group_chat_buffer)
            full_payload += "\n\n请针对上述讨论，回答我当前的提问：\n"

        full_payload += f"[{sender_name}] {clean_content}"
        self.group_chat_buffer.clear()

        logger.info(f"[Group -> AGY Terminal] 转发打包消息，长度: {len(full_payload)}")
        await self.terminal_manager.send_message(full_payload)

    async def start(self) -> None:
        """启动整个桥接服务"""
        # 1. 启动后台日志增量监听协程
        asyncio.create_task(self.log_listener.start_listening(self.handle_reply))

        # 2. 自动同步 QQ 自定义菜单与指令面板
        asyncio.create_task(self.qq_client.sync_menu_and_panels())

        # 3. 启动常驻终端
        await self.terminal_manager.start(fresh=False)

        # 4. 运行 QQ 客户端连接与事件循环
        try:
            await self.qq_client.connect_and_listen(self.handle_event)
        finally:
            self.terminal_manager.terminate()
            logger.info("BridgeApp 已终止运行")


# 向后兼容类名
QQBridge = BridgeApp

_lock_socket = None


def acquire_single_instance_lock(port: int = 28712) -> bool:
    """通过本地 TCP 端口绑定防止 Windows / 桌面环境下重复启动多个桥接实例"""
    global _lock_socket
    try:
        _lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _lock_socket.bind(("127.0.0.1", port))
        return True
    except socket.error:
        return False


def cli() -> int:
    """CLI 入口"""
    if "--version" in sys.argv or "-V" in sys.argv:
        print(f"AGY-QQ-Bridge v{VERSION}")
        return 0

    if "--help" in sys.argv or "-h" in sys.argv:
        print("用法: agy-qq-bridge [选项]")
        print("  --help, -h       显示此帮助")
        print("  --version, -V    显示版本号")
        return 0

    if sys.platform == "win32":
        if not acquire_single_instance_lock():
            logger.error("检测到已有 agy-qq-bridge 实例在后台运行，禁止重复启动。退出。")
            return 0

    try:
        app = BridgeApp()
        asyncio.run(app.start())
    except KeyboardInterrupt:
        logger.info("用户手动中断退出")
    return 0


if __name__ == "__main__":
    sys.exit(cli())