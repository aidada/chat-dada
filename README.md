# chat-dada

`chat-dada` 是一个本地多智能体任务平台，当前主线架构基于 FastAPI、LangGraph、deepagents、PostgreSQL、Redis。

它支持两类核心工作流：

- 直接对话：轻量问答、解释、翻译、改写
- 编排任务：深度研究、专利草稿、归零报告、PPT 生成

当前前端页面在 `static/index.html`，HTTP 入口在 `main.py`。

## 重构后的主干架构

这次重构后，仓库已经形成比较清晰的 6 层分工：

1. `main.py`
   HTTP / SSE / WebSocket 兼容入口，负责上传、建任务、事件订阅、对话管理、调试开关。
2. `runtime/`
   任务生命周期、PostgreSQL 持久化、Redis Pub/Sub、任务恢复、用户回复恢复执行、会话上下文注入。
3. `task_platform/`
   根状态图、领域路由、流式事件适配、追问中断桥接、trace metadata。
4. `workflows/`
   通用动态编排层，定义 `DomainSpec`、策略选择器，以及 `planning / sequential / parallel / iterative` 执行图。
5. `domain_agents/`
   各领域的业务实现。研究域已经接入新的动态 orchestrator，其他领域保持独立 runner。
6. `capabilities/` / `tools/` / `storage/` / `core/`
   共用能力、外部工具、用户记忆、模型配置、日志与基础设施。

## 请求执行链路

```text
Browser / API Client
        │
        ├── POST /upload
        ├── POST /tasks
        ├── GET  /tasks/{task_id}
        ├── GET  /tasks/{task_id}/events
        └── POST /tasks/{task_id}/reply
        │
        ▼
FastAPI (main.py)
        │
        ▼
TaskService (runtime/task_runtime.py)
        ├── PostgreSQL: task_runs / task_events / conversations
        ├── Redis Pub/Sub: SSE 广播与重放
        └── ConversationContextBuilder: 多轮上下文注入
        │
        ▼
Dispatcher (runtime/task_dispatcher.py)
        ├── general_chat  -> capabilities/general_chat.py
        └── orchestrator  -> task_platform/root_graph.py
                               │
                               ├── research     -> domain_agents/research/orchestrated.py
                               │                    └── workflows/orchestrator.py
                               ├── patent       -> domain_agents/patent/orchestrated.py
                               ├── zero_report  -> domain_agents/zero_report/orchestrated.py
                               └── ppt          -> domain_agents/ppt/orchestrated.py
```

## 当前领域执行现状

| 领域 | 当前注册入口 | 执行方式 | 说明 |
| --- | --- | --- | --- |
| `general_chat` | `capabilities/general_chat.py` | 直接 LLM 对话 | 支持流式输出、会话上下文注入 |
| `research` | `domain_agents/research/orchestrated.py` | 新的动态 orchestrator | 使用 `workflows/orchestrator.py`，按状态在 `planning / sequential / parallel / iterative` 间切换 |
| `patent` | `domain_agents/patent/orchestrated.py` | 动态 orchestrator | 使用 `workflows/orchestrator.py`，默认顺序执行，评审失败时可迭代优化 |
| `zero_report` | `domain_agents/zero_report/orchestrated.py` | 动态 orchestrator | 使用 `workflows/orchestrator.py`，通常先 planning 再 sequential / iterative |
| `ppt` | `domain_agents/ppt/orchestrated.py` | orchestrator + deterministic render | 内容生成走 orchestrator，`.pptx` 渲染在 wrapper 中完成 |

补充说明：

- `research / patent / zero_report / ppt` 当前生产注册表都默认走 `*_orchestrated.py`。
- `domain_agents/*/agent.py` 仍保留为 legacy / direct-run 兼容实现，其中 patent / zero_report 包含确定性 artifact scaffold，可作为回退或本地调试路径。

## 目录结构

```text
.
├── main.py                         # FastAPI 入口
├── core/                           # 模型、日志、LangSmith、对象存储等基础设施
├── runtime/                        # TaskService、任务恢复、对话上下文、交互桥接
├── task_platform/                  # 根图、路由、流式事件适配、trace、interrupt
├── workflows/                      # 通用动态编排层（DomainSpec + 策略图）
├── domain_agents/                  # 领域实现
│   ├── research/
│   ├── patent/
│   ├── zero_report/
│   └── ppt/
├── capabilities/                   # 可复用能力模块
├── tools/                          # 搜索、翻译、图片、代码执行、研究笔记等工具
├── storage/                        # 用户记忆 V2 数据结构与存储逻辑
├── ppt_engine/                     # PPT DSL 与 PPTX 渲染
├── static/                         # 当前前端页面
├── scripts/                        # 数据库初始化脚本
├── tests/                          # 重构后的测试
├── docs/                           # 设计文档、实施计划、待办
├── data/                           # 运行期数据
├── uploads/                        # 上传文件
└── outputs/                        # 生成产物
```

### 关键文件定位

| 文件 | 作用 |
| --- | --- |
| `main.py` | API、SSE、下载、上传、会话接口、调试接口 |
| `runtime/task_runtime.py` | `TaskService`、事件落库、Redis 广播、任务恢复、reply 恢复 |
| `runtime/conversation_context.py` | 多轮上下文策略：raw / summary / summary+retrieval |
| `task_platform/root_graph.py` | 根 LangGraph：路由、追问、领域运行、结果持久化 |
| `task_platform/domain_registry.py` | 领域注册中心 |
| `task_platform/streaming.py` | LangGraph stream part -> 统一 UI 事件 |
| `workflows/orchestrator.py` | 通用动态执行图，按状态自动选策略 |
| `workflows/strategy_selector.py` | 规则优先、LLM 兜底的策略选择器 |
| `domain_agents/research/orchestrated.py` | 当前研究域主入口 |
| `domain_agents/research/agent.py` | 旧版研究图、worker、evidence/citation 持久化 |
| `storage/user_store_v2.py` | 用户记忆召回、归档、旧版 profile 迁移 |

## 共享能力与工具

### capabilities

- `general_chat.py`：直接对话与流式输出
- `planner.py`：研究类任务分解
- `context_manager.py`：研究上下文压缩
- `progress_tracker.py`：研究进度与缺口跟踪
- `memory.py`：研究任务级外部记忆
- `citation_manager.py`：引用管理
- `evidence_store.py`：结构化证据收集
- `review_gates.py`：领域评审门控
- `budget_policy.py`：预算策略
- `ppt_capability.py`：跨域 PPT 能力
- `toolkits/browser_toolkit.py`：浏览器任务封装

### tools

- `web_search.py`：Tavily 搜索
- `brave_search.py`：Brave 搜索
- `academic_search.py`：学术搜索
- `exa_search.py`：Exa 深搜
- `translator.py`：翻译
- `summarizer.py`：摘要
- `code_executor.py`：Python 执行
- `image_gen.py`：图片生成
- `image_to_diagram.py`：图片转图表/流程图
- `research_notes.py`：研究笔记持久化

说明：

- 研究、专利、归零报告三个领域的可用工具集合，最终都从 `domain_agents/research/tools.py` 的 `CORE_TOOLS` 扩展而来。
- 浏览器能力通过 `browser-use` 封装在 `capabilities/toolkits/browser_toolkit.py`。

## 会话上下文与用户记忆

### Conversation Context

`runtime/conversation_context.py` 负责把历史对话压缩后注入模型提示词，当前策略是：

- `<= 5` 轮：直接拼原始对话
- `6 - 20` 轮：滚动摘要 + 最近 3 轮原文
- `> 20` 轮：滚动摘要 + 最近 3 轮原文 + pgvector 语义检索 top-k

### User Memory V2

用户记忆位于 `data/memory/<user_id>/`，当前结构已经从旧版 `profile.md` 迁移到结构化 JSON：

```text
data/memory/<user_id>/
├── facts.json
├── pending_facts.json
├── projects.json
├── meta.json
└── timeline/
    ├── hot/
    │   └── YYYY-MM-DD.md
    └── warm/
        └── YYYY-MM.md
```

当前行为：

- 任务执行前按 `user_id` 召回 facts、active projects、stale projects
- 任务完成后写入 hot timeline，并尝试抽取新的稳定事实
- pending facts 超阈值时自动触发合并
- 旧版 `profile.md` 会在首次访问时自动迁移

## 运行时数据落点

```text
data/
├── memory/            # 用户记忆 V2
├── research/          # 研究任务数据、检查点、报告
├── patent/            # 专利任务结构化产物
└── zero_report/       # 归零报告结构化产物

uploads/               # 用户上传文件
outputs/               # 最终输出文件（pptx/docx/xlsx/jpg/vsdx 等）
```

## API 概览

### 任务与文件

| 接口 | 说明 |
| --- | --- |
| `POST /upload` | 上传附件，返回服务端路径与 `/uploads/...` 地址 |
| `GET /uploads/{filename}` | 访问已上传文件 |
| `GET /download/{filename}` | 下载生成产物 |
| `POST /tasks` | 创建任务 |
| `GET /tasks/{task_id}` | 获取任务快照 |
| `GET /tasks/{task_id}/events` | SSE 事件流，支持 `Last-Event-ID` / `after_seq` |
| `POST /tasks/{task_id}/reply` | 回复追问并恢复执行 |

### 任务诊断与追踪

| 接口 | 说明 |
| --- | --- |
| `GET /tasks/{task_id}/artifacts` | 产物列表 |
| `GET /tasks/{task_id}/review` | review / budget 信息 |
| `GET /tasks/{task_id}/provenance` | 关键事件与产物溯源 |
| `GET /tasks/{task_id}/trace` | 监控摘要 |
| `GET /tasks/{task_id}/replay` | 快照 + 全量事件 |
| `GET /api/traces` | 进程内 trace 历史 |

### 会话与调试

| 接口 | 说明 |
| --- | --- |
| `GET/POST /conversations` | 列表 / 创建对话 |
| `PATCH/DELETE /conversations/{id}` | 更新 / 删除对话 |
| `GET /conversations/{id}/entries` | 对话条目 |
| `GET/POST /api/langsmith` | LangSmith 状态与开关 |
| `GET/POST /api/verbose` | verbose 日志开关 |
| `POST /api/log-level` | 动态修改日志级别 |
| `GET /` | 当前前端页面 |

### SSE 常见事件类型

- `start`
- `step`
- `token`
- `result_delta`
- `question`
- `user_reply`
- `file`
- `result`
- `error`
- `monitoring`

### WebSocket 兼容路径

`/ws` 仍保留，但只是兼容旧客户端。新客户端应优先使用：

- `POST /tasks`
- `GET /tasks/{task_id}/events`

## 数据库与基础设施

### PostgreSQL

初始化脚本在 `scripts/init.sql`，当前核心表：

- `task_runs`
- `task_events`
- `conversations`

另外：

- `task_runs.conversation_id` 用于绑定会话
- `conversations.context_summary / summary_through_seq` 用于会话摘要缓存
- `task_events.embedding vector(1536)` 用于长对话语义检索

### Redis

Redis 只承担实时广播职责：

- 任务执行中写入事件
- SSE 客户端通过 Pub/Sub 收到最新事件
- 断线后再从 PostgreSQL 事件表补历史

## 安装与运行

### 环境要求

- Python 3.13
- PostgreSQL
- Redis
- 推荐 `uv`

### 本地开发

```bash
uv venv .venv
source .venv/bin/activate

uv pip install -e .
playwright install chromium
```

如果希望和容器环境完全一致，也可以直接安装锁定依赖：

```bash
uv pip install -r requirements.txt
```

初始化数据库：

```bash
psql -U chatdada -d chatdada -f scripts/init.sql
```

启动服务：

```bash
uvicorn main:app --reload --port 8000
```

启动后访问：

- `http://localhost:8000/`

### Docker Compose

仓库已提供 `docker-compose.yml`，包含：

- `api`
- `postgres`
- `redis`

直接启动：

```bash
docker compose up --build
```

`scripts/init.sql` 会自动挂载到 Postgres 初始化目录。

## 环境变量

### 最小可运行配置

按当前默认模型映射，最小配置通常是：

```bash
CO_API_KEY=your_proxy_key
DATABASE_URL=postgresql://chatdada:chatdada@localhost:5432/chatdada
REDIS_URL=redis://localhost:6379
```

### 常见可选配置

```bash
# 搜索
TAVILY_API_KEY=...
BRAVE_SEARCH_API_KEY=...
EXA_API_KEY=...

# 长对话语义检索 embedding
GEMINI_API_KEY=...

# 其他 provider
OPENAI_API_KEY=...
MOONSHOT_API_KEY=...
GOOGLE_API_KEY=...
ANTHROPIC_API_KEY=...
YESCODE_GEMINI_BASE_URL=https://co.yes.vg/gemini

# 研究数据目录
RESEARCH_DATA_DIR=data/research

# 运行时
LLM_TIMEOUT_SECONDS=7200
LLM_MAX_RETRIES=2

# LangSmith
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=...
LANGSMITH_PROJECT=chat-dada
LANGSMITH_ENDPOINT=...

# 图片生成
IMAGE_GEN_API_URL=https://co.yes.vg/v1/chat/completions
IMAGE_GEN_MODEL=gemini-3.1-flash-image-landscape

# R2 对象存储
R2_ACCOUNT_ID=...
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET_NAME=chatdada-uploads
R2_PRESIGN_EXPIRES=1800
```

### 模型配置入口

模型 provider、role 到 model 的映射统一定义在 `core/models.py`。

当前可以直接从这里看三件事：

- 哪些角色在用什么模型
- 哪些 provider 需要哪些 API key
- `thinking_level` 会怎样映射到底层请求参数

## 测试

直接运行：

```bash
pytest
```

和本次重构最相关的测试文件包括：

- `tests/test_platform_refactor.py`
- `tests/test_task_streaming.py`
- `tests/test_conversation_context.py`
- `tests/test_user_store_v2.py`
- `tests/test_models.py`

## 目前最值得注意的代码事实

- 根级 HTTP 入口已经收敛到 `main.py`，不再有分散的 API 主入口。
- 研究域已经从“固定图”切到“通用动态 orchestrator + DomainSpec”模式。
- 会话上下文和用户记忆现在是两个独立层：一个面向当前对话串联，一个面向长期用户画像。
- PostgreSQL 不只是任务持久化，还承担事件回放与长对话语义检索基础。
- README 里的结构说明现在以当前代码为准，不再沿用重构前的模块边界表述。
