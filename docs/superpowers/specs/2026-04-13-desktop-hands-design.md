# Desktop Hands — 桌面端工具执行通道

> Tauri 桌面端作为 Hands 执行端，通过 WebSocket 向服务端暴露预装工具能力。服务端 Brain 可将工具调用路由到用户桌面执行，产物直接在本地生成。

## Context

当前 PPT 生成全链路在服务端完成：Brain 编排 → OfficeCLI 在服务器执行 → .pptx 存入 `outputs/` → 用户通过 HTTP 下载。

问题：

1. 用户在 Tauri 桌面端使用时，生成的 .pptx 需要先传到服务器再下载回来，多余一跳
2. OfficeCLI 等本地工具无法利用用户桌面的资源（本地模板、字体、Office 应用）
3. 架构上，`agent/hands/` 的 Brain/Hands 分层已经为远程执行端做了设计（ToolGateway 支持 `local`/`remote` 路由），但桌面端执行尚未实现

目标：让 Tauri 客户端成为一个真正的 Hands 执行端，PPT 等工具调用可以在用户桌面完成。

## Goals

| Goal | Description |
|------|-------------|
| **Desktop Hands 通道** | Tauri 通过 WebSocket 与服务端建立双向工具调用通道 |
| **能力自动上报** | 客户端连接时主动上报本地可用工具清单 |
| **结构化工具调用** | 只暴露结构化操作（create/add/set/...），不暴露任意命令行 |
| **优雅降级** | 桌面端不在线时自动 fallback 到服务端执行，对 workflow 透明 |
| **本地产物** | 生成的文件保存到用户本地目录，支持一键打开 |
| **复用权限分级** | 复用 Tauri 已有的 Safe/Cautious/Dangerous 权限机制 |

## Non-Goals

- 通用 CLI 自动发现（只做预装工具）
- MCP Server 兼容
- 用户自行安装工具/插件
- 文件上传同步到服务端
- 多设备同时在线冲突处理

## Architecture

### 系统全景

```
┌─ 服务端 (chat-dada) ──────────────────────────────────────────┐
│                                                                │
│  Coordinator → PPT Workflow → officecli 结构化操作              │
│                    ↓                                           │
│  ToolGateway.execute(tool_call)                                │
│   ├─ 无 Desktop 连接 → LocalToolExecutor (服务端 OfficeCLI)    │
│   └─ 有 Desktop 连接 → DesktopToolExecutor                     │
│                            ↓                                   │
│  DesktopHandsManager                                           │
│   ├─ 管理所有客户端连接 (user_id → connection)                  │
│   ├─ 维护每个客户端的 capabilities 缓存                         │
│   └─ 路由 tool_call 到对应用户的连接                            │
│                            ↓                                   │
│  WebSocket endpoint: /ws/desktop-hands                         │
│                                                                │
└────────────────────┬───────────────────────────────────────────┘
                     │ wss://
┌────────────────────┴───────────────────────────────────────────┐
│  Tauri 客户端 (chat-dada-front)                                │
│                                                                │
│  DesktopHandsClient                                            │
│   ├─ WebSocket 长连接 + 自动重连                                │
│   ├─ 启动时扫描预装工具 → 上报 capabilities                     │
│   ├─ 收到 tool_call → 权限检查 → 分发执行                       │
│   ├─ 推送 tool_progress（实时进度）                              │
│   └─ 返回 tool_result（成功/失败 + artifacts）                   │
│                                                                │
│  预装工具                                                       │
│   └─ OfficeCLI (Tauri sidecar)                                  │
│                                                                │
│  已有 Tool System (Rust)                                        │
│   └─ shell / filesystem / screenshot / ...                      │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

### ToolGateway 路由决策

```
tool_call 进入 ToolGateway
    ↓
1. tool_call.tool_name 在 DESKTOP_ELIGIBLE_TOOLS 中？
   └─ 否 → LocalToolExecutor（服务端）
    ↓
2. context.user_id 有活跃的 Desktop Hands 连接？
   └─ 否 → LocalToolExecutor（fallback）
    ↓
3. tool_call.tool_name 在客户端 capabilities 中？
   └─ 否 → LocalToolExecutor（fallback）
    ↓
4. → DesktopToolExecutor（桌面端执行）
```

Web 端用户和 Tauri 端用户走同一个 PPT workflow，只是执行位置不同。

## WebSocket Protocol

### 连接

```
wss://{server}/ws/desktop-hands?token={session_token}
```

认证复用已有的 session token（cookie 中的 `chat_dada_session`）。

### 消息格式

所有消息为 JSON，统一信封：

```json
{
  "type": "tool_call",
  "id": "msg_abc123",
  "timestamp": "2026-04-13T10:00:00Z",
  "payload": { ... }
}
```

### 消息类型

#### C→S: `capabilities`（连接握手）

客户端连接后立即发送，上报本地可用工具：

```json
{
  "type": "capabilities",
  "id": "msg_001",
  "timestamp": "...",
  "payload": {
    "client_version": "0.2.0",
    "platform": "darwin-aarch64",
    "tools": [
      {
        "name": "officecli",
        "version": "1.2.0",
        "operations": [
          {
            "name": "create",
            "description": "创建新的 Office 文档",
            "parameters": {
              "type": "object",
              "properties": {
                "filename": {"type": "string"},
                "format": {"enum": ["pptx", "docx", "xlsx"]}
              },
              "required": ["filename"]
            },
            "permission_level": "cautious"
          },
          {
            "name": "add",
            "description": "向文档添加内容",
            "parameters": { ... },
            "permission_level": "cautious"
          },
          {
            "name": "set",
            "description": "设置文档属性",
            "parameters": { ... },
            "permission_level": "cautious"
          },
          {
            "name": "get",
            "description": "查询文档内容或属性",
            "parameters": { ... },
            "permission_level": "safe"
          },
          {
            "name": "query",
            "description": "搜索文档内容",
            "parameters": { ... },
            "permission_level": "safe"
          },
          {
            "name": "validate",
            "description": "校验文档结构",
            "parameters": { ... },
            "permission_level": "safe"
          },
          {
            "name": "batch",
            "description": "批量执行多个操作",
            "parameters": {
              "type": "object",
              "properties": {
                "operations": {
                  "type": "array",
                  "items": {"$ref": "#/definitions/operation"}
                }
              }
            },
            "permission_level": "cautious"
          },
          {
            "name": "watch",
            "description": "监听文档变更",
            "parameters": { ... },
            "permission_level": "safe"
          }
        ]
      }
    ]
  }
}
```

#### S→C: `capabilities_ack`

```json
{
  "type": "capabilities_ack",
  "id": "msg_002",
  "payload": {
    "accepted": ["officecli"],
    "rejected": []
  }
}
```

#### S→C: `tool_call`（下发执行）

```json
{
  "type": "tool_call",
  "id": "msg_010",
  "payload": {
    "invocation_id": "inv_abc123",
    "task_id": "task_xyz",
    "tool": "officecli",
    "operation": "create",
    "params": {
      "filename": "quarterly-report.pptx"
    },
    "timeout_ms": 30000
  }
}
```

模型只能调用已上报的结构化操作，不能传任意命令字符串。

#### C→S: `tool_progress`（执行进度）

```json
{
  "type": "tool_progress",
  "id": "msg_011",
  "payload": {
    "invocation_id": "inv_abc123",
    "progress": 0.5,
    "message": "正在写入第 3/6 张幻灯片"
  }
}
```

#### C→S: `tool_result`（执行结果）

```json
{
  "type": "tool_result",
  "id": "msg_012",
  "payload": {
    "invocation_id": "inv_abc123",
    "success": true,
    "output": "Created quarterly-report.pptx with 6 slides",
    "artifacts": [
      {
        "type": "local_file",
        "name": "quarterly-report.pptx",
        "path": "/Users/xxx/Documents/ChatDaDa/quarterly-report.pptx",
        "mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "size_bytes": 245760
      }
    ],
    "execution_time_ms": 3200
  }
}
```

#### S→C: `tool_cancel`（取消执行）

```json
{
  "type": "tool_cancel",
  "id": "msg_013",
  "payload": {
    "invocation_id": "inv_abc123",
    "reason": "task_cancelled"
  }
}
```

#### 双向: `ping` / `pong`（心跳）

30 秒间隔，3 次未响应判定断连。

## 结构化操作（不暴露任意命令行）

模型看到的工具定义是结构化的操作列表，而非 `shell(command: str)`。

以 OfficeCLI 为例，暴露的操作集：

| 操作 | 说明 | 权限级别 |
|:---|:---|:---|
| `create` | 创建新文档 | Cautious |
| `add` | 添加内容（幻灯片、段落、图表等） | Cautious |
| `set` | 设置属性（标题、样式、布局等） | Cautious |
| `get` | 查询内容或属性 | Safe |
| `query` | 搜索文档内容 | Safe |
| `validate` | 校验文档结构 | Safe |
| `batch` | 批量执行多个操作 | Cautious |
| `watch` | 监听文档变更 | Safe |

Tauri 端收到 `tool_call` 后，将结构化参数转换为 OfficeCLI 的具体命令执行。这个转换逻辑在客户端内部，模型无法直接控制命令行。

## 文件输出

### 输出目录

用户可在 Tauri 应用设置中配置输出目录，默认：

- macOS: `~/Documents/ChatDaDa/`
- Windows: `%USERPROFILE%\Documents\ChatDaDa\`

每个任务在该目录下创建子目录：`{output_dir}/{task_id}/`

### 前端展示

当 `artifact_ref.type === "local_file"` 时，TaskCard 展示本地文件卡片：

```
┌─────────────────────────────────────┐
│ 📄 quarterly-report.pptx            │
│ ~/Documents/ChatDaDa/task_xyz/      │
│ [打开文件]  [打开文件夹]             │
└─────────────────────────────────────┘
```

"打开文件"调用 Tauri 的 `shell.open()` API，用系统默认应用打开。
"打开文件夹"调用 `shell.open()` 打开所在目录。

## 安全模型

复用 Tauri 端已有的三级权限：

| 级别 | 行为 | 示例 |
|:---|:---|:---|
| **Safe** | 自动执行，无弹窗 | get, query, validate |
| **Cautious** | 首次确认，后续同类自动 | create, add, set, batch |
| **Dangerous** | 每次确认 | （当前无，预留） |

确认弹窗示例："服务端请求创建 quarterly-report.pptx，允许？ [允许] [始终允许] [拒绝]"

用户选择"始终允许"后，同一工具的同一操作类型在当前会话内不再弹窗。

## 连接生命周期

```
Tauri 启动
    ↓
用户登录
    ↓
建立 WebSocket 连接 → 上报 capabilities
    ↓
保持心跳（30s 间隔）
    ↓
收到 tool_call → 权限检查 → 执行 → 回传 result
    ↓
断线 → 指数退避重连（1s, 2s, 4s, ... max 30s）
    ↓
重连成功 → 重新上报 capabilities
    ↓
用户登出或关闭应用 → 正常断开
```

服务端侧：连接断开后，DesktopHandsManager 移除该用户的连接和 capabilities 缓存。后续工具调用自动 fallback 到 LocalToolExecutor。

## File Changes

### 服务端 (chat-dada)

| 文件 | 改动 |
|:---|:---|
| `web/routers/desktop_hands.py` | **新增** WebSocket endpoint `/ws/desktop-hands`，认证、消息分发 |
| `agent/hands/desktop_manager.py` | **新增** DesktopHandsManager：连接管理、capabilities 缓存、消息路由 |
| `agent/hands/desktop_executor.py` | **新增** DesktopToolExecutor：实现 ToolExecutor 协议，通过 WebSocket 下发调用并等待结果 |
| `agent/hands/gateway.py` | **修改** 添加 desktop 路由分支和降级逻辑 |
| `agent/hands/protocol.py` | **修改** ToolResult.artifacts 支持 `local_file` 类型 |
| `agent/session/protocol.py` | **修改** 新增 `tool.routed_desktop` 事件类型（记录工具被路由到桌面端） |

### 前端 (chat-dada-front)

| 文件 | 改动 |
|:---|:---|
| `src/desktop-hands/client.ts` | **新增** DesktopHandsClient：WebSocket 连接管理、能力上报、消息收发 |
| `src/desktop-hands/executor.ts` | **新增** 本地工具执行器：接收 tool_call → 权限检查 → 调用 Tauri sidecar → 返回结果 |
| `src/desktop-hands/types.ts` | **新增** 协议消息类型定义 |
| `src/desktop-hands/officecli.ts` | **新增** OfficeCLI 操作 → 命令转换层（结构化参数 → CLI 命令） |
| `src-tauri/tauri.conf.json` | **修改** 声明 OfficeCLI sidecar (`bundle.externalBin`) |
| `src-tauri/binaries/` | **新增** OfficeCLI 平台二进制文件 |
| `src/components/chat/TaskCard.tsx` | **修改** 支持展示 `local_file` artifact 卡片 |
| `src/components/settings/SettingsModal.tsx` | **修改** 添加输出目录配置项 |
