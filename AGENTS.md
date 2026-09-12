# AGENTS.md — AGY-QQ-Bridge 开发者与 AI 代理指南

欢迎来到 **AGY-QQ-Bridge** 代码库。本文件专为参与维护、重构或在此系统上扩展功能的 AI 代理与开发者编写，概述系统核心架构、运行机制、双平台实现差异及操作规范。

---

## 1. 项目定位与核心设计哲学

**AGY-QQ-Bridge** 是将本地运行的 **Google Antigravity CLI (`agy`)** 实例通过官方 QQ 开放平台 WebSocket 网关直连至 QQ 私聊（C2C）通道的轻量级桥接系统。

### 核心设计哲学：完全解耦与去状态机
传统的问答式机器人往往使用“等待响应”的同步阻塞式状态机，极易在长耗时任务、工具调用审批或网络抖动时导致死锁与超时。本项目采用以下架构：

```mermaid
flowchart TD
    subgraph QQ_Platform [QQ 官方云平台]
        QQUser([用户手机/桌面 QQ]) <-->|C2C 私聊| QQGateway[QQ 官方 WS 网关]
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
    BrainLog -.->|3. 增量扫描读取| LogListener
    LogListener -->|4. REST API 异步推送回复| QQGateway
```

1. **输入与输出完全解耦**：
   - 输入端仅负责模拟物理按键向 CLI 发送字符，立即返回。
   - 输出端通过后台常驻协程监听 AGY 的脑部日志文件（`transcript.jsonl`），以增量偏移（file seek）读取模型最终回复并推送回 QQ。
2. **支持“一次输入，多次回复”**：
   - AGY 在执行复杂长任务时分步输出的状态和回复，均能被增量监听协程逐条捕获回传。
3. **原生多模态透传**：
   - 收到 QQ 图片、文件、语音等附件时不执行本地下载缓存，直接提取临时直链以 `[附件(name): url]` 形式拼接入 Prompt，交由 AGY 原生多模态解析。
4. **本地零 Token 快速诊断拦截**：
   - 常见的系统状态巡检（如 `/status`、`git status`）与控制指令（`/help`、`/stop`）均在桥接层本地瞬间完成，耗时毫秒级且完全绕过大模型推理，节省 100% Token。
5. **QQ 开放平台自定义菜单与面板自动化集成**：
   - 原生对接 QQ 开放平台 OpenAPI，启动时自动注册/同步私聊底部自定义菜单与全局指令面板（Panel），支持富交互快捷点按。

---

## 2. 代码库目录结构

```text
AGY-QQ-Bridge/
├── AGENTS.md                  # 本文件：AI 代理与开发者指南
├── README.md                  # 项目主文档（面向 Linux / 通用用户）
├── pyproject.toml             # Python 项目元数据
├── requirements.txt           # 核心依赖清单
├── .env.example               # 环境变量配置模板
├── agy-qq-bridge.py           # 向后兼容启动入口（Linux / 通用）
├── agy-conversation-monitor.py# 本地与 QQ 双通道日志监控工具
├── 启动机器人.bat              # Windows 一键启动脚本
├── manage_menu_panel.py       # QQ 开放平台自定义菜单与指令面板管理工具
├── tests/                     # 自动化测试套件
│   └── test_refactor.py       # 模块重构与核心交互指令单元测试
├── src/
│   └── agy_qq_bridge/         # 核心模块化分层架构
│       ├── __init__.py
│       ├── __main__.py
│       ├── config.py          # 环境与配置中心（.env加载、网络常量、路径与DNS修复）
│       ├── diagnostics.py     # 零 Token 消耗的本地 Git 与工作区秒级诊断
│       ├── session_manager.py # 会话生命周期（历史检视、Triple Persistence重命名、90s/3次跳转）
│       ├── terminal/          # 虚拟终端抽象层
│       │   ├── __init__.py    # 自适应终端工厂函数 create_terminal_manager()
│       │   ├── base.py        # BaseTerminalManager 虚拟终端抽象基类
│       │   ├── tmux.py        # Linux TmuxTerminalManager 终端管理器
│       │   └── winpty.py      # Windows WinptyTerminalManager 终端管理器（ConPTY + \x1b[c握手）
│       ├── qq_client.py       # QQ OpenAPI 通信、REST 收发、菜单面板同步与 WS 事件监听
│       ├── log_listener.py    # transcript.jsonl 增量偏移寻址（seek）、截断自愈与模型回复广播
│       └── bridge.py          # 核心调度中心 BridgeApp
├── windows/
│   ├── README.md              # Windows 原生部署说明
│   └── agy_qq_bridge_win.py   # Windows 原生轻量启动入口（调用 WinptyTerminalManager）
├── docs/                      # 架构演化案例与研究报告
│   ├── CASE_STUDY.md          # 终端交互方案的演进历史
│   ├── EXECUTIVE_BRIEF.md     # 系统边界与架构摘要
│   └── GOOGLE_FEEDBACK.md     # 针对 CLI 观察能力的反馈建议
└── evidence/                  # 历史版本调试与设计证据链
```

---

## 3. 双平台运行时架构差异与模块分层

本项目采用清晰的分层解耦设计，平台差异完全隔离在终端适配层：

| 特性 / 组件 | Linux 运行时 ([terminal/tmux.py](file:///E:/Git/AGY-QQ-Bridge/src/agy_qq_bridge/terminal/tmux.py)) | Windows 运行时 ([terminal/winpty.py](file:///E:/Git/AGY-QQ-Bridge/src/agy_qq_bridge/terminal/winpty.py)) |
| :--- | :--- | :--- |
| **终端载体** | `tmux` Session（默认名称 `0`） | Windows 伪控制台 ConPTY (`pywinpty.PtyProcess`) |
| **消息发送方式** | `tmux send-keys -t 0 message Enter` | `PtyProcess.write(message + "\r\n")` |
| **启动挂起处理** | 原生 PTY，通常无需终端探测握手 | 自动应答终端能力探测 (`\x1b[c` $\rightarrow$ `\x1b[?1;2c`) |
| **异步网络解析** | 默认 asyncio resolver | 修复 Windows Proactor 事件循环：使用 `ThreadedResolver`（位于 `config.py`） |
| **会话隔离机制** | 单用户独立环境 | `AGY_WORKSPACE` 工作区隔离 + Prompt 指纹内容比对 |
| **服务保活方式** | `pm2` / `systemd` | Windows 任务计划程序 / `nssm` / 批处理 |
| **打断安全策略 (`/stop`)** | 结合 `is_busy` 状态：空闲发 `Escape`；忙碌发 `C-c` + `Escape` | 结合 `is_busy` 状态：空闲发 `\x1b` 防误退；忙碌发单次 `\x03` + `\x1b` 且带进程自愈守护 |
| **会话检视与切换 (`/history`, `/resume`)** | 智能路径解析 + 杀旧建新 tmux session + `cd <cwd>` + `--conversation <id>` + 跳过历史日志绑定 | 智能路径解析 + 杀旧建新 ConPTY 进程 + `cwd=<cwd>` + `--conversation <id>` + 跳过历史日志绑定 |
| **会话重命名持久化 (`/rename`)** | 三重持久化（`session_manager.py`：`annotations/<cid>.pbtxt` + `cache/conversation_metadata.json` + `conversation_summaries.db`） | 三重持久化（`session_manager.py`：`annotations/<cid>.pbtxt` + `cache/conversation_metadata.json` + `conversation_summaries.db`） |
| **菜单/面板自同步** | 启动时异步协程调用 `qq_client.sync_menu_and_panels()` | 启动时异步协程调用 `qq_client.sync_menu_and_panels()` |

---

## 4. 会话定位与动态绑定机制（Windows 特别关注）

在 Windows 原生开发环境下，用户常在 Antigravity IDE 中同时工作。为避免桥接器误抓 IDE 的聊天日志，[windows/agy_qq_bridge_win.py](file:///E:/Git/AGY-QQ-Bridge/windows/agy_qq_bridge_win.py) 实现了**三重会话判定机制**：

1. **工作区隔离 (`AGY_WORKSPACE`)**：
   - AGY CLI 内部根据运行工作区在 `~/.gemini/antigravity-cli/cache/last_conversations.json` 中记录当前会话。
   - 桥接器启动时默认使用 `USERPROFILE`，并在每次检测时优先检查该工作区对应的专属会话 ID，避免串入项目目录（如 `E:\Git\...`）的会话。
2. **首条消息指纹校验 (`transcript_has_prompt`)**：
   - 在用户发送 `/new` 重置会话后，新会话文件夹不会立即生成，直到发送第一条消息。
   - 桥接器会记录发出的 Prompt 文本（如“早上”），在新日志文件诞生时校验其前数行是否包含该 Prompt，确保 100% 绑定到本机器人所拉起的会话。
3. **冷启动与热切换区分 (`is_cold_start`)**：
   - **冷启动**：服务刚刚开启时，跳过现有文件的所有历史记录（`_last_log_size = stat().st_size`），防止历史回复刷屏。
   - **热切换 / 新会话**：在运行时通过 `/new` 或换会话拉起的新日志，强制从第 0 字节开始监听（`_last_log_size = 0`），确保第一条回复绝不漏发。

---

## 5. 配置参数表 (`.env`)

所有运行时配置均支持通过项目根目录或脚本同级目录下的 `.env` 文件覆盖：

```env
# 核心凭证（必须）
APP_ID=你的QQ机器人AppID
CLIENT_SECRET=你的QQ机器人Secret
MASTER_OPENID=你的管理员OpenID（留空时会自动绑定首次私聊你的用户）

# 启动与路径配置
AGY_START_CMD=C:\Users\Administrator\AppData\Local\agy\bin\agy.exe --dangerously-skip-permissions
BRAIN_DIR=C:\Users\Administrator\.gemini\antigravity-cli\brain
LOG_DIR=C:\Users\Administrator\.agy-qq-bridge

# 进阶会话控制（可选）
AGY_WORKSPACE=C:\Users\Administrator\.agy-qq-bridge\workspace   # 机器人独立工作区
TARGET_CONV_ID=                                                 # 固定锁死指定会话UUID
EXCLUDE_CONV_IDS=                                               # 排除的会话UUID（逗号分隔）
```

---

## 6. 指令处理协议与本地拦截机制

当用户在 QQ 私聊（C2C）或群聊中发送内容时，系统优先在桥接层进行本地意图拦截与权限校验，未匹配本地指令时才作为 Prompt 写入终端：

### 6.1 核心交互指令与安全防护

*   **`/new` / `/reset` / `/清空` / `/新对话`**：
    1. 终止当前正在运行的 AGY 终端进程/tmux 会话。
    2. 设置 `fresh_time = time.time()`，并将内部绑定的日志置空。
    3. 重置终端忙碌标记 `is_busy = False`。
    4. 拉起不带 `-c`（不续接）的全新常驻 AGY CLI 进程。
    5. 立即向 QQ 回复会话已重置，等待首条新指令唤起新会话日志。
    *(群聊场景下仅限 MASTER_OPENID 管理员触发)*

*   **`/stop` / `/停止` / `/kill` (智能终端安全防护机制)**：
    1. **状态感知 (`is_busy`)**：检查当前终端是否处于命令执行/流式输出中。
    2. **空闲状态防误退**：若终端处于空闲就绪状态（`is_busy == False`），**绝对禁止**发送连续 Ctrl+C（避免导致底层 CLI 进程被操作系统意外杀死），仅发送无害的 `\x1b`（Escape 键）清理残留交互，并友好提示“当前处于空闲就绪状态，无需中断”。
    3. **忙碌执行打断**：若确实正在生成或执行长耗时工具（`is_busy == True`），向终端物理写入单次 `\x03`（Ctrl+C）并延迟追加 `\x1b`，恢复命令行提示符。
    4. **进程保活自愈**：Windows ConPTY 环境下在打断后自动检查进程存活状态，若异常退出则立即拉起新进程守护。
    *(群聊场景下仅限 MASTER_OPENID 管理员触发)*

*   **`/git status` / `git status` / `/git diff` / `/git` (本地秒级诊断拦截)**：
    1. 桥接器通过异步子进程在当前机器人绑定的 `AGY_WORKSPACE` 目录下直接执行 `git status -s` 和 `git branch --show-current`。
    2. 耗时通常小于 20ms，格式化为 Git 状态代码块直接返回给 QQ 用户。
    3. **零 Token 消耗**：完全不提交给大模型，避免长耗时模型分析与上下文浪费，杜绝大模型产生 Git 状态幻觉。

*   **`/history` / `/历史` / `/sessions` (本地会话历史检视与 AGY 原生 100% 对齐)**：
    1. **原生对齐与空会话过滤**：与 AGY CLI 原生 `/resume` 机制 100% 保持一致，直接扫描 `conversations/` 目录与 `conversation_summaries.db`，严格过滤步骤数为 0 的初始空库（48KB），杜绝死会话与历史幻觉，毫秒级加载。
    2. 智能缩短工作区路径（如 `E:\Git\AGY-QQ-Bridge` 显示为 `AGY-QQ-Bridge`，主目录显示为 `~`）。
    3. **灵活范围与分页支持**：支持范围如 `/history 31-40`、`/history 31~40`、`/history 31 40`，页码如 `/history p4`、`/history page 4`，以及自定义数量如 `/history 10`。列表序号采用全局连续真实索引，降级时兜底扫描 `history.jsonl`。
    4. **单次分页安全上限 (`max_page_size = 15`)**：单次查询数量严格限制最多展示 15 条会话，彻底防止触发 QQ 单条消息 4000 字符限制或移动端刷屏卡顿。
    5. **移动端视觉排版优先与 UUID 脱敏**：条目第一行优先突出展示序号与会话主题（`**[{idx}]** 💬 {title}`），第二行展示所属工作区与最近活跃时间（`📁 {ws} | 🕒 {time}`），彻底剔除冗长且对用户无意义的内部会话 UUID。
    6. **严格参数语法校验与防静默降级**：严密校验输入参数（支持纯数字、`31-40` 范围、`p4`/`page 4` 页码），拦截 `0`、负数、参数超量及非法字符并即时返回语法指引；校验未通过前不重置 90 秒倒计时与跳转计数器，杜绝静默兜底回退导致的用户困惑。

*   **`/resume` / `/切换` / `/switch` (无缝会话切换与工作区热同步)**：
    1. **极简操作与单个纯数字序号严格校验**：仅接收单个正整数编号（支持 `1` 或 `#1`），全面拦截多参数（如 `/resume 1 2`）、非数字乱码；若误输入范围（如 `31-40`）或页码（如 `p2`），系统能智能识别意图并主动指引使用对应的 `/history` 指令。
    2. **90 秒时效与单轮 3 次跳转上限**：严格限制在发送 `/history` 后的 **90 秒窗口期内**执行，且单轮列表**最多允许跳转 3 次**。超过 90 秒或达到 3 次跳转上限后，强制要求重新发送 `/history` 刷新列表，杜绝过时会话状态切换。
    3. 自动检查该会话原始工作区路径，若存在则同步将后台终端工作目录（`cwd`）热切换至该项目路径，并在 QQ 中明确告知变动。
    4. 传参 `--conversation <conv_id>` 热重启 AGY CLI 进程。
    5. 增量日志监听立即绑定至对应 `transcript.jsonl`，并将偏移量 `_last_log_size` 置为文件尾部（`stat().st_size`），**严禁向 QQ 回灌倒映既往历史回复**。回复仅展示会话主题与工作区，不展示内部 UUID。
    *(群聊场景下仅限 MASTER_OPENID 管理员触发)*

*   **`/rename <新主题>` / `/重命名 <新主题>` (即时修改当前活动会话主题与持久化)**：
    1. 自动获取当前已绑定的活跃会话 ID。
    2. **严格参数防误输校验（全指令防御体系）**：全面拦截误将 `/resume` 编号（如 `1`、`#1`、`[1]`）、`/history` 范围/页码（如 `31-40`、`31 40`、`p4`、`page 4`、`10 p4`）、系统指令（如 `/status`、`/new`、`/stop`）或内部 UUID 作为主题写入的行为；若检测到此类参数，主动友好指引正确的指令用法（如指引 `/resume 1` 或 `/history 31-40`），杜绝因误把 `/rename` 当作 `/resume` 或 `/history` 而破坏会话名称。
    3. **三重持久层同步更新，彻底防止重启后被 AGY 覆盖还原**：
       - `~/.gemini/antigravity-cli/annotations/<cid>.pbtxt`：写入 AGY 官方原生持久化注解（`title:"<新主题>"`），AGY 进程启动与重载时以此文件为最顶层信任源。
       - `~/.gemini/antigravity-cli/cache/conversation_metadata.json`：同步更新元数据快照缓存中的 `Title`。
       - `~/.gemini/antigravity-cli/conversation_summaries.db`：同步更新 SQLite 数据库中的 `title` 字段。
    4. 实时同步更新内存缓存，确保后续执行 `/history` 立即显示新名称。
    5. 回复用户并明确提示：“会话主题已从「旧主题」改为「新主题」”。
    *(群聊场景下仅限 MASTER_OPENID 管理员触发)*

*   **`/help` / `/帮助`**：
    1. 立即拦截并返回机器人的控制中心指令帮助说明，不透传给大模型消耗 Token。

*   **`/status` / `/状态`**：
    1. 立即返回当前机器人的工作区路径、活跃会话主题、终端存活状态、当前进程活跃度与管理员标识。

*   **常规对话**：
    1. 标记 `is_busy = True`。
    2. 物理写入 `\x1b`（Escape 键），退出可能因分页器（PAGER）或菜单卡死的状态。
    3. 物理写入消息内容并追加回车 `\r\n`。
    4. 增量日志监听到完整的最终回复时，自动复位 `is_busy = False`。

---

### 6.2 QQ 开放平台自定义菜单与指令面板生态集成 ([manage_menu_panel.py](file:///E:/Git/AGY-QQ-Bridge/manage_menu_panel.py))

本项目集成了 QQ 开放平台官方的交互式入口能力，桥接服务启动时会通过后台协程 `_sync_menu_and_panels()` 自动部署与维护：

1. **C2C 底部自定义菜单 (`/v2/menu`)**：
   - 仅对私聊用户生效，常驻于手机 QQ 键盘底部。
   - 包含快捷状态查询、工作区变动诊断、新对话重置、项目主页跳转与指令帮助。
   - **平台规范约束**：二级菜单最多 5 个，二级菜单标题长度严格限制（≤ 14 字符 / 约 7 个中文字符），超长会导致 API 报错。
2. **全局指令面板 (`/v2/panels`)**：
   - 支持 C2C 私聊、群聊和频道聊天，在输入框快捷唤起面板选择指令。
   - **平台配额约束与幂等维护**：QQ 开放平台规定每个机器人最多创建 20 个面板。系统在同步时通过面板备注（remark）进行精准幂等查找与更新（`ensure_panel`），杜绝因重复启动导致面板堆积触顶（400 错误）。
   - **字段长度限制避坑**：面板中每个指令条目的描述（`desc`）字符长度**严格不能超过 15 个字符**，超长（≥16字符）时平台会抛出语义极易误导的错误：`{"message":"超出数量限制","code":30013,"err_code":40030013}`。

---

## 7. 给后续 AI 代理的开发与维护准则

在修改或扩展本代码库时，请严格遵守以下规则：

1. **严禁硬编码任何具体的会话 UUID**：
   - 绝不能将个人调试产生的临时 UUID 写进源码中。所有的对话匹配必须基于工作区、Prompt 指纹或环境变量过滤。
2. **严禁将敏感凭证纳入 Git 跟踪**：
   - 保持 `.gitignore` 的有效性，严禁将包含真实 `APP_ID`、`CLIENT_SECRET` 的 `.env` 提交到仓库。
3. **维护终端握手机制的健壮性**：
   - Windows 平台使用 ConPTY 时，AGY CLI 会发送 `\x1b[c` 探测设备能力，必须在后台数据排空协程中自动响应 `\x1b[?1;2c`，否则会导致进程启动阻塞挂起。
4. **日志偏移量计算须区分场景**：
   - 修改 `bind_log` 逻辑时，牢记区分“服务初次启动（忽略既往历史）”与“会话动态创建（必须从头读取第一条新回复）”。
5. **严禁在终端空闲时滥发 Ctrl+C (`\x03`)**：
   - Windows ConPTY 与部分 CLI 工具在空闲提示符下收到多次连续 `\x03` 会直接导致主进程退出。必须检查 `is_busy` 状态，仅在任务忙碌时打断；空闲状态仅发 `\x1b`。
6. **轻量诊断与系统控制必须优先走本地拦截**：
   - 对于查询状态、工作区 Git 变动、重置会话等操作，必须由桥接层本地异步处理后直接回复用户，严禁透传给大模型消耗昂贵的上下文 Token 与时间。
7. **QQ 菜单与指令面板的配额与字段规范**：
   - 指令面板（Panels）全局上限 20 个，修改面板逻辑必须使用备注（remark）做幂等比对，禁止无脑调用新增接口。
   - 指令面板中每项指令的描述 `desc` 严格 ≤ 15 字符（超长会触发 `40030013 超出数量限制`）。
   - 菜单二级菜单项名称不可超过 14 个字符（约 7 个中文字符）。
