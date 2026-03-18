# Demo 部署方案设计

> 目标：将 chat-dada 部署到自有云服务器，提供给朋友使用，支持多用户认证和配额管理。

## 环境要求

| 项 | 选择 |
|---|---|
| 服务器 | Ubuntu 云服务器，有公网 IP |
| 域名 | 需要购买域名，绑定到服务器 IP |
| HTTPS | 使用 Caddy 自动申请 Let's Encrypt 证书 |
| 认证 | 多用户账号（邀请码注册 + JWT 登录） |
| 管理 | admin 角色可管理用户、重置密码、设置配额 |

---

## 1. 整体架构

```
Internet
   │
   ▼
┌─────────────────────┐
│  Caddy (Port 443)   │  ← 自动 HTTPS, Let's Encrypt
│  反向代理 → :8000   │
└─────────┬───────────┘
          │
┌─────────▼───────────┐
│  FastAPI (Port 8000) │  ← 新增: 认证中间件 + 登录/注册 API
│  + 静态前端          │
└─────────┬───────────┘
          │
    ┌─────┴─────┐
    ▼           ▼
┌────────┐ ┌────────┐
│Postgres│ │ Redis  │
│ :5432  │ │ :6379  │
└────────┘ └────────┘
```

关键安全设计：
- 只有 Caddy 的 80/443 对外暴露
- PostgreSQL 和 Redis 只在 Docker 内部网络可达
- API 服务不直接暴露到公网

---

## 2. 用户认证方案

### 2.1 角色模型

- **admin** — 管理员（你自己），最高权限，无配额限制
- **user** — 普通用户（朋友们），受配额限制

### 2.2 数据库表

```sql
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    display_name VARCHAR(100),
    role VARCHAR(20) DEFAULT 'user',        -- admin | user
    quota_limit INT DEFAULT 50,             -- 每月可用任务数，0=无限
    quota_used INT DEFAULT 0,               -- 本月已用
    quota_reset_at TIMESTAMP,               -- 下次重置时间
    invite_code VARCHAR(50),
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE invite_codes (
    code VARCHAR(50) PRIMARY KEY,
    max_uses INT DEFAULT 1,
    used_count INT DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW()
);
```

### 2.3 用户流程

**注册：**
1. 朋友获得邀请码
2. 打开网站 → 看到登录页面 → 点击注册
3. 输入邀请码 + 自定义用户名 + 密码 → 注册成功

**登录：**
1. 用户名 + 密码 → 获得 JWT token
2. 前端存 token 在 localStorage
3. 每次请求带 `Authorization: Bearer <token>`

**密码重置（管理员操作）：**
1. 朋友忘记密码 → 联系管理员
2. 管理员调 `/admin/users/{id}/reset-password` → 生成临时密码
3. 用户用临时密码登录后通过 `PUT /auth/change-password` 修改

### 2.4 认证 API

| 端点 | 方法 | 说明 | 需认证 |
|------|------|------|--------|
| `/auth/register` | POST | 邀请码 + 用户名 + 密码注册 | 否 |
| `/auth/login` | POST | 用户名 + 密码 → JWT | 否 |
| `/auth/me` | GET | 当前用户信息 | 是 |
| `/auth/change-password` | PUT | 修改密码 | 是 |

### 2.5 管理员 API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/admin/users` | GET | 查看所有用户 + 用量 |
| `/admin/users` | POST | 直接创建用户（无需邀请码） |
| `/admin/users/{id}/quota` | PUT | 设置配额 |
| `/admin/users/{id}/reset-password` | PUT | 重置密码（生成临时密码） |
| `/admin/users/{id}/toggle-active` | PUT | 启用/禁用用户 |
| `/admin/invite-codes` | POST | 生成邀请码 |

### 2.6 配额控制

- 每次 `POST /tasks` 时检查 `quota_used < quota_limit`
- 超额 → 返回 403 + "本月配额已用完，请联系管理员"
- 每月 1 号自动重置 `quota_used = 0`
- admin 用户 `quota_limit = 0`（无限制）

### 2.7 中间件逻辑

无需认证的路径：
- `/auth/*`（登录/注册）
- `/static/*`（静态资源）
- `GET /`（根路由）

其他 API 路径（`/tasks`、`/upload` 等）→ 验证 JWT，提取 `user_id`

### 2.8 初始化

通过环境变量 `ADMIN_USERNAME` + `ADMIN_PASSWORD` 自动创建 admin 账号（首次启动时）。

---

## 3. Docker Compose 配置

### 3.1 docker-compose.yml（修改）

```yaml
services:
  caddy:
    image: caddy:2-alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      - api
    restart: unless-stopped

  api:
    build: .
    expose:
      - "8000"                    # 只在内部网络暴露，不对外
    env_file: .env
    volumes:
      - ./data:/app/data
      - ./outputs:/app/outputs
      - ./uploads:/app/uploads
    depends_on:
      postgres: { condition: service_healthy }
      redis: { condition: service_healthy }
    restart: unless-stopped

  postgres:
    image: postgres:15-alpine
    # 不暴露 ports 到外部
    expose:
      - "5432"
    environment:
      POSTGRES_DB: chatdada
      POSTGRES_USER: chatdada
      POSTGRES_PASSWORD: ${DB_PASSWORD:-chatdada}
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./scripts/init.sql:/docker-entrypoint-initdb.d/init.sql
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U chatdada"]
      interval: 5s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  redis:
    image: redis:7-alpine
    expose:
      - "6379"
    command: redis-server --save "" --appendonly no
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 5s
      retries: 5
    restart: unless-stopped

volumes:
  postgres_data:
  caddy_data:
  caddy_config:
```

### 3.2 Caddyfile（新建）

```
your-domain.com {
    reverse_proxy api:8000
}
```

Caddy 自动处理：Let's Encrypt 证书申请/续期、HTTP→HTTPS 重定向、WebSocket/SSE 代理。

---

## 4. 前端改动

### 4.1 文件结构

```
static/
├── index.html      # 主应用页面（小改：引入 auth.js，请求带 token）
├── login.html      # 新建 — 登录 + 注册页面
└── auth.js         # 新建 — 共享的认证逻辑（token 管理、跳转）
```

### 4.2 路由逻辑

- `GET /` → 检查是否有有效 token
  - 无 token → 重定向到 `/static/login.html`
  - 有 token → 返回 `index.html`
- `GET /static/login.html` → 登录/注册表单，不需要认证
- `auth.js` → 封装 localStorage 存取 token、过期跳转、fetch 拦截器

---

## 5. 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `Caddyfile` | 新建 | 反向代理配置（2 行） |
| `docker-compose.yml` | 修改 | 加 Caddy 服务、去掉 DB/Redis 对外端口 |
| `Dockerfile` | 修改 | 加 bcrypt、PyJWT 依赖 |
| `requirements.txt` | 修改 | 加 bcrypt、PyJWT |
| `scripts/init.sql` | 修改 | 加 users、invite_codes 表 |
| `auth/__init__.py` | 新建 | 认证模块 |
| `auth/models.py` | 新建 | User 数据模型 |
| `auth/routes.py` | 新建 | 登录/注册 API |
| `auth/middleware.py` | 新建 | JWT 验证中间件 |
| `auth/admin.py` | 新建 | 管理员 API |
| `main.py` | 修改 | 注册认证路由和中间件 |
| `static/login.html` | 新建 | 登录/注册页面 |
| `static/auth.js` | 新建 | token 管理 + 请求拦截 |
| `static/index.html` | 小改 | 引入 auth.js，请求带 token |

**不改动：** orchestrator、storage、capabilities、core 等现有业务逻辑。user_id 从认证 token 中获取，替代现在的 "anonymous"。

---

## 6. 部署步骤

### 6.1 服务器准备

```bash
# 安装 Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# 安装 Docker Compose（如果未随 Docker 安装）
sudo apt install docker-compose-plugin
```

### 6.2 域名配置

购买域名后，在 DNS 管理中添加 A 记录：
```
类型: A
名称: @（或 chat 等子域名）
值: <服务器公网 IP>
TTL: 600
```

### 6.3 部署

```bash
# 克隆代码
git clone <repo-url> chat-dada && cd chat-dada

# 配置环境变量
cp .env.example .env
vim .env   # 填入 API Key、DB 密码、ADMIN 账号等

# 修改 Caddyfile 中的域名
vim Caddyfile

# 启动
docker compose up -d

# 查看日志
docker compose logs -f
```

### 6.4 验证

1. 浏览器打开 `https://your-domain.com`
2. 看到登录页面 → 用 admin 账号登录
3. 创建邀请码 → 发给朋友
4. 朋友注册并使用

---

## 7. 新增依赖

```
bcrypt>=4.0.0
PyJWT>=2.8.0
```

---

## 8. 环境变量（新增）

| 变量 | 说明 | 示例 |
|------|------|------|
| `ADMIN_USERNAME` | 管理员用户名 | `admin` |
| `ADMIN_PASSWORD` | 管理员密码 | `your-secure-password` |
| `JWT_SECRET` | JWT 签名密钥 | 随机 32 字符串 |
| `JWT_EXPIRE_HOURS` | Token 过期时间 | `72`（3 天） |
