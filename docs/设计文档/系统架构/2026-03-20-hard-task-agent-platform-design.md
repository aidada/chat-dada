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

## 记忆与上下文分层

`deepagents` 确实自带 memory/context management，但它解决的是 **agent 工作区上下文管理**，不是完整的 **产品级记忆体系**。

当前系统里实际上已经存在 4 类不同性质的记忆：

1. **用户长期记忆**：用户画像、偏好、项目生命周期、近期时间线
2. **会话上下文记忆**：多轮任务/对话的摘要、最近轮次、检索增强上下文
3. **领域任务上下文记忆**：如 research 的 `raw -> compact -> summary`
4. **任务 artifact / 证据记忆**：findings、sources、draft、final report、附件证据

另外还有一类不应再被当成“业务记忆”的内容：

5. **执行状态恢复**：checkpoint / interrupt / resume

这一类应由 LangGraph runtime/checkpointer 接管，而不是由领域 agent 自己保存 JSON 状态。

### 目标记忆分层图

```text
                         ┌────────────────────────────────────┐
                         │        User Memory Layer           │
                         │ facts / preferences / projects     │
                         │ structured, cross-task, product    │
                         └────────────────┬───────────────────┘
                                          │
                         ┌────────────────▼───────────────────┐
                         │    Conversation Memory Layer       │
                         │ summary / recent rounds / recall   │
                         │ task-thread or conversation scoped │
                         └────────────────┬───────────────────┘
                                          │
                         ┌────────────────▼───────────────────┐
                         │     Workspace Memory Layer         │
                         │ AGENTS.md / skills / files         │
                         │ deepagents-native working memory   │
                         └────────────────┬───────────────────┘
                                          │
                  ┌───────────────────────▼────────────────────────┐
                  │         Domain Context Memory Layer            │
                  │ research raw/compact/summary, patent notes,    │
                  │ zero-report timeline/root-cause structures     │
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

### deepagents 自带 memory/context 的能力边界

`deepagents` 原生提供的内容主要是：

- `memory=[...]` 对 `AGENTS.md` 的自动注入
- `FilesystemBackend` 读写工作区文件
- 长任务自动摘要与大输出落文件
- subagent 的隔离上下文
- skills 的按需加载

这些能力非常适合：

- 复杂任务拆解
- 长文档/长研究的工作区管理
- 子代理协作时控制上下文膨胀
- 让 agent 在文件工作区里逐步构建中间产物

但它不直接解决：

- 结构化用户画像与项目生命周期
- 产品级跨任务记忆召回
- 会话轮次检索与摘要治理
- 研究领域的来源保真与证据链压缩
- 平台级 task/thread checkpoint 语义

### 与当前方案的对比

| 维度 | 当前方案 | deepagents 内建方案 | 结论 |
|------|----------|---------------------|------|
| 用户长期记忆 | `MemoryStoreV2`，有 facts/projects/confidence/timeline | `AGENTS.md` 文本记忆 | 当前方案明显更强，应保留 |
| 会话上下文 | 摘要 + recent + retrieval | 自动摘要，偏 agent 内部 | 当前方案更适合产品级会话记忆，应保留思路 |
| 任务工作区 | 目前分散在 research memory / 文件落盘 | `FilesystemBackend` + 文件工具天然支持 | deepagents 更适合，建议采用 |
| 研究上下文压缩 | `ResearchContext` 的 `raw -> compact -> summary` | 通用 auto-summarization | research 域保留现有方案更好 |
| artifact / 证据 | findings/summaries/final_report 落盘 | 可写文件，但无固定 schema | 当前方案应升级为结构化 artifact/evidence store |
| checkpoint / resume | `ResearchMemory` 自定义 JSON checkpoint | graph 侧由 LangGraph 更适合接管 | 应放弃自定义 checkpoint |

### 兼容策略

推荐策略不是“二选一”，而是分层兼容：

#### 保留当前方案的部分

- `storage/user_store_v2.py`
  - 保留为产品级用户长期记忆
- `runtime/conversation_context.py`
  - 升级为通用会话记忆服务
- `capabilities/context_manager.py`
  - 保留为 research 域上下文压缩能力
- `capabilities/memory.py` 中的 findings / summaries / final report 落盘思想
  - 提炼为 artifact/evidence store

#### 引入 deepagents 的部分

- 工作区文件系统记忆
- 长任务自动摘要
- subagent 隔离上下文
- skills / AGENTS.md 规则注入

#### 应替换掉的部分

- `capabilities/memory.py` 中的自定义 checkpoint/resume
  - 由 LangGraph checkpointer 接管

### 最终建议

最合适的落点是：

```text
UserMemory              -> 保留现有结构化方案
ConversationMemory      -> 保留现有策略并服务化
WorkspaceMemory         -> 使用 deepagents
DomainContextMemory     -> 保留 research 等领域专用方案
Artifact/EvidenceMemory -> 由现有 research memory 升级而来
ExecutionCheckpoint     -> 改用 LangGraph
```

也就是说：

> **产品级 memory 继续用当前方案；agent 工作区 memory 用 deepagents；执行恢复交给 LangGraph。**

## Browser Capability 设计

当前仓库中，`browser_use` 只在两个地方被实际使用：

- `agents/search_agent.py` 中的 `browser_navigate`
- `agents/deep_research/run.py` 中的 `browser_navigate`

底层还有一层统一适配：

- `core/models.py` 中的 `_BrowserUseResponsesAdapter`
- `core/models.py` 中的 `get_browser_use_llm(...)`

这说明当前系统已经具备 browser automation 的基础能力，但其组织方式仍是：

1. **能力分散**：`search` 和 `deep_research` 各自内嵌一份工具
2. **返回值过弱**：当前浏览器工具主要返回字符串总结，不利于前端渲染和证据追踪
3. **缺少平台约束**：没有统一的启用条件、速率控制、审计结构和 artifact 输出规范

因此，重构后不应把 `browser_use` 当成“某个 agent 的私有工具”，而应提升为 **共享 browser capability**。

### 目标定位

`browser_use` 在目标架构中的角色是：

> **高成本、低吞吐、强交互的网页执行能力。用于动态页面、多步交互、结构化页面证据采集，而不是默认搜索路径。**

它适合解决的问题：

- JS-heavy 页面抓取
- 登录后/交互后页面内容提取
- 分页、展开、点击后可见信息采集
- 需要截图、页面证据和步骤审计的网页任务

它不适合作为：

- 默认搜索入口
- 通用问答的常规路径
- 没有明确页面交互需求时的第一选择

### 目标分层位置

推荐将其上收为共享 toolkit：

```text
capabilities/
└── toolkits/
    └── browser_toolkit.py

或

tools/
└── browser_navigate.py
```

由领域 agent 显式引用，而不是在每个 agent 内部各写一遍。

### 在各领域 agent 中的适用性

#### 1. Research Agent

最适合使用 `browser_use`。

典型场景：

- 访问搜索结果中的具体网页
- 抓取需要展开/滚动/点击后才能看到的研究资料
- 获取动态网页中的事实、表格、引用和截图
- 对网页证据进行可追溯采集

建议策略：

- 优先用搜索/API 工具发现来源
- 仅当需要深入页面细节时再升级到 browser capability

#### 2. Patent Agent

可以使用，但应克制。

典型场景：

- 浏览 Google Patents / Espacenet / CNIPA / USPTO 的具体专利详情页
- 采集现有技术页面、产品规格页、技术公告页
- 获取需要页面交互后可见的对比证据

不建议：

- 让 browser capability 成为 prior-art 检索默认主路径

建议策略：

- 检索优先走专利检索工具/API
- 页面级核实和证据补充再用浏览器

#### 3. Zero Report Agent

仅在需要接 Web 门户时启用。

典型场景：

- 工单系统
- 监控平台
- 事故时间线后台
- 内部 wiki / 知识库页面

如果数据已通过日志、附件、结构化接口可获得，则不应使用浏览器。

### 目标输出结构

重构后，browser capability 不应继续只返回字符串，而应返回结构化结果：

```json
{
  "summary": "页面任务总结",
  "visited_urls": ["..."],
  "page_artifacts": [
    {
      "url": "...",
      "title": "...",
      "html_path": "...",
      "screenshot_path": "...",
      "text_excerpt": "..."
    }
  ],
  "extracted_evidence": [
    {
      "type": "quote|table|fact|screenshot",
      "content": "...",
      "source_url": "...",
      "locator": "selector/xpath/section"
    }
  ],
  "execution_log": [
    {"step": 1, "action": "open", "target": "..."},
    {"step": 2, "action": "click", "target": "..."}
  ]
}
```

这样才能支撑：

- 文档溯源卡片
- 页面截图卡片
- 工具调用进度条
- 证据引用与审计

### 运行约束

由于 browser capability 慢、贵、脆，必须加平台级约束：

1. 并发限制
2. 最大步数限制
3. 任务超时
4. 域级白名单/策略
5. 截图和页面 artifact 的存储策略
6. LangSmith trace tags（区分 browser task）

### 最终建议

重构后应将 `browser_use` 定义为：

> **共享的 browser capability，由 research 域优先使用，patent 域谨慎使用，zero_report 域按需使用；禁止作为平台默认执行路径。**

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

## 风险

1. **一次性替换过大**：如果同时替换 runtime、orchestrator、agent、存储，风险过高
2. **deepagents 过度接管**：如果把 deepagents 当整个平台，会失去对平台边界的控制
3. **领域 schema 设计不足**：如果没有结构化 artifact，最终还是会退化成“长 prompt + 长文本输出”
4. **HITL 设计滞后**：若先做 agent 再补 review gate，后续会返工

## 结论

目标架构应当是：

> **以 LangGraph 为平台运行时，以 deepagents 为复杂领域 agent harness，以 research / patent / zero_report 三个领域 agent 为核心产品能力，以 v2 streaming + interrupts + durable execution + structured artifacts 为统一产品协议。**

对于 memory/context，最终结论是：

> **保留现有结构化用户记忆和领域上下文方案，引入 deepagents 的 workspace/context 管理能力，并将 checkpoint/resume 统一迁移到 LangGraph runtime。**

这条路线比继续堆叠当前 orchestrator/scheduler 更适合“hard-task agent”目标，也能保留现有 API 外壳作为迁移缓冲层。
