# 吱一声（Chirp）🐹

一个面向钉钉未读消息的 macOS 桌面悬浮助手：帮你判断**哪些消息先处理、对方想表达什么，以及可以怎样回复**。

Chirp 通过 [DingTalk Workspace CLI（DWS）](https://github.com/open-dingtalk/dingtalk-workspace-cli) 读取未读会话，用本地 Laya 或云端 Jev 分析意图、情绪、紧急度、重要度和操纵话术风险，再通过悬浮卡片展示结果。支持接入兼容 OpenAI Chat Completions 的模型，生成详细解读和回复建议。

## 项目背景

工作消息往往混杂着任务请求、会议通知、信息分享和闲聊。未读数量只能告诉我们“还有多少没看”，难以回答“现在最该处理哪一条”。当消息带有截止时间、情绪压力，或与自己的日程冲突时，判断和回复还需要额外精力。

Chirp 将这一过程拆成三个步骤：

1. **筛选与排序**：从未读会话中提取最近消息，按紧急度、重要度、回复需求等信号排序。
2. **理解与建议**：展示意图、情绪、可能的操纵话术及回复选项；提供日程和待办上下文时，还能辅助判断时间冲突。
3. **回到钉钉处理**：复制建议、打开对应会话，由你完成沟通。

应用独立运行，不依赖特定 AI 宿主，也不需要部署后端服务。当前“一键回复”的行为是复制已有建议并打开钉钉，发送由用户在钉钉中完成。

## 功能概览

| 功能 | 当前能力 |
| --- | --- |
| 未读会话聚合 | 定时读取未读会话，默认最多 100 个，包含免打扰会话 |
| 消息分析 | 意图、情绪、紧急度、重要度、PUA / 操纵话术风险、回复可行性 |
| 双决策引擎 | 本地 Laya、云端 Jev，可在设置中切换；另有 Mock 演示引擎 |
| 深度解读 | 可选 LLM 生成解读和回复；未配置或调用失败时使用结构化模板 |
| 两阶段展示 | 先显示结构化分析和排序，再逐条补充 LLM 解读 |
| 处境感知 | 读取本地日程 / 待办上下文文件；当前未内置自动同步 |
| 处理记录 | 跳转或忽略后隐藏当前消息，同会话出现更新消息时可再次显示 |
| 桌面交互 | 置顶悬浮窗、卡片详情、复制回复、跳转钉钉、暂停刷新、收起窗口 |

## 技术选型

| 层次 | 选型 | 用途与考虑 |
| --- | --- | --- |
| 应用语言 | Python | 串联 CLI、模型调用和桌面界面，使用虚拟环境管理依赖 |
| 桌面界面 | PySide6 / Qt | 实现悬浮窗口、卡片、设置面板和剪贴板交互 |
| 钉钉接入 | DWS + `subprocess` + JSON | 复用官方 CLI 的登录态和数据能力 |
| 云端决策 | [TypeSafe Jev](https://typesafe.ai) | 经 HTTP 提交结构化判断题，不需要本地模型依赖 |
| 本地决策 | Laya + ModelScope | 默认使用 `convaiinnovations/laya-multilingual`；模型缓存后可本地推理 |
| 详细解读 | OpenAI 兼容 Chat Completions API | 接入用户选择的模型服务，生成自然语言解读和回复 |
| 后台任务 | `QThread` + `ThreadPoolExecutor` | 数据拉取和分析放到后台，消息分析默认最多 4 路并发 |
| 配置与状态 | `.env` + 本地 JSON | 保存模型配置、消息快照和隐藏记录，无需数据库 |
| 应用打包 | PyInstaller | 按目标 Python / 依赖架构生成 macOS `.app` |

### 如何选择决策引擎

| | 云端 Jev | 本地 Laya | Mock |
| --- | --- | --- | --- |
| 适用场景 | 希望轻量安装、使用云端分析 | 希望消息判断在本机完成 | 界面演示、开发验证 |
| 必要配置 | `TYPESAFE_API_KEY` | 安装本地模型依赖 | 无模型 Key |
| 安装依赖 | `requirements.txt` | 额外安装 `requirements-local.txt` | `requirements.txt` |
| 模型加载 | 调用云端 API | 首次下载，后续复用缓存 | 不调用真实决策模型 |
| 消息去向 | 发往 TypeSafe API | 决策推理在本机执行 | 使用模拟判断结果 |

决策引擎负责“判断与打分”，可选 LLM 负责“解释与写回复”。两者独立配置：即使选择本地 Laya，启用远程 LLM 后，消息和上下文仍会发送到该 LLM 服务。DWS 实时同步本身也需要联网。

## 架构设计

运行链路可以概括为：消息来源 → 统一分析 → 排序展示 → 详细解读 → 用户操作。

- `sources.py` 负责从 DWS、缓存或 Mock 数据读取消息。
- `analyzers.py` 负责构建判断题、计算优先级和回复建议；`engine.py` 提供 Jev、Laya、Mock 三种可替换引擎。
- `explainer.py` 在排序结果之后生成 LLM 解读，未配置 LLM 时使用本地模板。
- `main.py` 负责 Qt 窗口、后台刷新、卡片详情、跳转钉钉和隐藏记录。
- `context.py` 提供日程/待办上下文，`settings.py` 负责配置和运行时热重载。

每次刷新先完成结构化分析并显示列表，再异步补充详细解读。用户跳转或忽略消息后，程序把会话时间水位写入本地缓存，后续刷新只隐藏已经处理的旧消息。

模块职责、脚本分类和完整依赖关系见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

## 安装与配置

### 1. 准备环境

| 软件 | 要求 |
| --- | --- |
| 操作系统 | macOS，支持源码运行或按架构打包 |
| 钉钉桌面客户端 | 已安装并登录，用于接收会话跳转 |
| Python | 建议使用 3.11 或 3.12，并创建项目虚拟环境 |
| Node.js / npm | 建议使用仍受支持的 LTS 版本；DWS 1.0.62 的 npm 包声明 Node.js ≥ 16.7 |
| DWS | 官方 `dingtalk-workspace-cli`；下文参数按 v1.0.62 核对 |

可从 [Python 官网](https://www.python.org/downloads/)、[Node.js 官网](https://nodejs.org/)和[钉钉官网](https://www.dingtalk.com/)安装所需软件。打开终端检查：

```bash
python3 --version
node --version
npm --version
```

### 2. 安装 DWS

```bash
npm install -g dingtalk-workspace-cli
dws --version
command -v dws
```

保留 `command -v dws` 输出的路径，后面可以填入应用的“钉钉 CLI”设置。DWS 需要是能够在普通终端独立运行的官方 CLI。

### 3. 登录 DWS

在本机终端执行：

```bash
dws auth login
```

按终端和浏览器提示完成钉钉登录、选择组织及 OAuth 授权，等待命令返回。随后检查登录状态和当前账号：

```bash
dws auth status --format json
dws profile list --format json
```

若有多个账号，先确认本项目要使用哪一个。需要切换时，将下面的占位内容替换成 `profile list` 返回的实际 `profile`：

```bash
dws profile switch '<corpId:userId>'
```

Chirp 当前沿用 DWS 的默认账号，没有单独的组织选择配置。切换账号后重新核对 `RADAR_SELF_ID`，并清空旧的隐藏记录。

远程终端或无法接收本地浏览器回调时，可改用 `dws auth login --device`。钉钉国际版使用 `dws auth login --intl`，两种参数可以组合。

### 4. 授权并验证消息读取

OAuth 登录解决账号身份认证；读取消息时，还可能需要 DWS 的 **PAT 行为授权**。在启动桌面程序前，先在终端跑通它实际使用的读取命令：

```bash
dws chat +unread-chats --count 100 --format json
```

从结果中取一个实际会话的 `conversationId`，读取消息内容：

```bash
dws chat +chat-messages --group '<conversationId>' --order desc --no-reactions --format json
```

没有未读会话时，第一条命令返回空列表是正常情况。需要验证消息读取或获取自己的 `senderId` 时，可以先通过下面的命令找到一个近期参与过的会话：

```bash
dws chat +recent-conversations --format json
```

如果命令提示缺少行为权限，按返回的授权链接或 `hint` / `actions` 完成授权。需要手动授予 scope 时，用返回的**实际 scope**替换占位内容；以下命令先预览，再由你确认执行：

```bash
# 预览授权范围；不写入授权
dws pat chmod '<命令返回的实际scope>' --grant-type permanent --dry-run --format json

# 核对范围后执行；permanent 表示持续有效，适合后台轮询
dws pat chmod '<命令返回的实际scope>' --grant-type permanent --yes --format json
```

如果返回待完成的浏览器授权流程，按链接继续，完成后重新运行上面的消息读取命令验证。当前主流程需要读取会话和消息的权限；日程、待办是可选扩展，消息发送权限并非必需。

DWS 也支持 `dws auth login --recommend`，在登录后批量授予服务端推荐权限。推荐集合可能覆盖比本项目更多的功能，应先了解其范围再选择使用。组织策略导致的权限限制，应按错误提示联系管理员处理。

DWS 自行管理登录凭据，Chirp 直接调用已登录的 CLI，无需将 DWS token 或 AppSecret 填入项目配置。

### 5. 安装项目依赖

取得源码后，进入项目根目录。以下示例假设目录名为 `Chirp`：

```bash
cd Chirp
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

选择本地 Laya 时，额外安装：

```bash
python -m pip install -e '.[local]'
```

本地模式会安装 Laya、ModelScope 及其模型运行依赖，占用空间明显高于云端模式。首次分析会下载模型，优先使用 ModelScope，失败时尝试 Hugging Face；下载和加载耗时取决于网络与机器配置。

### 6. 配置必要参数

在项目根目录复制配置模板；如果已有 `.env`，直接编辑现有文件：

```bash
cp -n .env.example .env
```

用文本编辑器打开 `.env`，选择下面一种运行方式。

**云端 Jev：**

```dotenv
RADAR_SOURCE=dws
RADAR_ENGINE=jev
TYPESAFE_API_KEY=替换为你的TypeSafe_API_Key
RADAR_SELF_ID=替换为你自己的senderId
RADAR_POLL_INTERVAL=30
```

Jev Key 从 [TypeSafe](https://typesafe.ai) 获取。服务可用性和计费以提供方为准。

**本地 Laya：**

```dotenv
RADAR_SOURCE=dws
RADAR_ENGINE=laya
RADAR_SELF_ID=替换为你自己的senderId
RADAR_POLL_INTERVAL=30
```

本地 Laya 不需要 `TYPESAFE_API_KEY`，但需要完成上一步的本地依赖安装。

**如何获得 `RADAR_SELF_ID`：** 使用第 4 步的消息读取命令，在返回结果中找到一条自己发过的消息，复制该条记录的 `senderId`。它用于优先过滤自己的消息，不要填昵称或手机号，也不要未经核对直接使用其他接口的用户 ID。

**指定 DWS 路径：** 如果终端可运行 DWS，但应用找不到它，将 `command -v dws` 的实际输出填入配置，例如：

```dotenv
DWS_BIN=/实际安装目录/bin/dws
```

这里必须是单个可执行文件路径，不要填写 `npx dws` 或附加命令参数。从 Finder 启动 `.app` 时尤其建议显式配置路径。

**可选：启用 LLM 详细解读。** 三项必须同时有值：

```dotenv
RADAR_LLM_BASE_URL=https://your-provider.example/v1
RADAR_LLM_API_KEY=替换为你的LLM_API_Key
RADAR_LLM_MODEL=替换为服务商支持的模型名
```

上述地址是占位示例。填写服务商提供的 API 基础地址，程序会自动追加 `/chat/completions`，不要重复填写这一段。不配置 LLM 也能完成消息判断，并使用模板生成解读与回复。

### 7. 启动项目

在已激活的虚拟环境中运行：

```bash
python main.py
```

启动后点击标题栏 **⚙ 设置**，可以修改决策引擎、Jev Key、LLM 三项配置、DWS 路径、自己的 `senderId` 和刷新间隔。保存后配置写入 `.env`，应用重新加载并刷新。直接编辑 `.env` 后请重启应用。

正常使用时确认界面显示实时来源、预期的决策引擎，且状态栏没有拉取错误。没有未读消息时，列表为空是正常的。

### 只想先体验界面

安装核心依赖后即可使用模拟数据，无需先登录 DWS 或下载决策模型。下面同时关闭可选 LLM，避免已有配置触发模型请求：

```bash
RADAR_MOCK=true RADAR_LLM_BASE_URL= RADAR_LLM_API_KEY= RADAR_LLM_MODEL= python main.py
```

此模式强制使用 `MockSource` 和 `MockEngine`，展示结果仅用于演示。

## 使用指南

### 日常处理流程

1. **浏览列表**：按综合分从高到低查看会话卡片，快速了解紧急度、情绪、意图和风险提示。会话数与未读消息数分别展示。
2. **展开详情**：点击卡片，查看消息原文、结构化指标、详细解读和回复选项。若提示“详细解读生成中”，可先查看已完成的判断。
3. **选择回复**：点击回复选项复制内容；需要时编辑措辞，再到钉钉中粘贴发送。
4. **跳转或忽略**：点击“跳转钉钉”打开对应会话，或点击“忽略”隐藏当前消息。两者都会记录本地处理水位。
5. **恢复隐藏内容**：点击标题栏的垃圾桶图标清空隐藏标记，仍由当前数据源返回的消息会重新出现。

| 控件 | 行为 |
| --- | --- |
| 卡片 | 进入详细分析 |
| 回复选项 | 复制该选项到剪贴板 |
| 📂 跳转钉钉 | 打开会话，并在 Chirp 中隐藏当前消息 |
| 🤖 一键回复 | 有回复建议时复制第一条，再打开会话；建议尚未生成时应等待或手动复制 |
| 忽略 / 卡片上的 ✕ | 仅隐藏当前消息 |
| ⚙ | 打开设置 |
| ↻ | 手动刷新 |
| ⏸ | 暂停 / 恢复自动刷新 |
| 🗑 | 清空本地隐藏标记并刷新 |
| — | 收起 / 展开窗口 |
| 标题栏 ✕ | 退出应用 |

Chirp 的“隐藏”是本地处理记录，不调用钉钉的标记已读接口。打开会话后，钉钉客户端自身可能更新已读状态。

### 可选：提供日程与待办上下文

当前刷新流程从 `.cache/context.json` 读取上下文，**不会自动调用 DWS 同步日程和待办**。没有该文件也能运行，但程序并不了解你的真实安排。

需要处境感知时，可由外部脚本整理数据并写入此文件，或通过 `RADAR_CONTEXT` 指定文件绝对路径。格式如下，日期和内容请替换成实际安排：

```json
{
  "fetched_at": "2026-09-27T13:00:00+08:00",
  "today_events": [
    {
      "eventId": "example-event-1",
      "title": "项目评审",
      "start": "2026-09-27T14:00:00+08:00",
      "end": "2026-09-27T16:00:00+08:00"
    }
  ],
  "next_event": null,
  "due_today_todos": [
    {
      "taskId": "example-task-1",
      "title": "提交周报",
      "dueTime": "2026-09-27T18:00:00+08:00",
      "priority": 30
    }
  ],
  "open_todos": []
}
```

时间支持 ISO 字符串或时间戳；待办优先级为 `10` 低、`20` 普通、`30` 高、`40` 紧急。生成文件的一方需要筛选未完成待办、维护当天日程并持续更新；读取器不会根据完成状态过滤，也不会依据 `fetched_at` 自动拒绝过期数据。

### 可选：使用本地消息快照

设置 `RADAR_SOURCE=cache` 可读取 `.cache/inbox.json`，也可用 `RADAR_INBOX` 指定文件绝对路径。快照格式由 `sources.py` 中的 `CacheSource` 定义，包含 `fetched_at` 和 `messages`。

界面会显示“本地快照”及快照时间。在此模式下刷新只重读文件，更新内容需要由外部流程完成。未显式设置数据源时，程序优先选择可用的官方 DWS，否则尝试已有快照。

## 配置参数参考

| 参数 | 是否必需 / 默认值 | 说明 |
| --- | --- | --- |
| `RADAR_ENGINE` | 建议显式设置 | `jev` / `laya` / `mock`；未指定时，有 Jev Key 则尝试 Jev，否则使用 Mock |
| `TYPESAFE_API_KEY` | Jev 模式必需 | 云端决策模型 Key |
| `RADAR_SOURCE` | 默认自动选择 | `dws` 实时读取或 `cache` 本地快照；模板显式选择 `dws` |
| `DWS_BIN` | 默认 `dws` | CLI 可执行路径；留作注释表示使用 PATH 查找，不要在 `.env` 中写空值 |
| `RADAR_SELF_ID` | 建议配置 | 自己发送的消息中的 `senderId` |
| `RADAR_POLL_INTERVAL` | `30` | 自动刷新间隔，单位秒；设置面板允许 5～600 秒 |
| `DWS_TIMEOUT` | `20` | 每次 DWS 子进程调用的等待上限，单位秒 |
| `RADAR_LLM_BASE_URL` | 可选 | LLM API 基础地址 |
| `RADAR_LLM_API_KEY` | 可选 | LLM Key；与基础地址、模型名同时配置才启用 |
| `RADAR_LLM_MODEL` | 可选 | LLM 服务支持的模型名 |
| `RADAR_CONTEXT` | 默认 `.cache/context.json` | 日程与待办上下文路径 |
| `RADAR_INBOX` | 默认 `.cache/inbox.json` | 本地消息快照路径 |
| `RADAR_MOCK` | 默认关闭 | `true` / `1` / `yes` 强制桌面应用使用模拟数据和模拟决策引擎；不自动关闭 LLM |
| `RADAR_MOCK_FILE` | 可选 | 指定模拟消息 JSON 文件，格式参见 `MockSource` |
| `RADAR_LOG_LEVEL` | `INFO` | 日志级别；需在进程启动前设置，例如 `RADAR_LOG_LEVEL=DEBUG python main.py` |

高级模型选项 `RADAR_LAYA_MODEL` 的默认值是 `convaiinnovations/laya-multilingual`。它在模块导入时读取，日志级别 `RADAR_LOG_LEVEL` 也在加载 `.env` 前读取，因此这两项需要在启动进程前通过环境变量设置。一般使用默认值即可。

`.env` 使用简单的 `KEY=VALUE` 格式，注释请独占一行，不支持变量插值或 shell 命令展开。启动时进程环境变量优先于 `.env`；设置面板保存后会更新当前进程中的对应值。

### 配置与数据保存位置

| 内容 | 源码运行 | 打包后的 `.app` |
| --- | --- | --- |
| 应用配置 | 项目根目录 `.env` | `~/.dingtalk-radar/.env` |
| 本地数据 | 项目根目录 `.cache/` | `~/.dingtalk-radar/.cache/` |
| 隐藏记录 | `.cache/watermarks.json` | `~/.dingtalk-radar/.cache/watermarks.json` |
| DWS 凭据 | 由 DWS 管理 | 由 DWS 管理 |

`.cache/last_unread.json` 保存最近一次未读会话原始返回；消息快照和上下文文件也可能含会话内容。分享项目时仅提供无凭据的 `.env.example`，排除真实 `.env`、`.cache/` 和含个人数据的日志。现有 `.gitignore` 已忽略 `.env`、`.cache/`、构建目录和日志。

## 项目材料

- [吱一声 · 黑客松作品路演（PPTX）](docs/吱一声-黑客松作品路演.pptx)

## 项目结构

```text
Chirp/
├── README.md                 # 项目介绍、安装和使用说明
├── .env.example              # 无凭据的配置模板
├── pyproject.toml            # 包元数据、依赖和 chirp 命令
├── main.py                   # 兼容旧运行方式的入口
├── src/chirp/                # 正式 Python 包
│   ├── domain/               # 消息分析与上下文规则
│   ├── infrastructure/       # DWS、Jev、Laya、缓存适配
│   ├── application/          # 解读服务
│   ├── ui/                   # Qt 界面和设置
│   ├── app.py                # 应用启动和依赖组装
│   └── assets/               # 应用图标
├── docs/                     # 项目材料：黑客松作品路演 PPT
├── scripts/                  # 数据准备、冒烟、回归、演示和 Laya 调试脚本
├── artifacts/                # 本地验证生成的截图/日志，不是运行时输入
├── docs/                     # 路演等项目文档和演示材料
├── 吱一声-x86_64.spec        # PyInstaller 云端/轻量打包配置
├── 钉钉雷达-x86_64.spec      # 兼容旧名称的 PyInstaller 配置
├── build_app.sh              # macOS 打包脚本
└── .cache/                   # 本地运行数据，按需生成
```

模块职责、依赖方向、运行时数据流和脚本分类见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

## 开发验证与打包

### 离线验证

在已安装核心依赖的虚拟环境中运行：

```bash
# 验证引擎抽象和返回值解析，不下载模型
python scripts/test_engine.py

# 使用模拟数据验证分析流水线
RADAR_MOCK=true python scripts/smoke_pipeline.py

# 无窗口界面回归，并生成预览图
QT_QPA_PLATFORM=offscreen python scripts/check_regressions.py
```

界面回归覆盖刷新后的详情更新、长回复换行与复制、会话 / 未读计数、快照时间及实时拉取失败等场景，预览图输出到 `artifacts/`。

### 打包 macOS 应用

```bash
# 使用当前架构的环境；已安装 Laya 时收集本地推理依赖
./build_app.sh

# 云端版本：排除本地模型依赖
./build_app.sh --cloud
```

脚本会安装 PyInstaller，并输出到 `dist/吱一声-arm64.app` 或 `dist/吱一声-x86_64.app`。建议分别在对应架构的 Mac 和依赖环境中打包、验证；本地推理依赖的架构需要与目标一致。

打包产物不包含 DWS、用户 API Key 或聊天缓存。使用者仍需安装并登录 DWS，再通过应用设置配置模型及 CLI 路径。云端包不包含 Laya 依赖，应选择 Jev 或 Mock 引擎。

## 常见问题

### 终端能用 DWS，应用却提示找不到

运行 `command -v dws`，将结果填入设置中的“钉钉 CLI”。从 Finder 启动时，PATH 可能与终端不同。若路径来自某个 AI 宿主的包装脚本，或输出包含 `pending-post-tool-use`，应改用 npm 安装的官方独立 CLI。DWS 的 npm 启动入口也需要能找到 Node.js；若日志显示找不到 `node`，可先从已配置 Node.js 的终端运行源码版本。

### 已登录，仍提示无权限

`dws auth status` 验证登录态，业务读取命令验证消息权限，两者都需要通过。按安装步骤中的 PAT 授权流程处理，并确认当前组织正确。桌面程序不能代替你完成交互式登录或授权，先在终端跑通读取命令。

### 列表为空，或者只有一张卡片

先运行 `dws chat +unread-chats --format json` 检查当前是否存在未读会话，再点击 🗑 清空隐藏标记。每个会话只展示一条消息，多个未读消息来自同一会话时仍只有一张卡片。若提示拉取错误，应先解决连接问题；也可用 Mock 模式检查界面是否正常。

### 显示“本地快照”，刷新没有新消息

检查是否设置了 `RADAR_SOURCE=cache`，或系统没有找到官方 DWS 而选择了快照。配置正确的 `DWS_BIN`，将数据源设为 `dws` 后重启。快照模式本身不会联网更新消息。

### 显示 Mock 引擎，如何启用真实分析

在设置中明确选择 Jev 并填写 Key，或安装本地依赖后选择 Laya。`RADAR_MOCK=true` 会强制使用模拟数据和模拟决策引擎，需要移除该设置并重启。

### Laya 首次启动慢或加载失败

首次分析需要下载和加载模型。检查终端日志、可用磁盘空间以及 ModelScope / Hugging Face 的连通性；确认运行应用的 Python 环境已安装 `requirements-local.txt`。加载失败后修复依赖或网络，再重启。需要先体验时可使用 Mock，已有 Jev Key 时可切换到云端模式。

### LLM 一直生成中，或退回了模板

核对 Base URL、API Key 和模型名，确认服务支持 `/chat/completions` 及项目要求的 JSON 文本输出。当前客户端请求还携带 `enable_thinking=false`；如果服务商拒绝该扩展字段，需要在 `explainer.py` 中适配。调用失败会记录日志并回退到模板，结构化判断仍可使用。

### 修改了 `.env` 但没有生效

直接编辑后重启应用，并检查启动终端是否已有同名环境变量覆盖配置。源码与 `.app` 使用不同的配置目录；本地模型 ID 等在导入阶段读取的高级选项，应在启动前设置环境变量。

## 当前边界与后续方向

当前按未读会话的最近一条消息分析，不覆盖全部对话历史和已读未回复消息；跳转以打开会话为主，不保证定位到具体消息。日程、待办和快照依赖外部更新，模型判断也会受上下文完整性影响。

后续可扩展 DWS 事件推送、日程 / 待办自动同步、连续对话分析，以及经用户确认的待发送回复队列。当前实现仍以轮询、单条消息分析和用户手动发送为主。

## 作者与联系

作者：Murasaki、Joey

邮箱：[joeyj6295@gmail.com](mailto:joeyj6295@gmail.com)
