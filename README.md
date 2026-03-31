# chat-dada

`chat-dada` 是一个本地优先的多智能体任务平台后端。当前代码已经切到清晰的分层结构，主要入口不再集中在单个大文件里。

- `apps/web`：FastAPI 入口、路由、中间件、静态文件、SSE
- `domain`：认证、任务、对话、额度等业务服务
- `infra`：SQLAlchemy 模型、仓储、数据库会话、Google OAuth
- `agent_runtime`：任务执行、路由、事件流、LangGraph 运行时
- `domain_agents`：`research / patent / zero_report / ppt` 领域工作流
- `task_platform`、`capabilities`、`tools`、`workflows`：共享的流式协议、检索、规划和编排组件

`main.py` 现在只是兼容入口，真正的 FastAPI 应用在 `apps/web/app.py`。

## 当前结构

```text
apps/web/
  app.py
  config.py
  deps/
  middleware/
  routers/
domain/
  auth/
  billing/
  conversations/
  tasks/
infra/
  db/
    base.py
    models/
    repositories/
  oauth/
agent_runtime/
domain_agents/
task_platform/
capabilities/
tools/
workflows/
```

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
│   ├── runtime/                 #   ← agent_runtime/ (图执行引擎)
│   ├── platform/                #   ← task_platform/ (任务编排 + 注册)
│   ├── domains/                 #   ← domain_agents/ (research, patent, ppt, zero_report)
│   ├── capabilities/            #   ← capabilities/ (review_gates, budget, memory...)
│   ├── workflows/               #   ← workflows/ (通用编排框架)
│   ├── tools/                   #   ← tools/ (search, image_gen, code_executor...)
│   └── ppt_engine/              #   ← ppt_engine/ (PPT DSL 渲染)
│
├── domain/                      # 📦 DOMAIN LAYER (业务逻辑, 不变)
│   ├── auth/, billing/, conversations/, tasks/, agents/
│
├── infra/                       # 🔧 INFRASTRUCTURE (db, oauth, events + 吸收 storage/)
│   ├── db/  oauth/  events/
│   └── storage/                 #   ← storage/ (user_store, user_models)
│
├── core/                        # ⚙️ SHARED UTILITIES (不变)
│
├── tests/  alembic/  scripts/  docs/  skills/    # 辅助
├── data/  logs/  outputs/  uploads/               # 运行时数据
├── main.py + pyproject.toml + Dockerfile + ...    # 配置
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

1. 路由器根据 `mode`、附件和关键词决定走 `general_chat` 还是 orchestrator
2. `agent_runtime/task_execution.py` 负责落库、事件流、回复恢复和取消
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

| 领域 | 当前入口 | 说明 |
| --- | --- | --- |
| `general_chat` | `agent_runtime/dispatcher.py` + `capabilities/general_chat.py` | 轻量问答 |
| `research` | `domain_agents/research/orchestrated.py` | 模块化科研工作流 |
| `patent` | `domain_agents/patent/orchestrated.py` | 专利草稿工作流 |
| `zero_report` | `domain_agents/zero_report/orchestrated.py` | 归零报告工作流 |
| `ppt` | `domain_agents/ppt/orchestrated.py` | 生成内容后再渲染 `.pptx` |

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

- 后端主结构已经迁到 `apps/web`、`domain`、`infra`、`agent_runtime` 和 `domain_agents`
- 旧的 `runtime/*.py` 和 `task_platform/root_graph.py` / `task_platform/router.py` 主实现已移除
- Google 登录和邮箱密码登录都已接入
- 主业务 HTTP 接口已经要求登录
- quota 管理接口和前端额度展示已经接入
- `conversation_context` 已经支持 raw / summary / summary+retrieval 三种策略
- `cost_usd` 现在按 `MODEL_PRICING_JSON` 基于 LLM token 使用量估算；未配置时会退化为 `0`
- 生产上线前，必须替换 `APP_SESSION_SECRET`，并确认 `SESSION_COOKIE_SECURE`、`SESSION_COOKIE_DOMAIN`、`CORS_ALLOWED_ORIGINS` 与域名配置一致
