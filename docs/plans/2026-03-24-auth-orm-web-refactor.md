# 2026-03-24 认证、ORM 与 Web/Agent 分层改造蓝图

## 目标

把当前项目从“匿名用户 + 手写 SQL + `main.py` 集中式路由”推进到以下结构：

1. 认证体系：Google OIDC + 邮箱密码
2. 登录态：自有 session cookie
3. 数据访问：SQLAlchemy 2.0 async + Alembic
4. 分层结构：
   - `apps/web` 负责 HTTP、Cookie、路由
   - `domain/*` 负责业务规则
   - `infra/*` 负责数据库、OAuth 等基础设施
   - `agent_runtime/*` 负责任务执行与流式事件
   - `domain_agents/*` 继续保留领域 agent

## 现状问题

### 认证问题

- 现在没有真实用户体系，接口靠前端传 `user_id`
- `user_id` 默认值是 `"anonymous"`
- 没有 `users`、`sessions`、`oauth_accounts` 表

### 数据访问问题

- SQL 大量散落在 `runtime/task_runtime.py`
- 一个类混合：
  - task_runs
  - task_events
  - conversations
  - 事件恢复
  - reply
  - append_event

### 结构问题

- `main.py` 混合了：
  - app 初始化
  - 静态资源路径
  - upload/download
  - task routes
  - websocket/SSE
  - conversations
  - debug/system routes
- `web` 与 `agent runtime` 没有明确边界

## 最终结构

```text
apps/
  web/
    app.py
    runtime.py
    deps.py
    routers/
      auth.py
      tasks.py
      conversations.py
      files.py
      system.py

domain/
  auth/
    schemas.py
    password.py
    services.py

infra/
  db/
    base.py
    session.py
    models/
      user.py
      oauth_account.py
      user_session.py
      conversation.py
      task_run.py
      task_event.py
    repositories/
      user_repo.py
      session_repo.py
  oauth/
    google.py

agent_runtime/
  __init__.py
  service.py

domain_agents/
  research/
  patent/
  zero_report/
  ppt/
```

## 认证设计

### 登录方式

这期同时支持：

1. Google 登录
2. 邮箱密码注册
3. 邮箱密码登录

### 登录态

统一采用服务端 session cookie：

- Cookie：`HttpOnly`
- `SameSite=Lax`
- 生产环境 `Secure=True`
- Cookie 中只存随机 session token
- 数据库存 `session_token_hash`

### 用户模型

#### users

- `id`
- `email`
- `email_verified`
- `display_name`
- `avatar_url`
- `password_hash` 可空
- `status`
- `created_at`
- `last_login_at`

#### oauth_accounts

- `id`
- `user_id`
- `provider`
- `provider_user_id`
- `provider_email`
- `created_at`
- `updated_at`

#### user_sessions

- `id`
- `user_id`
- `session_token_hash`
- `expires_at`
- `created_at`
- `last_seen_at`
- `revoked_at`
- `user_agent`
- `ip_address`

### 账户合并规则

- Google 登录时优先按 `(provider, provider_user_id)` 查绑定
- 若未绑定但邮箱已存在，且 Google `email_verified=true`
  - 自动绑定到已有用户
- 邮箱密码注册时邮箱唯一
- Google 用户后续可以再设置密码

## API 设计

### 新增 Auth 路由

- `POST /auth/register`
- `POST /auth/login`
- `GET /auth/google/login`
- `GET /auth/google/callback`
- `POST /auth/logout`
- `GET /auth/me`

### 现有接口的演进方向

第一阶段：

- 保留现有 task/conversation 接口签名，避免当前前端和 runtime 一次性断裂
- 引入 `current_user` 依赖和 session 体系
- auth 路由可独立运行

第二阶段：

- 去掉客户端显式 `user_id`
- `tasks`、`conversations` 接口统一从 session 获取当前用户

## ORM 设计

### 选型

- SQLAlchemy 2.0 async
- Alembic

### 迁移策略

第一阶段：

- 先引入 ORM 基座和 auth 相关表
- 同时为现有表建立 ORM model
- 当前 task runtime 仍可暂时使用 `asyncpg`

第二阶段：

- 按模块把 `runtime/task_runtime.py` 中的 SQL 迁到 repository

## 实施顺序

### Phase 1: 脚手架与基座

- 建立 `apps/web`
- 建立 `infra/db`
- 建立 `infra/oauth`
- 建立 `domain/auth`
- `main.py` 变成薄包装

### Phase 2: Auth 基础设施

- ORM models
- session repository
- user repository
- password hashing
- Google OIDC client

### Phase 3: 路由与会话

- `/auth/register`
- `/auth/login`
- `/auth/google/login`
- `/auth/google/callback`
- `/auth/logout`
- `/auth/me`

### Phase 4: 数据访问迁移

- conversations 仓储
- tasks 仓储
- task events 仓储
- conversation summary 仓储

### Phase 5: 全面鉴权

- 去掉裸 `user_id`
- 前端对接 `/auth/me`
- task/conversation 接口全量切换到 `current_user`

## 这轮改造的可交付成果

本轮先完成：

1. 蓝图文档落盘
2. `apps/web` 分层骨架
3. ORM 基座与模型
4. auth service / router
5. Google OIDC + 密码登录基础代码
6. `main.py` 改为 web app 薄包装

本轮暂不完成：

- 前端登录页改造
- 全量 task SQL 迁移
- 全部业务接口切换为强制登录
