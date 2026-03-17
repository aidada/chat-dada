# 数据库迁移设计：SQLite → PostgreSQL + Redis

**日期：** 2026-03-17
**状态：** 设计完成，待实施
**背景：** 随着团队协作、并发任务和多实例部署需求增加，SQLite 已无法满足需求

---

## 一、迁移动因

| 问题 | 当前状况 | 目标 |
|------|---------|------|
| 排查问题困难 | SQLite 文件，无可视化工具 | PostgreSQL + pgAdmin/DBeaver |
| 写并发冲突 | SQLite WAL 模式下仍有锁竞争 | PostgreSQL MVCC，无写锁阻塞 |
| 多实例部署 | SQLite 文件无法跨实例共享 | PostgreSQL 作为共享数据层 |
| SSE 广播限制 | 订阅者存内存，单实例有效 | Redis Pub/Sub，跨实例广播 |

---

## 二、目标架构

```
┌─────────────────────────────────────────────────────┐
│                   API 实例（可水平扩展）                │
│  FastAPI + TaskService                               │
│   ├─ 任务状态读写  ──────────────→  PostgreSQL        │
│   ├─ SSE 事件广播  ──────────────→  Redis Pub/Sub    │
│   └─ 用户回复等待  ──────────────→  Redis BLPOP      │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│                   存储层                              │
│  PostgreSQL 15  ─ 任务状态、事件历史（持久化）          │
│  Redis 7        ─ SSE 广播、用户回复信号（实时）        │
│  Shared Volume  ─ data/research/ 研究文件（文件系统）  │
└─────────────────────────────────────────────────────┘
```

**部署策略：**
- 本地/自托管：Docker Compose（postgres + redis + api）
- 云端扩容：环境变量切换至 RDS + ElastiCache，**应用代码零改动**

---

## 三、PostgreSQL Schema

### task_runs 表

```sql
CREATE TABLE task_runs (
    task_id             TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    status              TEXT NOT NULL,
    task_text           TEXT NOT NULL,
    mode                TEXT NOT NULL DEFAULT 'auto',
    thinking_level      TEXT NOT NULL,
    route_name          TEXT,
    route_reason        TEXT,
    route_confidence    REAL,
    request_payload     JSONB NOT NULL,
    result_text         TEXT,
    error_text          TEXT,
    pending_question    JSONB,
    created_at          TIMESTAMPTZ NOT NULL,
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_task_runs_user_id  ON task_runs (user_id);
CREATE INDEX idx_task_runs_status   ON task_runs (status);
CREATE INDEX idx_task_runs_created  ON task_runs (created_at DESC);
```

### task_events 表

```sql
CREATE TABLE task_events (
    task_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    event_type  TEXT NOT NULL,
    payload     JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (task_id, seq)
);

CREATE INDEX idx_task_events_task_seq ON task_events (task_id, seq);
```

**对比 SQLite 的关键升级：**

| 字段类型 | SQLite | PostgreSQL | 收益 |
|---------|--------|-----------|------|
| JSON 字段 | TEXT | JSONB | 可直接查询字段，如 `request_payload->>'mode'` |
| 时间戳 | TEXT (ISO 字符串) | TIMESTAMPTZ | 时区感知、范围查询高效 |
| 新增索引 | 无 | user_id / status / created_at | 按用户、状态过滤高频操作 |

---

## 四、Redis 使用规范

### SSE 事件广播

```
Channel 命名：task:{task_id}:events
流向：任务执行 → PUBLISH → 所有实例的 SSE 订阅者
```

- 每个事件序列化为 JSON 后 PUBLISH
- 各实例本地不再维护 `_subscribers` dict
- 断线重连仍从 PostgreSQL `task_events` 表回放历史（Redis 不做持久化兜底）

### 用户回复等待

```
Key 命名：task:{task_id}:reply
流向：任务 BLPOP 阻塞等待 → 用户回复 LPUSH → 任务恢复执行
```

- 替代现有 `_pending_replies: dict[str, asyncio.Future]`
- 超时：1 小时（与当前 human-in-the-loop 超时一致）
- 支持跨实例：用户请求打到任意实例均可唤醒任务

---

## 五、与现有重构机制的兼容性分析

参考文档：`docs/plans/2026-03-17-01` 至 `2026-03-17-06`

| 机制 | 存储依赖 | 兼容性 | 备注 |
|------|---------|--------|------|
| 机制1：分阶段上下文缩减 | 无（纯内存） | ✅ 完全兼容 | 无存储交互 |
| 机制2：外部记忆系统 | **文件系统** `data/research/{task_id}/` | ✅ 兼容，需注意 | 见下方说明 |
| 机制3：层级任务分解 | 无（内存中的 ResearchPlan） | ✅ 完全兼容 | 无存储交互 |
| 机制4：Multi-Agent 架构 | **文件系统**（依赖机制2） | ✅ 兼容，需注意 | 见下方说明 |
| 机制5：检索锚定 | 无（内存中的 CitationRegistry） | ✅ 完全兼容 | 引用元数据内嵌于文件 |
| 机制6：注意力操纵 | **文件系统**（checkpoint JSON） | ✅ 兼容，需注意 | 见下方说明 |

### 重要：文件系统共享（多实例部署时）

机制 2、4、6 均依赖 `data/research/{task_id}/` 目录进行工作文件共享：
- 机制2 将研究过程的 findings/summaries/checkpoints 写入此目录
- 机制4 的多个 Worker Agent 通过此目录协调成果（不直接通信）
- 机制6 的 ProgressTracker checkpoint 也写入此目录

**单实例（Docker Compose）：** 挂载本地 volume，无问题
**多实例（云端）：** 必须使用共享存储（AWS EFS、NFS 等），否则不同实例上的 Worker 无法共享文件

> 这是多实例部署时唯一需要额外处理的基础设施依赖，数据库和 Redis 层本身对多实例透明。

---

## 六、代码改动范围

### task_runtime.py

**`TaskRunStore` 类：**
- `sqlite3` → `asyncpg`（连接池，异步）
- 移除 `threading.Lock`（asyncpg 连接池自带并发安全）
- JSON 字段：直接传 `dict`，asyncpg 原生支持 JSONB 序列化
- 时间戳：使用 `datetime` 对象替代 ISO 字符串

**`TaskService` 类：**
- `_subscribers: dict[str, set[Queue]]` → Redis Pub/Sub
- `_pending_replies: dict[str, Future]` → Redis BLPOP/LPUSH
- `_background_tasks` 不变（本地 asyncio 任务追踪）

### 新增依赖

```
asyncpg>=0.29.0      # PostgreSQL 异步驱动
redis[asyncio]>=5.0  # Redis 异步客户端
```

### 新增环境变量

```
DATABASE_URL=postgresql://chatdada:password@localhost:5432/chatdada
REDIS_URL=redis://localhost:6379
```

---

## 七、实施顺序

1. **基础设施** — 添加 `docker-compose.yml`，包含 PostgreSQL + Redis
2. **Schema 初始化** — 添加 `init.sql`，服务启动时自动建表
3. **替换 TaskRunStore** — SQLite → asyncpg，接口不变
4. **替换 SSE 状态** — 内存 dict → Redis，保持业务逻辑不变
5. **验证** — 单实例功能测试，然后双实例 SSE 广播测试

**数据迁移：** 任务历史无需迁移，SQLite 文件归档备用即可。

---

## 八、未来云端切换

从 Docker Compose 切换到云托管时，**仅需修改环境变量**：

```
# 本地
DATABASE_URL=postgresql://chatdada:password@localhost:5432/chatdada
REDIS_URL=redis://localhost:6379

# 云端（示例）
DATABASE_URL=postgresql://user:pass@rds-endpoint:5432/chatdada
REDIS_URL=redis://elasticache-endpoint:6379
```

文件系统（`data/research/`）需额外挂载共享存储（EFS/NFS）。
