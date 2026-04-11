# Chat-Dada Streaming Protocol Design

> **Status**: Draft
> **Date**: 2026-04-11
> **Scope**: chat-dada (backend) ↔ chat-dada-front (frontend) 之间的事件流协议标准化

## 1. Problem Statement

当前 chat-dada 后端与 chat-dada-front 前端之间的数据流转存在以下问题：

- **无类型安全**：后端 `EventType` 枚举包含 25+ 个扁平字符串类型，前端 `StreamEvent` 接口使用 `[key: string]: any`，跨边界没有类型保证
- **逻辑与框架耦合**：事件解析逻辑嵌入 React hooks（`useTaskStream.ts`），无法复用到其他客户端
- **扩展困难**：新增事件类型需要同时修改后端枚举、SSE 推送逻辑、前端 hooks 和 UI 组件
- **多端复用不可能**：如果要做移动端或桌面端（如 Rust+GUI），必须完全重写数据流转逻辑

## 2. Design Goal

建立一套**语言无关、框架无关**的分层事件流协议，使得：

1. 协议规范文档是唯一真相源（不绑定任何编程语言）
2. 核心业务逻辑（事件处理、状态归约）写一次，所有平台复用
3. 新增事件类型只需扩展命名空间，不破坏现有客户端
4. 传输层可替换（SSE → WebSocket），不影响业务逻辑
5. 允许对当前前端进行破坏性变更

## 3. Architecture Overview

### 3.1 Five-Layer Architecture

```
Layer 0 — Protocol Spec (Markdown document, language-agnostic)
   ↓ human reads spec, writes types
Layer 1 — Type Definitions (per-language implementations)
   ↓ typed events
Layer 2 — Transport (SSE / WebSocket / Mock)
   ↓ typed ProtocolEvent stream
Layer 3 — State Machine (pure reducer, no framework dependency)
   ↓ reactive TaskState
Layer 4 — Framework Binding (React hooks / SwiftUI / egui / Flutter)
```

**Key principles:**

- **Layer 0** 是唯一真相源。它是一份 Markdown/YAML 文档，定义所有事件类型、字段、约束，不绑定任何语言
- **Layer 1** 是 Layer 0 的语言实现。TypeScript 版本先写，Rust/Swift/Dart 版本各自翻译即可，因为 Layer 0 不使用任何语言特有特性
- **Layers 2-3** 是平台无关的核心逻辑，写一次即可
- **Layer 4** 是极薄的框架适配层，每个平台约 20 行代码

### 3.2 Current vs Proposed

| Aspect | Current | Proposed |
|--------|---------|----------|
| Event types | 25+ flat strings | 8 namespaces, ~24 structured types |
| Type safety | `[key: string]: any` | Discriminated union per event |
| Source of truth | Implicit convention | Protocol spec document (Layer 0) |
| Client logic | Embedded in React hooks | Pure reducer (framework-agnostic) |
| Multi-platform | Rewrite per platform | Share Layers 1-3, thin Layer 4 adapter |
| Transport | Hardcoded SSE | Pluggable (SSE/WebSocket/Mock) |

## 4. Event Type System

### 4.1 Namespace Hierarchy

从现有 25+ 个扁平类型映射到 8 个命名空间，使用 `category.action` 的命名格式。

#### lifecycle/ — Task 生命周期

| Event Type | Old Type | Persistence | Description |
|-----------|----------|-------------|-------------|
| `lifecycle.started` | `start` | canonical | Task 开始执行 |
| `lifecycle.completed` | `result` | canonical | Task 成功完成 |
| `lifecycle.failed` | `error` | canonical | Task 执行失败 |
| `lifecycle.cancelled` | `cancel_requested` | canonical | Task 被取消 |

#### content/ — AI 输出内容流

| Event Type | Old Type | Persistence | Description |
|-----------|----------|-------------|-------------|
| `content.delta` | `token`, `result_delta` | transient | 增量文本片段 |
| `content.done` | `result` (body) | canonical | 完整文本内容 |

#### thinking/ — 思维链

| Event Type | Old Type | Persistence | Description |
|-----------|----------|-------------|-------------|
| `thinking.delta` | `thinking` | transient | 增量思维链片段 |
| `thinking.done` | *(new)* | transient | 思维链完成信号 |

#### tool/ — 工具调用

| Event Type | Old Type | Persistence | Description |
|-----------|----------|-------------|-------------|
| `tool.started` | `tool_call_started` | canonical | 工具调用开始 |
| `tool.completed` | `tool_call_finished` | canonical | 工具调用成功 |
| `tool.failed` | `tool_call_failed` | canonical | 工具调用失败 |

#### interaction/ — 人机交互

| Event Type | Old Type | Persistence | Description |
|-----------|----------|-------------|-------------|
| `interaction.question` | `question` | canonical | 向用户提问 |
| `interaction.answer` | `user_reply` | canonical | 用户回答 |

#### artifact/ — 产物管理

| Event Type | Old Type | Persistence | Description |
|-----------|----------|-------------|-------------|
| `artifact.created` | `file` | canonical | 文件/产物生成 |
| `artifact.staged` | `stage_artifacts` | canonical | 产物暂存到预览区 |

#### progress/ — 执行进度

| Event Type | Old Type | Persistence | Description |
|-----------|----------|-------------|-------------|
| `progress.step` | `step` | canonical | 步骤更新 |
| `progress.node` | `node` | transient | 节点状态（实时） |
| `progress.plan` | `plan` | canonical | 执行计划 |
| `progress.brief` | `brief` | canonical | 简要进度描述 |
| `progress.dag` | `task_dag`, `dag_progress` | transient | DAG 拓扑/进度 |
| `progress.checkpoint` | `checkpoint` | canonical | 检查点 |

#### system/ — 系统级

| Event Type | Old Type | Persistence | Description |
|-----------|----------|-------------|-------------|
| `system.monitoring` | `monitoring` | transient | 监控数据 |
| `system.heartbeat` | *(new)* | transient | 连接保活 |

### 4.2 Persistence Rules

- **canonical** — 持久化到 DB，有 `seq` 编号，断线重连后可重放（通过 `seq` 去重）
- **transient** — 仅实时推送，无 `seq` 编号，断线即丢失，不可重放

### 4.3 Extension Rules

- 新增事件 = 新增一个 `category.action` 类型
- 客户端遇到未知 `type` → 忽略（forward-compatible）
- 新增整个分类 = 新增命名空间（如 `billing/`）

## 5. Event Envelope & Payload Schema

### 5.1 Base Envelope

所有事件共享以下基础字段：

```
EventBase:
  type:      string    # "category.action" 格式，discriminator
  taskId:    string    # 所属 task ID
  timestamp: string    # ISO 8601 时间戳
```

Canonical 事件额外包含：

```
CanonicalEventBase extends EventBase:
  seq:       number    # 单调递增序列号，用于去重和重放
```

### 5.2 Event-Specific Payloads

每个事件类型有严格定义的 `payload` 结构。以下是核心事件的 payload 定义：

#### lifecycle.started

```
payload:
  config:    object?   # 可选的 task 配置信息
```

#### lifecycle.completed

```
payload:
  summary:   string?   # 可选的完成摘要
```

#### lifecycle.failed

```
payload:
  code:      string    # 错误代码
  message:   string    # 错误描述
  details:   object?   # 可选的详细信息
```

#### lifecycle.cancelled

```
payload:
  reason:    string?   # 取消原因
```

#### content.delta

```
payload:
  text:      string    # 增量文本片段
```

#### content.done

```
payload:
  text:      string    # 完整文本内容
```

#### thinking.delta

```
payload:
  text:      string    # 增量思维链片段
```

#### thinking.done

```
payload:              # 空 payload，仅作为信号
```

#### tool.started

```
payload:
  toolCallId: string   # 工具调用唯一 ID
  name:       string   # 工具名称
  args:       object   # 工具参数
```

#### tool.completed

```
payload:
  toolCallId: string   # 对应的工具调用 ID
  output:     any      # 工具执行结果
```

#### tool.failed

```
payload:
  toolCallId: string   # 对应的工具调用 ID
  error:      string   # 错误描述
```

#### interaction.question

```
payload:
  interruptType: "clarification" | "human_input"
  content:       string    # 问题内容
  context:       string?   # 可选上下文
  placeholder:   string?   # 输入框占位符
```

#### interaction.answer

```
payload:
  content:   string    # 用户回答内容
```

#### artifact.created

```
payload:
  name:      string    # 文件/产物名
  url:       string?   # 可选的访问 URL
  mimeType:  string?   # MIME 类型
  size:      number?   # 文件大小（bytes）
```

#### artifact.staged

```
payload:
  artifacts: array     # [{ name, url, mimeType }]
```

#### progress.step

```
payload:
  name:      string    # 步骤名称
  status:    "running" | "completed" | "failed"
```

#### progress.node

```
payload:
  nodeId:    string    # 节点 ID
  status:    string    # 节点状态
  metadata:  object?   # 节点元数据
```

#### progress.plan

```
payload:
  steps:     array     # [{ name, description, status }]
```

#### progress.brief

```
payload:
  text:      string    # 简要描述
```

#### progress.dag

```
payload:
  nodes:     array     # [{ id, label, status, deps }]
```

#### progress.checkpoint

```
payload:
  data:      object    # 检查点数据
```

#### system.monitoring

```
payload:
  metrics:   object    # 监控指标
```

#### system.heartbeat

```
payload:              # 空 payload，仅保活信号
```

### 5.3 Discriminated Union

事件通过 `type` 字段进行区分。在 TypeScript 中表现为：

```typescript
type ProtocolEvent =
  | LifecycleStartedEvent | LifecycleCompletedEvent
  | LifecycleFailedEvent  | LifecycleCancelledEvent
  | ContentDeltaEvent     | ContentDoneEvent
  | ThinkingDeltaEvent    | ThinkingDoneEvent
  | ToolStartedEvent      | ToolCompletedEvent    | ToolFailedEvent
  | InteractionQuestionEvent | InteractionAnswerEvent
  | ArtifactCreatedEvent  | ArtifactStagedEvent
  | ProgressStepEvent     | ProgressNodeEvent     | ProgressPlanEvent
  | ProgressBriefEvent    | ProgressDagEvent      | ProgressCheckpointEvent
  | SystemMonitoringEvent | SystemHeartbeatEvent
```

在 Rust 中表现为 `enum ProtocolEvent { ... }`，在 Swift 中表现为 `enum ProtocolEvent { ... }` — 语言不同，结构相同。

## 6. Task Lifecycle State Machine

### 6.1 States

| State | Description | Terminal |
|-------|-------------|----------|
| `queued` | Task 已创建，等待执行 | No |
| `running` | Task 正在执行，事件流进行中 | No |
| `waiting_for_user` | Task 暂停，等待用户回答问题 | No |
| `succeeded` | Task 成功完成 | Yes |
| `failed` | Task 执行失败 | Yes |
| `cancelled` | Task 被用户取消 | Yes |

### 6.2 Transitions

```
queued ──lifecycle.started──→ running

running ──interaction.question──→ waiting_for_user
waiting_for_user ──interaction.answer──→ running

running ──lifecycle.completed──→ succeeded
running ──lifecycle.failed──→ failed
(any non-terminal) ──lifecycle.cancelled──→ cancelled
```

**Rules:**

- `running` ↔ `waiting_for_user` 可来回切换（支持多轮交互）
- `succeeded` / `failed` / `cancelled` 是终态，不可转出
- 任何非终态都可以 → `cancelled`（用户随时可取消）
- 状态转换**必须**由对应的 lifecycle/interaction 事件触发

### 6.3 REST API Contract

| Method | Endpoint | Purpose | Response |
|--------|----------|---------|----------|
| POST | `/tasks` | 创建任务 | `{ taskId, status: "queued" }` |
| GET | `/tasks/{id}` | 获取快照 | `TaskSnapshot` |
| GET | `/tasks/{id}/events` | SSE 事件流 | `text/event-stream` |
| POST | `/tasks/{id}/reply` | 回答问题 | `{ taskId, status: "running" }` |
| POST | `/tasks/{id}/cancel` | 取消任务 | `{ taskId, status: "cancelled" }` |
| GET | `/tasks/{id}/replay` | 完整回放 | `{ task: TaskSnapshot, events: ProtocolEvent[] }` |

### 6.4 SSE Connection

**连接参数:**

- `GET /tasks/{id}/events?after_seq=42` — 从 seq=42 之后开始（断线重连）
- `Last-Event-Id: 42` — SSE 标准 header，效果同 `after_seq`

**SSE 帧格式:**

```
id: 42
event: content.delta
data: {"type":"content.delta","taskId":"...","timestamp":"...","payload":{"text":"hello"}}
```

- `id` 字段仅 canonical 事件有（对应 `seq`）
- `event` 字段为事件类型
- `data` 字段为完整的 JSON 事件对象

## 7. Client Architecture (Layers 2-4)

### 7.1 Layer 2 — Transport

Transport 层负责建立连接、解析 SSE/WebSocket 帧、处理重连。

**接口定义:**

```
EventTransport:
  connect(taskId: string, opts?: { afterSeq?: number }) → EventStream

EventStream:
  onEvent(handler: (event: ProtocolEvent) → void) → void
  onError(handler: (error: Error) → void) → void
  onClose(handler: () → void) → void
  close() → void
```

**Implementations:**

| Transport | Use Case | Key Feature |
|-----------|----------|-------------|
| `SSETransport` | Current default | EventSource + auto-reconnect + seq replay |
| `WebSocketTransport` | Future (mobile) | 双向通信 |
| `MockTransport` | Testing | 注入预设事件序列 |

Transport 的输出是**类型化的 `ProtocolEvent`**，传递给 Layer 3。

### 7.2 Layer 3 — State Machine (Pure Reducer)

核心是一个纯函数 reducer，接收当前状态和事件，返回新状态：

```
function reduce(state: TaskState, event: ProtocolEvent) → TaskState
```

**TaskState 结构:**

```
TaskState:
  status:    "queued" | "running" | "waiting_for_user" | "succeeded" | "failed" | "cancelled"
  content:   string          # 累积的文本内容
  thinking:  string          # 累积的思维链
  toolCalls: ToolCallState[] # [{ id, name, args, status, output?, error? }]
  artifacts: ArtifactState[] # [{ name, url?, mimeType? }]
  question:  QuestionState?  # 当前待回答的问题
  steps:     StepState[]     # 执行步骤列表
  plan:      PlanState?      # 执行计划
  lastSeq:   number          # 最后处理的 canonical seq（用于重连）
```

**Key properties:**

- **确定性**: 给同样的事件序列，一定产生同样的状态 → 可测试、可调试
- **可回放**: replay 端点返回事件数组，客户端 reduce 一遍就能恢复完整状态
- **跨平台**: 纯函数，不依赖任何 UI 框架
- **时间旅行**: 保存事件日志就能回到任意历史状态

### 7.3 Layer 4 — Framework Binding

每个 UI 框架只需一个极薄的适配层（约 20 行代码），将 Transport → Reducer 的管道接入框架的响应式系统。

**React:**

```
useTaskStream(taskId) → TaskState
  // useState + useEffect, ~20 lines
```

**SwiftUI:**

```
@Observable TaskViewModel
  // Combine / async stream, ~20 lines
```

**Rust/egui:**

```
TaskStore struct
  // channel + repaint, ~20 lines
```

**Flutter:**

```
ChangeNotifier
  // StreamSubscription, ~20 lines
```

## 8. Migration Strategy

### 8.1 Backend Changes (chat-dada)

- 重构 `EventType` 枚举：从扁平字符串改为 `category.action` 格式
- 重构 `emit_event` / `emit_progress`：统一使用新的事件信封格式
- 更新 SSE 帧格式：`event` 字段使用新的事件类型名
- 数据库 `TaskEvent` 模型：`event_type` 列存储新格式

### 8.2 Frontend Changes (chat-dada-front)

- 删除 `StreamEvent` 的 `[key: string]: any` 接口
- 实现 Layer 1 类型定义（discriminated union）
- 实现 Layer 2 `SSETransport`（从 `useTaskStream.ts` 中抽取连接逻辑）
- 实现 Layer 3 `reduce()` 纯函数（从 `useTaskStream.ts` 和 `taskHelpers.ts` 中抽取状态逻辑）
- 实现 Layer 4 `useTaskStream` hook（约 20 行，仅做 React 绑定）

### 8.3 Breaking Changes

当前前端允许破坏性变更，不需要向后兼容层。

## 9. Non-Goals

- 第三方客户端 SDK（暂不考虑）
- JSON Schema 维护（不维护双份 artifact）
- OpenAI / MCP / A2A 等外部协议兼容（自有协议优先）
- 认证/鉴权协议（不在本次范围内）
