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
│   └── tracing.py                # LangSmith tags / metadata / sampling
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
│   ├── review_gates.py           # domain-independent review gate primitives
│   ├── doc_workspace.py          # 文件工作区 / artifact 版本管理
│   ├── memory.py                 # thread/user memory
│   └── toolkits/                 # 搜索、文档、代码执行、图表等通用工具包
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

## 兼容与迁移原则

迁移期间保留：

- `POST /tasks`
- `GET /tasks/{id}`
- `GET /tasks/{id}/events`
- `POST /tasks/{id}/reply`

兼容层负责把旧接口映射到新的 root graph 执行，不要求前端一次性重写。

## 风险

1. **一次性替换过大**：如果同时替换 runtime、orchestrator、agent、存储，风险过高
2. **deepagents 过度接管**：如果把 deepagents 当整个平台，会失去对平台边界的控制
3. **领域 schema 设计不足**：如果没有结构化 artifact，最终还是会退化成“长 prompt + 长文本输出”
4. **HITL 设计滞后**：若先做 agent 再补 review gate，后续会返工

## 结论

目标架构应当是：

> **以 LangGraph 为平台运行时，以 deepagents 为复杂领域 agent harness，以 research / patent / zero_report 三个领域 agent 为核心产品能力，以 v2 streaming + interrupts + durable execution + structured artifacts 为统一产品协议。**

这条路线比继续堆叠当前 orchestrator/scheduler 更适合“hard-task agent”目标，也能保留现有 API 外壳作为迁移缓冲层。
