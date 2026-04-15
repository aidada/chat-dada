# Hard-Task Agent 平台重构设计

## 背景

当前系统已经具备三个关键基础：

1. `POST /tasks` + `SSE /tasks/{task_id}/events` 的任务式交互外壳
2. 基于 LangGraph 的 `deep_research` agent，以及研究专用的上下文/进度/检查点能力
3. 一个可工作的多能力注册表与编排壳（orchestrator + scheduler + templates）

但如果目标从“通用聊天平台”切换为“可稳定完成高难任务的 agent 平台”，当前架构会出现明显瓶颈：

1. **运行时语义分裂**：任务生命周期在 `runtime/`，执行编排在 `orchestrator/`，复杂 agent 语义又在 `agents/deep_research/`
2. **graph 能力没有上升为平台底座**：LangGraph 目前只是部分 agent 的内部实现，而不是统一的持久化/中断/流式执行模型
3. **领域能力未产品化**：科研、专利、归零报告都需要自己的 artifact schema、review gate、证据链和领域规则，不能只靠通用 prompt
4. **并行子代理可见性不足**：现有 parallel worker 仍然被包在单节点里，缺少 graph-native 的 streaming、resume、observability

因此，目标重构不应是“再优化一次 orchestrator”，而应是：

> 将系统重构为一个 **LangGraph-native 的 hard-task agent 平台**，以单根 graph 为运行时核心，以领域 agent 为任务执行单元，以 deepagents 作为复杂 agent harness，以统一流式事件协议和 artifact 体系支撑前端与交互。

## 目标

1. 用 **单根 LangGraph** 替换当前“custom runtime + scheduler + leaf graphs”的执行中心
2. 让 **科研 / 专利 / 归零报告** 成为一等公民的领域 agent，而不是通用 orchestrator 下的普通 step
3. 将 **streaming / interrupt / resume / checkpointer / LangSmith tracing** 统一纳入同一执行模型
4. 使用 **deepagents** 简化复杂 agent 的创建，但只作为领域 agent harness，不作为产品顶层抽象
5. 保留当前任务 API 外壳，作为迁移期间的兼容层

## 设计原则

### 1. Runtime First

执行模型优先于 agent prompt。先决定系统如何流式执行、暂停、恢复、审阅，再决定 prompt 和 tool。

### 2. Domain Over Generic

科研、专利、归零报告共享基础设施，但不共享完整工作流。领域 agent 必须有独立的：

- artifact schema
- tool 组合
- review gate
- completion criteria
- rendering pipeline

### 3. LangGraph Native

统一使用 LangGraph 的：

- subgraphs
- streaming v2
- interrupts
- durable execution
- checkpointer

避免继续堆叠“自定义 runtime 语义”与“graph 内部语义”。

### 4. Deepagents as Harness, Not Shell

`deepagents.create_deep_agent()` 适合用来快速构建复杂 agent 的 plan / subagent / filesystem / context 管理，但不应直接替代产品 API、任务存储、权限边界和前端事件协议。

### 5. UI 面向执行可见性，而非原始推理链

前端展示的是：

- 执行过程
- 节点状态
- 工具调用
- 证据汇总
- artifact 演化
- 人机交互 gate

而不是承诺暴露“模型原始思维链”。

### 6. Maturity Over Appearance

成熟 deep thinking 的判断标准不是“会写长分析”或“看起来思考很多”，而是：

- 有统一 runtime
- 有 layered memory/context
- 有 verifier/reviewer
- 有 budget protocol
- 有 artifact-first 输出
- 有 HITL/interrupt/resume
- 有评估与观测

详细判断依据与实现指导见：

- [Deep Thinking 成熟度判断与实现指导](./2026-03-20-deep-thinking-maturity-guide.md)
- [Deep Thinking 实施验收清单](../../实施计划/待执行/2026-03-20-deep-thinking-acceptance-checklist.md)
- [General Chat 定位与路由语义设计](./2026-03-21-general-chat-positioning-design.md)

## 目标分层

### 1. 目标目录与职责

```text
chat-dada/
├── main.py
├── runtime/
│   ├── api_shell.py              # POST /tasks / SSE / reply 兼容外壳
│   ├── thread_store.py           # task_id ↔ thread_id, task snapshot, event cursor
│   ├── event_adapter.py          # StreamPart -> SSE event protocol
│   └── artifact_store.py         # 文件/对象存储封装
│
├── platform/
│   ├── root_graph.py             # 单根 LangGraph
│   ├── router.py                 # 领域路由：research/patent/zero_report/chat
│   ├── state.py                  # RootState / typed artifacts / UI event payloads
│   ├── streaming.py              # graph.astream(... version="v2") 适配
│   ├── interrupts.py             # 审批/澄清/补件等 HITL gate
│   ├── tracing.py                # LangSmith tags / metadata / sampling
│   ├── domain_registry.py        # 领域 agent 注册（供 route_domain 使用）
│   └── renderer_registry.py      # 渲染器注册（按 artifact 类型路由）
│
├── domain_agents/
│   ├── research/
│   │   ├── agent.py              # deepagents / custom subgraph wrapper
│   │   ├── prompts.py
│   │   ├── tools.py
│   │   ├── schemas.py            # findings / citations / evidence map
│   │   ├── reviewers.py          # 覆盖度、引用质量、结构检查
│   │   └── renderers.py          # markdown / docx / ppt adapter
│   ├── patent/
│   │   ├── agent.py
│   │   ├── prompts.py
│   │   ├── tools.py
│   │   ├── schemas.py            # claims / embodiments / prior-art matrix
│   │   ├── reviewers.py
│   │   └── renderers.py
│   └── zero_report/
│       ├── agent.py
│       ├── prompts.py
│       ├── tools.py
│       ├── schemas.py            # timeline / root cause / actions / owner matrix
│       ├── reviewers.py
│       └── renderers.py
│
├── capabilities/
│   ├── evidence_store.py         # URL/file/table/quote 统一证据抽象
│   ├── citation_manager.py       # 引用标准化、去重、编号
│   ├── review_gates.py           # ReviewGate 框架（structural + semantic checks）
│   ├── budget_policy.py          # 全局用户配额检查与降级策略
│   ├── retry_policy.py           # 工具/LLM 调用重试与容错策略
│   ├── ppt_capability.py         # PPT storyline 规划 + DSL 生成 + 渲染
│   ├── context_manager.py        # 领域上下文保真压缩（research raw/compact/summary）
│   ├── doc_workspace.py          # 文件工作区 / artifact 版本管理
│   ├── memory.py                 # thread/user memory
│   └── toolkits/                 # 搜索、文档、代码执行、图表、browser 等通用工具包
│
├── agents/                       # 兼容层：对旧 registry 继续暴露 run()
├── orchestrator/                 # 迁移期兼容目录，逐步退役
└── docs/
```

### 2. 目标依赖方向

```text
runtime shell
    ↓
platform root graph
    ↓
domain_agents
    ↓
capabilities / tools / renderers / storage
```

约束：

- `runtime/` 不直接理解领域工作流，只负责任务 API、event fan-out、reply 注入、artifact 下载
- `platform/` 管根 graph、thread state、streaming、interrupt、trace metadata
- `domain_agents/` 管任务完成逻辑
- `capabilities/` 管可复用能力，不依赖具体领域 agent

## 目标架构图

```text
                                  ┌──────────────────────────────┐
                                  │         Frontend UI          │
                                  │ chat / task panel / review   │
                                  └──────────────┬───────────────┘
                                                 │
                           POST /tasks, GET /events, POST /reply
                                                 │
                                  ┌──────────────▼───────────────┐
                                  │      Runtime API Shell       │
                                  │ task snapshot / SSE / files  │
                                  └──────────────┬───────────────┘
                                                 │ task_id ↔ thread_id
                                                 │
                                  ┌──────────────▼───────────────┐
                                  │     Platform Root Graph      │
                                  │ route / stream / interrupt   │
                                  │ checkpoint / trace / state   │
                                  └───────┬─────────┬────────────┘
                                          │         │
                                          │         │ shared stream v2
                                          │         │
                      ┌───────────────────▼─┐   ┌──▼─────────────────────┐
                      │  General Chat Path   │   │  Hard-Task Domain Path │
                      │ single-step / short  │   │ research/patent/report │
                      └──────────────────────┘   └──────────┬─────────────┘
                                                            │
                        ┌───────────────────────────────────┼───────────────────────────────────┐
                        │                                   │                                   │
             ┌──────────▼──────────┐             ┌──────────▼──────────┐             ┌──────────▼──────────┐
             │   Research Agent    │             │    Patent Agent     │             │  Zero Report Agent  │
             │ deepagents/subgraph │             │ deepagents/subgraph │             │ deepagents/subgraph │
             └──────────┬──────────┘             └──────────┬──────────┘             └──────────┬──────────┘
                        │                                   │                                   │
                        └───────────────────┬───────────────┴───────────────┬───────────────────┘
                                            │                               │
                               ┌────────────▼─────────────┐      ┌─────────▼─────────┐
                               │ Shared Capabilities      │      │ Shared Storage     │
                               │ evidence/citations/HITL  │      │ postgres/redis/fs  │
                               │ doc workspace/review     │      │ checkpoints/files  │
                               └──────────────────────────┘      └───────────────────┘
```

## 根图（Root Graph）职责

根图不是“再做一个大 orchestrator prompt”，而是平台运行时的显式 graph：

### RootState 建议字段

- `task_id`
- `user_id`
- `intent`
- `route_decision`
- `needs_clarification`
- `input_payload`
- `attachments`
- `thread_context`
- `domain_artifact_refs`
- `ui_events`
- `review_requests`
- `final_result`
- `error`

### 根图节点建议

1. `normalize_input`
2. `route_domain`
3. `run_general_chat`
4. `run_research_agent`
5. `run_patent_agent`
6. `run_zero_report_agent`
7. `review_gate`
8. `render_outputs`
9. `persist_summary`
10. `finish`

### `general_chat` 的定位

`general_chat` 在目标架构中不应视为与 `research / patent / zero_report` 对称的独立领域。

它更适合作为：

- `fallback path`
- `conversation gateway`
- `needs_clarification` 的澄清入口

这意味着：

1. 当用户只是闲聊或快速问答时，`route_domain` 可以直接返回 `direct_chat`
2. 当领域意图明确时，`route_domain` 返回 `domain_selected`
3. 当意图不明确时，`route_domain` 返回 `needs_clarification`，先进入 `run_general_chat` 补信息，再二次路由

详细说明见：

- [General Chat 定位与路由语义设计](./2026-03-21-general-chat-positioning-design.md)

### `platform/router.py` 目标语义

建议 `platform/router.py` 至少支持三类路由结果：

1. `direct_chat`
2. `domain_selected`
3. `needs_clarification`

其中 `needs_clarification` 不是失败态，而是显式的对话澄清态。

### 根图执行方式

统一使用：

```python
async for part in graph.astream(
    input_state,
    config={"configurable": {"thread_id": task_id}},
    version="v2",
    subgraphs=True,
    stream_mode=["updates", "messages", "custom", "tasks", "checkpoints"],
):
    ...
```

`astream_events()` 可以保留给调试界面或内部 observability，不作为产品主协议。

## 领域 Agent 设计

### 1. Research Agent

目标：论文调研、竞品研究、技术综述、证据驱动报告

建议子代理：

- `research_planner`
- `web_researcher`
- `paper_researcher`
- `evidence_synthesizer`
- `citation_reviewer`

核心 artifact：

- `ResearchQuestion`
- `EvidenceItem`
- `EvidenceMap`
- `ResearchFinding`
- `ResearchReportDraft`

### 2. Patent Agent

目标：技术方案整理、现有技术检索、专利初稿、权利要求树

建议子代理：

- `technical_disclosure_analyst`
- `prior_art_researcher`
- `claim_drafter`
- `specification_drafter`
- `patent_reviewer`

核心 artifact：

- `TechnicalDisclosure`
- `PriorArtItem`
- `ClaimTree`
- `SpecificationDraft`
- `PatentRiskNotes`

### 3. Zero Report Agent

目标：归零报告、问题复盘、根因分析、整改计划闭环

建议子代理：

- `incident_structurer`
- `timeline_builder`
- `root_cause_analyst`
- `corrective_action_planner`
- `report_reviewer`

核心 artifact：

- `IncidentFactSet`
- `Timeline`
- `RootCauseTree`
- `ActionMatrix`
- `ZeroReportDraft`

## Deepagents 在目标架构中的位置

适合交给 `deepagents` 的部分：

- planning/todo
- subagent delegation
- filesystem workspace
- 长上下文任务分解
- 通用 research-style agent harness

不建议交给 `deepagents` 的部分：

- 顶层 API/runtime shell
- task_id / thread_id / SSE 协议
- 平台级权限与 artifact 存储边界
- 领域 artifact schema
- 审批与 review gate 编排

推荐模式：

```text
Runtime/API shell
    -> Root LangGraph
        -> Domain agent node
            -> deepagents-backed compiled graph
                -> domain-specific subagents + tools
```

## 记忆与上下文分层

`deepagents` 自带 memory/context management（`SummarizationMiddleware`、`FilesystemBackend`、subagent 隔离上下文），但它解决的是 **通用上下文窗口管理**，不是完整的 **产品级记忆体系**。

经过对比分析，当前系统的领域上下文压缩（`ResearchContext` 的 `raw → compact → summary` 三层压缩）在证据来源保真、基于证据强度的差异化压缩、结构化行提取等方面，优于 deepagents 的通用 `SummarizationMiddleware`。因此领域上下文压缩应保留为独立层。

### 目标记忆分层（5 层）

```text
                         ┌────────────────────────────────────┐
                         │        User Memory Layer           │
                         │ facts / preferences / projects     │
                         │ structured, cross-task, product    │
                         └────────────────┬───────────────────┘
                                          │
                         ┌────────────────▼───────────────────┐
                         │      Thread Memory Layer           │
                         │ conversation history + workspace   │
                         │ deepagents Summarization/FS 统一管理│
                         └────────────────┬───────────────────┘
                                          │
                  ┌───────────────────────▼────────────────────────┐
                  │         Domain Context Memory Layer            │
                  │ research raw/compact/summary, patent notes,    │
                  │ zero-report timeline/root-cause structures     │
                  │ 证据保真压缩，非通用摘要可替代                     │
                  └───────────────────────┬────────────────────────┘
                                          │
                         ┌────────────────▼───────────────────┐
                         │     Artifact / Evidence Layer      │
                         │ findings / citations / drafts /    │
                         │ matrices / final report / sources  │
                         └────────────────┬───────────────────┘
                                          │
                         ┌────────────────▼───────────────────┐
                         │  Execution Checkpoint Layer        │
                         │ thread state / interrupts / resume │
                         │ LangGraph-owned, not business mem  │
                         └────────────────────────────────────┘
```

### 各层职责与实现方

| 层 | 职责 | 实现方 |
|---|---|---|
| User Memory | 跨任务用户画像、偏好、项目生命周期 | 保留 `user_store_v2`（结构化 facts/projects/confidence） |
| Thread Memory | 会话历史摘要 + 工作区文件管理 | deepagents（SummarizationMiddleware + FilesystemBackend） |
| Domain Context | 领域证据保真压缩（source_urls/evidence_strength/key_claims） | 保留 `ResearchContext`，后续扩展到 patent/zero_report |
| Artifact/Evidence | 结构化产物和证据存储 | 平台 `capabilities/evidence_store.py` |
| Checkpoint | 执行恢复 | LangGraph checkpointer |

### 为什么不把 Domain Context 合并到 Thread Memory

deepagents 的 `SummarizationMiddleware` 是对消息历史的通用 LLM 压缩，而 `ResearchContext` 做了 4 件 deepagents 不做的事：

1. **证据来源保真**：`FindingEntry` 把 `source_urls`、`evidence_strength`、`key_claims` 作为一等字段保留
2. **基于证据强度的差异化压缩**：strong evidence 用 LLM 精细摘要，weak evidence 用截取关键行
3. **结构化行提取**：识别 markdown 标题、列表项、数据模式，优先保留结构信息
4. **三层独立预算的 prompt 组装**：summary / compact / raw 三层各自控制 token 预算

对于研究/专利/归零报告这种溯源是核心需求的场景，通用摘要会导致早期发现的关键 URL 和证据强度信息丢失。

### 兼容策略

```text
UserMemory              -> 保留现有结构化方案（user_store_v2）
ThreadMemory            -> deepagents（Summarization + Filesystem + subagent isolation）
DomainContextMemory     -> 保留 ResearchContext，后续扩展
Artifact/EvidenceMemory -> 由现有 research memory 升级为结构化 evidence store
ExecutionCheckpoint     -> LangGraph checkpointer
```

> **产品级 memory 用现有方案；通用上下文管理用 deepagents；领域证据压缩保留专用方案；执行恢复交给 LangGraph。**

## Browser Capability

重构后，`browser_use` 不再以私有工具形式分散在各 agent 内，而是上收为共享 browser capability（`capabilities/toolkits/browser_toolkit.py`）。

定位：高成本、低吞吐、强交互的网页执行能力，用于动态页面、多步交互、结构化页面证据采集，不是默认搜索路径。

各领域适用性：research 域优先使用，patent 域谨慎使用（仅页面核实），zero_report 域按需使用（仅门户采集）。

详细接口草图、Schema 定义、运行约束与各领域策略见：

- [Browser Capability 设计](./2026-03-20-browser-capability-design.md)

## Deep Thinking 成熟度与制作标准

本设计是成熟 deep thinking 的平台骨架，不是成熟能力本身。架构正确不等于能力已经成熟。

成熟 deep thinking agent 的判断标准（8 个维度）、制作标准（6 条优先序）、以及可执行的验收检查项，见独立文档：

- [Deep Thinking 成熟度判断与实现指导](./2026-03-20-deep-thinking-maturity-guide.md)
- [Deep Thinking 实施验收清单](../../实施计划/待执行/2026-03-20-deep-thinking-acceptance-checklist.md)

## 流式事件协议

### 产品主协议

使用 LangGraph v2 `StreamPart` 作为内部统一流格式，并在 runtime shell 中翻译成对前端友好的事件：

| StreamPart type | 前端语义 | 典型 UI |
|-----------------|----------|---------|
| `updates` | 节点进入/退出/状态变化 | 进度卡、步骤树 |
| `messages` | token 流 | 正文流式输出 |
| `custom` | 领域事件 | 文档溯源卡、引用卡、artifact 演化 |
| `tasks` | 子代理/子任务生命周期 | 子代理进度条 |
| `checkpoints` | 保存点 | “可恢复”标记、恢复提示 |

### 调试协议

仅在开发/调试模式启用 `astream_events(version="v2")`，用于完整 runnable 生命周期观测。

## HITL 与 Review Gate

目标系统中，人机交互不是“问用户一句话再继续”这么简单，而应区分三类：

1. **澄清**：输入不全，需要补充约束
2. **审批**：关键节点需要人确认，如专利 claim tree / 零报告根因
3. **回退**：审阅未通过，回到某个子图重写

统一使用 LangGraph interrupts / `Command(resume=...)` 表达，而不是让每个 agent 自己发明交互语义。

## 持久化与 Artifact 策略

### 1. 任务与线程

- `task_id` 继续作为对外主键
- `thread_id` 默认映射为 `task_id`
- 根图 checkpointer 基于 `thread_id`

### 2. Artifact

所有领域任务最终都产出可审阅 artifact，而不只是字符串：

- markdown/docx/pptx
- 引用清单
- prior-art matrix
- root cause tree
- action matrix

建议将 artifact 元数据入库，将大文件落对象存储或文件系统。

### 3. Evidence

引入统一 evidence store：

- URL 证据
- 附件证据
- 表格/截图/引用片段
- 来源位置、采集时间、摘要、可信度

研究、专利、归零报告都依赖这个抽象。

### 4. 记忆迁移原则

在 memory 相关模块迁移时，遵循以下原则：

1. **不丢语义**
   - 用户画像、项目状态、时间线这类产品资产必须保留
2. **不重复造轮子**
   - deepagents 已经擅长的 workspace/context 交给 deepagents
3. **不混淆职责**
   - checkpoint/resume 不再由业务 memory 管理
4. **不让文本文件替代结构化产品记忆**
   - `AGENTS.md` 只能补充规则和工作区上下文，不能替代 `MemoryStoreV2`
5. **不让浏览器工具变成默认搜索器**
   - browser capability 只在页面级交互/证据采集需要时启用

## 兼容与迁移原则

迁移期间保留：

- `POST /tasks`
- `GET /tasks/{id}`
- `GET /tasks/{id}/events`
- `POST /tasks/{id}/reply`

兼容层负责把旧接口映射到新的 root graph 执行，不要求前端一次性重写。

## Review Gate 框架

Review gate 不是一个大 prompt，而应区分确定性规则和 LLM 语义判断：

### 标准结构

```python
class ReviewGate:
    structural_checks: list[Callable]   # 确定性规则，无 LLM
    semantic_checks: list[LLMCheck]     # LLM 判断，有 prompt + 评分标准
    gate_policy: Literal["pass_all", "pass_ratio"]  # 通过策略
    on_fail: Literal["retry", "escalate", "interrupt"]  # 失败后行为
```

### structural_checks 示例

- 引用完整性：每个 `[n]` 标记是否都有对应的 source URL（正则匹配）
- 权利要求依赖：从属权利要求是否引用了已有权利要求号（结构化校验）
- 行动项时限：每个行动项是否有责任人和截止日期（schema 校验）
- Artifact 字段完整性：必填字段是否为空（Pydantic validator）

### semantic_checks 示例

- 研究缺口：现有发现是否覆盖了用户提出的所有子问题
- 术语一致性：说明书中的术语是否和权利要求中使用的一致
- 因果链合理性：根因分析是否逻辑自洽

### 实现位置

基础框架放在 `capabilities/review_gates.py`，各领域的 `reviewers.py` 继承并填充具体规则。

## Budget Policy 设计

预算策略基于用户全局配额，而非单任务硬限。

### 接口设计

```python
class BudgetPolicy:
    def check(self, user_quota: UserQuota, current_usage: BudgetUsage) -> BudgetDecision:
        """检查用户剩余配额，决定是否继续。"""
        ...

class BudgetDecision(Enum):
    CONTINUE = "continue"           # 余量充足，正常继续
    ESCALATE = "escalate_to_human"  # 接近阈值，触发 interrupt 询问用户
```

### 核心逻辑

1. 不做单任务预算限制
2. 在任务关键节点检查用户全局剩余额度
3. 接近用户剩余配额时，通过 graph interrupt 主动询问用户是否继续
4. 如果任务预计消耗较大（如并行 worker、browser 操作），提前告知用户预估成本

### 实现位置

`capabilities/budget_policy.py`，由 root graph 或领域 agent 在关键节点调用。

## Retry Policy 设计

任务级别的错误恢复策略，覆盖 LLM API 超时、browser 崩溃、工具调用失败等场景。

### 接口设计

```python
class RetryPolicy:
    max_retries: int = 3             # 单工具调用最大重试次数
    retry_delay_seconds: float = 2.0 # 重试间隔
    fallback_provider: str | None    # LLM 失败时的备选 provider
    on_exhaust: Literal["skip", "stop", "escalate"]  # 重试耗尽后行为
```

### 与 checkpoint 的关系

LangGraph checkpoint 保存的是节点间的状态。如果节点内部执行（第 N 步）崩溃，checkpoint 保存的是第 N-1 步的状态。RetryPolicy 负责：

- 节点内部工具调用的重试
- 重试耗尽后决定跳过/停止/升级为人工
- 进程级崩溃后，从 checkpoint 恢复时自动重试失败节点

### 实现位置

`capabilities/retry_policy.py`，与 `BudgetPolicy` 同级，由 platform 层消费。

## PPT Capability 设计

PPT 不是独立领域 agent，而是跨领域共享的输出能力。research 的成果可以输出为 PPT，patent 的技术交底可以输出为 PPT，zero_report 的汇报也可以输出为 PPT。

### 职责

1. **Storyline 规划**：根据领域 artifact 生成 PPT 叙事结构
2. **DSL 生成**：将叙事结构转换为 PPT DSL schema
3. **渲染**：将 DSL 渲染为 .pptx 文件

### 与现有实现的关系

当前系统已有成熟的 PPT 管线：

- `ppt_engine/dsl_schema.py`：Slide DSL 定义
- `ppt_engine/renderer.py`：DSL → .pptx 渲染
- `agents/writer_agent.py`：PPT DSL 生成能力

重构后，这些资产应收口到 `capabilities/ppt_capability.py`，由各领域 agent 的 `renderers.py` 按需调用。

### 实现位置

`capabilities/ppt_capability.py`，作为共享渲染能力供所有领域使用。

## Registry 架构拆分

当前 `core/registry.py` 是一个扁平注册表（6 agents + 9 tools + 5 renderers），重构后应拆分为：

### 1. DomainRegistry（`platform/domain_registry.py`）

- 供 `route_domain` 和 root graph 使用
- 只注册 `general_chat / research / patent / zero_report` 这类领域入口
- 不暴露细粒度工具
- 替代今天 planner 对 `registry_summary()` 的依赖

### 2. RendererRegistry（`platform/renderer_registry.py`）

- 渲染器单独管理，按 artifact 类型和目标格式路由
- 例如 markdown/docx/pptx 是 artifact output strategy，不是同层级 agent/tool

### 3. Domain Tool Manifest

- 每个领域 agent 自己声明工具集和共享 capability 注入点
- `domain_agents/research/tools.py`、`domain_agents/patent/tools.py` 等
- browser capability、citation、evidence 这类共享能力从这里按需接入

### 4. Policy/Review 配置

- budget policy、retry policy、review gate 不做成 LLM-facing registry
- 这些是 runtime 配置和 typed policy object，供 graph/node 使用，不进 `registry_summary()`

## 并行 Worker 容错策略

当并行 worker 从 `asyncio.gather` 迁移到 graph-native fan-out/fan-in（`Send`）时，需要处理部分失败场景。

### LangGraph `Send` 的默认行为

所有分支完成后才进入 fan-in 节点。如果一个分支抛异常，整个 graph 进入错误状态。

### 目标容错策略

1. 每个 worker 分支内部捕获异常，返回 `WorkerResult(status=ok|partial|error)`
2. fan-in 节点根据成功 worker 数量决定后续行为：
   - 全部成功：正常合成
   - 部分成功（≥1 个 ok）：用已有结果继续，标记缺失部分
   - 全部失败：触发 interrupt 让用户决定重试或终止
3. 并行分支设置超时：超时后用已有结果继续，不无限等待

### 实现位置

容错逻辑内嵌在 `domain_agents/*/agent.py` 的 fan-in 节点中，`WorkerResult` schema 定义在各领域的 `schemas.py` 中。

## 风险

1. **一次性替换过大**：如果同时替换 runtime、orchestrator、agent、存储，风险过高
2. **deepagents 过度接管**：如果把 deepagents 当整个平台，会失去对平台边界的控制
3. **领域 schema 设计不足**：如果没有结构化 artifact，最终还是会退化成“长 prompt + 长文本输出”
4. **HITL 设计滞后**：若先做 agent 再补 review gate，后续会返工
5. **并行失败传播**：graph-native fan-out 中一个分支失败可能阻塞全图，必须在 worker 内部做容错

## 结论

目标架构应当是：

> **以 LangGraph 为平台运行时，以 deepagents 为复杂领域 agent harness，以 research / patent / zero_report 三个领域 agent 为核心产品能力，以 v2 streaming + interrupts + durable execution + structured artifacts 为统一产品协议。**

对于 memory/context，最终结论是：

> **保留现有结构化用户记忆和领域上下文方案，引入 deepagents 的 workspace/context 管理能力，并将 checkpoint/resume 统一迁移到 LangGraph runtime。**

这条路线比继续堆叠当前 orchestrator/scheduler 更适合“hard-task agent”目标，也能保留现有 API 外壳作为迁移缓冲层。
