"""
agy_qq_bridge.menu_panel — QQ 开放平台自定义菜单与指令面板管理
提供原生异步 API，用于部署、同步及校验 C2C 底部菜单与 C2C/Group 指令面板
"""
import os
import sys
import json
import argparse
import asyncio
from pathlib import Path
from typing import Dict, Any

import httpx

if __name__ == "__main__" and not __package__:
    _src_path = Path(__file__).resolve().parent.parent
    if str(_src_path) not in sys.path:
        sys.path.insert(0, str(_src_path))
    from agy_qq_bridge.config import API_BASE, TOKEN_URL, setup_logger
else:
    from .config import API_BASE, TOKEN_URL, setup_logger

logger = setup_logger("agy_qq_bridge.menu_panel")

# ================= 默认推荐配置 =================
DEFAULT_CUSTOM_MENU: Dict[str, Any] = {
    "menu": {
        "items": [
            {
                "type": "send_message",
                "name": "新对话",
                "send_message": "/new"
            },
            {
                "type": "send_message",
                "name": "停止执行",
                "send_message": "/stop"
            },
            {
                "type": "menu",
                "name": "快捷功能",
                "sub_menu_items": [
                    {
                        "type": "send_message",
                        "name": "使用帮助",
                        "send_message": "/help"
                    },
                    {
                        "type": "send_message",
                        "name": "运行状态",
                        "send_message": "/status"
                    },
                    {
                        "type": "send_message",
                        "name": "历史会话",
                        "send_message": "/history"
                    },
                    {
                        "type": "send_message",
                        "name": "工作区变动",
                        "send_message": "git status"
                    },
                    {
                        "type": "link",
                        "name": "项目主页",
                        "link": "https://github.com/lzx722/AGY-QQ-Bridge"
                    }
                ]
            }
        ]
    }
}

DEFAULT_C2C_PANEL: Dict[str, Any] = {
    "scope": "c2c",
    "target_type": "all",
    "panel": {
        "remark": "AGY-QQ-Bridge C2C 指令面板",
        "items": [
            {
                "type": "command",
                "name": "/new",
                "desc": "清空上下文并新建会话"
            },
            {
                "type": "command",
                "name": "/stop",
                "desc": "发送中断信号终止任务"
            },
            {
                "type": "command",
                "name": "/history",
                "desc": "查看最近历史会话列表"
            },
            {
                "type": "command",
                "name": "/resume",
                "desc": "恢复并继续历史会话"
            },
            {
                "type": "command",
                "name": "/rename",
                "desc": "重命名当前会话主题"
            },
            {
                "type": "command",
                "name": "/help",
                "desc": "查看快捷指令与帮助"
            },
            {
                "type": "command",
                "name": "/status",
                "desc": "查看桥接器运行状态"
            },
            {
                "type": "command",
                "name": "git status",
                "desc": "查看工作区代码变动"
            }
        ]
    }
}

DEFAULT_GROUP_PANEL: Dict[str, Any] = {
    "scope": "group",
    "target_type": "all",
    "panel": {
        "remark": "AGY-QQ-Bridge 群聊指令面板",
        "items": [
            {
                "type": "command",
                "name": "/new",
                "desc": "新建会话(管理员)"
            },
            {
                "type": "command",
                "name": "/stop",
                "desc": "终止任务(管理员)"
            },
            {
                "type": "command",
                "name": "/history",
                "desc": "历史会话(管理员)"
            },
            {
                "type": "command",
                "name": "/resume",
                "desc": "恢复会话(管理员)"
            },
            {
                "type": "command",
                "name": "/rename",
                "desc": "重命名主题(管理员)"
            },
            {
                "type": "command",
                "name": "/help",
                "desc": "查看机器人指令帮助"
            },
            {
                "type": "command",
                "name": "/status",
                "desc": "查看机器人运行状态"
            }
        ]
    }
}


# ================= 异步 OpenAPI 核心操作 =================

def _make_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"QQBot {token}",
        "Content-Type": "application/json",
        "User-Agent": "AGY-QQ-Bridge/2.2",
    }


async def get_menu(
    client: httpx.AsyncClient,
    token: str,
    api_base: str = API_BASE,
) -> Dict[str, Any]:
    """查询全局自定义菜单"""
    resp = await client.get(
        f"{api_base}/v2/menu",
        headers=_make_headers(token),
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"查询菜单失败 [{resp.status_code}]: {resp.text}")
    return resp.json()


async def set_menu(
    client: httpx.AsyncClient,
    token: str,
    menu_payload: Dict[str, Any],
    api_base: str = API_BASE,
) -> Dict[str, Any]:
    """设置或更新全局自定义菜单"""
    resp = await client.put(
        f"{api_base}/v2/menu",
        headers=_make_headers(token),
        json=menu_payload,
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"设置菜单失败 [{resp.status_code}]: {resp.text}")
    return resp.json()


async def clear_menu(
    client: httpx.AsyncClient,
    token: str,
    api_base: str = API_BASE,
) -> Dict[str, Any]:
    """清空全局自定义菜单"""
    return await set_menu(client, token, {"menu": {"items": []}}, api_base)


async def list_panels(
    client: httpx.AsyncClient,
    token: str,
    scope: str = "c2c",
    limit: int = 50,
    api_base: str = API_BASE,
) -> Dict[str, Any]:
    """查询指定作用域的指令面板列表 (scope: c2c, group, channel, dm)"""
    resp = await client.get(
        f"{api_base}/v2/panels",
        headers=_make_headers(token),
        params={"scope": scope, "limit": limit},
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"查询面板列表失败 [{resp.status_code}]: {resp.text}")
    return resp.json()


async def create_panel(
    client: httpx.AsyncClient,
    token: str,
    panel_payload: Dict[str, Any],
    api_base: str = API_BASE,
) -> Dict[str, Any]:
    """创建指令面板"""
    resp = await client.post(
        f"{api_base}/v2/panels",
        headers=_make_headers(token),
        json=panel_payload,
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"创建面板失败 [{resp.status_code}]: {resp.text}")
    return resp.json()


async def update_panel(
    client: httpx.AsyncClient,
    token: str,
    panel_id: str,
    panel_data: Dict[str, Any],
    api_base: str = API_BASE,
) -> Dict[str, Any]:
    """更新指定指令面板配置"""
    body = {"panel": panel_data.get("panel", panel_data)}
    resp = await client.put(
        f"{api_base}/v2/panels/{panel_id}",
        headers=_make_headers(token),
        json=body,
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"更新面板失败 [{resp.status_code}]: {resp.text}")
    return resp.json()


async def delete_panel(
    client: httpx.AsyncClient,
    token: str,
    panel_id: str,
    api_base: str = API_BASE,
) -> bool:
    """删除指定指令面板"""
    resp = await client.delete(
        f"{api_base}/v2/panels/{panel_id}",
        headers=_make_headers(token),
        timeout=15.0,
    )
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"删除面板失败 [{resp.status_code}]: {resp.text}")
    return True


async def ensure_panel(
    client: httpx.AsyncClient,
    token: str,
    panel_payload: Dict[str, Any],
    api_base: str = API_BASE,
) -> Dict[str, Any]:
    """
    幂等创建或更新指令面板：
    通过 remark 查找是否已存在同类面板，若存在则就地更新，避免消耗 20 个配额上限。
    """
    scope = panel_payload.get("scope", "c2c")
    target_remark = panel_payload.get("panel", {}).get("remark", "")

    existing = await list_panels(client, token, scope=scope, limit=50, api_base=api_base)
    records = existing.get("records") or []
    for rec in records:
        p_remark = rec.get("panel", {}).get("remark", "")
        pid = rec.get("panel_id")
        if pid and (p_remark == target_remark or p_remark.startswith("AGY-QQ-Bridge")):
            res = await update_panel(client, token, pid, panel_payload, api_base=api_base)
            return {"panel_id": pid, "action": "updated", "version": res.get("version")}

    res = await create_panel(client, token, panel_payload, api_base=api_base)
    return {"panel_id": res.get("panel_id"), "action": "created"}


async def sync_defaults(
    client: httpx.AsyncClient,
    token: str,
    api_base: str = API_BASE,
) -> Dict[str, Any]:
    """一键初始化或同步默认自定义菜单与 C2C/Group 指令面板"""
    results: Dict[str, Any] = {}
    results["menu"] = await set_menu(client, token, DEFAULT_CUSTOM_MENU, api_base=api_base)
    results["panel_c2c"] = await ensure_panel(client, token, DEFAULT_C2C_PANEL, api_base=api_base)
    try:
        results["panel_group"] = await ensure_panel(client, token, DEFAULT_GROUP_PANEL, api_base=api_base)
    except Exception as e:
        results["panel_group"] = {"error": str(e)}
    return results


# ================= 命令行调试入口 (可选排障使用) =================

def cli() -> int:
    parser = argparse.ArgumentParser(description="QQ 机器人自定义菜单与指令面板管理 (模块化 CLI)")
    parser.add_argument("--sync-all", action="store_true", help="一键同步/更新所有默认配置（自定义菜单 + C2C面板 + 群聊面板）")
    parser.add_argument("--menu-get", action="store_true", help="查询当前全局自定义菜单")
    parser.add_argument("--menu-clear", action="store_true", help="清空全局自定义菜单")
    parser.add_argument("--panel-list", type=str, nargs="?", const="c2c", choices=["c2c", "group", "channel", "dm"], help="查询指定场景的指令面板列表 (默认 c2c)")
    parser.add_argument("--panel-delete", type=str, help="删除指定 panel_id 的指令面板")

    args = parser.parse_args()

    if len(sys.argv) == 1:
        parser.print_help()
        return 0

    app_id = os.environ.get("APP_ID", "")
    client_secret = os.environ.get("CLIENT_SECRET", "")
    if not app_id or not client_secret:
        print("❌ 错误：环境变量中未找到 APP_ID 或 CLIENT_SECRET，请先在 .env 中配置。")
        return 1

    async def _run():
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(TOKEN_URL, json={"appId": app_id, "clientSecret": client_secret})
            resp.raise_for_status()
            token = resp.json().get("access_token")
            if not token:
                print("❌ 获取 access_token 失败")
                return 1

            if args.sync_all:
                print("🔄 正在自动同步自定义菜单与指令面板...")
                res = await sync_defaults(client, token)
                print(f"✅ 自定义菜单同步完成 (版本: {res.get('menu', {}).get('version')})")
                print(f"✅ C2C 指令面板: {res.get('panel_c2c')}")
                print(f"✅ 群聊指令面板: {res.get('panel_group')}")

            if args.menu_get:
                menu_res = await get_menu(client, token)
                print(json.dumps(menu_res, ensure_ascii=False, indent=2))

            if args.menu_clear:
                clear_res = await clear_menu(client, token)
                print(f"✅ 已清空全局自定义菜单，版本号: {clear_res.get('version')}")

            if args.panel_list:
                plist = await list_panels(client, token, scope=args.panel_list)
                print(json.dumps(plist, ensure_ascii=False, indent=2))

            if args.panel_delete:
                await delete_panel(client, token, args.panel_delete)
                print(f"✅ 指令面板 {args.panel_delete} 删除成功！")

            return 0

    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(cli())
