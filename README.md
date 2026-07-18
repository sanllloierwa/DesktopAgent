# Desktop Agent

AI 驱动的跨平台桌面自动化代理——用自然语言描述任务，Agent 自主规划并执行。

> [!WARNING]
> 最近新增的 Agnes MCP 视觉链路、视觉坐标定位、原生键鼠模拟、微信桌面流程和任务终态审计，
> 目前仅完成代码级检查与单元测试，**尚未进行真实桌面场景的完整端到端测试**。
> 请勿直接用于支付、删除、发布、批量发送等高风险操作。

## 架构概览

**Plan → Execute → Observe → Verify** 闭环：

| 模块 | 文件 | 职责 |
| --- | --- | --- |
| **Planner** | `src/agent/planner.py` | 将用户目标分解为有序的 `Step` 序列（工具调用 + 参数），失败时自动重规划 |
| **Executor** | `src/agent/executor.py` | 从 `ToolRegistry` 查找工具并执行，返回 `ActionResult` |
| **Observer** | `src/agent/observer.py` | 聚合感知数据（DOM 快照、UIA 树、截图）为统一的 `Context` 对象 |
| **Verifier** | `src/agent/verifier.py` | 通过规则 + LLM 校验单步结果与最终目标，避免只完成中间步骤却误报成功 |
| **Memory** | `src/agent/memory.py` | 三层记忆：短期（滑动窗口）、工作记忆（当前会话）、长期（ChromaDB 向量库） |

## 已完成功能

### 工具层（29 个已注册工具）

- **桌面控制**：启动/关闭应用、窗口聚焦、Unicode 剪贴板输入、快捷键、坐标点击、鼠标移动、滚轮和拖拽
- **WPS/Word COM 自动化**（`src/tools/desktop/wps_com.py`）：基于 `pywin32` COM 接口——创建文档、写入文本、设置字体/对齐、保存、导出 PDF、插入图片
- **浏览器操作**（`src/tools/browser/navigate.py`）：基于 Playwright——导航、点击（CSS/文本/角色策略）、输入、截图、DOM 摘要、文本提取
- **AI 工具**（`src/tools/ai/`）：文章生成、文本摘要、Agnes 屏幕分析和结构化 UI 目标定位

## 实验性功能（尚未进行真实场景测试）

以下功能已经接入代码并覆盖部分单元测试，但**尚未进行真实桌面端到端测试**，稳定性、坐标准确性和第三方应用兼容性仍需验证。

| 功能 | 当前实现 | 测试状态 |
| --- | --- | --- |
| **Agnes MCP 视觉分析** | `desktop_screenshot → analyze_screen → src/vision_mcp`，支持 MCP/direct 两种传输 | 尚未进行长时间稳定性和复杂界面测试；已观察到上游请求偶发超时 |
| **Kimi K3 文本与视觉** | OpenAI 兼容文本生成；视觉侧复用 `image_url` + MCP 链路 | 已完成参数与消息格式单元测试，尚未使用真实 Kimi Key 进行端到端测试 |
| **结构化视觉定位** | `locate_screen_element` 返回目标边界框、物理屏幕坐标和置信度 | 尚未在不同 DPI、窗口缩放、遮挡和动态界面中进行真实点击测试 |
| **模拟键鼠操作** | `focus_window`、`desktop_keypress`、`desktop_click`、`desktop_move_mouse`、`desktop_scroll`、`desktop_drag` | 尚未进行跨应用端到端测试；当前主要面向 Windows |
| **多显示器/DPI 坐标** | 截图携带 `left/top/width/height`，启用 Per-Monitor DPI Awareness，并支持负坐标 | 尚未在多显示器排列和不同缩放比例组合下测试 |
| **微信桌面自动化** | 规划使用窗口聚焦、快捷键搜索、文本输入、Enter 提交和截图复验 | 尚未进行真实账号、登录流程、群聊搜索和消息发送测试 |
| **最终目标审计** | 计划耗尽后由 LLM 检查用户最终目标，未完成时触发补充规划 | 尚未进行大规模任务回归，可能产生额外模型调用或保守失败 |

推荐的实验性视觉操作闭环：

```text
desktop_screenshot
  → locate_screen_element
  → desktop_click / desktop_move_mouse / desktop_scroll / desktop_drag
  → desktop_screenshot
  → analyze_screen
```

定位置信度低于默认阈值 `0.70` 或坐标越界时会拒绝点击，但这不能替代人工确认。

### 平台预设

- **WPS**：完整文档创建流水线（新建 → 撰写 → 排版 → 导出 PDF）
- **知乎**：登录、写文、配图、发布、搜索、互动
- **微信**：搜索公众号、关注、发送消息（预设与近期桌面流程均尚未进行真实场景测试）

### 感知层

- `screenshot.py`：`mss` + Pillow 屏幕截图，输出 base64 PNG 及物理屏幕坐标元数据
- `uia_parser.py`：Windows UI Automation 树遍历，提取前台窗口的可交互控件
- `vision.py`：通过 Agnes 分析截图，并可将 UI 目标转换为带置信度的结构化屏幕坐标（尚未进行真实点击测试）

### 用户界面（三种模式）

| 模式 | 启动方式 | 说明 |
| --- | --- | --- |
| Gradio Web UI | `--ui` | 浏览器内操作，任务卡片 + 截图预览 + 设置面板 |
| CustomTkinter 桌面应用 | `--local-ui` | Windows 原生窗口，功能同 Web UI |
| REPL 命令行 | `--interactive` | 终端交互模式 |

### 配置系统

- YAML 配置文件（`config/default.yaml`）：Agent 限制、LLM 提供商/模型、浏览器设置、桌面应用路径、日志
- 环境变量 / `.env`：API 密钥（DeepSeek、Anthropic、OpenAI、Agnes、Kimi）
- UI 设置持久化（`~/.desktop-agent/settings.json`）：优先级 用户保存 > 环境变量 > .env

### LLM 多后端支持

统一适配层（`src/utils/llm_factory.py`），支持 Anthropic、OpenAI、DeepSeek、Kimi K3、Agnes 和 Ollama 本地模型。Kimi K3 会使用其专用参数约束：不显式传入 `temperature`，并使用 `max_completion_tokens`。

#### Kimi K3 配置

文本生成可直接在 Gradio 或 CustomTkinter 的设置页选择 `kimi / kimi-k3` 并填写 Kimi API Key，也可在 `.env` 中配置：

```dotenv
KIMI_API_KEY=sk-xxx
# 同时兼容官方环境变量名：MOONSHOT_API_KEY
```

如果希望由 Kimi K3 承担桌面截图分析，需要同时修改 `config/default.yaml` 中完整的视觉配置，不能只改模型名：

```yaml
vision:
  provider: kimi
  model: kimi-k3
  base_url: "https://api.moonshot.ai/v1"
  transport: mcp
```

当前仍保留 Agnes 为默认视觉模型；Kimi K3 视觉链路尚未使用真实桌面场景和真实 API Key 进行端到端测试。

### 事件系统

`src/ui/events.py` 定义类型化 `AgentEvent` 和 `EventBus`：`PLAN_START`、`PLAN_DONE`、`STEP_START`、`STEP_DONE`、`STEP_RETRY`、`ERROR`、`TASK_DONE`。Agent 核心与 UI 完全解耦。

---

## 待完成与改进方向

### 1. 多模态交互

#### 语音输入

- 集成语音识别（Whisper / Faster-Whisper），支持麦克风实时录音转文字
- 将语音指令直接送入 Planner 生成任务计划
- 执行过程中的关键节点支持语音播报反馈（TTS）

#### 图像理解与桌面内容提取（部分已实现，尚未完成真实场景测试）

- 已支持主显示器截图和视觉 LLM 分析；指定窗口/区域的完整工具接口仍待补充
- 支持"打开这个文件夹里的图片，把标题改成图片里的文字"一类跨应用信息提取
- 已加入截图 + 视觉定位 + 坐标点击原型，但尚未在 UIA 无法覆盖的真实控件上系统测试

#### 指定窗口的信息获取

- 从窗口列表中选择目标窗口（而非仅操作前台窗口）
- 读取目标窗口的文本内容（UIA TextPattern / 截图 OCR）
- 实现跨窗口的复制粘贴与信息流转

### 2. 可执行文件生成

- 使用 PyInstaller 或 Nuitka 将项目打包为独立 `.exe`
- 内置 Python 运行时与所有依赖，免安装直接运行
- 生成安装包（NSIS / WiX），支持桌面快捷方式、开始菜单注册
- 提供 CLI 版本（无 GUI 依赖的轻量 exe）和完整版两种构建选项

### 3. 更多样化的软件项目支持

#### 办公套件扩展

- Microsoft Office 原生支持（Word、Excel、PowerPoint）——利用 `win32com` 已具备的基础，补充 Excel 数据操作、PPT 幻灯片生成
- LibreOffice / OpenOffice 跨平台支持（UNO API）

#### 设计工具

- Figma 插件 / API 集成——自动生成设计稿、导出资源
- Canva 自动化——模板化图片与海报生成

#### 开发工具

- VS Code / JetBrains IDE 集成——打开项目、运行终端命令、管理 Git
- 终端 / Shell 深度整合——执行脚本、监控输出、错误恢复

#### 通讯与协作

- 企业微信 / 钉钉 / 飞书——消息发送、群管理、文件分享
- Slack / Discord——频道消息、Bot 交互
- 邮件客户端（Outlook / Thunderbird）——邮件撰写与发送

#### 文件与系统管理

- 文件资源管理器操作——批量重命名、分类整理、压缩解压
- 系统设置调整——音量、亮度、网络开关

### 4. 其他增强项

| 项目 | 说明 |
| --- | --- |
| **测试覆盖** | 已有视觉桥接、坐标转换、键鼠工具、重规划和终态审计单元测试；真实应用集成测试与端到端测试仍缺失 |
| **人机交互模拟** | 已支持基础键鼠操作；自然鼠标轨迹、随机延迟和真实应用兼容性仍待完善与测试 |
| **会话持久化** | 知乎/微信登录态保存与恢复，凭证安全管理 |
| **跨平台适配** | Linux（X11/Wayland + AT-SPI）与 macOS（Accessibility API）感知层 |
| **CI/CD** | 自动化构建、测试、打包流水线 |

---

## 快速开始

```bash
# 安装依赖
pip install -e .

# 配置 API 密钥
cp .env.example .env
# 编辑 .env 填入你的 API Key

# 启动（任选一种）
python -m src.main --ui          # Gradio Web 界面
python -m src.main --local-ui    # 原生桌面界面
python -m src.main --interactive # 命令行 REPL

# 视觉 MCP 健康检查（不会分析屏幕）
python scripts/debug_agnes_mcp.py health
```

### 视觉 MCP 调试产物

视觉 MCP 默认会为每次调用生成一个 ZIP 调试包。未指定目录时，文件位于系统临时目录的
`desktop-agent/vision-mcp` 子目录；`analyze_screen`、`locate_screen_element` 和调试脚本的
输出中会包含绝对路径 `mcp_artifact_path`。

每个 ZIP 包含：

- `manifest.json`：调用时间、耗时、模型、传输方式和执行状态；
- `request.json`：MCP 工具名以外的调用参数，截图 base64 会替换为简短引用；
- `response.json` 或 `error.json`：MCP 返回内容或规范化错误；
- `screenshot.png`（或对应图片后缀）：本次视觉请求实际使用的截图。

可在 `config/default.yaml` 的 `vision` 节调整：

```yaml
artifact_output_enabled: true  # 是否输出调试包
artifact_dir: ""              # 留空使用系统临时目录
artifact_retention: 50         # 自动保留最近的文件数
```

调试包可能包含桌面上的敏感信息，仅应在本机排障时使用并及时清理。

## 项目结构

```text
DesktopAgent/
├── config/default.yaml       # 默认配置
├── src/
│   ├── main.py               # 程序入口
│   ├── agent/                # 核心 Agent 循环（planner, executor, observer, verifier, memory）
│   ├── tools/                # 工具层（desktop/, browser/, ai/）
│   ├── vision_mcp/           # Agnes 视觉 MCP 客户端、服务端与后端
│   ├── ui/                   # 用户界面（gradio, ctk, events, bridge）
│   └── utils/                # LLM 工厂、配置管理、日志、感知
├── tests/                    # 单元测试（真实应用端到端测试待补充）
├── scripts/                  # 辅助脚本
└── docs/                     # 设计文档
```

## 技术栈

- **Python** >= 3.11
- **Playwright** — 浏览器自动化
- **pywinauto / uiautomation** — Windows UI 自动化
- **pywin32** — COM 接口（WPS/Office 文档控制）
- **Anthropic / OpenAI / DeepSeek / Kimi / Agnes** — 多 LLM 与多模态后端
- **Gradio / CustomTkinter** — Web 与原生双界面
- **ChromaDB** — 长期记忆向量存储
- **mss / Pillow / OpenCV** — 屏幕感知与图像处理

## 当前状态

本项目仍处于实验和实习开发阶段。基础单元测试不代表真实桌面环境可用；尤其是视觉定位、模拟键鼠、微信操作、多显示器坐标和最终目标审计等近期功能，均**尚未进行完整真实场景测试**。请在隔离环境中谨慎验证，不对自动化结果作可靠性保证。
