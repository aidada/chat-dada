# Local Agent — 通用多智能体任务平台

> **V2 Universal Agent** — 从 PPT-only 多智能体系统升级为注册表驱动的通用任务平台

## 目录

- [项目简介](#项目简介)
- [架构总览](#架构总览)
- [系统工作流](#系统工作流)
- [模块详解](#模块详解)
  - [注册表 (Registry)](#注册表-registry)
  - [编排层 (Orchestrator)](#编排层-orchestrator)
  - [Agents (智能体)](#agents-智能体)
  - [Tools (工具)](#tools-工具)
  - [Renderers (渲染器)](#renderers-渲染器)
  - [PPT Engine](#ppt-engine)
- [目录结构](#目录结构)
- [快速开始](#快速开始)
- [API 接口](#api-接口)
- [V1 遗留代码说明](#v1-遗留代码说明)

---

## 项目简介

Local Agent 是一个基于 **FastAPI + LangGraph + WebSocket** 的多智能体任务平台。用户通过 Web 界面发送任务，系统自动分析意图、调度多个 Agent/Tool/Renderer 协同工作，最终返回结果（文本、PPT、Word、Excel 等）。

**核心特性：**
- 注册表驱动：零硬编码，所有能力通过统一注册表管理
- 混合路由：模板匹配已知意图 + LLM 自由规划未知意图
- 依赖图调度：支持步骤间 `depends_on` 依赖，自动并发执行无依赖步骤
- 实时推送：WebSocket 逐步推送执行进度
- 多格式输出：PPT / Word / Excel / 图片 / 纯文本

---

## 架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│                        前端 (index.html)                        │
│                    WebSocket ↕ JSON 消息                        │
├─────────────────────────────────────────────────────────────────┤
│                      FastAPI (main.py)                          │
│               GET /  ·  GET /download  ·  WS /ws                │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────────── Orchestrator 编排层 ────────────────────┐    │
│  │                                                         │    │
│  │  ┌─────────┐   ┌──────────┐   ┌────────────────────┐   │    │
│  │  │ Planner │──▶│Templates │   │    Scheduler       │   │    │
│  │  │ 意图分类 │   │ 预设模板  │   │ 依赖图并发调度     │   │    │
│  │  └────┬────┘   └──────────┘   └────────────────────┘   │    │
│  │       │                              ▲                  │    │
│  │       └──────────────────────────────┘                  │    │
│  │                Runner (入口)                             │    │
│  └─────────────────────────────────────────────────────────┘    │
│                         │ resolve_fn()                           │
│              ┌──────────┴──────────┐                            │
│              │   Registry 注册表    │                            │
│              │  17 个已注册能力     │                            │
│              └──────────┬──────────┘                            │
│         ┌───────────────┼───────────────┐                      │
│         ▼               ▼               ▼                      │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐              │
│  │   Agents    │ │    Tools    │ │  Renderers  │              │
│  │  (LLM 循环) │ │  (单次调用)  │ │  (纯代码)   │              │
│  │             │ │             │ │             │              │
│  │ · search    │ │ · web_search│ │ · ppt_render│              │
│  │ · doc_analyst│ │ · translator│ │ · word_render│             │
│  │ · writer    │ │ · summarizer│ │ ·excel_render│             │
│  │ · general   │ │ · code_exec │ │ ·visio_render│             │
│  │ · deep_res  │ │ · academic  │ │             │              │
│  │ · data_anal │ │ · image_gen │ │             │              │
│  │             │ │ · img2diag │ │             │              │
│  └─────────────┘ └─────────────┘ └─────────────┘              │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 三层能力模型

| 类型 | 说明 | 特点 | 示例 |
|------|------|------|------|
| **Agent** | 智能体，包含 LLM 推理循环 | 可多轮调用工具、有状态 | search, writer, deep_research |
| **Tool** | 单次调用工具 | 无状态、一进一出 | web_search, translator, code_executor |
| **Renderer** | 文件渲染器 | 纯代码、不调用 LLM | ppt_render, word_render, excel_render, visio_render |

---

## 系统工作流

```
用户输入任务
     │
     ▼
┌─────────────┐
│   Planner   │  ① 意图分类 (LLM)
│  classify_  │
│  and_plan() │
└──────┬──────┘
       │
       ├── confidence ≥ 0.5 ──▶ 匹配预设模板 (templates.py)
       │                         7 种模板: ppt_report, research_report,
       │                         data_analysis, quick_question, translate_doc,
       │                         image_to_visio, image_generation
       │
       └── confidence < 0.5 ──▶ LLM 自由规划 (基于 registry_summary)
                                  动态生成 steps + context
       │
       ▼
┌─────────────┐
│   Runner    │  ② 路由执行
└──────┬──────┘
       │
       ├── ppt_report ──▶ 专用 PPT 流程（向后兼容 V1）
       ├── quick_question ──▶ 直接 LLM 回答
       └── 其他 ──▶ Scheduler 依赖图执行
                      │
                      ▼
              ┌─────────────┐
              │  Scheduler  │  ③ 波次并发执行
              │ execute_plan│
              └──────┬──────┘
                     │
          ┌──── Wave 1 ────┐
          │  step 1  step 2 │  (无依赖，并发)
          └────────┬────────┘
                   │
          ┌──── Wave 2 ────┐
          │     step 3      │  (依赖 1,2)
          └────────┬────────┘
                   │
          ┌──── Wave 3 ────┐
          │     step 4      │  (依赖 3)
          └────────────────┘
```

### 预设任务模板

| 模板 | 描述 | 执行流水线 |
|------|------|-----------|
| `ppt_report` | 研究主题 → 生成 PPT | search ∥ doc_analyst → writer → ppt_render |
| `research_report` | 深度研究 → 生成 Word | deep_research ∥ doc_analyst → writer → word_render |
| `data_analysis` | 数据文件分析 | doc_analyst → data_analyst → writer |
| `quick_question` | 直接问答 | general_chat |
| `translate_doc` | 文档翻译 | doc_analyst → translator → word_render |
| `image_to_visio` | 图片转图表文件 | image_to_diagram → visio_render |
| `image_generation` | 文本生成图片 | image_gen |

> `∥` 表示并行执行，`→` 表示有依赖的顺序执行

---

## 模块详解

### 注册表 (Registry)

**文件：** `registry.py`

系统的核心枢纽。所有能力在此注册，Orchestrator 通过名称查找和动态导入（`importlib`）实现零硬编码调度。

```python
# 注册一个能力
register("search", fn_path="agents.search_agent:run_search",
         cap_type="agent", description="Web search + browser scraping")

# 调用时动态解析
fn = resolve_fn("search")   # → agents.search_agent.run_search
result = await fn(input_data)
```

**已注册能力清单（17 个）：**

| 名称 | 类型 | 模块路径 | 说明 |
|------|------|---------|------|
| search | agent | agents.search_agent:run_search | Web 搜索 + 浏览器抓取 |
| doc_analyst | agent | agents.doc_agent:run_doc_analysis | PDF/文本文件解析 |
| writer | agent | agents.writer_agent:run_writer | Slide DSL JSON 生成 |
| general_chat | agent | agents.general_chat:run | 直接问答 |
| deep_research | agent | agents.deep_research:run | 多轮深度研究（Web + 学术） |
| data_analyst | agent | agents.data_analyst:run | 数据分析 + 代码执行 |
| web_search | tool | tools.web_search:run | Tavily 搜索 |
| translator | tool | tools.translator:run | LLM 翻译 |
| summarizer | tool | tools.summarizer:run | LLM 摘要 |
| code_executor | tool | tools.code_executor:run | Python 沙箱执行 |
| academic_search | tool | tools.academic_search:run | 学术论文搜索（Semantic Scholar + arXiv） |
| image_gen | tool | tools.image_gen:run | 文本生成图片（Nano Banana2 API） |
| image_to_diagram | tool | tools.image_to_diagram:run | 图片转结构化图表 JSON（Vision 模型） |
| ppt_render | renderer | ppt_engine.renderer:render_pptx | Slide DSL → .pptx |
| word_render | renderer | renderers.word_renderer:run | Markdown → .docx |
| excel_render | renderer | renderers.excel_renderer:run | 结构化数据 → .xlsx |
| visio_render | renderer | renderers.visio_renderer:run | 图表 JSON → Visio（占位，输出 JSON） |

---

### 编排层 (Orchestrator)

**目录：** `orchestrator/`

| 文件 | 职责 |
|------|------|
| `runner.py` | 主入口，替代旧版 `agents/orchestrator.py`，保持相同回调接口 |
| `planner.py` | 意图分类 + LLM 自由规划 |
| `scheduler.py` | 依赖图调度器，按波次并发执行 |
| `templates.py` | 7 个预设任务模板 |

**Runner** 是 `main.py` 的唯一入口：
```python
from orchestrator.runner import run_orchestrator as run_agent
```

---

### Agents (智能体)

**目录：** `agents/`

每个 Agent 是一个 LangGraph `StateGraph` 子图，包含 LLM 推理 + 工具调用循环。

| 文件 | Agent | 特点 |
|------|-------|------|
| `search_agent.py` | 搜索 Agent | Tavily + browser-use，最多 10 步 |
| `doc_agent.py` | 文档分析 Agent | PDF/文本文件读取和提取 |
| `writer_agent.py` | 写作 Agent | 生成 Slide DSL JSON |
| `general_chat.py` | 通用对话 Agent | 单次 LLM 调用，无工具 |
| `deep_research.py` | 深度研究 Agent | Web + 学术 + 浏览器，最多 15 步 |
| `data_analyst.py` | 数据分析 Agent | Python 代码执行 + 数据文件读取，最多 10 步 |

> `orchestrator.py` 是 V1 遗留代码，已由 `orchestrator/runner.py` 替代。参见 [V1 遗留代码说明](#v1-遗留代码说明)。

#### 动态工具注入

Agent 除了自带的核心 tools 外，还可以在构建 graph 时从 registry 动态获取额外工具。通过 `available_to` 字段控制哪些 agent 可以使用哪些 registry tools：

```python
# registry.py 中注册时指定
register("image_gen", ..., available_to=["deep_research", "data_analyst"])

# agent 构建 graph 时自动注入
from registry import get_tools_for_agent
dynamic = get_tools_for_agent("deep_research", exclude_names=core_names)
all_tools = CORE_TOOLS + dynamic  # 合并核心 + 动态工具
```

**当前动态工具分配：**

| Registry Tool | 可用 Agent | 原因 |
|---|---|---|
| image_gen | deep_research, data_analyst | 研究/分析可能需要生成配图 |
| image_to_diagram | doc_analyst, deep_research | 文档分析可能遇到图片需解析 |
| summarizer | deep_research, data_analyst | 压缩长文本 |
| translator | deep_research | 研究可能遇到外文资料 |
| code_executor | deep_research | 研究可能需要验证代码 |

> `web_search` 和 `academic_search` 的 `available_to` 为空，因为相关 agent 已有自己的 `@tool` 版本。

---

### Tools (工具)

**目录：** `tools/`

独立的单次调用函数，统一接口 `async def run(input_data) -> dict`。

| 文件 | 功能 |
|------|------|
| `web_search.py` | Tavily 网络搜索 |
| `translator.py` | LLM 翻译 |
| `summarizer.py` | LLM 摘要 |
| `code_executor.py` | Python 沙箱执行（subprocess，30s 超时） |
| `academic_search.py` | Semantic Scholar + arXiv 搜索（httpx） |
| `image_gen.py` | 文本生成图片（Nano Banana2 API） |
| `image_to_diagram.py` | 图片转结构化图表 JSON（Vision 模型） |

---

### Renderers (渲染器)

**目录：** `renderers/` + `ppt_engine/`

纯代码文件生成，不调用 LLM。

| 文件 | 输出格式 | 依赖库 |
|------|---------|--------|
| `ppt_engine/renderer.py` | .pptx | python-pptx |
| `renderers/word_renderer.py` | .docx | python-docx |
| `renderers/excel_renderer.py` | .xlsx | openpyxl |
| `renderers/visio_renderer.py` | .json (占位) | — (python-vsdx 待实现) |

### PPT Engine

**目录：** `ppt_engine/`

| 文件 | 职责 |
|------|------|
| `dsl_schema.py` | Slide DSL 数据模型（Pydantic），定义 SlideDeck / Slide / Element 结构 |
| `renderer.py` | 将 SlideDeck DSL 渲染为 .pptx 文件 |

---

## 目录结构

```
.
├── main.py                    # FastAPI 入口，WebSocket 端点
├── registry.py                # 统一能力注册表（V2 核心）
├── models.py                  # LLM 配置中心，按角色分配模型
│
├── orchestrator/              # V2 编排层
│   ├── runner.py              # 主入口（替代旧 agents/orchestrator.py）
│   ├── planner.py             # 意图分类 + 自由规划
│   ├── scheduler.py           # 依赖图调度器
│   └── templates.py           # 预设任务模板
│
├── agents/                    # 智能体（LLM 循环）
│   ├── search_agent.py        # 搜索 Agent
│   ├── doc_agent.py           # 文档分析 Agent
│   ├── writer_agent.py        # 写作 Agent
│   ├── general_chat.py        # 通用对话 Agent（V2 新增）
│   ├── deep_research.py       # 深度研究 Agent（V2 新增）
│   ├── data_analyst.py        # 数据分析 Agent（V2 新增）
│   └── orchestrator.py        # ⚠️ V1 遗留，已由 orchestrator/runner.py 替代
│
├── tools/                     # 独立工具（V2 新增）
│   ├── web_search.py          # Tavily 搜索
│   ├── translator.py          # LLM 翻译
│   ├── summarizer.py          # LLM 摘要
│   ├── code_executor.py       # Python 沙箱执行
│   ├── academic_search.py     # 学术论文搜索
│   ├── image_gen.py           # 文本生成图片（Nano Banana2）
│   └── image_to_diagram.py    # 图片转结构化图表（Vision）
│
├── renderers/                 # 文件渲染器（V2 新增）
│   ├── word_renderer.py       # Markdown → .docx
│   ├── excel_renderer.py      # 结构化数据 → .xlsx
│   └── visio_renderer.py      # 图表 JSON → Visio（占位）
│
├── ppt_engine/                # PPT 渲染引擎
│   ├── dsl_schema.py          # Slide DSL 数据模型
│   └── renderer.py            # SlideDeck → .pptx
│
├── old/                       # ⚠️ V1 遗留备份
│   └── agent.py               # 最初的单 agent 代码
│
├── static/
│   └── index.html             # 前端页面
│
├── outputs/                   # 生成文件输出目录
├── docs/plans/                # 设计文档和实施计划
├── requirements.txt           # Python 依赖
└── pyproject.toml             # 项目配置
```

---

## 快速开始

### 环境要求

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (推荐) 或 pip

### 安装

```bash
# 创建虚拟环境
uv venv .venv
source .venv/bin/activate

# 安装依赖
uv pip install -r requirements.txt

# 安装 playwright 浏览器（搜索 Agent 需要）
playwright install chromium
```

### 配置

编辑 `models.py` 中的 `MODEL_CONFIGS`，设置各角色的 LLM 模型和 API Key。

如需搜索功能，设置环境变量：
```bash
export TAVILY_API_KEY="your-api-key"
```

### 运行

```bash
uvicorn main:app --reload --port 8000
```

浏览器访问 `http://localhost:8000`，通过 Web 界面发送任务。

---

## API 接口

| 端点 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 返回前端页面 |
| `/download/{filename}` | GET | 下载生成的文件 |
| `/ws` | WebSocket | 任务交互通道 |

### WebSocket 消息格式

**客户端 → 服务端：**
```json
{"task": "帮我研究人工智能发展趋势并生成PPT"}
```

**服务端 → 客户端：**
```json
{"type": "start", "content": "开始执行: ..."}
{"type": "step",  "content": "🔍 Search Agent: searching..."}
{"type": "file",  "url": "/download/report_abc123.pptx", "name": "report_abc123.pptx"}
{"type": "result","content": "PPT 已生成：共 8 页"}
{"type": "error", "content": "错误信息"}
```

---

## V1 遗留代码说明

以下文件属于 V1 版本（PPT-only 多智能体系统），已被 V2 架构替代，保留仅供参考：

| 文件 | 状态 | 替代方案 |
|------|------|---------|
| `agents/orchestrator.py` | **⚠️ 已废弃** | `orchestrator/runner.py` — 注册表驱动的通用编排器 |
| `old/agent.py` | **⚠️ 已归档** | V1 最初的单文件 agent，已拆分为多模块架构 |

**V1 → V2 主要变化：**

| 维度 | V1 | V2 |
|------|----|----|
| 能力范围 | 仅 PPT 生成 | 通用任务（PPT/Word/Excel/研究/分析/翻译/问答） |
| 调度方式 | 硬编码 4 步流水线 | 注册表 + 依赖图 + 模板/自由规划 |
| Agent 数量 | 3 个（search, doc, writer） | 6 个 + 7 tools + 4 renderers = **17 个能力** |
| 意图理解 | 无（全部走 PPT 流程） | LLM 意图分类 + 7 种模板 + 自由规划 |
| 并发控制 | 手动 `asyncio.gather` | 调度器自动按依赖波次并发 |
| 扩展方式 | 改代码 | `registry.py` 注册即可 |

---

## 技术栈

| 技术 | 用途 |
|------|------|
| FastAPI | Web 框架 + WebSocket |
| LangGraph | Agent 状态图 |
| LangChain | LLM 接口层 |
| browser-use | 浏览器自动化 |
| python-pptx | PPT 生成 |
| python-docx | Word 生成 |
| openpyxl | Excel 生成 |
| Tavily | 网络搜索 |
| httpx | HTTP 客户端（学术搜索） |
| Playwright | 浏览器引擎 |
