# AGY-QQ-Bridge-Windows: 极简 Windows 原生异步 QQ 桥接器

本项目是 Google Antigravity CLI (`agy`) 在 Windows 原生环境下的 QQ 机器人通道桥接程序。

---

## 🌟 核心特性与架构

由于 Windows 系统不支持原生的 `tmux`，本项目采用 Windows 伪控制台 **ConPTY**（基于 `pywinpty`）与核心模块化架构：

*   **模块化分层**：基于核心 `src/agy_qq_bridge` 分层架构，Windows 入口文件 `windows/agy_qq_bridge_win.py` 仅保留终端适配与启动编排，核心调度、QQ OpenAPI、日志监听与会话管理全局复用。
*   **ConPTY 进程常驻保活**：在后台拉起常驻 ConPTY 虚拟终端，保持 `agy` 进程处于持续交互就绪状态。
*   **终端握手自愈 (`\x1b[c`)**：后台排空协程自动识别并应答 AGY 启动时的设备能力探测请求（`\x1b[c` $\rightarrow$ `\x1b[?1;2c`），杜绝启动挂起。
*   **按键流物理模拟**：通过 Windows 虚拟终端句柄直接模拟物理键盘输入（`\r\n`），支持 Escape 退出 TUI / PAGER 卡死状态。
*   **解耦增量日志读取**：输出端通过增量文件偏移（seek）读取 `transcript.jsonl`，实现输入与输出完全解耦，支持一次输入多次回复。
*   **单实例防双开锁**：内置本地 TCP 端口（28712）互斥锁，防止重复点击导致多实例冲突。

---

## 🛠️ 安装与部署指南

### 1. 安装 Windows 环境依赖

在 Windows 控制台（PowerShell 或 CMD）中执行以下命令：
```powershell
pip install pywinpty httpx aiohttp
```

### 2. 配置环境变量

在项目根目录或 `windows/` 目录下创建 `.env` 环境变量配置文件：
```env
APP_ID=你的QQ机器人AppID
CLIENT_SECRET=你的QQ机器人密钥
MASTER_OPENID=你的管理员OpenID（留空时首次私聊会自动绑定）
AGY_START_CMD=C:\Users\Administrator\AppData\Local\agy\bin\agy.exe --dangerously-skip-permissions
BRAIN_DIR=C:\Users\Administrator\.gemini\antigravity-cli\brain
LOG_DIR=C:\Users\Administrator\.agy-qq-bridge
```

### 3. 一键启动

*   **快捷启动**：直接双击项目根目录下的 **`启动机器人.bat`** 即可。
*   **命令行启动**：
    ```powershell
    cd windows
    python agy_qq_bridge_win.py
    ```
*   **开机后台自启（可选）**：
    推荐使用 `NSSM` 或 Windows 任务计划程序将 `启动机器人.bat` 或 `agy_qq_bridge_win.py` 包装为标准的系统后台服务。

---

## 💬 常用交互指令

在 QQ 私聊或群聊中直接发送以下指令即可操控本地 AI 助手：

| 指令 | 说明 |
| :--- | :--- |
| **`/help`** | 查看机器人控制中心快捷指令指引 |
| **`/status`** | 查看当前机器人工作区路径、活动会话主题与 ConPTY 存活状态 |
| **`/history`** | 检视历史会话列表（支持 `/history 10`、`/history 31-40`、`/history p4`） |
| **`/resume <编号>`** | 快速切换并恢复至指定会话（90 秒内有效，最多跳转 3 次） |
| **`/rename <新主题>`** | 重命名当前活动会话，三层持久化（注解文件、元数据缓存、SQLite）同步保存 |
| **`/new`** | 重置 ConPTY 会话，拉起全新无上下文会话 |
| **`/stop`** | 发送中断信号（空闲发 Escape 防误退，忙碌发单次 Ctrl+C 安全打断） |
| **`git status`** | 本地秒级检查当前工作区 Git 分支与变动状态（零 Token 消耗） |
| **任意文字/图片** | 原生透传至后台 Google Antigravity CLI 进行深度推理与多模态解析 |
