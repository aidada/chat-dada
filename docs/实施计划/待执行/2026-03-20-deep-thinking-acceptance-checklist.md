# Deep Thinking 实施验收清单

## 目的

本文档将 [Deep Thinking 成熟度判断与实现指导](../../设计文档/系统架构/2026-03-20-deep-thinking-maturity-guide.md) 中的判断标准，转化为可执行、可验收、可挂到实施计划的检查项。

它回答两个实施层问题：

1. 每个重构阶段结束时，应该验证什么
2. 什么时候可以宣称某个领域 agent 具备成熟 deep thinking 能力

## 使用方式

- 每完成一个 Phase，先检查对应 `Phase Exit Criteria`
- 每准备让某个领域 agent 升级为默认路径，检查 `Domain Readiness Gate`
- 每准备对外宣称“成熟 deep thinking 能力”，检查 `Release Readiness Gate`

## 全局验收维度

所有阶段最终都要汇总到这 8 个维度：

1. Runtime
2. Planning
3. Memory / Context
4. Verifier / Reviewer
5. Budget
6. Artifact
7. HITL
8. Evaluation / Observability

如果某一阶段只实现局部能力，也必须明确它改善了哪几个维度。

## Phase Exit Criteria

### Phase 1: 平台骨架 + 流式协议 + HITL

退出条件：

- root graph 已成为新的执行入口候选
- `task_id -> thread_id` 映射已固定
- root graph 可稳定运行 legacy `general_chat` 与 `deep_research`
- graph v2 streaming 已能桥接为现有 SSE 事件
- root trace 已进入 LangSmith，且包含 `task_id`、domain、mode 等 metadata
- `UserMemoryProvider`、`ThreadMemoryProvider`、`CheckpointProvider` 接口已定义
- `reply_to_task` 已桥接到 graph `resume`
- 中断、恢复、继续 streaming 能贯通根图与子图
- `waiting_for_user` 不再是业务层私有状态，而是 graph-native interrupt
- agent 自定义 checkpoint 逻辑已从业务 memory 中剥离
- LangGraph checkpointer 已接管恢复路径
- router 已支持 `direct_chat / domain_selected / needs_clarification` 语义
- `BudgetPolicy` 与 `RetryPolicy` 接口已定义（本阶段只定义接口）

通过标准：

- Runtime: 基础通过 + 中断恢复通过
- HITL: 基础通过
- Memory / Context: 接口通过，checkpoint 职责边界通过
- Budget: 接口已定义（尚未集成到领域）
- Evaluation / Observability: tracing 基础通过

不通过信号：

- root graph 只是旁路 demo，主流程仍无法切换
- streaming 桥接不稳定，前端语义丢失
- tracing 没有 root-level 元数据
- 回复用户后只能从头执行
- 子图 interrupt 无法恢复
- 业务层仍保留平行 checkpoint 主路径

### Phase 2: Research 领域 Agent + Deepagents Harness

退出条件：

- `domain_agents/research` 已成为独立目录和执行单元
- parallel workers 已变成 graph-native fan-out/fan-in
- 并行 worker 有容错策略：部分失败时已有结果仍可用于合成
- `EvidenceItem` / `CitationMap` 已成为结构化 schema
- research review gate 已使用 `ReviewGate` 框架：
  - structural_checks：引用完整性（正则匹配）、字段完整性（Pydantic）
  - semantic_checks：结构完整性、研究缺口提示（LLM）
- browser capability 已被 research 域接入，但不是默认执行路径
- research agent 已存在 deepagents-backed 实现
- workspace memory（deepagents）与 product memory（user_store_v2）边界清晰
- deepagents subagents 可通过 root graph streaming 可见
- deepagents 没有吞掉 artifact / review gate / tracing 这些平台语义
- 保留非-deepagents fallback 路径
- `capabilities/review_gates.py` 基础框架已可用
- `capabilities/ppt_capability.py` 已可由 research renderers 调用
- `BudgetPolicy` 已在 research 关键节点集成

通过标准：

- Planning: 研究任务分解通过 + 复杂计划 harness 通过
- Artifact: research artifact 基础通过
- Verifier / Reviewer: research 域基础通过，review gate 区分 structural/semantic checks
- Memory / Context: workspace memory 通过，Domain Context 保真压缩通过
- Runtime: deepagents 与平台 runtime 兼容通过
- Budget: 全局配额检查已集成
- Evaluation / Observability: 并行子图可见性通过

不通过信号：

- 并行 worker 仍被包在单节点里
- 并行 worker 一个分支失败导致全图崩溃
- findings / citations 仍主要存在于自由文本中
- review gate 仍只是 prompt 约束，没有 structural_checks
- 引入 deepagents 后 root graph 失去可观测性
- AGENTS.md/workspace 与产品记忆混写
- deepagents 实现变成不可替换的黑盒

### Phase 3: Patent Agent

退出条件：

- `domain_agents/patent` 已具备独立 prompts / tools / schemas / reviewers
- 已有 `ClaimTree`、`PriorArtMatrix`、`SpecDraft` 等 artifact
- patent reviewer 使用 `ReviewGate` 框架：
  - structural_checks：权利要求依赖校验、必填字段检查
  - semantic_checks：术语一致性、现有技术映射覆盖度
- browser capability 仅在需要专利网站页面操作时启用
- `BudgetPolicy` 已在 patent 关键节点集成

通过标准：

- Artifact: patent artifact 基础通过
- Verifier / Reviewer: patent 域基础通过，reviewer 可输出结构化缺陷
- Budget: patent 域全局配额检查通过

不通过信号：

- 仍使用 research 的 artifact 语义勉强复用
- prior-art 证据无法映射到 claim 结构
- reviewer 无法给出结构化缺陷
- reviewer 没有 structural_checks，只靠 LLM 判断

### Phase 4: Zero Report Agent

退出条件：

- `domain_agents/zero_report` 已具备独立目录与 reviewers
- 已有 `Timeline`、`RootCauseTree`、`ActionMatrix` 等 artifact
- reviewer 使用 `ReviewGate` 框架：
  - structural_checks：行动项必须有责任人和时限、时间线事件有时间戳
  - semantic_checks：因果链闭环、时间线完整性
- 支持基于日志/工单/知识库证据的结构化归档
- `BudgetPolicy` 已在 zero_report 关键节点集成

通过标准：

- Artifact: zero-report artifact 基础通过
- Verifier / Reviewer: zero-report 域基础通过
- HITL: 审批与修订流程通过
- Budget: zero_report 域全局配额检查通过

不通过信号：

- 报告仍是线性长文，无结构对象
- 根因链与行动项无法回溯到证据
- reviewer 只能做表面文风检查

### Phase 5: 平台收口与默认切换

退出条件：

- root graph 已成为默认执行中心
- 旧 orchestrator/scheduler 主路径已退为兼容层或完成下线
- 共享能力层已稳定：
  - memory interfaces（5 层分层）
  - browser capability
  - evidence / citation
  - review gates（structural + semantic）
  - budget policy / retry policy
  - ppt capability
- research / patent / zero_report 已都能通过统一 streaming、interrupt、trace、artifact 协议工作
- `DomainRegistry` 已替代旧 `registry_summary()` 的路由职责

通过标准：

- Runtime: 平台级通过
- Memory / Context: 5 层分层通过
- Budget: 平台级全局配额通过
- Evaluation / Observability: 平台级通过

不通过信号：

- 领域 agent 仍大量绕过 root graph
- 各域仍自定义一套事件协议
- 平台共享能力无法稳定复用

### Phase 6: Artifact / Review / Tracing 闭环

退出条件：

- artifact / review gate / evidence 可通过 API 查询
- LangSmith dashboard / alert / eval 已建立
- 至少一个领域已有失败回放集和基准任务集
- 平台侧可以对 trace、artifact、review gate 做统一回放和审计

注：前端 UI 消费（React + 后续 Tauri）作为独立项目推进，不在本闭环验收范围。

通过标准：

- Artifact: 后端 API 闭环通过
- Evaluation / Observability: 平台级闭环通过

不通过信号：

- 结构化 artifact 存在，但无 API 可查询
- review gate 只能在日志中看到，无法通过 API 处理
- 评估与 tracing 仍停留在零散脚本或手工分析

## Domain Readiness Gate

当某个领域 agent 准备成为默认执行路径时，至少满足：

1. 有独立 artifact schema
2. 有 `ReviewGate` 实现（含 structural_checks + semantic_checks）
3. 有 evidence / citation 回溯链
4. 有 `BudgetPolicy` 集成（全局配额检查 + 接近阈值 interrupt）
5. 有 `RetryPolicy` 集成（工具/LLM 调用容错）
6. 有 interrupt / resume（多阶段人机对齐）
7. 有至少一组领域任务集评估结果
8. 有 PPT 输出能力（通过共享 `ppt_capability`）

否则只能算”可用原型”，不能算”默认域 agent”。

## Release Readiness Gate

当团队准备对外宣称“成熟 deep thinking 能力”时，至少满足：

1. Research / Patent / Zero Report 中至少一个域达到稳定默认
2. 平台级 runtime、streaming、interrupt、checkpoint 已统一
3. layered memory 已落地，不再混杂业务 checkpoint
4. verifier 不是 prompt 附属，而是显式 runtime 组件
5. artifact-first 输出已成为标准路径
6. 有 LangSmith trace、失败回放、基准任务集和成本指标

## 建议验收节奏

- Phase 1: 先验 runtime、streaming、HITL、checkpoint、BudgetPolicy/RetryPolicy 接口
- Phase 2: 先让 research 达到”深思原型可验证”（含 deepagents + review gate 框架 + 并行容错）
- Phase 3-4: 再让 patent / zero_report 复制成熟模式
- Phase 5: 再讨论平台级默认切换
- Phase 6: 再完成后端闭环与对外能力表述（前端独立推进）

## 最终结论

对本次重构而言，最重要的不是“尽快把 agent 做得像在深度思考”，而是：

> **让 deep thinking 变成有验收标准、有退出条件、有回放与评估的工程能力。**
