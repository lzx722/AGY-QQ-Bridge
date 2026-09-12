"""
QQ 开放平台通信客户端
封装 OpenAPI Token 鉴权、REST 消息收发、自定义菜单/指令面板同步及 WebSocket 网关事件监听
"""
import sys
import time
import uuid
import json
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any, Callable, Awaitable

import httpx
import aiohttp

from .config import (
    API_BASE,
    TOKEN_URL,
    GATEWAY_URL_PATH,
    CONNECT_TIMEOUT,
    RECONNECT_BACKOFF,
    HEARTBEAT_INTERVAL,
    setup_logger,
)

logger = setup_logger("agy_qq_bridge.qq_client")


class QQClient:
    """QQ 官方开放平台客户端"""

    def __init__(
        self,
        app_id: str,
        client_secret: str,
        master_openid: str = "",
        api_base: str = API_BASE,
        token_url: str = TOKEN_URL,
    ):
        self.app_id = app_id
        self.client_secret = client_secret
        self.master_openid = master_openid
        self.api_base = api_base
        self.token_url = token_url

        self.access_token: Optional[str] = None
        self.token_expires_at: float = 0.0
        self.session_id: Optional[str] = None
        self.last_seq: Optional[int] = None
        self.bot_openid: str = ""
        self.last_heartbeat_ack_time: float = 0.0

        self.http_client: Optional[httpx.AsyncClient] = None
        self.seen_messages: Dict[str, float] = {}
        self.running: bool = False
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None

    def get_http_client(self) -> httpx.AsyncClient:
        if self.http_client is None:
            self.http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        return self.http_client

    async def ensure_token(self) -> str:
        """获取或刷新 QQ OpenAPI 访问令牌 (Access Token)"""
        if self.access_token and time.time() < self.token_expires_at - 60:
            return self.access_token

        client = self.get_http_client()
        resp = await client.post(
            self.token_url,
            json={"appId": self.app_id, "clientSecret": self.client_secret},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError(f"获取 QQ Access Token 失败: {data}")

        expires_in = int(data.get("expires_in", 7200))
        self.access_token = token
        self.token_expires_at = time.time() + expires_in
        logger.info(f"[QQ Client] Token 刷新成功，有效期 {expires_in} 秒")
        return token

    async def get_gateway_url(self) -> str:
        """获取 QQ 官方 WebSocket 网关接入地址"""
        token = await self.ensure_token()
        client = self.get_http_client()
        resp = await client.get(
            f"{self.api_base}{GATEWAY_URL_PATH}",
            headers={"Authorization": f"QQBot {token}", "User-Agent": "AGY-QQ-Bridge/2.0"},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        url = data.get("url")
        if not url:
            raise RuntimeError(f"获取 Gateway URL 失败: {data}")
        return url

    def _next_msg_seq(self, target_id: str = "default") -> int:
        time_part = int(time.time()) % 100000000
        rand = int(uuid.uuid4().hex[:4], 16)
        return (time_part ^ rand) % 65536

    def is_duplicate(self, msg_id: str) -> bool:
        """检查消息是否重复，并清理过期记录"""
        now = time.time()
        if msg_id in self.seen_messages and now - self.seen_messages[msg_id] < 300:
            return True
        self.seen_messages[msg_id] = now
        if len(self.seen_messages) > 1000:
            for k in list(self.seen_messages.keys()):
                if now - self.seen_messages[k] > 600:
                    del self.seen_messages[k]
        return False

    async def send_c2c_message(self, user_openid: str, content: str) -> bool:
        """向指定 QQ 私聊 (C2C) 用户发送 Markdown 消息"""
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
                f"{self.api_base}/v2/users/{user_openid}/messages",
                headers=headers,
                json=body,
                timeout=30.0,
            )
            if resp.status_code >= 400:
                logger.error(f"[QQ Client] C2C 发送失败 [{resp.status_code}]: {resp.text[:200]}")
                return False
            return True
        except Exception as e:
            logger.error(f"[QQ Client] C2C 发送异常: {e}")
            return False

    async def send_group_message(
        self,
        group_openid: str,
        content: str,
        reply_to: Optional[str] = None
    ) -> bool:
        """向指定 QQ 群聊发送 Markdown 消息"""
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
                f"{self.api_base}/v2/groups/{group_openid}/messages",
                headers=headers,
                json=body,
                timeout=30.0,
            )
            if resp.status_code >= 400:
                logger.error(f"[QQ Client] 群消息发送失败 [{resp.status_code}]: {resp.text[:200]}")
                return False
            return True
        except Exception as e:
            logger.error(f"[QQ Client] 群消息发送异常: {e}")
            return False

    async def sync_menu_and_panels(self) -> None:
        """在后台自动同步 QQ 自定义菜单与指令面板"""
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
            logger.info(
                f"[QQ Client] 菜单与面板同步完毕: 菜单版本 {res.get('menu', {}).get('version')}, "
                f"C2C面板: {res.get('panel_c2c', {}).get('action')}, 群面板: {res.get('panel_group', {}).get('action')}"
            )
        except Exception as e:
            logger.warning(f"[QQ Client] 自动同步菜单面板跳过/失败: {e}")

    async def _send_identify(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        token = await self.ensure_token()
        payload = {
            "op": 2,
            "d": {
                "token": f"QQBot {token}",
                "intents": (1 << 25) | (1 << 30) | (1 << 12) | (1 << 26),
                "shard": [0, 1],
                "properties": {
                    "$os": sys.platform,
                    "$browser": "agy-qq-bridge",
                    "$device": "agy-qq-bridge",
                },
            },
        }
        await ws.send_json(payload)
        logger.info("[QQ Client] Identify 认证凭证已发送")

    async def _send_resume(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        token = await self.ensure_token()
        payload = {
            "op": 6,
            "d": {"token": f"QQBot {token}", "session_id": self.session_id, "seq": self.last_seq},
        }
        await ws.send_json(payload)
        logger.info(f"[QQ Client] Resume 会话恢复已发送 (session={self.session_id}, seq={self.last_seq})")

    async def _heartbeat_sender(self, ws: aiohttp.ClientWebSocketResponse, interval: float) -> None:
        try:
            while self.running and ws and not ws.closed:
                await asyncio.sleep(interval)
                now = time.time()
                # 看门狗检测：若超过 2.5 倍心跳周期无 ACK，强制关闭重连
                if self.last_heartbeat_ack_time > 0 and (now - self.last_heartbeat_ack_time > interval * 2.5):
                    logger.warning(
                        f"[QQ Client] 心跳响应超时 ({now - self.last_heartbeat_ack_time:.1f}s 无 ACK)，强制重连..."
                    )
                    if ws and not ws.closed:
                        await ws.close()
                    break

                if ws and not ws.closed:
                    await ws.send_json({"op": 1, "d": self.last_seq})
                    logger.debug("[QQ Client] 心跳已发送")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.debug(f"[QQ Client] 心跳异常: {e}")

    async def connect_and_listen(
        self,
        event_handler: Callable[[str, Dict[str, Any]], Awaitable[None]]
    ) -> None:
        """主 WebSocket 连接与事件监听循环"""
        self.running = True

        try:
            gateway_url = await self.get_gateway_url()
            logger.info(f"[QQ Client] Gateway URL: {gateway_url}")
        except Exception as e:
            logger.error(f"[QQ Client] 获取 Gateway 失败: {e}")
            raise

        while self.running:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(
                        gateway_url,
                        timeout=aiohttp.ClientTimeout(total=CONNECT_TIMEOUT),
                        heartbeat=HEARTBEAT_INTERVAL,
                    ) as ws:
                        self.ws = ws
                        logger.info("[QQ Client] WebSocket 已建立连接")
                        heartbeat_task = asyncio.create_task(
                            self._heartbeat_sender(ws, HEARTBEAT_INTERVAL)
                        )

                        try:
                            while self.running and ws and not ws.closed:
                                msg = await ws.receive()
                                if msg.type == aiohttp.WSMsgType.TEXT:
                                    try:
                                        payload = json.loads(msg.data)
                                    except json.JSONDecodeError:
                                        logger.warning(f"[QQ Client] JSON 解析失败: {msg.data[:100]}")
                                        continue

                                    op = payload.get("op")
                                    d = payload.get("d")
                                    s = payload.get("s")
                                    t = payload.get("t")

                                    if s is not None:
                                        self.last_seq = s

                                    # op 10 Hello
                                    if op == 10:
                                        d_data = d if isinstance(d, dict) else {}
                                        interval_ms = d_data.get("heartbeat_interval", 30000)
                                        logger.info(f"[QQ Client] 收到 Hello, 心跳间隔={interval_ms}ms")
                                        self.last_heartbeat_ack_time = time.time()
                                        if self.session_id and self.last_seq is not None:
                                            await self._send_resume(ws)
                                        else:
                                            await self._send_identify(ws)
                                        continue

                                    # op 11 Heartbeat ACK
                                    if op == 11:
                                        self.last_heartbeat_ack_time = time.time()
                                        logger.debug("[QQ Client] 收到心跳 ACK")
                                        continue

                                    # op 7 Server Reconnect
                                    if op == 7:
                                        logger.info("[QQ Client] 服务端请求重连 (op 7)")
                                        if ws and not ws.closed:
                                            await ws.close()
                                        break

                                    # op 9 Invalid Session
                                    if op == 9:
                                        resumable = bool(d) if d is not None else False
                                        if not resumable:
                                            logger.info("[QQ Client] 无效会话 (op 9，不可续接)，重置 session_id")
                                            self.session_id = None
                                            self.last_seq = None
                                        else:
                                            logger.info("[QQ Client] 无效会话 (op 9，可续接)")
                                        if ws and not ws.closed:
                                            await ws.close()
                                        break

                                    # op 0 Dispatch
                                    if op == 0 and t:
                                        logger.info(f"[QQ Client] 事件分发: event_type={t}")
                                        if t == "READY":
                                            if isinstance(d, dict):
                                                self.session_id = d.get("session_id")
                                                user = d.get("user") if isinstance(d.get("user"), dict) else {}
                                                self.bot_openid = str(user.get("id", ""))
                                                logger.info(
                                                    f"[QQ Client] READY 成功: session_id={self.session_id}, "
                                                    f"bot_openid={self.bot_openid}"
                                                )
                                        elif t == "RESUMED":
                                            logger.info("[QQ Client] 会话已无缝恢复 (RESUMED)")

                                        # 调用事件处理器
                                        task = asyncio.create_task(event_handler(t, d if isinstance(d, dict) else {}))
                                        task.add_done_callback(self._log_task_exception)

                                elif msg.type == aiohttp.WSMsgType.CLOSE:
                                    logger.warning("[QQ Client] 收到 WebSocket 关闭帧")
                                    break
                                elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                                    raise RuntimeError("WebSocket 异常关闭")
                        finally:
                            heartbeat_task.cancel()

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self.running:
                    logger.error(f"[QQ Client] 连接异常: {e}")
                    backoff = RECONNECT_BACKOFF[0]
                    logger.info(f"[QQ Client] {backoff} 秒后尝试重新连接...")
                    await asyncio.sleep(backoff)

        logger.info("[QQ Client] 事件监听循环已停止")

    def _log_task_exception(self, t: asyncio.Task) -> None:
        if not t.cancelled():
            exc = t.exception()
            if exc:
                logger.error(f"[QQ Client Task Error] 异步消息处理异常: {exc}", exc_info=exc)
