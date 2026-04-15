# Hard-Task Agent 平台重构实施计划

**Goal:** 将当前“custom runtime + orchestrator + leaf agents”架构，分阶段重构为以 LangGraph 为核心运行时、以 deepagents 为复杂领域 agent harness、以 research/patent/zero_report 为主要能力面的 hard-task agent 平台。

**Architecture:** 单根 LangGraph 作为执行中心；runtime 仅保留 API/SSE/存储外壳；领域 agent 作为 subgraph 或 deepagents-backed graph；统一采用 v2 streaming、interrupt、checkpoint、artifact 模型。

**Tech Stack:** Python 3.13, FastAPI, LangGraph, deepagents, PostgreSQL, Redis, filesystem/object storage, LangSmith

参考：

- [Hard-Task Agent 平台重构设计](../../设计文档/系统架构/2026-03-20-hard-task-agent-platform-design.md)
- [Browser Capability 设计](../../设计文档/系统架构/2026-03-20-browser-capability-design.md)
- [Deep Thinking 成熟度判断与实现指导](../../设计文档/系统架构/2026-03-20-deep-thinking-maturity-guide.md)
- [Deep Thinking 实施验收清单](./2026-03-20-deep-thinking-acceptance-checklist.md)
- [General Chat 定位与路由语义设计](../../设计文档/系统架构/2026-03-21-general-chat-positioning-design.md)

---

## 总体策略

重构采用 **兼容壳保留、执行中心内迁、领域能力先行** 的策略：

1. 外部 API 先保持不变
2. 内部先引入新的 root graph
3. 先迁 `deep_research`，把它做成新的第一个领域 agent
4. 稳定后再迁 `orchestrator` 和其他能力
5. 最后再删除旧 scheduler/template 主路径

这样做的好处是：

- 前端和调用方不需要一次性重写
- 可以边迁移边保留线上可用版本
- 先验证最复杂的 research 路径，再推广到 patent / zero_report

## 验收原则

每个 Phase 结束时，都应以 [Deep Thinking 实施验收清单](./2026-03-20-deep-thinking-acceptance-checklist.md) 作为退出标准，而不是只看“功能已经能跑”。

最低要求：

1. 必须明确该阶段改善了哪几个成熟度维度
2. 必须定义通过信号与不通过信号
3. 没有通过阶段验收，不进入下一阶段默认切换

## Browser Use 迁移原则

当前 `browser_use` 的实际代码落点只有：

1. `agents/search_agent.py::browser_navigate`
2. `agents/deep_research/run.py::browser_navigate`
3. `core/models.py::get_browser_use_llm`

本次重构要求：

1. 不再让 `browser_use` 以私有工具形式分散在各 agent 内
2. 上收为共享 `browser capability`
3. `research` 域优先接入，用于 JS-heavy 页面、多步导航、证据采集
4. `patent` 域仅在专利站点页面核实或补充检索时受控启用
5. `zero_report` 域仅在工单、监控、wiki 门户采集时按需启用
6. 不作为平台默认执行路径，必须受 domain policy、budget、review gate 约束
7. 保留 `get_browser_use_llm(...)` 作为底层模型适配层，但由共享 capability 统一消费

---

## Phase 1: 平台骨架 + 流式协议 + HITL

### 目标

把 LangGraph 从 leaf-agent 内部实现提升为平台运行时骨架，并统一 streaming / user reply / interrupt 为 LangGraph-native 语义。

### 交付物

- `platform/` 目录（root graph / state / router / streaming / interrupts / tracing / domain_registry / renderer_registry）
- `RootState` / root graph skeleton
- 统一的 graph streaming adapter + v2 `StreamPart` 到前端事件的稳定映射
- `task_id -> thread_id` 映射策略
- memory 分层接口雏形（user / thread / checkpoint）
- review/clarification interrupt 机制
- `reply_to_task` 与 `Command(resume=...)` 桥接
- LangGraph checkpointer 接管执行恢复
- `capabilities/budget_policy.py` 与 `capabilities/retry_policy.py` 接口定义

### 任务

1. 创建 `platform/` 目录和基础模块：
   - `platform/root_graph.py`
   - `platform/state.py`
   - `platform/router.py`
   - `platform/streaming.py`
   - `platform/interrupts.py`
   - `platform/tracing.py`
   - `platform/domain_registry.py`
   - `platform/renderer_registry.py`
2. 定义 `RootState`
3. 创建最小 root graph：
   - `normalize_input`
   - `route_domain`
   - `run_legacy_general_chat`
   - `run_legacy_deep_research`
   - `finish`
4. 在不改前端协议的前提下，将 graph v2 stream 翻译为现有 SSE 事件格式
5. 为 root graph 接入 LangSmith tags/metadata
6. 抽出 memory 接口：
   - `UserMemoryProvider`
   - `ThreadMemoryProvider`
   - `CheckpointProvider`
7. 定义 router 语义：
   - 在 `platform/router.py` 中定义 `direct_chat / domain_selected / needs_clarification`
   - 低置信度时先进入 `run_legacy_general_chat` 做澄清，再回到 `route_domain`
8. 设计内部事件协议：
   - `updates` / `messages` / `custom` / `tasks` / `checkpoints`
9. 将 `runtime/task_interaction.py` 从”任意 handler 回调”升级为 graph interrupt bridge
10. 增加中断恢复测试：
    - 根图中断 / 子图中断 / 恢复后继续 streaming
11. 将当前 `waiting_for_user` 状态改为 graph interrupt 主导
12. 将业务层 checkpoint 从 agent memory 中剥离，切到 LangGraph checkpointer
13. 定义 `BudgetPolicy` 和 `RetryPolicy` 接口（本阶段只定义接口，不要求领域集成）

### 影响文件

- Create: `platform/*`
- Create: `capabilities/budget_policy.py`
- Create: `capabilities/retry_policy.py`
- Update: `runtime/task_runtime.py`
- Update: `runtime/task_dispatcher.py`
- Update: `runtime/task_interaction.py`
- Update: `main.py`
- Update: `storage/user_store_v2.py`（接口适配，非语义重写）
- Update: `runtime/conversation_context.py`（抽象提升）
- Update: `agents/general_chat.py`（仅作为 legacy conversation gateway 接入）
- Update: `capabilities/memory.py`（checkpoint 职责下沉）

### 验证

- `POST /tasks` 和 SSE 路径保持可用
- root graph 能跑 legacy `general_chat` / `deep_research`
- graph streaming 能转成现有 UI 事件
- LangSmith 能看到 root trace
- 低置信度路由可进入 `needs_clarification`，并回到 `route_domain`
- 用户追问/审批能中断并恢复
- 中断时前端能显示”待处理 gate”
- 恢复后 graph 从正确节点继续

### 风险控制

- 保留旧执行路径 behind flag
- 默认先只让少数任务类型走 root graph
- 首先只让 research 域使用新的 interrupt 流程
- 兼容旧 `ask_user()` 直到所有路径迁完

### Phase Exit Criteria

- root graph 可桥接 legacy 主路径
- SSE 协议与 LangSmith tracing 同时成立
- memory 接口已抽象完成
- router 已支持 `needs_clarification` 语义
- 中断恢复贯通根图与子图
- LangGraph checkpointer 接管恢复
- `waiting_for_user` 不再是私有业务主语义
- `BudgetPolicy` / `RetryPolicy` 接口已定义

---

## Phase 2: 重构 Deep Research + Deepagents Harness

### 目标

把 `deep_research` 从”当前系统里最复杂的 leaf agent”升级为 `domain_agents/research`，同时引入 `deepagents` 作为 agent harness。

### 交付物

- `domain_agents/research/`
- research artifact schema
- graph-native 并行 worker 方案（含容错策略）
- 统一 evidence/citation 能力
- 共享 browser capability 的 research 域接入
- deepagents-backed research agent 版本
- deepagents 与现有工具/能力层的适配桥
- workspace memory 与 product memory 的明确边界
- 共享 `capabilities/review_gates.py` 框架
- 共享 `capabilities/ppt_capability.py` 接入

### 任务

1. 创建 `domain_agents/research/`：
   - `agent.py` / `prompts.py` / `tools.py` / `schemas.py` / `reviewers.py` / `renderers.py`
2. 保留现有 `deep_research` 的有效资产：
   - 上下文管理（ResearchContext 三层压缩）
   - progress tracker
   - 工具选择策略
   - report rewrite
   - findings / summaries / final report 落盘语义
3. 把 parallel worker 从单节点 `asyncio.gather` 改成 graph-native fan-out/fan-in：
   - subgraphs 或 `Send`
   - `subgraphs=True` 可见
4. 定义并行 worker 容错策略：
   - 每个 worker 内部捕获异常，返回 `WorkerResult(status=ok|partial|error)`
   - fan-in 节点根据成功 worker 数量决定继续合成或触发 interrupt
   - 并行分支设置超时，超时后用已有结果继续
5. 引入统一 `EvidenceItem` / `CitationMap`
6. 实现 `capabilities/review_gates.py` 基础框架：
   - `structural_checks`（确定性规则）+ `semantic_checks`（LLM 判断）
   - 在 research agent 内率先使用：结构完整性、引用完整性、研究缺口提示
7. 抽出共享 browser capability（`capabilities/toolkits/browser_toolkit.py`）
8. 将当前私有 `browser_use` 工具迁移到共享实现
9. 收口 PPT 能力到 `capabilities/ppt_capability.py`（storyline 规划 + DSL 生成 + 渲染）
10. 在 `domain_agents/research/agent.py` 中实现 `create_deep_agent(...)` 版本
11. 定义 deepagents subagents：
    - `web_researcher` / `paper_researcher` / `evidence_synthesizer`
12. 将现有搜索工具、研究笔记、citation manager 暴露为 deepagents 工具
13. 保持 domain artifact / review gate 在平台侧，不让 deepagents 吞掉产品层语义
14. 明确 memory 边界：
    - `MemoryStoreV2` 继续作为产品级用户记忆
    - deepagents 接管 Thread Memory（会话历史 + 工作区）
    - `ResearchContext` 保留为领域上下文压缩
    - LangGraph 接管 checkpoint

### 影响文件

- Create: `domain_agents/research/*`
- Create: `capabilities/review_gates.py`
- Create: `capabilities/ppt_capability.py`
- Create: `capabilities/toolkits/browser_toolkit.py`
- Update: `agents/deep_research/run.py` 作为兼容 shim
- Update: `agents/deep_research/graphs.py` 或逐步退役
- Update: `capabilities/` 下的 evidence/citation 能力
- Update: `domain_agents/research/agent.py`
- Update: `pyproject.toml` 依赖约束与使用说明
- Update: `platform/root_graph.py`
- Update: `core/models.py`

### 验证

- simple/hierarchical/parallel research 都能运行
- 并行子代理可在 stream 中单独可见
- 并行 worker 部分失败时，已有结果仍可用于合成
- 引用和 artifact 能结构化输出
- 旧 `deep_research.run()` 入口仍可用
- browser capability 仅在需要页面级交互时被触发
- review gate 包含 structural_checks + semantic_checks
- deepagents 版 research 能完成复杂研究任务
- 仍能通过 root graph streaming / interrupt / tracing 工作
- workspace 与 artifact 目录可控
- PPT capability 可由 research renderers 调用

### 风险控制

- 先保留旧 graph，与新 research domain agent 并存
- 用 feature flag 切换新旧 research pipeline
- 维持一个非-deepagents fallback 实现

### Phase Exit Criteria

- 并行 worker graph-native 可见，且有容错策略
- citation / evidence / review gate 已结构化
- browser capability 已受控接入
- deepagents 与平台 runtime 兼容
- workspace memory 与 product memory 已分层
- fallback 路径仍可工作
- PPT capability 已可用

---

## Phase 3: 新建 Patent Agent

### 目标

建立专利撰写领域 agent，而不是把专利任务临时塞给 writer/research 组合。

### 交付物

- `domain_agents/patent/`
- prior-art / claim tree / specification artifact schema
- review gate：术语一致性、权利要求依赖、实施例支撑
- 可选的 browser capability 页面核实路径

### 任务

1. 创建专利领域目录和 schema
2. 定义子代理：
   - `technical_disclosure_analyst`
   - `prior_art_researcher`
   - `claim_drafter`
   - `specification_drafter`
   - `patent_reviewer`
3. 增加专利专用工具和 reviewer
4. 增加渲染：
   - markdown/docx
   - claim appendix / prior-art matrix
5. 仅在需要页面核实时接入 browser capability，不作为主检索路径
6. 不直接复用 research 私有 browser tool，而是复用共享 `browser capability`

### 验证

- 技术交底输入能稳定产出 claim tree + 说明书草稿
- prior-art 引用清晰
- 人工 review gate 可插入
- browser capability 只在页面核实场景触发，而不是默认检索路径

### Phase Exit Criteria

对应清单：`Phase 3: Patent Agent`

- `ClaimTree` / `PriorArtMatrix` / `SpecDraft` 已成型
- patent reviewer 可输出结构化缺陷
- browser capability 仅受控启用

---

## Phase 4: 新建 Zero Report Agent

### 目标

建立归零报告领域 agent，用结构化问题复盘和整改闭环替代“自由写作型报告”。

### 交付物

- `domain_agents/zero_report/`
- timeline / root cause / action matrix schema
- 审批和整改回退 gate
- 按需启用的 browser capability 门户接入点

### 任务

1. 创建归零报告目录和 schema
2. 定义子代理：
   - `incident_structurer`
   - `timeline_builder`
   - `root_cause_analyst`
   - `corrective_action_planner`
   - `report_reviewer`
3. 定义 zero-report artifact 组装和渲染
4. 定义整改措施 review gate
5. 仅在工单/监控/wiki 门户采集时接入 browser capability
6. browser capability 的页面采集结果必须沉淀为结构化证据，而不是只返回自由文本

### 验证

- 输入事故材料后可输出时间线、根因树、整改矩阵、正式报告草稿
- 可在关键节点要求人工审批或补件
- 页面采集类 `browser_use` 结果可进入 zero-report 证据链

### Phase Exit Criteria

对应清单：`Phase 4: Zero Report Agent`

- `Timeline` / `RootCauseTree` / `ActionMatrix` 已成型
- reviewer 可检查闭环与责任时限
- 审批/补件 gate 已接入

---

## Phase 5: 迁移 Orchestrator 语义到 Root Graph

### 目标

让现有 `orchestrator/` 退役为兼容壳，而不再作为主执行中心。

### 交付物

- 新的 `route_domain` 逻辑成为主路径
- 模板系统收缩为兼容层或领域入口别名
- scheduler 退役

### 任务

1. 将当前 `orchestrator/templates.py` 的意图映射转成 root graph route aliases
2. 用 domain agent nodes 替换 `execute_plan()` 主逻辑
3. 将 legacy orchestrator 保留为 shim
4. 清理不再需要的 scheduler/template 假设

### 验证

- `run_orchestrator()` 仍可响应旧入口
- 实际执行已走 root graph/domain graph
- 新旧路径结果等价或更强

### Phase Exit Criteria

对应清单：`Phase 5: 平台收口与默认切换`

- root graph 已成为默认执行中心
- 旧 orchestrator/scheduler 主路径已退为兼容层
- 共享能力层已稳定复用

---

## Phase 6: 统一 Artifact、Review、Tracing（后端）+ 前端独立文档

### 目标

完成后端产品层闭环。前端重构（React + 后续 Tauri 桌面端）作为独立项目文档推进。

### 交付物（后端）

- 统一 artifact API（浏览/下载/版本）
- review gate API
- provenance/evidence 查询接口
- LangSmith dashboard/alert/eval

### 交付物（前端 - 独立文档）

前端重构为独立项目，使用 React 框架，后续使用 Tauri 做成跨平台桌面客户端。前端设计文档另行建立，不在本实施计划中展开。

前端目标包括但不限于：

- 消费统一 graph streaming 协议
- artifact 侧边栏和来源卡片
- review gate 卡片和恢复入口
- 步骤树、token 流、子代理进度

### 任务（后端）

1. 完善 artifact 元数据 API
2. 完善 review gate 查询与恢复 API
3. 完善 evidence provenance 查询 API
4. 建立 LangSmith 项目、dashboard、alert
5. 为三类领域 agent 建立评估集

### 验证

- artifact / review gate / evidence 可通过 API 查询
- LangSmith dashboard / alert / eval 已建立
- 至少一个领域可做失败回放与基准评估

### Phase Exit Criteria

对应清单：`Phase 6: Artifact / Review / Tracing 闭环`

- 后端 API 已支持统一协议
- LangSmith dashboard / alert / eval 已建立
- 至少一个领域可做失败回放与基准评估

---

## 关键实施顺序

按优先级排序的真实落地顺序：

1. Phase 1: 平台骨架 + 流式协议 + HITL
2. Phase 2: 重构 Deep Research + Deepagents Harness
3. Phase 3: 新建 Patent Agent
4. Phase 4: 新建 Zero Report Agent
5. Phase 5: 退役旧 orchestrator/scheduler 主路径
6. Phase 6: 完成后端闭环与观测体系（前端独立推进）

这条顺序的理由：

- 先解决平台执行中心 + 流式协议，再做领域迁移
- 先证明最复杂的 research 任务跑通（含 deepagents），再复制到 patent / zero_report
- 最后才删旧 orchestrator，避免过早切断稳定路径
- 前端重构独立推进，不阻塞后端能力建设

---

## 与当前文件的映射关系

| 当前模块 | 目标去向 |
|----------|----------|
| `runtime/task_runtime.py` | 保留为 API 壳，但内部执行切到 root graph |
| `runtime/task_dispatcher.py` | 收缩为 route alias 或兼容入口 |
| `orchestrator/runner.py` | 兼容 shim，逐步退役 |
| `orchestrator/scheduler.py` | 迁移完成后退役 |
| `orchestrator/templates.py` | 迁为 route presets / legacy aliases |
| `orchestrator/planner.py` | 对 `registry_summary()` 的依赖移除，改由 `route_domain` 消费 `DomainRegistry` |
| `agents/deep_research/*` | 提炼资产后迁入 `domain_agents/research/` |
| `agents/general_chat.py` | 作为 root graph 的 conversation gateway / fallback path 保留，后续可单独重构 |
| `storage/user_store_v2.py` | 保留并抽象为产品级用户记忆（User Memory Layer） |
| `runtime/conversation_context.py` | 合并到 Thread Memory，由 deepagents SummarizationMiddleware 接管 |
| `capabilities/context_manager.py` | 保留为 Domain Context Memory，迁入 research 域 |
| `capabilities/memory.py` | findings/artifact 语义保留；checkpoint 语义移除（交给 LangGraph） |
| `capabilities/planner.py` | 迁入 `domain_agents/research/` 或由 deepagents planning 替代 |
| `core/registry.py` | 迁移为兼容层，最终拆分为 `platform/domain_registry.py`、`platform/renderer_registry.py` 和各领域 tool manifest |
| `agents/search_agent.py::browser_navigate` | 上收为共享 browser capability |
| `agents/deep_research/run.py::browser_navigate` | 上收为共享 browser capability |
| `core/models.py::get_browser_use_llm` | 保留，继续作为 browser capability 的统一模型适配层 |
| `ppt_engine/*` + `agents/writer_agent.py`（PPT 部分） | 收口到 `capabilities/ppt_capability.py` |

---

## 验收标准

重构完成后，应满足以下条件：

1. 所有任务都通过 root graph 执行
2. research / patent / zero_report 成为一等领域 agent
3. 前端只消费统一的 graph-based streaming 协议
4. 中断/恢复、审阅 gate、子代理并行都对前端可见
5. LangSmith 能按 task/domain/subagent 查看完整 trace
6. 删除旧 scheduler 后系统仍稳定运行

---

## 方法论骨架

本次重构建立可扩展的方法论框架，具体领域方法论（深度研究方法论、专利撰写工作流、归零报告方法论）将在后续独立窗口中详细设计。

### 方法论应包含的组成部分

每个领域方法论至少应定义：

1. **阶段定义**：任务从输入到输出经过哪些阶段（如研究：提问 → 检索 → 分析 → 合成 → 审阅 → 定稿）
2. **判断标准**：每个阶段的进入/退出条件
3. **工具策略**：每个阶段优先使用哪些工具、什么条件下升级（如从搜索升级到 browser）
4. **Review 规则**：每个阶段的 structural_checks 和 semantic_checks
5. **Artifact 产出**：每个阶段应产出什么结构化中间产物
6. **人机交互点**：哪些阶段需要 interrupt 对齐用户需求

### 与领域 agent 的关系

方法论不是写在 prompt 里的长指令，而是领域 agent 的执行图（graph）结构和 review gate 配置的依据。方法论定义了"做什么"，agent 实现了"怎么做"。

---

## 非目标

以下内容不作为本轮核心目标：

- 追求一个“万能”通用 agent prompt
- 一次性重写所有前端页面
- 一次性替换所有 storage 方案
- 在第一阶段就让 patent / zero_report 达到 production quality
- 用 `AGENTS.md` 替换结构化产品记忆
- 在本轮完整实现每个领域的详细方法论（方法论骨架先行，细节后续窗口讨论）
- 在本轮完成前端 React 重构（前端作为独立项目推进）

---

## 结论

这不是一次“把代码搬进 LangGraph”的技术迁移，而是一次产品底座迁移：

> **先把运行时改成 graph-native，再把领域工作流做成产品化 agent。**

只要按上述阶段推进，风险可控，而且每个阶段都能交付可验证的中间成果，不需要等待“大重写结束”才能见效果。
