#!/usr/bin/env python3
"""
manage_menu_panel.py — QQ 机器人自定义菜单与指令面板管理工具
支持：
  1. 全局自定义菜单（私聊底部常驻菜单栏）的查询、一键部署推荐配置、清空、自定义 JSON 导入。
  2. 指令面板（C2C / 群聊 快捷指令面板）的查询列表、创建推荐面板、删除、自定义 JSON 导入。
"""

import os
import sys
import json
import argparse
from pathlib import Path
from typing import Optional, Dict, Any
import httpx

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ================= 环境变量加载 =================
def load_env(env_path: str = ".env"):
    paths = [
        Path(env_path),
        Path(__file__).parent / env_path,
        Path(__file__).parent / "windows" / env_path,
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
                            k, v = line.split("=", 1)
                            os.environ[k.strip()] = v.strip().strip('"').strip("'")
                return p
            except Exception:
                pass
    return None

ENV_LOADED = load_env()

API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"

# ================= 默认推荐配置 =================
DEFAULT_CUSTOM_MENU = {
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

DEFAULT_C2C_PANEL = {
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
                "desc": "发送中断信号终止当前任务"
            },
            {
                "type": "command",
                "name": "/help",
                "desc": "查看常用快捷指令与帮助"
            },
            {
                "type": "command",
                "name": "/status",
                "desc": "查看桥接器与终端运行状态"
            },
            {
                "type": "command",
                "name": "git status",
                "desc": "查看当前 Git 代码工作区状态"
            }
        ]
    }
}

DEFAULT_GROUP_PANEL = {
    "scope": "group",
    "target_type": "all",
    "panel": {
        "remark": "AGY-QQ-Bridge 群聊指令面板",
        "items": [
            {
                "type": "command",
                "name": "/new",
                "desc": "清空上下文并新建会话 (管理员)"
            },
            {
                "type": "command",
                "name": "/stop",
                "desc": "发送中断信号终止任务 (管理员)"
            },
            {
                "type": "command",
                "name": "/help",
                "desc": "查看机器人指令帮助说明"
            },
            {
                "type": "command",
                "name": "/status",
                "desc": "查看当前机器人运行状态"
            }
        ]
    }
}


class QQMenuPanelManager:
    def __init__(self, app_id: Optional[str] = None, client_secret: Optional[str] = None):
        self.app_id = app_id or os.environ.get("APP_ID", "")
        self.client_secret = client_secret or os.environ.get("CLIENT_SECRET", "")
        if not self.app_id or not self.client_secret:
            raise ValueError(
                "APP_ID 或 CLIENT_SECRET 未配置！请在 .env 文件中设置，或在实例化时传入。"
            )
        self.client = httpx.Client(timeout=15.0)
        self._token: Optional[str] = None

    def get_token(self) -> str:
        if self._token:
            return self._token
        resp = self.client.post(
            TOKEN_URL,
            json={"appId": self.app_id, "clientSecret": self.client_secret}
        )
        if resp.status_code != 200:
            raise RuntimeError(f"获取 Access Token 失败 [{resp.status_code}]: {resp.text}")
        data = resp.json()
        self._token = data.get("access_token")
        if not self._token:
            raise RuntimeError(f"返回数据未包含 access_token: {data}")
        return self._token

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"QQBot {self.get_token()}",
            "Content-Type": "application/json",
            "User-Agent": "AGY-QQ-Bridge-Manager/1.0"
        }

    # ================= 自定义菜单接口 (Menu) =================
    def get_menu(self) -> Dict[str, Any]:
        """查询全局自定义菜单"""
        url = f"{API_BASE}/v2/menu"
        resp = self.client.get(url, headers=self._headers())
        if resp.status_code != 200:
            raise RuntimeError(f"查询菜单失败 [{resp.status_code}]: {resp.text}")
        return resp.json()

    def set_menu(self, menu_payload: Dict[str, Any]) -> Dict[str, Any]:
        """设置/覆盖全局自定义菜单"""
        url = f"{API_BASE}/v2/menu"
        resp = self.client.put(url, headers=self._headers(), json=menu_payload)
        if resp.status_code != 200:
            raise RuntimeError(f"设置菜单失败 [{resp.status_code}]: {resp.text}")
        return resp.json()

    def clear_menu(self) -> Dict[str, Any]:
        """清空全局自定义菜单"""
        return self.set_menu({"menu": {"items": []}})

    # ================= 指令面板接口 (Panels) =================
    def list_panels(self, scope: str = "c2c", limit: int = 20) -> Dict[str, Any]:
        """查询指令面板列表 (scope: c2c, group, channel, dm)"""
        url = f"{API_BASE}/v2/panels"
        params = {"scope": scope, "limit": limit}
        resp = self.client.get(url, headers=self._headers(), params=params)
        if resp.status_code != 200:
            raise RuntimeError(f"查询面板列表失败 [{resp.status_code}]: {resp.text}")
        return resp.json()

    def create_panel(self, panel_payload: Dict[str, Any]) -> Dict[str, Any]:
        """创建指令面板"""
        url = f"{API_BASE}/v2/panels"
        resp = self.client.post(url, headers=self._headers(), json=panel_payload)
        if resp.status_code != 200:
            raise RuntimeError(f"创建面板失败 [{resp.status_code}]: {resp.text}")
        return resp.json()

    def get_panel(self, panel_id: str) -> Dict[str, Any]:
        """查询指定面板详情"""
        url = f"{API_BASE}/v2/panels/{panel_id}"
        resp = self.client.get(url, headers=self._headers())
        if resp.status_code != 200:
            raise RuntimeError(f"查询面板详情失败 [{resp.status_code}]: {resp.text}")
        return resp.json()

    def update_panel(self, panel_id: str, panel_data: Dict[str, Any]) -> Dict[str, Any]:
        """修改指定指令面板配置"""
        url = f"{API_BASE}/v2/panels/{panel_id}"
        body = {"panel": panel_data.get("panel", panel_data)}
        resp = self.client.put(url, headers=self._headers(), json=body)
        if resp.status_code != 200:
            raise RuntimeError(f"更新面板失败 [{resp.status_code}]: {resp.text}")
        return resp.json()

    def ensure_panel(self, panel_payload: Dict[str, Any]) -> Dict[str, Any]:
        """幂等创建或更新指令面板，避免重复创建超过 20 个限制"""
        scope = panel_payload.get("scope", "c2c")
        target_remark = panel_payload.get("panel", {}).get("remark", "")

        existing = self.list_panels(scope=scope, limit=50)
        records = existing.get("records") or []
        for rec in records:
            p_remark = rec.get("panel", {}).get("remark", "")
            pid = rec.get("panel_id")
            if pid and (p_remark == target_remark or p_remark.startswith("AGY-QQ-Bridge")):
                res = self.update_panel(pid, panel_payload)
                return {"panel_id": pid, "action": "updated", "version": res.get("version")}

        res = self.create_panel(panel_payload)
        return {"panel_id": res.get("panel_id"), "action": "created"}

    def ensure_all_defaults(self) -> Dict[str, Any]:
        """一键初始化或同步默认自定义菜单与 C2C/Group 指令面板"""
        results = {}
        results["menu"] = self.set_menu(DEFAULT_CUSTOM_MENU)
        results["panel_c2c"] = self.ensure_panel(DEFAULT_C2C_PANEL)
        try:
            results["panel_group"] = self.ensure_panel(DEFAULT_GROUP_PANEL)
        except Exception as e:
            results["panel_group"] = {"error": str(e)}
        return results

    def delete_panel(self, panel_id: str) -> bool:
        """删除指定指令面板"""
        url = f"{API_BASE}/v2/panels/{panel_id}"
        resp = self.client.delete(url, headers=self._headers())
        if resp.status_code not in (200, 204):
            raise RuntimeError(f"删除面板失败 [{resp.status_code}]: {resp.text}")
        return True


# ================= 格式化展示辅助函数 =================
def print_menu_display(menu_data: Dict[str, Any]):
    version = menu_data.get("version", "未知")
    menu = menu_data.get("menu") or {}
    items = menu.get("items") or []

    print(f"\n📋 [当前生效的全局自定义菜单] (版本: {version})")
    if not items:
        print("  当前未配置任何自定义菜单项（显示为空）。")
        return

    for idx, item in enumerate(items, 1):
        itype = item.get("type", "unknown")
        iname = item.get("name", "未命名")
        if itype == "send_message":
            print(f"  [{idx}] 🔘 按钮: {iname} → 填入指令: '{item.get('send_message')}'")
        elif itype == "link":
            print(f"  [{idx}] 🔗 链接: {iname} → 跳转 URL: {item.get('link')}")
        elif itype == "switch":
            print(f"  [{idx}] 🎚️ 开关: {iname} → switch_id={item.get('switch', {}).get('switch_id')}")
        elif itype == "menu":
            sub_items = item.get("sub_menu_items") or []
            print(f"  [{idx}] 📂 折叠二级菜单: {iname} (含 {len(sub_items)} 个子项):")
            for sidx, sub in enumerate(sub_items, 1):
                stype = sub.get("type")
                sname = sub.get("name")
                if stype == "send_message":
                    print(f"       ({sidx}) 🔘 {sname} → '{sub.get('send_message')}'")
                elif stype == "link":
                    print(f"       ({sidx}) 🔗 {sname} → {sub.get('link')}")
    print()


def print_panels_display(panels_data: Dict[str, Any], scope: str):
    records = panels_data.get("records") or []
    print(f"\n📑 [已配置的指令面板列表 - scope: {scope}] (共 {len(records)} 个)")
    if not records:
        print("  当前场景下暂无任何生效的指令面板。")
        return

    for idx, rec in enumerate(records, 1):
        pid = rec.get("panel_id")
        target_type = rec.get("target_type")
        panel = rec.get("panel") or {}
        remark = panel.get("remark", "无备注")
        items = panel.get("items") or []
        print(f"  [{idx}] 面板 ID: {pid} | 范围: {target_type} | 备注: {remark}")
        for pitem in items:
            ptype = pitem.get("type")
            pname = pitem.get("name")
            pdesc = pitem.get("desc", "")
            only_admin = " [管理员专属]" if pitem.get("only_admin") else ""
            if ptype == "command":
                print(f"       • 指令: {pname:<10} | 说明: {pdesc}{only_admin}")
            elif ptype == "link":
                print(f"       • 链接: {pname:<10} | URL: {pitem.get('link')}{only_admin}")
    print()


# ================= 交互式控制台 =================
def run_interactive(mgr: QQMenuPanelManager):
    while True:
        print("=" * 60)
        print("        QQ 机器人自定义菜单与指令面板管理")
        print("=" * 60)
        print("【自定义菜单 - C2C 单聊窗口底部常驻菜单栏】")
        print("  1. 查看当前全局自定义菜单")
        print("  2. 一键设置推荐自定义菜单（新对话 / 停止 / 快捷帮助）")
        print("  3. 清空全局自定义菜单")
        print("  4. 从自定义 JSON 文件导入设置菜单")
        print("-" * 60)
        print("【指令面板 - 聊天框快捷指令面板】")
        print("  5. 查看 C2C 单聊指令面板列表")
        print("  6. 查看群聊指令面板列表")
        print("  7. 一键创建推荐 C2C 指令面板")
        print("  8. 一键创建推荐群聊指令面板")
        print("  9. 删除指定指令面板 (需面板 ID)")
        print("  10. 从自定义 JSON 文件创建面板")
        print("-" * 60)
        print("  0. 退出")
        print("=" * 60)

        choice = input("请选择操作编号 [0-10]: ").strip()
        if choice == "0":
            print("👋 已退出。")
            break
        elif choice == "1":
            try:
                res = mgr.get_menu()
                print_menu_display(res)
            except Exception as e:
                print(f"❌ 获取失败: {e}")
        elif choice == "2":
            try:
                print("\n即将应用以下推荐菜单：")
                print(json.dumps(DEFAULT_CUSTOM_MENU, ensure_ascii=False, indent=2))
                confirm = input("确认设置？[y/N]: ").strip().lower()
                if confirm == "y":
                    res = mgr.set_menu(DEFAULT_CUSTOM_MENU)
                    print(f"✅ 设置成功！新版本号: {res.get('version')}")
            except Exception as e:
                print(f"❌ 设置失败: {e}")
        elif choice == "3":
            try:
                confirm = input("⚠️ 确定要清空全局自定义菜单吗？[y/N]: ").strip().lower()
                if confirm == "y":
                    res = mgr.clear_menu()
                    print(f"✅ 已清空菜单配置！新版本号: {res.get('version')}")
            except Exception as e:
                print(f"❌ 清空失败: {e}")
        elif choice == "4":
            path = input("请输入 JSON 文件路径: ").strip().strip('"').strip("'")
            if not Path(path).exists():
                print(f"❌ 文件不存在: {path}")
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                res = mgr.set_menu(data)
                print(f"✅ 导入并设置成功！新版本号: {res.get('version')}")
            except Exception as e:
                print(f"❌ 设置失败: {e}")
        elif choice == "5":
            try:
                res = mgr.list_panels(scope="c2c")
                print_panels_display(res, scope="c2c")
            except Exception as e:
                print(f"❌ 查询失败: {e}")
        elif choice == "6":
            try:
                res = mgr.list_panels(scope="group")
                print_panels_display(res, scope="group")
            except Exception as e:
                print(f"❌ 查询失败: {e}")
        elif choice == "7":
            try:
                print("\n即将创建以下 C2C 全局面版：")
                print(json.dumps(DEFAULT_C2C_PANEL, ensure_ascii=False, indent=2))
                confirm = input("确认创建？[y/N]: ").strip().lower()
                if confirm == "y":
                    res = mgr.create_panel(DEFAULT_C2C_PANEL)
                    print(f"✅ 面板创建成功！Panel ID: {res.get('panel_id')}")
            except Exception as e:
                print(f"❌ 创建失败: {e}")
        elif choice == "8":
            try:
                print("\n即将创建以下群聊全局面版：")
                print(json.dumps(DEFAULT_GROUP_PANEL, ensure_ascii=False, indent=2))
                confirm = input("确认创建？[y/N]: ").strip().lower()
                if confirm == "y":
                    res = mgr.create_panel(DEFAULT_GROUP_PANEL)
                    print(f"✅ 面板创建成功！Panel ID: {res.get('panel_id')}")
            except Exception as e:
                print(f"❌ 创建失败: {e}")
        elif choice == "9":
            pid = input("请输入要删除的 panel_id: ").strip()
            if not pid:
                print("❌ panel_id 不能为空")
                continue
            try:
                confirm = input(f"⚠️ 确认删除面板 {pid}？[y/N]: ").strip().lower()
                if confirm == "y":
                    mgr.delete_panel(pid)
                    print(f"✅ 面板 {pid} 已成功删除！")
            except Exception as e:
                print(f"❌ 删除失败: {e}")
        elif choice == "10":
            path = input("请输入 JSON 文件路径: ").strip().strip('"').strip("'")
            if not Path(path).exists():
                print(f"❌ 文件不存在: {path}")
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                res = mgr.create_panel(data)
                print(f"✅ 面板创建成功！Panel ID: {res.get('panel_id')}")
            except Exception as e:
                print(f"❌ 创建失败: {e}")
        else:
            print("输入无效，请重新选择。")
        input("\n按回车键继续...")


def main():
    parser = argparse.ArgumentParser(description="QQ 机器人自定义菜单与指令面板管理工具")
    parser.add_argument("--menu-get", action="store_true", help="查询当前全局自定义菜单")
    parser.add_argument("--menu-set-default", action="store_true", help="一键设置推荐的全局自定义菜单")
    parser.add_argument("--menu-clear", action="store_true", help="清空全局自定义菜单")
    parser.add_argument("--menu-set-json", type=str, help="从指定 JSON 文件设置自定义菜单")

    parser.add_argument("--panel-list", type=str, nargs="?", const="c2c", choices=["c2c", "group", "channel", "dm"], help="查询指定场景的指令面板列表 (默认 c2c)")
    parser.add_argument("--panel-create-default", type=str, choices=["c2c", "group"], help="一键创建推荐的指令面板 (可选 c2c 或 group)")
    parser.add_argument("--panel-create-json", type=str, help="从指定 JSON 文件创建指令面板")
    parser.add_argument("--panel-delete", type=str, help="删除指定 panel_id 的指令面板")
    parser.add_argument("--sync-all", action="store_true", help="一键同步/更新所有默认配置（自定义菜单 + C2C面板 + 群聊面板）")

    args = parser.parse_args()

    try:
        mgr = QQMenuPanelManager()
    except Exception as e:
        print(f"❌ 初始化失败: {e}")
        print("请检查 .env 是否存在且配置了 APP_ID 与 CLIENT_SECRET。")
        sys.exit(1)

    # 如果没有传递任何命令行参数，则进入交互式界面
    if len(sys.argv) == 1:
        run_interactive(mgr)
        return

    # 命令行操作逻辑
    if args.menu_get:
        res = mgr.get_menu()
        print_menu_display(res)

    if args.menu_set_default:
        res = mgr.set_menu(DEFAULT_CUSTOM_MENU)
        print(f"✅ 推荐自定义菜单设置成功！版本号: {res.get('version')}")

    if args.menu_clear:
        res = mgr.clear_menu()
        print(f"✅ 自定义菜单已清空！版本号: {res.get('version')}")

    if args.menu_set_json:
        with open(args.menu_set_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        res = mgr.set_menu(data)
        print(f"✅ 菜单 JSON 设置成功！版本号: {res.get('version')}")

    if args.panel_list:
        res = mgr.list_panels(scope=args.panel_list)
        print_panels_display(res, scope=args.panel_list)

    if args.panel_create_default:
        payload = DEFAULT_C2C_PANEL if args.panel_create_default == "c2c" else DEFAULT_GROUP_PANEL
        res = mgr.create_panel(payload)
        print(f"✅ 推荐指令面板 ({args.panel_create_default}) 创建成功！Panel ID: {res.get('panel_id')}")

    if args.panel_create_json:
        with open(args.panel_create_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        res = mgr.create_panel(data)
        print(f"✅ 指令面板创建成功！Panel ID: {res.get('panel_id')}")

    if args.panel_delete:
        mgr.delete_panel(args.panel_delete)
        print(f"✅ 指令面板 {args.panel_delete} 删除成功！")

    if args.sync_all:
        print("🔄 正在自动同步自定义菜单与指令面板...")
        res = mgr.ensure_all_defaults()
        print(f"✅ 全局自定义菜单同步完成 (版本: {res.get('menu', {}).get('version')})")
        print(f"✅ C2C 指令面板: {res.get('panel_c2c')}")
        print(f"✅ 群聊指令面板: {res.get('panel_group')}")


if __name__ == "__main__":
    main()
