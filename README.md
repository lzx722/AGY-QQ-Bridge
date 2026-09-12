# AGY-QQ-Bridge: 极简 C2C 异步日志增量 QQ 桥接器

[![Repo](https://img.shields.io/badge/GitHub-lzx722%2FAGY--QQ--Bridge-blue?logo=github)](https://github.com/lzx722/AGY-QQ-Bridge)
[![Original Author](https://img.shields.io/badge/致敬原作者-zz327455573-orange)](https://github.com/zz327455573/AGY-QQ-Bridge)
[![Agent Keep](https://img.shields.io/badge/🏠_Agent_Keep-生态项目-blue)](https://github.com/zz327455573/agent-keep)

AGY-QQ-Bridge 是一个用于将本地常驻运行的 Google Antigravity (AGY) 实例直连到 QQ 个人私聊通道的轻量级桥接系统。

通过采用“去状态机、完全解耦收发、纯异步日志增量监听”的现代化设计，本项目彻底解决了一问一答式同步卡死、时序对不上、以及长耗时任务多次分段回复丢失的痛点。

---

## 🌟 核心架构与优势

```mermaid
flowchart TD
    subgraph QQ_Platform [QQ 官方云平台]
        QQUser([用户手机/桌面 QQ]) <-->|C2C 私聊 / 群聊| QQGateway[QQ 官方 WS 网关]
    end

    subgraph Bridge [AGY-QQ-Bridge 桥接服务]
        WSClient[aiohttp WebSocket 客户端]
        AgyMgr[进程/终端管理器 AgyProcessManager]
        LogListener[异步日志增量监听协程 log_listener]
    end

    subgraph AGY_CLI [Google Antigravity CLI]
        PTY[虚拟终端 ConPTY / tmux]
        AGYCore[AGY CLI 核心引擎]
        BrainLog[(transcript.jsonl 脑部结构化日志)]
    end

    QQGateway <-->|事件推送/心跳| WSClient
    WSClient -->|1. 写入消息与控制信号| AgyMgr
    AgyMgr -->|物理模拟按键 + 回车| PTY
    PTY <--> AGYCore
    AGYCore -->|2. 流式追加记录| BrainLog
    BrainLog -.->|3. 增量寻址扫描读取| LogListener
    LogListener -->|4. REST API 异步推送回复| QQGateway
```

*   **输入输出完全解耦**：QQ 消息只管模拟按键送入终端，后台协程只管增量监听日志并广播回传。无忙碌锁、无同步超时死锁。
*   **支持“一次输入，多次回复”**：完美契合 AI 助手在执行长耗时任务时分步骤、断断续续地汇报进度的行为特征，保证所有发出的文字 100% 被捕获回传。
*   **跨平台原生虚拟终端**：Linux 原生适配 `tmux`；Windows 原生适配微软伪控制台 `ConPTY`，自动完成 `\x1b[c` 握手探测应答并具备进程保活自愈与单实例端口锁防护。
*   **自适应重置与热绑定**：在执行 `/new` 切换或重置会话时，监听器会在 0.5 秒内自动绑定新生成的日志文件，并自动定位水位线，杜绝任何历史消息的重复刷屏与多实例逻辑混乱。

---

## 🛠️ 安装与运行指南

### 步骤 1：克隆项目仓库

```bash
git clone https://github.com/lzx722/AGY-QQ-Bridge.git
cd AGY-QQ-Bridge
```

### 步骤 2：安装依赖环境（推荐 Python 3.10+）

本项目采用现代 Python `pyproject.toml` 标准打包规范，内置全平台自适应依赖声明（在 Windows 环境下会自动额外安装 `pywinpty`）：

```bash
# 方式 A（推荐：以可编辑开发者模式安装，便于修改与同步）：
pip install -e .

# 方式 B（标准本地安装）：
pip install .
```

> **系统前置条件说明**：
> * **Linux 用户**：需要系统中已安装 `tmux`（`apt install tmux` 或 `yum install tmux`）。
> * **Windows 用户**：无需额外系统工具，已通过 ConPTY 原生支持伪终端。详见 [docs/WINDOWS.md](docs/WINDOWS.md)。

---

### 步骤 3：配置环境变量 (`.env`)

复制环境变量配置模板，并在根目录下创建 `.env`：

```bash
# Linux / macOS
cp .env.example .env

# Windows CMD / PowerShell
copy .env.example .env
```

打开 `.env` 并填入必要凭据：

```env
# 核心凭证（必须）
APP_ID=你的QQ机器人AppID
CLIENT_SECRET=你的QQ机器人Secret
MASTER_OPENID=你的管理员OpenID（留空时首次私聊会自动绑定）

# 启动与路径配置
AGY_START_CMD=C:\Users\Administrator\AppData\Local\agy\bin\agy.exe --dangerously-skip-permissions
AGY_WORKSPACE=C:\Users\Administrator\.agy-qq-bridge\workspace   # 机器人独立工作区（推荐设置）
BRAIN_DIR=C:\Users\Administrator\.gemini\antigravity-cli\brain
LOG_DIR=C:\Users\Administrator\.agy-qq-bridge

# Linux 环境专用
TMUX_SESSION=0                                                 # tmux 会话名称（默认 0）
```

---

### 步骤 4：启动机器人

#### 🪟 Windows 用户：
* **快捷一键启动**：直接双击项目根目录下的 **`A启动机器人.bat`**。
* **命令行启动**：
  ```powershell
  python -m agy_qq_bridge
  ```
  *(注：已内置 28712 端口单实例互斥锁，避免误双击造成多开冲突)*

#### 🐧 Linux 用户：
* **命令行直接运行**：
  ```bash
  python3 -m agy_qq_bridge
  # 或使用安装后的控制台命令
  agy-qq-bridge
  ```
* **使用 PM2 后台常驻保活（推荐）**：
  ```bash
  pm2 start "python3 -m agy_qq_bridge" --name agy-qq-bridge
  pm2 save
  
  # 查看运行状态与实时日志
  pm2 status
  pm2 logs agy-qq-bridge
  ```

---

## 💬 交互指令介绍

在 QQ 个人私聊或群聊中，您可以向您的机器人发送以下控制指令：

| 指令 | 作用 | 内部实现逻辑 |
| :--- | :--- | :--- |
| **`/help`** | 使用帮助 | 返回机器人的快捷指令手册、使用说明及操作指引 |
| **`/status`** | 运行状态 | 快速查看当前工作区路径、当前活跃会话主题、终端及桥接器存活状态 |
| **`/history`** | 历史会话 | 零 Token 扫描历史会话列表，展示项目名、活跃时间与会话主题并生成连续序号（支持范围如 `/history 31-40`、页码如 `/history p4`、数量如 `/history 10`） |
| **`/resume`** | 恢复会话 | 输入编号（如 `/resume 1`）切换会话，限 `/history` 后 90 秒内使用且最多跳转 3 次，自动热同步工作区目录 |
| **`/rename`** | 重命名会话 | 输入 `/rename <新主题>` 实时修改当前已绑定会话主题（全面防误当 resume/history 参数校验），在持久层中同步更新 |
| **`/new`** | 重置会话 | 强杀旧会话 ➔ 自动拉起全新无上下文 AGY 进程 ➔ 0.5s 内自动热绑定全新日志 |
| **`/stop`** | 强行终止 | 处于忙碌时发送 `Ctrl+C` 中断长耗时任务；处于空闲时智能发送 `Escape` 防误退 |
| **`git status`** | 工作区诊断 | 毫秒级本地执行并返回当前工作区 Git 变动状态，零 Token 消耗 |
| **任意文字** | 交互输入 | 发送 Escape 清理终端 TUI 状态 ➔ 物理模拟键入 ➔ 回车发送给 AGY |
| **图片/文件** | 多模态解析 | 原生提取 QQ 临时直链，以 `[附件(name): url]` 形式交由 AGY 多模态解析 |

---

## 📱 自定义菜单与指令面板配置

机器人支持两种便捷交互界面（基于 QQ 开放平台官方 OpenAPI）：
1. **自定义菜单 (Custom Menu)**：位于手机/桌面 QQ **单聊窗口底部常驻菜单栏**，支持点击直接填入指令（如 `/new`、`/stop`）或展开二级折叠菜单。
2. **指令面板 (Command Panel)**：位于**输入框快捷指令面板**（输入 `/` 或点击面板呼出），支持单聊（C2C）与群聊（Group），展示指令名称与功能介绍。

### 自动化注册与管理 (`menu_panel`)

* **全自动零干预注册**：桥接服务在启动时由后台原生异步协程（`menu_panel.py`）自动向 QQ 开放平台注册/同步单聊自定义菜单与 C2C/群聊指令面板，**完全无需任何手动配置**。
* **手动排障/查询（可选）**：
  若需手动检查或重置开放平台菜单，可执行排障命令：
  ```bash
  # 1. 查询当前生效的全局自定义菜单
  python -m agy_qq_bridge.menu_panel --menu-get

  # 2. 一键强制同步所有默认菜单与指令面板
  python -m agy_qq_bridge.menu_panel --sync-all

  # 3. 清空全局自定义菜单
  python -m agy_qq_bridge.menu_panel --menu-clear

  # 4. 查看指令面板列表 (c2c / group)
  python -m agy_qq_bridge.menu_panel --panel-list c2c
  ```

---

## 📝 更新日志

### v2.3.0 (2026-09-12)
*   **扁平化与极简工程重构 (Flat Layout)**：核心代码包直接提至项目根目录 `agy_qq_bridge/`，彻底消除 `src/` 冗余嵌套，原生运行与模块导入更加直接清爽。
*   **迁移至现代标准 `hatchling` 构建后端**：全面遵循 PEP 621 / PEP 660 规范，从根源上彻底绝杀任何 `*.egg-info` 临时构建目录污染，源码目录保持 100% 纤尘不染。
*   **依赖声明合并与单配置中心**：淘汰冗余的 `requirements.txt`，由 `pyproject.toml` 统一管理依赖与 Windows 平台自适应标记（`pywinpty`）。
*   **外围套件收敛与文档统一**：内置 TCP 28712 端口单实例防双开锁，将多源监控工具内聚为 `monitor.py`，并将 Windows 原生指南与演进实证库规整至 `docs/`，形成标准的 3 目录极简结构。

### v2.2.0 (2026-09-12)
*   **核心架构分层解耦重构**：将原先单体脚本重构为模块化架构（`config` 配置中心、`terminal` 终端抽象层、`session_manager` 会话生命周期、`qq_client` 通信客户端、`log_listener` 日志监听、`bridge` 调度中心），大幅消除冗余代码。
*   **跨平台虚拟终端抽象**：Linux (`TmuxTerminalManager`) 与 Windows (`WinptyTerminalManager`) 统一接口，自动应答 `\x1b[c` 握手并具备进程自愈能力。
*   **会话历史与切换增强 (`/history`, `/resume`)**：
    *   原生 100% 对齐 AGY 原生规则，过滤空会话；
    *   灵活支持范围模式（如 `/history 31-40`）、分页模式（如 `/history p4`）与单次 15 条安全上限；
    *   限制 `/resume` 在 `/history` 查询后 90 秒内使用且单轮最多跳转 3 次，防止误操作；
    *   移动端视觉排版优化（第一行会话名称、第二行工作区与时间，隐藏内部 UUID）。
*   **会话重命名三重持久化 (`/rename`)**：
    *   实现 `annotations/<cid>.pbtxt` + `conversation_metadata.json` + SQLite `conversation_summaries.db` 同步更新，杜绝重启被 AGY 覆盖；
    *   全指令参数防御机制，严防误将 resume/history 指令或参数当做主题写入。
*   **零 Token 诊断与智能打断**：`git status` 本地秒级直接执行；`/stop` 空闲状态发 `Escape` 防误退、忙碌状态单次 `Ctrl+C` 安全中断。
*   **QQ 开放平台自定义菜单与指令面板生态集成**：启动时通过异步协程自动向 QQ 网关注册同步单聊菜单与指令面板。

---

## 🙏 致谢 (Credits & Acknowledgments)

本项目基于 [zz327455573/AGY-QQ-Bridge](https://github.com/zz327455573/AGY-QQ-Bridge) 与 [Agent Keep](https://github.com/zz327455573/agent-keep) 生态项目进行深度重构与架构升级。

由衷感谢原作者 **[zz327455573](https://github.com/zz327455573)** 及开源社区贡献者的最初探索与卓越灵感，奠定了通过增量日志监听与虚拟终端解耦操控 Antigravity CLI 的开创性思路！