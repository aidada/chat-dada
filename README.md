# chat dada / Local Agent

一个基于 FastAPI、LangGraph 和流式任务运行时的通用多智能体平台。支持聊天、深度研究（含层级分解与并行执行）、专利撰写、归零报告、PPT 生成的任务系统。

前端页面位于 [static/index.html](static/index.html)，后端入口位于 [main.py](main.py)。

## 当前能力概览

- 任务提交采用 `POST /tasks`，事件流采用 `GET /tasks/{task_id}/events` 的 SSE 模式，支持断线重放。
- 任务状态和事件持久化到 **PostgreSQL**，跨实例事件广播通过 **Redis Pub/Sub**。
- 支持人机交互追问。任务运行中可进入 `waiting_for_user`，再通过 `POST /tasks/{task_id}/reply` 继续。
- 通过 `task_platform/root_graph.py` 统一调度，按领域路由到 `domain_agents/` 下的专用图（research、patent、zero_report、ppt）。
- 领域 agent 执行前会从 `data/memory/` 召回用户历史片段，任务结束后回写长期记忆。
- 深度研究支持三种图模式：简单循环、层级分解、并行工作者，配合三层上下文管理、进度追踪与外部研究记忆。
- 支持多种输出形态：直接回答、PPT、图片。
- LangSmith tracing 集成：启动时自动验证连接，每次 graph 执行注入业务 metadata，支持运行时动态开关。

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
TaskService / TaskRunStore (runtime/task_runtime.py)
        │              ├── PostgreSQL: task_runs + task_events
        │              └── Redis Pub/Sub: SSE 广播
        ▼
Task Dispatcher (runtime/task_dispatcher.py)
        │
        ├── general_chat  ──→  capabilities/general_chat.py
        └── orchestrator  ──→  task_platform/root_graph.py
                                    │
                          ┌─────────┼──────────┬──────────────┬──────┐
                          ▼         ▼          ▼              ▼      ▼
                      research   patent   zero_report        ppt   general_chat
                      (domain_agents/)                              (fallback)
```

### 调度流程

1. `task_dispatcher.py` 按关键词和模式将任务路由为 `general_chat` 或 `orchestrator`
2. `orchestrator` 路由进入 `task_platform/root_graph.py`（LangGraph 状态图）
3. `root_graph` 通过 `router.py` 决定 `execution_path`：
   - `research` → `domain_agents/research/`
   - `patent` → `domain_agents/patent/`
   - `zero_report` → `domain_agents/zero_report/`
   - `ppt` → `domain_agents/ppt/`
   - `general_chat` → `capabilities/general_chat.py`（直接问答）
   - `needs_clarification` → 向用户追问后重新路由

## 任务模式

`POST /tasks` 支持 3 种模式：

| mode | 行为 |
| --- | --- |
| `auto` | 默认模式。按附件和关键词在 `general_chat` 与 `orchestrator` 之间自动路由 |
| `chat` | 强制走直接对话，不允许附件 |
| `agent` | 强制走编排器 |

`thinking_level` 当前支持 `low` / `medium` / `high`，会通过 [core/models.py](core/models.py) 映射到底层模型的推理强度参数。

## Domain Agents

所有领域 agent 统一注册在 [task_platform/domain_registry.py](task_platform/domain_registry.py)。

| 领域 | 入口 | 别名 | 说明 |
| --- | --- | --- | --- |
| `research` | `domain_agents/research/agent.py` | `deep_research` | 结构化研究报告，含证据链、引用管理、评审门控、并行工作者 |
| `patent` | `domain_agents/patent/agent.py` | `专利` | 专利撰写，含权利要求树、技术方案、评审门控 |
| `zero_report` | `domain_agents/zero_report/agent.py` | `归零`, `postmortem` | 归零报告生成，含行动矩阵、评审门控 |
| `ppt` | `domain_agents/ppt/agent.py` | `幻灯片`, `powerpoint` | PPT 生成，含大纲规划、搜索、文档分析、幻灯片撰写、渲染 |

### Tools

| 名称 | 入口 | 说明 |
| --- | --- | --- |
| `web_search` | `tools.web_search:run` | Tavily 搜索 |
| `brave_search` | `tools.brave_search:run` | Brave 搜索 |
| `academic_search` | `tools.academic_search:run` | Semantic Scholar + arXiv |
| `exa_search` | `tools.exa_search:run` | Exa AI 深度搜索 |
| `translator` | `tools.translator:run` | LLM 翻译 |
| `summarizer` | `tools.summarizer:run` | LLM 摘要 |
| `code_executor` | `tools.code_executor:run` | Python 沙箱执行 |
| `image_gen` | `tools.image_gen:run` | 文本生成图片 |
| `image_to_diagram` | `tools.image_to_diagram:run` | 图片转结构化图表 |
| `research_notes` | `tools.research_notes` | 研究笔记持久化存储与召回 |

### Renderers

| 名称 | 入口 | 输出 |
| --- | --- | --- |
| `ppt_render` | `ppt_engine.renderer:render_pptx` | `.pptx` |

## 深度研究系统

深度研究是系统中最复杂的领域 agent，实现位于 `domain_agents/research/` 子包，支持三种图执行模式：

| 模式 | 流程 | 适用场景 |
| --- | --- | --- |
| Simple | planner → tools loop → finish | 简单问题，单线研究 |
| Hierarchical | plan → route subtasks → individual research → judge → synthesize | 复杂多维度问题 |
| Parallel | plan → parallel workers → synthesis | 独立子问题可并行 |

子包结构（`domain_agents/research/`）：

| 文件 | 职责 |
| --- | --- |
| `agent.py` | 入口函数、领域 runner |
| `config.py` | 状态定义、研究配置、报告模板（default / academic_paper_guidance） |
| `graphs.py` | LangGraph 图构建（simple / hierarchical / parallel） |
| `prompts.py` | 系统提示词与报告模板选择 |
| `tools.py` | 领域工具（web_search / academic_search / browser / ask_user / research_notes） |
| `schemas.py` | 领域数据模型 |
| `renderers.py` | Markdown 渲染 |
| `reviewers.py` | 评审门控 |
| `worker.py` | 并行研究工作者（Wave-based 并行执行，最多 3 个并发） |
| `utils.py` | 报告改写与辅助函数 |
| `legacy_runner.py` | 遗留兼容入口 |

依赖的 capabilities 模块：

| 模块 | 文件 | 职责 |
| --- | --- | --- |
| 研究规划器 | `capabilities/planner.py` | 将问题分解为 2-5 个带依赖的子任务 |
| 上下文管理器 | `capabilities/context_manager.py` | 三层压缩：最近 2 步保留原文，旧步骤取 200 字摘要，全局研究摘要 |
| 进度追踪器 | `capabilities/progress_tracker.py` | 记录已完成搜索、关键发现（FIFO, max 10）、剩余缺口 |
| 研究记忆 | `capabilities/memory.py` | 文件系统外部记忆，支持发现/摘要/检查点/恢复 |
| 引用管理 | `capabilities/citation_manager.py` | URL 去重、脚注/参考文献渲染 |
| 证据库 | `capabilities/evidence_store.py` | 结构化证据收集与按类型过滤 |
| 预算策略 | `capabilities/budget_policy.py` | LLM 调用预算管控 |
| 评审门控 | `capabilities/review_gates.py` | 领域评审质量门控 |
| 重试策略 | `capabilities/retry_policy.py` | 工具调用重试策略 |
| PPT 能力 | `capabilities/ppt_capability.py` | 跨域 PPT 管线（storyline → DSL → 渲染） |
| 浏览器工具包 | `capabilities/toolkits/browser_toolkit.py` | Browser-use 封装 |

报告模板：

| profile | 说明 |
| --- | --- |
| `default` | 问题导向报告，含 6 个必需章节（直接结论、证据链、机理与成立条件等） |
| `academic_paper_guidance` | 期刊论文准备指南，含 8 个章节（文献综述正文、研究空白、写作建议等） |

运行参数：最多 15 步，每 5 步保存检查点，每 6 步生成中间摘要。

## PPT 生成系统

PPT 领域 agent 位于 `domain_agents/ppt/`，实现完整的幻灯片生成管线：

| 文件 | 职责 |
| --- | --- |
| `agent.py` | 入口 runner：大纲规划 → 并行搜索/文档分析 → 撰写 → 渲染 |
| `search_agent.py` | Web 搜索与页面抓取 |
| `doc_agent.py` | 读取并分析 PDF/文本/附件 |
| `writer_agent.py` | 生成幻灯片 DSL JSON |

流程：LLM 生成大纲 → 并行执行搜索和文档分析 → Writer 生成 Slide DSL → `ppt_engine/renderer.py` 渲染为 `.pptx`。

## 流式任务协议

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
curl -O http://localhost:8000/download/your_file.pptx
```

### 7. 其他接口

| 接口 | 说明 |
| --- | --- |
| `GET /tasks/{task_id}/artifacts` | 任务产物引用列表 |
| `GET /tasks/{task_id}/review` | 评审结果与预算信息 |
| `GET /tasks/{task_id}/provenance` | 溯源（领域、产物、关键事件） |
| `GET /tasks/{task_id}/trace` | 监控追踪数据 |
| `GET /tasks/{task_id}/replay` | 完整快照 + 全部事件重放 |
| `GET /api/traces` | 历史监控列表 |
| `GET/POST /conversations` | 对话管理（列表、创建） |
| `PATCH/DELETE /conversations/{id}` | 对话更新、删除 |
| `GET /conversations/{id}/entries` | 对话条目列表 |

## WebSocket 兼容路径

[main.py](main.py) 仍保留 `/ws`，用于兼容旧客户端，但新客户端应优先使用：

- `POST /tasks`
- `GET /tasks/{task_id}/events`

## 用户记忆

记忆模块位于 [storage/user_store_v2.py](storage/user_store_v2.py)。

默认存储路径：

```text
data/memory/<user_id>/
├── profile.md
├── summaries/YYYY/YYYY-MM.md
└── timeline/YYYY/MM/YYYY-MM-DD.md
```

当前逻辑：

- 领域 agent 执行前会根据 `user_id` 召回相关历史片段。
- 任务结束后会把当前任务与结果写入时间线和月度摘要。
- 同时尝试抽取稳定画像，更新长期 profile。

如果不希望使用默认目录，可设置 `LOCAL_AGENT_MEMORY_DIR`。

## 目录结构

```text
.
├── main.py                        # FastAPI 入口（唯一根级模块）
│
├── task_platform/                 # LangGraph 任务平台
│   ├── root_graph.py              # 根状态图（路由 + 调度）
│   ├── router.py                  # 意图路由（研究/专利/零号报告/PPT/通用/追问）
│   ├── state.py                   # 根图状态定义
│   ├── domain_registry.py         # 领域 agent 注册表
│   ├── renderer_registry.py       # 领域渲染器注册表
│   ├── streaming.py               # LangGraph 流事件序列化
│   ├── interrupts.py              # 人机交互中断
│   ├── tracing.py                 # 追踪元数据构建
│   └── memory_interfaces.py       # 记忆提供者抽象接口
│
├── domain_agents/                 # 领域 agent（每个领域一个子包）
│   ├── research/                  # 研究报告领域
│   │   ├── agent.py               # 入口 runner
│   │   ├── config.py              # 状态定义、研究配置
│   │   ├── graphs.py              # LangGraph 图构建（simple / hierarchical / parallel）
│   │   ├── prompts.py             # 系统提示词
│   │   ├── schemas.py             # 领域数据模型
│   │   ├── tools.py               # 领域工具
│   │   ├── renderers.py           # Markdown 渲染
│   │   ├── reviewers.py           # 评审门控
│   │   ├── worker.py              # 并行研究工作者
│   │   ├── utils.py               # 报告改写辅助
│   │   └── legacy_runner.py       # 遗留兼容入口
│   ├── patent/                    # 专利撰写领域（同构）
│   ├── zero_report/               # 零号报告领域（同构）
│   └── ppt/                       # PPT 生成领域
│       ├── agent.py               # 入口 runner（大纲→搜索→分析→撰写→渲染）
│       ├── search_agent.py        # Web 搜索与页面抓取
│       ├── doc_agent.py           # 文档分析
│       └── writer_agent.py        # 幻灯片 DSL 生成
│
├── core/                          # 基础设施层
│   ├── models.py                  # 模型与 provider 配置中心
│   ├── registry.py                # 统一能力注册表
│   ├── logger.py                  # 结构化日志与监控汇总
│   ├── content_utils.py           # 输出文本提取与清洗
│   ├── langsmith_config.py        # LangSmith 开关、连接验证、run metadata
│   └── r2_storage.py              # R2 对象存储
│
├── runtime/                       # 任务运行时
│   ├── task_runtime.py            # 任务持久化 (PostgreSQL)、SSE (Redis Pub/Sub)
│   ├── task_dispatcher.py         # auto/chat/agent 路由判定
│   ├── task_interaction.py        # 任务内 ask_user 交互桥接
│   └── conversation_context.py    # 对话上下文管理
│
├── storage/                       # 持久化存储
│   ├── user_store_v2.py           # 用户记忆 V2
│   └── user_models.py             # 用户数据模型
│
├── capabilities/                  # 可复用能力模块
│   ├── general_chat.py            # 直接问答（流式文本增量）
│   ├── context_manager.py         # 三层上下文管理 (raw → compact → summary)
│   ├── progress_tracker.py        # 研究进度与缺口追踪
│   ├── memory.py                  # 研究外部文件记忆 (data/research/)
│   ├── planner.py                 # 研究子任务层级分解
│   ├── citation_manager.py        # 引用管理与脚注渲染
│   ├── evidence_store.py          # 结构化证据收集
│   ├── budget_policy.py           # LLM 调用预算策略
│   ├── review_gates.py            # 评审质量门控
│   ├── retry_policy.py            # 工具重试策略
│   ├── ppt_capability.py          # 跨域 PPT 管线
│   └── toolkits/
│       └── browser_toolkit.py     # Browser-use 封装
│
├── tools/                         # 工具实现
│   ├── web_search.py              # Tavily 搜索
│   ├── brave_search.py            # Brave 搜索
│   ├── academic_search.py         # 学术检索 (Semantic Scholar + arXiv)
│   ├── exa_search.py              # Exa AI 深度搜索
│   ├── translator.py              # LLM 翻译
│   ├── summarizer.py              # LLM 摘要
│   ├── code_executor.py           # Python 沙箱
│   ├── image_gen.py               # 图片生成
│   ├── image_to_diagram.py        # 图片转图表
│   └── research_notes.py          # 研究笔记持久化
│
├── ppt_engine/                    # PPT 渲染引擎
│   ├── dsl_schema.py              # 幻灯片 DSL 定义
│   └── renderer.py                # PPTX 渲染
│
├── scripts/
│   └── init.sql                   # PostgreSQL 建表脚本
│
├── docs/                          # 设计文档与计划
├── static/index.html              # 当前前端页面
├── uploads/                       # 上传文件
├── outputs/                       # 生成文件
├── data/                          # 运行时数据
│   ├── memory/                    # 用户记忆
│   ├── research/                  # 研究任务外部记忆
│   ├── patent/                    # 专利任务数据
│   └── zero_report/               # 零号报告数据
├── logs/                          # 日志输出
└── tests/                         # 单元测试
```

## 安装与运行

### 环境要求

- Python 3.13
- PostgreSQL
- Redis
- `uv` 或 `pip`

### 基础设施

启动 PostgreSQL 和 Redis 后，执行建表脚本：

```bash
psql -U chatdada -d chatdada -f scripts/init.sql
```

默认连接地址（可通过环境变量覆盖）：

```bash
DATABASE_URL=postgresql://chatdada:chatdada@localhost:5432/chatdada
REDIS_URL=redis://localhost:6379
```

### 安装依赖

推荐使用项目虚拟环境：

```bash
uv venv .venv
source .venv/bin/activate

uv pip install -e .

playwright install chromium
```

说明：

- 依赖已定义在 `pyproject.toml`，使用 `uv pip install -e .` 即可安装全部依赖。
- `playwright install chromium` 建议执行，因为研究领域 agent 会使用 `browser-use`。

### 最小可用配置

默认模型配置见 [core/models.py](core/models.py)。按当前仓库默认值，至少需要：

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

# 数据库
DATABASE_URL=postgresql://chatdada:chatdada@localhost:5432/chatdada
REDIS_URL=redis://localhost:6379

# 运行时
LOCAL_AGENT_MEMORY_DIR=data/memory
RESEARCH_DATA_DIR=data/research
LLM_TIMEOUT_SECONDS=7200
LLM_MAX_RETRIES=2

# LangSmith（可选 — 启用后自动注入 tracing metadata）
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=your_langsmith_key
LANGSMITH_PROJECT=chat-dada

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

当前角色到模型的默认映射定义在 [core/models.py](core/models.py)：

| role | provider | model |
| --- | --- | --- |
| `orchestrator` | `proxy` | `gpt-5.4` |
| `search` | `proxy` | `gpt-5.4` |
| `doc_analyst` | `google_proxy` | `gemini-3.1-pro-preview-customtools` |
| `writer` | `proxy` | `gpt-5.4` |
| `deep_research` | `google_proxy` | `gemini-3.1-pro-preview-customtools` |

如果要切换模型或 provider，直接修改 `PROVIDERS` 和 `MODEL_CONFIGS`。

## 数据库 Schema

任务持久化使用 PostgreSQL，建表脚本位于 [scripts/init.sql](scripts/init.sql)。

```sql
-- task_runs: 任务主表
task_id, user_id, status, task_text, mode, thinking_level,
route_name, route_reason, route_confidence,
request_payload (JSONB), pending_question (JSONB),
result_text, error_text,
created_at, started_at, finished_at, updated_at

-- task_events: 事件流表
task_id, seq, event_type, payload (JSONB), created_at
```

索引：`task_runs(user_id)`、`task_runs(status)`、`task_runs(created_at DESC)`、`task_events(task_id, seq)`。

服务启动时会自动标记中断的任务（queued/running/waiting_for_user）为 failed。

## 监控与调试

| 接口 | 说明 |
| --- | --- |
| `GET /api/langsmith` | 返回 LangSmith tracing 当前状态与连接验证结果 |
| `POST /api/langsmith` | `{"enabled": bool}` 动态开关 LangSmith tracing（无需重启） |
| `GET /api/verbose` / `POST /api/verbose` | 查看或切换 verbose 输出 |
| `POST /api/log-level` | 动态调整日志级别 |

服务启动时会自动验证 LangSmith 连接，按结果打印 info/warning 日志。每次 graph 执行会自动注入 `task_id`、`user_id`、`domain`、`mode` 到 LangSmith run metadata 与 tags，可在 LangSmith 控制台按业务维度筛选。

任务结束时会追加一个 `monitoring` 事件，内容来自 [core/logger.py](core/logger.py) 的采集汇总，包括耗时、LLM 调用数、token 统计和错误信息。

## 测试

当前测试位于 [tests](tests)：

- `test_platform_refactor.py` 覆盖新架构（流适配、领域路由、中断恢复、引用管理、证据收集）
- `test_task_streaming.py` 覆盖任务提交、SSE、追问回复、HTTP 接口
- `test_models.py` 覆盖模型适配层
- `test_deep_research.py` 覆盖深度研究 agent
- `test_context_manager.py` 覆盖三层上下文管理
- `test_progress_tracker.py` 覆盖进度追踪
- `test_research_memory.py` 覆盖研究外部记忆
- `test_research_planner.py` 覆盖研究子任务分解
- `test_research_worker.py` 覆盖并行研究工作者
- `test_research_notes.py` 覆盖研究笔记工具
- `test_conversation_context.py` 覆盖对话上下文
- `test_user_models.py` 覆盖用户数据模型
- `test_user_store_v2.py` 覆盖用户记忆 V2
- `test_exa_search.py` 覆盖 Exa 搜索工具
- `test_logger.py` 覆盖日志系统

运行：

```bash
python -m pytest tests/ -v
```
