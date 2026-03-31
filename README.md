# chat-dada

`chat-dada` 是一个本地优先的多智能体任务平台后端。所有源码归入 5 个语义清晰的顶级包，Web 与 Agent 完全隔离：

- `web/`：FastAPI 入口、路由、中间件、静态文件、SSE
- `agent/`：全部 AI/LLM 相关 — 运行时、任务编排、领域 agent、能力组件、工具
- `domain/`：认证、任务、对话、额度等业务服务
- `infra/`：SQLAlchemy 模型、仓储、数据库会话、Google OAuth、用户存储
- `core/`：LLM 工厂、日志、内容处理等共享工具

`main.py` 是兼容启动入口，真正的 FastAPI 应用在 `web/app.py`。

## 项目结构

```
chat-dada/
├── web/                         # 🌐 WEB LAYER (Controller + HTTP)
│   ├── app.py                   #   FastAPI app factory
│   ├── config.py                #   WebSettings
│   ├── runtime.py               #   TaskService wiring
│   ├── routers/                 #   HTTP endpoints (auth, tasks, conversations...)
│   ├── deps/                    #   FastAPI DI (auth, services, billing...)
│   └── middleware/              #   Errors, sessions
│
├── agent/                       # 🤖 AGENT LAYER (全部 AI/LLM 相关)
│   ├── runtime/                 #   LangGraph 图执行引擎、路由、事件流
│   ├── platform/                #   任务编排、注册、DAG 规划、能力组合
│   ├── domains/                 #   领域 agent (research, patent, ppt, zero_report)
│   │   └── _base/               #   AgentProtocol / AgentManifest / AgentContext
│   ├── capabilities/            #   可复用组件 (review_gates, budget, memory...)
│   ├── workflows/               #   通用编排框架 (orchestrator, strategy_selector)
│   ├── tools/                   #   LLM 可调用工具 (search, image_gen, code_executor)
│   └── ppt_engine/              #   PPT DSL 渲染引擎
│
├── domain/                      # 📦 DOMAIN LAYER (业务逻辑)
│   ├── auth/                    #   认证、OAuth、密码
│   ├── billing/                 #   额度、用量
│   ├── conversations/           #   对话管理、上下文
│   ├── tasks/                   #   任务执行服务
│   └── agents/                  #   Agent 查询服务
│
├── infra/                       # 🔧 INFRASTRUCTURE
│   ├── db/                      #   SQLAlchemy models, repositories, session
│   ├── oauth/                   #   Google OIDC client
│   ├── events/                  #   Redis Pub/Sub
│   └── storage/                 #   用户记忆持久化
│
├── core/                        # ⚙️ SHARED UTILITIES
│   ├── models.py                #   LLM 工厂 (OpenAI, Claude, Gemini)
│   ├── logger.py                #   结构化日志
│   ├── r2_storage.py            #   Cloudflare R2
│   └── content_utils.py         #   文本处理
│
├── tests/                       # 测试
├── alembic/                     # 数据库迁移
├── scripts/  docs/  skills/     # 辅助
├── data/  logs/  outputs/  uploads/  # 运行时数据
└── main.py + pyproject.toml + Dockerfile + ...   # 配置
```

## 当前能力

### 认证

当前已经支持：

- `POST /auth/register`
- `POST /auth/login`
- `GET /auth/google/login`
- `GET /auth/google/callback`
- `POST /auth/logout`
- `GET /auth/me`

登录态使用自有 session cookie，不把 Google token 当作业务 session。

补充实现细节：

- 密码哈希使用 `pwdlib` 的推荐算法
- session token 只以 `SHA-256` 哈希形式存进数据库
- `get_current_user` 会从 cookie 中恢复当前用户
- `get_admin_user` 通过 `ADMIN_EMAILS` 限定管理员

### 任务

任务创建和查看接口：

- `POST /tasks`
- `GET /tasks/{task_id}`
- `GET /tasks/{task_id}/artifacts`
- `GET /tasks/{task_id}/artifact-file?path=...`
- `GET /tasks/{task_id}/review`
- `GET /tasks/{task_id}/provenance`
- `GET /tasks/{task_id}/trace`
- `GET /tasks/{task_id}/replay`
- `POST /tasks/{task_id}/reply`
- `POST /tasks/{task_id}/cancel`
- `GET /tasks/{task_id}/events`

创建任务时支持的关键字段：

- `task`
- `mode`：`auto` / `chat` / `agent`
- `thinking_level`：`low` / `medium` / `high`
- `file_paths`
- `conversation_id`

任务流现在是：

1. 路由器根据 `mode`、附件和关键词决定走 `general_chat`、单领域 orchestrator 还是 `composite`（跨领域 DAG 组合）
2. `agent/runtime/task_execution.py` 负责落库、事件流、回复恢复和取消
3. 事件通过 Redis Pub/Sub 推到 SSE
4. 结果、review、budget 和 artifact_refs 会持久化到 Postgres

SSE 事件流会：

- 先回放 `after_seq` 之后的历史事件
- 再持续推送新事件
- 在空闲时发送 keep-alive
- 附带 `stream_meta`，里面包含 `auth_lookup_ms`、连接数和连接池使用情况

### 对话

对话接口：

- `GET /conversations`
- `POST /conversations`
- `PATCH /conversations/{conversation_id}`
- `DELETE /conversations/{conversation_id}`
- `GET /conversations/{conversation_id}/entries`

当前对话上下文构建逻辑不是简单拼接历史，而是分层处理：

- 1 到 5 轮：直接使用原始对话
- 6 到 20 轮：先做摘要，再拼最近几轮
- 20 轮以上：摘要 + 最近对话 + 向量检索补回相关片段

这里用到了：

- `conversations.context_summary`
- `conversations.summary_through_seq`
- `task_events.embedding` 的 pgvector 向量

### 额度

额度接口：

- `GET /me/quota`
- `GET /admin/users/{user_id}/quota`
- `PUT /admin/users/{user_id}/quota`

当前支持的额度维度：

- 日 / 周 / 月任务数
- 日 / 周 / 月 token 数
- 日 / 周 / 月成本美元

补充配置：

- `ADMIN_EMAILS`
- `MODEL_PRICING_JSON`

`MODEL_PRICING_JSON` 用来把 token 使用量估算为 `cost_usd`。如果未配置或解析失败，`cost_usd` 会退化为 `0`，并在启动时给出 warning。

### 领域工作流

当前领域入口如下：

| 领域           | 入口                                                                 | 说明                                    |
| -------------- | -------------------------------------------------------------------- | --------------------------------------- |
| `general_chat` | `agent/runtime/dispatcher.py` + `agent/capabilities/general_chat.py` | 轻量问答                                |
| `research`     | `agent/domains/research/orchestrated.py`                             | 模块化科研工作流                        |
| `patent`       | `agent/domains/patent/orchestrated.py`                               | 专利草稿工作流                          |
| `zero_report`  | `agent/domains/zero_report/orchestrated.py`                          | 归零报告工作流                          |
| `ppt`          | `agent/domains/ppt/orchestrated.py`                                  | 生成内容后再渲染 `.pptx`                |
| `composite`    | `agent/runtime/root_graph.py` → `agent/platform/task_planner.py`     | 跨领域 DAG 组合（如「先调研再做 PPT」） |

`research` 工作流已经支持的产物类型：

- `literature_review`
- `paper_guidance`
- `paper_outline`
- `research_proposal`

研究工作流大致是：

1. intake 把用户输入整理成结构化 brief
2. planner 生成模块化 plan
3. worker 分模块检索、起草、校验
4. aggregator 聚合模块草稿
5. evaluator 打分并给出修订目标
6. optimizer 只重写低分模块
7. synthesizer 输出最终科研结果

`patent`、`zero_report` 和 `ppt` 也都走 orchestrated graph，但各自的 subagent 组合和产物不同：

- `patent`：`technical_disclosure -> prior_art -> claims -> spec -> review`
- `zero_report`：`timeline -> root_cause -> actions -> draft -> review`
- `ppt`：`outline -> research -> write -> render`

### 调试与静态资源

当前还保留这些调试接口：

- `GET /api/traces`
- `GET /api/langsmith`
- `POST /api/langsmith`
- `GET /api/verbose`
- `POST /api/verbose`
- `POST /api/log-level`

静态资源和文件接口：

- `GET /`：返回前端构建产物的 `index.html`
- `POST /upload`：上传文件
- `GET /uploads/{filename}`：访问上传文件
- `GET /download/{filename}`：下载生成文件

## 数据库与迁移

项目同时保留两种数据库初始化方式：

### 1. 初始 SQL

`scripts/init.sql`

适合本地快速起库或容器初始化。

### 2. Alembic

当前已经引入：

- `alembic.ini`
- `alembic/env.py`
- `alembic/versions/*`

推荐在已有数据库上执行：

```bash
.venv/bin/python -m alembic upgrade head
```

当前主要表：

- `users`
- `oauth_accounts`
- `user_sessions`
- `user_quotas`
- `usage_events`
- `task_runs`
- `task_events`
- `conversations`

其中 `task_events` 已开启 `pgvector` 向量列，用于对话检索。

## 本地开发

### 环境要求

- Python 3.13
- PostgreSQL
- Redis
- Node.js / npm
- 推荐 `uv`

### 安装后端依赖

```bash
uv venv .venv
source .venv/bin/activate
uv pip install -e .
playwright install chromium
```

如需严格按锁文件安装：

```bash
uv pip install -r requirements.txt
```

### 初始化数据库

如果是新库：

```bash
psql -U chatdada -d chatdada -f scripts/init.sql
```

如果要补当前 migration：

```bash
.venv/bin/python -m alembic upgrade head
```

### 启动后端

```bash
uvicorn main:app --reload --port 8000
```

后端默认访问：

- [http://127.0.0.1:8000](http://127.0.0.1:8000)

### 启动前端

前端不在本仓库内，而是在同级目录：

- `../chat-dada-front`

开发模式：

```bash
cd ../chat-dada-front
npm install
npm run dev -- --host 127.0.0.1 --port 5173
```

前端开发地址：

- [http://127.0.0.1:5173](http://127.0.0.1:5173)

构建产物会输出到：

- `../chat-dada-front/dist`

后端也可以直接服务这个构建产物，默认会从 `FRONTEND_DIST_DIR` 读取。

### Docker Compose

当前 `docker-compose.yml` 包含：

- `api`
- `postgres`
- `redis`

启动：

```bash
docker compose up --build
```

注意：

- `api` 默认通过 `FRONTEND_DIST_DIR=/app/frontend-dist` 读取前端构建产物
- 默认挂载的是同级目录 `../chat-dada-front/dist`
- 如果你的目录结构不同，需要改 `FRONTEND_DIST_DIR` 或 volume

## 本地 Google 登录调试

Google 控制台本地建议配置：

### 已获授权的 JavaScript 来源

- `http://127.0.0.1:5173`
- `http://localhost:5173`
- `http://127.0.0.1:8000`
- `http://localhost:8000`

### 已获授权的重定向 URI

- `http://127.0.0.1:8000/auth/google/callback`
- `http://localhost:8000/auth/google/callback`

### `.env` 关键项

```env
DATABASE_URL=postgresql://chatdada:chatdada@localhost:5432/chatdada
REDIS_URL=redis://localhost:6379
FRONTEND_DIST_DIR=../chat-dada-front/dist

GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_CALLBACK_URL=http://127.0.0.1:8000/auth/google/callback

APP_BASE_URL=http://127.0.0.1:8000
FRONTEND_REDIRECT_URL=http://127.0.0.1:5173

SESSION_COOKIE_SECURE=false
SESSION_COOKIE_SAMESITE=lax
SESSION_COOKIE_DOMAIN=
CORS_ALLOWED_ORIGINS=http://127.0.0.1:5173,http://localhost:5173
APP_SESSION_SECRET=replace-this-with-a-random-secret

ADMIN_EMAILS=
MODEL_PRICING_JSON=
```

### 研究 / 搜索 / LLM provider

```env
TAVILY_API_KEY=
BRAVE_SEARCH_API_KEY=
EXA_API_KEY=
GEMINI_API_KEY=

CO_API_KEY=
OPENAI_API_KEY=
MINIMAX_API_KEY=
MOONSHOT_API_KEY=
GOOGLE_API_KEY=
ANTHROPIC_API_KEY=
```

### 调试与 tracing

```env
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=
LANGSMITH_PROJECT=
LANGSMITH_ENDPOINT=

LLM_TIMEOUT_SECONDS=7200
LLM_MAX_RETRIES=2
```

## 测试

当前比较关键的后端回归包括：

```bash
.venv/bin/python -m unittest \
  tests.test_auth_password \
  tests.test_auth_deps \
  tests.test_auth_routes \
  tests.test_quota_routes \
  tests.test_task_sse_auth_lifecycle \
  tests.test_conversation_context \
  tests.test_platform_refactor
```

如果你要重点验证研究工作流收敛，也可以补跑：

```bash
.venv/bin/python -m unittest tests.test_research_worker_convergence
```

前端构建检查：

```bash
cd ../chat-dada-front
npm run build
```

## 当前要点

- 所有源码归入 5 个顶级包：`web/`、`agent/`、`domain/`、`infra/`、`core/`
- 领域 agent 支持 `AgentProtocol` 插件化注册（`agent/domains/_base/protocol.py`）
- 跨领域任务支持 DAG 组合：`task_planner` 拆解 → `step_runner` 拓扑并行执行
- 自审门控：`gate_runner` 实现 run → review → retry 循环
- Google 登录和邮箱密码登录都已接入
- 主业务 HTTP 接口已经要求登录
- quota 管理接口和前端额度展示已经接入
- `conversation_context` 已经支持 raw / summary / summary+retrieval 三种策略
- `cost_usd` 现在按 `MODEL_PRICING_JSON` 基于 LLM token 使用量估算；未配置时会退化为 `0`
- 生产上线前，必须替换 `APP_SESSION_SECRET`，并确认 `SESSION_COOKIE_SECURE`、`SESSION_COOKIE_DOMAIN`、`CORS_ALLOWED_ORIGINS` 与域名配置一致
