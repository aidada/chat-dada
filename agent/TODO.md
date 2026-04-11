# Agent 架构重构 TODO

> 基线：v3.2 设计文档 `docs/设计文档/系统架构/2026-04-09-managed-agents-architecture-evolution.md`
> 更新：2026-04-10

## Phase 1：恢复能力归位到 Session

- [x] 1.1 Coordinator checkpointer 持久化（`build_coordinator_graph(checkpointer)` 接收外部注入）
- [x] 1.2 引入 `Session.wake(task_id)` 语义（`agent/session/runtime.py`）
- [ ] 1.3 校验 CoordinatorState 恢复字段完整性
  - 确认 `completed_tasks`, `failed_tasks`, `task_vars`, `skill_runs`, `task_dag` 均在 checkpoint 中持久化
  - 用 `aget_state()` 断言验证
- [ ] 1.4 明确 checkpoint 归属 Session 的代码边界
  - Brain 层代码不直接操作 checkpoint 存储结构
- [ ] 1.5 清理 `_dag_resume_state` 手动恢复机制
  - 验证持久化 checkpoint 能完整恢复 6 个字段后，删除 `executor.py` 手动序列化 + `root_graph.py` 手动反序列化（L79-91）
  - 如果不能完整恢复，明确两者分工边界

## Phase 2：Session 层独立 + 真相源归位

- [x] 2.1 提取 `SessionRuntime`（含 `emit_event` + `emit_progress` 双接口）
- [ ] 2.2 `task_events` 成为 canonical history，`task_runs` 降级为 projection
  - [ ] 确认前端 replay contract：`start/step/task/node/checkpoint/brief/plan/question/user_reply/file/error/result/stage_artifacts` 必须 canonical
  - [ ] transient 降级白名单：`token/streaming_content/thinking/dag_progress/result_delta/monitoring_live/custom`
  - [ ] `review` 事件归属确认：canonical 还是只走 projection/API
- [x] 2.3 清理 TaskRunStore asyncpg.Pool 残留（主路径已用 SessionFactory）
  - [ ] `_recover_interrupted_tasks()` 中 `self.pool.fetch()` → SessionFactory
  - [ ] `ConversationContextBuilder` 接收 SessionFactory 而非 raw pool
  - [ ] 删除 `TaskRunStore.pool` 字段和 `connect()/close()` 中的 pool 管理
- [x] 2.3b Conversation 方法独立安置到 `ConversationService`
- [ ] 2.4 `_runner_tasks` 降级为 process-local cancel handle
  - [ ] 新增 `Session.request_cancel()` / `is_cancel_requested()`（已在 SessionRuntime 中定义）
  - [ ] `cancel_running_task()` 先写 canonical cancel 事件，再尽力取消本地 runner
  - [ ] 跨实例 cooperative cancel 协议（依赖 Redis 传播）
- [ ] 2.5 拆解 `request_payload` JSONB 万能口袋
  - [ ] `clarification_history` canonical source → `task_events`
  - [ ] SessionRuntime 提供 `get_clarification_history()` 从事件流重建（已有过渡实现）
  - [ ] `pending_question` / `nested_interrupt_pending` 拆出为明确 projection
  - [ ] `interrupt_state` / `_dag_resume_state` 删除（依赖 Phase 1.5）
- [ ] 2.6 TaskService 重构为纯 Harness Runtime
  - [x] 注入 SessionRuntime，`record_event()` 委托给 `session.emit_event()`
  - [ ] `_execute_task()` 内所有 `_store.xxx()` 迁移到 `session.xxx()`
  - [ ] 删除 TaskService 对 `TaskRunRepository` / `TaskEventRepository` 的直接依赖
- [ ] 2.7 明确 Brain 与 Session 边界
  - `grep -rn "TaskRunRepository\|TaskEventRepository" agent/` 结果应为零
- [x] 2.8 `_safe_emit` 进度流统一迁移
  - [x] 10 处散装 `_safe_emit` → `agent/platform/emit.py` 收口
  - [ ] `direct_answer` 的 ad-hoc token writer → `emit_progress`
  - [ ] 验证 `stream_nested_graph()` transport bridge 未被误删

## Phase 3：Harness 无状态化 + Brain↔Hands 解耦

- [x] 3.1 明确 Harness 边界（TaskService + root_graph + Coordinator + domain skill runners）
- [x] 3.2 ToolProtocol 补充 prepare/provision 语义（`agent/hands/protocol.py`）
- [x] 3.3 LocalExecutor 封装服务端工具（`agent/hands/local_executor.py`）
- [ ] 3.4 RemoteDesktopExecutor + Tauri transport
  - [ ] 服务端 desktop request push + result callback + 在线会话管理
  - [ ] `chat-dada-front` Tauri 端订阅待执行请求 + 回传结果
  - [ ] `request_id`/`task_id`/`tool_name`/`timeout_ms` 稳定协议字段
  - [ ] 取消、超时、掉线 transport 状态机
- [x] 3.5 ToolGateway 成为唯一 tool-call 事件权威点（`agent/hands/gateway.py`）
  - [ ] 接入 `LocalToolExecutor`，注册现有 `agent/tools/` 工具
  - [ ] `bind_deepagents_tools()` 从过渡实现迁移到纯 gateway adapter
- [ ] 3.6 Domain Skill Runner 改造成纯编排
  - [ ] `research/worker.py` + `research/tools.py`：砍掉 `from agent.tools.xxx import`，改用 gateway
  - [ ] `patent/agent.py`：`get_patent_tools()` → `gateway.bind_deepagents_tools("patent")`
  - [ ] `zero_report/agent.py`：`get_zero_report_tools()` → `gateway.bind_deepagents_tools("zero_report")`
  - [ ] `ppt/workflow.py`：`get_ppt_tools()` → `gateway.bind_deepagents_tools("ppt")`
- [ ] 3.7 Hands taxonomy 文档化：Local Desktop Hands vs Ephemeral Remote Hands

## Phase 4：安全边界 + 可观测性

- [ ] 4.1 凭证永不进入 Hands
  - [ ] `ToolContext.get_secret()` 接入 VaultService（当前读 env var）
  - [ ] 桌面端从 OS-native store 自行读取本地凭证
  - [ ] 跨边界敏感能力走代理 API / capability token
- [ ] 4.2 business event 与 transport 分离
  - [ ] `EventType` 枚举白名单验证（`task_event_repo.py`）
  - [ ] SSE `id` 字段只分配给 canonical events
  - [ ] `emit_progress()` 走 SSE 但不写 `id`，前端不纳入 seq 去重
- [ ] 4.3 补充 context strategy 可观测性
  - [ ] `last_event_seq` / `latest_checkpoint_id` / `harness_context_strategy` 可观测字段

## 验证检查点

- [ ] `grep -rn "TaskRunRepository\|TaskEventRepository" agent/` → 零结果
- [ ] `grep -rn "asyncpg" agent/` → 零结果
- [ ] `rg -n "^def _safe_emit|_safe_emit\(" agent/` → 仅 `platform/emit.py`
- [ ] `rg -n "get_stream_writer\(" agent/` → 仅 `platform/emit.py` + `platform/streaming.py`
- [ ] `grep -rn "from agent\.tools\." agent/domains/` → 零结果
- [ ] 注入 Mock SessionRuntime + Mock ToolGateway，TaskService 全流程通过
- [ ] 页面刷新后 `taskPanelSteps` / `extractPlanModules` / `currentStageArtifactPanel` 恢复正常
- [ ] kill -9 后 `Session.wake(task_id)` 恢复执行，已完成 skill 不重跑
