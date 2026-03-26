# chat-dada

`chat-dada` 是一个本地优先的多智能体任务平台。当前后端主线已经切到更清晰的分层结构：

- Web API：FastAPI
- Agent 编排：LangGraph + deepagents
- 数据库：PostgreSQL + pgvector
- 实时事件：Redis Pub/Sub + SSE
- 认证：Google OIDC + 邮箱密码 + 自有 session cookie
- 前端：React/Vite，位于同级仓库 `../chat-dada-front`

## 当前真实结构

后端当前推荐阅读路径如下：

```text
apps/
  web/
    app.py
    config.py
    runtime.py
    deps/
    middleware/
    routers/
      auth.py
      tasks.py
      conversations.py
      files.py
      system.py

domain/
  auth/
    models.py
    repositories.py
    schemas.py
    services.py
    google_oidc.py
    password.py
  tasks/
    repositories.py
    schemas.py
    services.py
  conversations/
    context.py
    repositories.py
    schemas.py
    services.py
  agents/
    services.py
  billing/
    services.py

infra/
  db/
    base.py
    session.py
    models/
    repositories/
  oauth/
    google.py
  events/
    redis_pubsub.py

agent_runtime/
  dispatcher.py
  interaction.py
  root_graph.py
  task_execution.py

domain_agents/
  research/
  patent/
  zero_report/
  ppt/

task_platform/
  domain_registry.py
  interrupts.py
  memory_interfaces.py
  renderer_registry.py
  state.py
  streaming.py
  tracing.py
```

说明：

- `apps/web` 只负责 HTTP、Cookie、SSE、路由与中间件。
- `domain/*` 负责业务语义，不直接处理 HTTP。
- `infra/*` 负责数据库、OAuth、事件通道等基础设施。
- `agent_runtime/*` 是任务执行主路径。
- `domain_agents/*` 保留四个领域 agent：`research / patent / zero_report / ppt`。
- 旧的 `runtime/task_runtime.py`、`runtime/task_dispatcher.py`、`task_platform/root_graph.py` 等主实现文件已经移除，不再作为阅读入口。

## 当前执行链路

```text
Browser / Frontend
    │
    ├── /auth/*
    ├── /tasks
    ├── /tasks/{task_id}
    ├── /tasks/{task_id}/events
    └── /conversations/*
    │
    ▼
apps/web/routers/*
    │
    ▼
domain/* services
    │
    ▼
agent_runtime/task_execution.py
    │
    ├── PostgreSQL
    ├── Redis Pub/Sub
    ├── agent_runtime/root_graph.py
    └── domain_agents/*
```

## 认证体系

当前后端已经支持：

- `POST /auth/register`
- `POST /auth/login`
- `GET /auth/google/login`
- `GET /auth/google/callback`
- `POST /auth/logout`
- `GET /auth/me`

登录态采用自有 session cookie，不直接把 Google token 当作业务登录态。

数据库里与认证相关的表有：

- `users`
- `oauth_accounts`
- `user_sessions`

## 额度系统

当前已经落地第一版用户额度模型，支持：

- 日限额
- 周限额
- 月限额

额度维度包括：

- 任务数
- token 数
- 成本美元

相关表：

- `user_quotas`
- `usage_events`

相关服务：

- `domain/billing/services.py`

当前已经接入：

- 任务开始前做额度检查
- 任务完成后记录 usage event
- `GET /me/quota` 查询当前用户额度
- `GET /admin/users/{user_id}/quota` 查询指定用户额度
- `PUT /admin/users/{user_id}/quota` 更新指定用户额度
- 上游 `weekly_limit_exceeded` 等错误会被映射为更友好的客户端提示

补充配置：

- `ADMIN_EMAILS`
  - 逗号分隔的管理员邮箱列表，用于开放 quota 管理接口
- `MODEL_PRICING_JSON`
  - 用于把 LLM token 使用量估算为 `cost_usd`
  - 例如：

```json
{
  "gpt-5.4": { "input_per_1m": 1.25, "output_per_1m": 10.0 },
  "default": { "total_per_1m": 2.0 }
}
```

## 领域执行现状

| 领域 | 当前主入口 | 说明 |
| --- | --- | --- |
| `general_chat` | `agent_runtime/dispatcher.py` + `capabilities/general_chat.py` | 轻量问答 |
| `research` | `domain_agents/research/orchestrated.py` | 模块化科研工作流 |
| `patent` | `domain_agents/patent/orchestrated.py` | 专利草稿工作流 |
| `zero_report` | `domain_agents/zero_report/orchestrated.py` | 归零报告工作流 |
| `ppt` | `domain_agents/ppt/orchestrated.py` | 生成内容后再做 `.pptx` 渲染 |

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

- [chat-dada-front](/Users/luozhongxu/workspace/chat-dada-front)

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

后端也可以直接服务这个构建产物。

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
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=

APP_BASE_URL=http://127.0.0.1:8000
GOOGLE_CALLBACK_URL=http://127.0.0.1:8000/auth/google/callback

# 本地如果前端 dev server 常驻：
FRONTEND_REDIRECT_URL=http://127.0.0.1:5173

# 如果更稳地直接回后端：
# FRONTEND_REDIRECT_URL=http://127.0.0.1:8000

SESSION_COOKIE_SECURE=false
SESSION_COOKIE_SAMESITE=lax
SESSION_COOKIE_DOMAIN=
CORS_ALLOWED_ORIGINS=http://127.0.0.1:5173,http://localhost:5173
APP_SESSION_SECRET=replace-this-with-a-random-secret
```

## Docker Compose

当前 `docker-compose.yml` 包含：

- `api`
- `postgres`
- `redis`

启动：

```bash
docker compose up --build
```

注意：

- `api` 现在通过 `FRONTEND_DIST_DIR` 读取前端构建产物
- 默认挂载的是同级目录 `../chat-dada-front/dist`
- 如果上线环境不是这个目录结构，需要改 `FRONTEND_DIST_DIR` 或 volume

## 环境变量

### 基础运行

```env
DATABASE_URL=postgresql://chatdada:chatdada@localhost:5432/chatdada
REDIS_URL=redis://localhost:6379
```

### 搜索与研究

```env
TAVILY_API_KEY=
BRAVE_SEARCH_API_KEY=
EXA_API_KEY=
GEMINI_API_KEY=
```

### 认证

```env
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
APP_BASE_URL=
GOOGLE_CALLBACK_URL=
FRONTEND_REDIRECT_URL=
SESSION_COOKIE_SECURE=
SESSION_COOKIE_SAMESITE=
SESSION_COOKIE_DOMAIN=
APP_SESSION_SECRET=
CORS_ALLOWED_ORIGINS=
```

### 调试与 tracing

```env
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=
LANGSMITH_PROJECT=
LANGSMITH_ENDPOINT=
```

## 测试

当前比较关键的后端回归包括：

```bash
.venv/bin/python -m unittest \
  tests.test_auth_password \
  tests.test_auth_deps \
  tests.test_auth_routes \
  tests.test_conversation_context \
  tests.test_platform_refactor
```

前端构建检查：

```bash
cd ../chat-dada-front
npm run build
```

## 当前要点

- 后端主结构已经迁到 `apps/domain/infra/agent_runtime/domain_agents`
- 旧的 `runtime/*.py` 和 `task_platform/root_graph.py` / `task_platform/router.py` 主实现已移除
- Google 登录和邮箱密码登录都已接入
- 主 HTTP 接口已要求登录
- quota 管理接口和前端额度展示已经接入
- `cost_usd` 现在按 `MODEL_PRICING_JSON` 基于 LLM token 使用量估算；未配置时会退化为 `0`
- 生产上线前，必须替换 `APP_SESSION_SECRET`，并确认 `SESSION_COOKIE_SECURE`、`SESSION_COOKIE_DOMAIN`、`CORS_ALLOWED_ORIGINS` 与域名配置一致
