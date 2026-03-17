# chat dada / Local Agent

一个基于 FastAPI、LangGraph 和流式任务运行时的通用多智能体平台。当前代码库已经从早期的 WebSocket-only PPT 生成器，演进为支持聊天、深度研究、文件分析、翻译、图片生成、Markdown/Word/Excel/PPT 输出的任务系统。

前端页面位于 [static/index.html](static/index.html)，后端入口位于 [main.py](main.py)。

## 当前能力概览

- 任务提交采用 `POST /tasks`，事件流采用 `GET /tasks/{task_id}/events` 的 SSE 模式，支持断线重放。
- 任务状态和事件持久化到 `data/task_runs.sqlite3`，不是纯内存队列。
- 支持人机交互追问。任务运行中可进入 `waiting_for_user`，再通过 `POST /tasks/{task_id}/reply` 继续。
- 编排链路支持用户记忆。`orchestrator` 路由会从 `data/memory/` 召回历史片段，并在任务结束后回写长期记忆。
- 能力通过统一注册表维护，当前共 19 个已注册能力：6 个 agents、8 个 tools、5 个 renderers。
- 支持多种输出形态：直接回答、Markdown、Word、Excel、PPT、图片、占位版 Visio JSON。

## 架构

```text
Browser / API Client
        │
        ├── POST /upload
        ├── POST /tasks
        ├── GET  /tasks/{task_id}
        ├── GET  /tasks/{task_id}/events   (SSE)
        └── POST /tasks/{task_id}/reply
        │
        ▼
FastAPI app (main.py)
        │
        ▼
TaskService / TaskRunStore (task_runtime.py)
        │              ├── SQLite: data/task_runs.sqlite3
        │              └── SSE replay / live subscribers
        ▼
Task Dispatcher (task_dispatcher.py)
        │
        ├── general_chat
        └── orchestrator
                │
                ├── memory recall / save
                ├── planner
                ├── template selection or free-form planning
                └── scheduler
                        │
                        ▼
                 registry.py
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
      agents         tools        renderers
```

## 任务模式

`POST /tasks` 支持 3 种模式：

| mode | 行为 |
| --- | --- |
| `auto` | 默认模式。按附件和关键词在 `general_chat` 与 `orchestrator` 之间自动路由 |
| `chat` | 强制走直接对话，不允许附件 |
| `agent` | 强制走编排器 |

`thinking_level` 当前支持 `low` / `medium` / `high`，会通过 [models.py](models.py) 映射到底层模型的推理强度参数。

## 已注册能力

能力注册定义见 [registry.py](registry.py)。

### Agents

| 名称 | 入口 | 说明 |
| --- | --- | --- |
| `general_chat` | `agents.general_chat:run` | 直接问答，支持流式文本增量 |
| `search` | `agents.search_agent:run_search` | Web 搜索与页面抓取 |
| `doc_analyst` | `agents.doc_agent:run_doc_analysis` | 读取并分析 PDF/文本/附件 |
| `writer` | `agents.writer_agent:run_writer` | 生成写作内容或幻灯片 DSL |
| `deep_research` | `agents.deep_research:run` | 多轮研究，支持 Web、学术检索、浏览器、追问 |
| `data_analyst` | `agents.data_analyst:run` | 数据分析与代码执行协同 |

### Tools

| 名称 | 入口 | 说明 |
| --- | --- | --- |
| `web_search` | `tools.web_search:run` | Tavily 搜索 |
| `brave_search` | `tools.brave_search:run` | Brave 搜索 |
| `academic_search` | `tools.academic_search:run` | Semantic Scholar + arXiv |
| `translator` | `tools.translator:run` | LLM 翻译 |
| `summarizer` | `tools.summarizer:run` | LLM 摘要 |
| `code_executor` | `tools.code_executor:run` | Python 沙箱执行 |
| `image_gen` | `tools.image_gen:run` | 文本生成图片 |
| `image_to_diagram` | `tools.image_to_diagram:run` | 图片转结构化图表 |

### Renderers

| 名称 | 入口 | 输出 |
| --- | --- | --- |
| `ppt_render` | `ppt_engine.renderer:render_pptx` | `.pptx` |
| `markdown_render` | `renderers.markdown_renderer:run` | `.md` |
| `word_render` | `renderers.word_renderer:run` | `.docx` |
| `excel_render` | `renderers.excel_renderer:run` | `.xlsx` |
| `visio_render` | `renderers.visio_renderer:run` | 当前为占位 JSON 输出 |

## 编排模板

模板定义见 [orchestrator/templates.py](orchestrator/templates.py)。

| intent | 典型流水线 | 当前输出 |
| --- | --- | --- |
| `ppt_report` | `search` + `doc_analyst` -> `writer` -> `ppt_render` | `.pptx` |
| `research_report` | `deep_research` + `doc_analyst` -> `markdown_render` | `.md` |
| `data_analysis` | `doc_analyst` -> `data_analyst` -> `writer` | 文本/结构化结果 |
| `quick_question` | `general_chat` | 直接文本回答 |
| `translate_doc` | `doc_analyst` -> `translator` -> `word_render` | `.docx` |
| `image_to_visio` | `image_to_diagram` -> `visio_render` | `.json` |
| `image_generation` | `image_gen` | 图片文件 |

补充说明：

- `ppt_report` 在 [orchestrator/runner.py](orchestrator/runner.py) 中仍保留了一条兼容旧版 PPT 的专用分支。
- 当任务不匹配模板时，`planner` 会基于注册表摘要自由规划步骤。

## 流式任务协议

当前主协议不是旧版 `/ws`，而是任务式 API。

### 1. 上传附件

```bash
curl -F "file=@/absolute/path/to/report.pdf" http://localhost:8000/upload
```

返回示例：

```json
{
  "path": "/absolute/server/path/uploads/abcd1234_report.pdf",
  "name": "report.pdf"
}
```

### 2. 创建任务

```bash
curl -X POST http://localhost:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "task": "分析这份附件并给我一份研究结论",
    "user_id": "alice",
    "mode": "auto",
    "thinking_level": "high",
    "file_paths": ["/absolute/server/path/uploads/abcd1234_report.pdf"]
  }'
```

返回示例：

```json
{
  "task_id": "task_1234567890ab",
  "status": "queued"
}
```

### 3. 查询任务快照

```bash
curl http://localhost:8000/tasks/task_1234567890ab
```

任务快照包含：

- `status`
- `route_name`
- `route_reason`
- `route_confidence`
- `pending_question`
- `result`
- `error`
- `last_seq`

### 4. 订阅事件流

```bash
curl -N http://localhost:8000/tasks/task_1234567890ab/events
```

SSE 事件支持 `Last-Event-ID` 和 `after_seq` 重放。当前会出现的用户态事件类型包括：

| type | 含义 |
| --- | --- |
| `start` | 任务开始 |
| `step` | 过程日志 |
| `question` | 任务向用户追问 |
| `user_reply` | 用户回复已写入任务上下文 |
| `result_delta` | 流式增量文本 |
| `file` | 生成了可下载文件 |
| `result` | 最终成功结果 |
| `error` | 最终失败结果 |
| `monitoring` | 本次任务的监控摘要 |

### 5. 回复追问

当任务状态变成 `waiting_for_user` 时：

```bash
curl -X POST http://localhost:8000/tasks/task_1234567890ab/reply \
  -H 'Content-Type: application/json' \
  -d '{"answer": "更关注工程实现与实验效果"}'
```

### 6. 下载生成文件

```bash
curl -O http://localhost:8000/download/your_file.docx
```

## WebSocket 兼容路径

[main.py](main.py) 仍保留 `/ws`，用于兼容旧客户端，但新客户端应优先使用：

- `POST /tasks`
- `GET /tasks/{task_id}/events`

## 用户记忆

记忆模块位于 [memory/store.py](memory/store.py)。

默认存储路径：

```text
data/memory/<user_id>/
├── profile.md
├── summaries/YYYY/YYYY-MM.md
└── timeline/YYYY/MM/YYYY-MM-DD.md
```

当前逻辑：

- `orchestrator` 路由开始前会根据 `user_id` 召回相关历史片段。
- 任务结束后会把当前任务与结果写入时间线和月度摘要。
- 同时尝试抽取稳定画像，更新长期 profile。

如果不希望使用默认目录，可设置 `LOCAL_AGENT_MEMORY_DIR`。

## 目录结构

```text
.
├── main.py                    # FastAPI 入口
├── task_runtime.py            # 任务持久化、SSE、回复恢复
├── task_dispatcher.py         # auto/chat/agent 路由判定
├── task_interaction.py        # 任务内 ask_user 交互桥接
├── models.py                  # 模型与 provider 配置中心
├── registry.py                # 统一能力注册表
├── logger.py                  # 结构化日志与监控汇总
├── content_utils.py           # 输出文本提取与清洗
│
├── orchestrator/
│   ├── planner.py             # intent 分类与自由规划
│   ├── runner.py              # 编排总入口
│   ├── scheduler.py           # 依赖图执行
│   └── templates.py           # 预设模板
│
├── agents/
├── tools/
├── renderers/
├── ppt_engine/
│
├── memory/                    # 记忆模块
├── static/index.html          # 当前前端页面
├── old/                       # 旧版前端与单体 agent 备份
├── uploads/                   # 上传文件
├── outputs/                   # 生成文件
├── data/                      # SQLite 与记忆数据
└── tests/                     # 单元测试
```

## 安装与运行

### 环境要求

- Python 3.13
- `uv` 或 `pip`

### 安装依赖

推荐使用项目虚拟环境：

```bash
uv venv .venv
source .venv/bin/activate

uv pip install -r requirements.txt
uv pip install langchain-google-genai langchain-tavily

playwright install chromium
```

说明：

- 当前代码直接 import `langchain_google_genai` 和 `langchain_tavily`，因此新环境建议显式安装。
- 如果只做非浏览器能力开发，`playwright install chromium` 仍建议执行，因为 `deep_research` 会使用 `browser-use`。

### 最小可用配置

默认模型配置见 [models.py](models.py)。按当前仓库默认值，至少需要：

```bash
CO_API_KEY=your_proxy_key
```

服务启动时会自动调用 `load_dotenv()` 读取仓库根目录的 `.env` 文件，也可以直接通过 shell 环境变量注入。

常见可选配置：

```bash
# 搜索
TAVILY_API_KEY=your_tavily_key
BRAVE_SEARCH_API_KEY=your_brave_key

# Provider / 模型
OPENAI_API_KEY=your_openai_key
GOOGLE_API_KEY=your_google_key
MOONSHOT_API_KEY=your_moonshot_key
ANTHROPIC_API_KEY=your_anthropic_key
YESCODE_GEMINI_BASE_URL=https://co.yes.vg/gemini

# 运行时
LOCAL_AGENT_MEMORY_DIR=data/memory
LLM_TIMEOUT_SECONDS=7200
LLM_MAX_RETRIES=2

# 图片生成
IMAGE_GEN_API_URL=https://co.yes.vg/v1/chat/completions
IMAGE_GEN_MODEL=gemini-3.1-flash-image-landscape
```

### 启动服务

```bash
uvicorn main:app --reload --port 8000
```

启动后访问：

- `http://localhost:8000/`

## 当前默认模型映射

当前角色到模型的默认映射定义在 [models.py](models.py)：

| role | provider | model |
| --- | --- | --- |
| `orchestrator` | `proxy` | `gpt-5.4` |
| `search` | `proxy` | `gpt-5.4` |
| `doc_analyst` | `google_proxy` | `gemini-3.1-pro-preview-customtools` |
| `writer` | `proxy` | `gpt-5.4` |
| `deep_research` | `google_proxy` | `gemini-3.1-pro-preview-customtools` |
| `data_analyst` | `proxy` | `gpt-5.4` |

如果要切换模型或 provider，直接修改 `PROVIDERS` 和 `MODEL_CONFIGS`。

## 监控与调试

除了业务事件流，后端还提供两个调试接口：

| 接口 | 说明 |
| --- | --- |
| `GET /api/verbose` / `POST /api/verbose` | 查看或切换 verbose 输出 |
| `POST /api/log-level` | 动态调整日志级别 |

任务结束时会追加一个 `monitoring` 事件，内容来自 [logger.py](logger.py) 的采集汇总，包括耗时、LLM 调用数、token 统计和错误信息。

## 测试

当前测试位于 [tests](tests)：

- `test_task_streaming.py` 覆盖任务提交、SSE、追问回复、HTTP 接口
- `test_models.py` 覆盖模型适配层
- `test_scheduler.py`、`test_orchestrator_runner.py` 覆盖编排执行
- `test_deep_research.py`、`test_word_renderer.py`、`test_markdown_renderer.py` 覆盖核心能力

运行：

```bash
python -m unittest discover -s tests
```

## 旧版代码说明

这些路径仍保留，但不再是主执行链路：

- [old/agent.py](old/agent.py)
- [old/index.html](old/index.html)
- [agents/orchestrator.py](agents/orchestrator.py)

当前应以 [orchestrator/runner.py](orchestrator/runner.py) 和 [task_runtime.py](task_runtime.py) 为准。
