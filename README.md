# Desktop Agent

AI 驱动的跨平台桌面自动化代理——用自然语言描述任务，Agent 自主规划并执行。

## 架构概览

**Plan → Execute → Observe → Verify** 闭环：

| 模块 | 文件 | 职责 |
| --- | --- | --- |
| **Planner** | `src/agent/planner.py` | 将用户目标分解为有序的 `Step` 序列（工具调用 + 参数），失败时自动重规划 |
| **Executor** | `src/agent/executor.py` | 从 `ToolRegistry` 查找工具并执行，返回 `ActionResult` |
| **Observer** | `src/agent/observer.py` | 聚合感知数据（DOM 快照、UIA 树、截图）为统一的 `Context` 对象 |
| **Verifier** | `src/agent/verifier.py` | 通过规则 + 可选 LLM 视觉校验步骤是否成功，支持覆盖误判 |
| **Memory** | `src/agent/memory.py` | 三层记忆：短期（滑动窗口）、工作记忆（当前会话）、长期（ChromaDB 向量库） |

## 已完成功能

### 工具层（18 个已注册工具）

- **桌面控制**：启动/关闭应用（WPS、微信、记事本、Word），通过 `subprocess` + `pygetwindow` 实现
- **WPS/Word COM 自动化**（`src/tools/desktop/wps_com.py`）：基于 `pywin32` COM 接口——创建文档、写入文本、设置字体/对齐、保存、导出 PDF、插入图片
- **浏览器操作**（`src/tools/browser/navigate.py`）：基于 Playwright——导航、点击（CSS/文本/角色策略）、输入、截图、DOM 摘要、文本提取
- **AI 工具**（`src/tools/ai/`）：文章生成（LLM）、文本摘要、屏幕分析（Anthropic 视觉 / DeepSeek 文本回退）

### 平台预设

- **WPS**：完整文档创建流水线（新建 → 撰写 → 排版 → 导出 PDF）
- **知乎**：登录、写文、配图、发布、搜索、互动
- **微信**：搜索公众号、关注、发送消息

### 感知层

- `screenshot.py`：`mss` + Pillow 屏幕截图，输出 base64 PNG
- `uia_parser.py`：Windows UI Automation 树遍历，提取前台窗口的可交互控件

### 用户界面（三种模式）

| 模式 | 启动方式 | 说明 |
| --- | --- | --- |
| Gradio Web UI | `--ui` | 浏览器内操作，任务卡片 + 截图预览 + 设置面板 |
| CustomTkinter 桌面应用 | `--local-ui` | Windows 原生窗口，功能同 Web UI |
| REPL 命令行 | `--interactive` | 终端交互模式 |

### 配置系统

- YAML 配置文件（`config/default.yaml`）：Agent 限制、LLM 提供商/模型、浏览器设置、桌面应用路径、日志
- 环境变量 / `.env`：API 密钥（DeepSeek、Anthropic、OpenAI）
- UI 设置持久化（`~/.desktop-agent/settings.json`）：优先级 用户保存 > 环境变量 > .env

### LLM 多后端支持

统一适配层（`src/utils/llm_factory.py`），支持 Anthropic、OpenAI、DeepSeek、Ollama 本地模型，一键切换。

### 事件系统

`src/ui/events.py` 定义类型化 `AgentEvent` 和 `EventBus`：`PLAN_START`、`PLAN_DONE`、`STEP_START`、`STEP_DONE`、`STEP_RETRY`、`ERROR`、`TASK_DONE`。Agent 核心与 UI 完全解耦。

---

## 待完成与改进方向

### 1. 多模态交互

#### 语音输入

- 集成语音识别（Whisper / Faster-Whisper），支持麦克风实时录音转文字
- 将语音指令直接送入 Planner 生成任务计划
- 执行过程中的关键节点支持语音播报反馈（TTS）

#### 图像理解与桌面内容提取

- 截取指定窗口或桌面区域，通过视觉 LLM 分析内容
- 支持"打开这个文件夹里的图片，把标题改成图片里的文字"一类跨应用信息提取
- 对 UIA 无法覆盖的控件（如图形按钮、自定义渲染区域），通过截图 + 视觉定位实现点击

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
| **测试覆盖** | `tests/` 目录已建，需补充单元测试、集成测试、端到端测试 |
| **人机交互模拟** | `humanize.py` 已搭建框架（随机延迟、鼠标轨迹），需接入执行器 |
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
python main.py --ui          # Gradio Web 界面
python main.py --local-ui    # 原生桌面界面
python main.py --interactive # 命令行 REPL
```

## 项目结构

```text
DesktopAgent/
├── main.py                   # 入口
├── config/default.yaml       # 默认配置
├── src/
│   ├── agent/                # 核心 Agent 循环（planner, executor, observer, verifier, memory）
│   ├── tools/                # 工具层（desktop/, browser/, ai/）
│   ├── ui/                   # 用户界面（gradio, ctk, events, bridge）
│   └── utils/                # LLM 工厂、配置管理、日志、感知
├── tests/                    # 测试（待补充）
├── scripts/                  # 辅助脚本
└── docs/                     # 设计文档
```

## 技术栈

- **Python** >= 3.11
- **Playwright** — 浏览器自动化
- **pywinauto / uiautomation** — Windows UI 自动化
- **pywin32** — COM 接口（WPS/Office 文档控制）
- **Anthropic / OpenAI / DeepSeek** — 多 LLM 后端
- **Gradio / CustomTkinter** — Web 与原生双界面
- **ChromaDB** — 长期记忆向量存储
- **mss / Pillow / OpenCV** — 屏幕感知与图像处理
