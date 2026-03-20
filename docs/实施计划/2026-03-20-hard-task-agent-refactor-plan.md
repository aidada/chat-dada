# Hard-Task Agent 平台重构实施计划

**Goal:** 将当前“custom runtime + orchestrator + leaf agents”架构，分阶段重构为以 LangGraph 为核心运行时、以 deepagents 为复杂领域 agent harness、以 research/patent/zero_report 为主要能力面的 hard-task agent 平台。

**Architecture:** 单根 LangGraph 作为执行中心；runtime 仅保留 API/SSE/存储外壳；领域 agent 作为 subgraph 或 deepagents-backed graph；统一采用 v2 streaming、interrupt、checkpoint、artifact 模型。

**Tech Stack:** Python 3.13, FastAPI, LangGraph, deepagents, PostgreSQL, Redis, filesystem/object storage, LangSmith

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

---

## Phase 1: 建立平台骨架

### 目标

把 LangGraph 从 leaf-agent 内部实现，提升为平台运行时骨架，但暂时不替换现有业务流。

### 交付物

- `platform/` 目录
- `RootState` / root graph skeleton
- 统一的 graph streaming adapter
- `task_id -> thread_id` 映射策略
- memory 分层接口雏形（user / conversation / checkpoint）

### 任务

1. 创建 `platform/` 目录和基础模块：
   - `platform/root_graph.py`
   - `platform/state.py`
   - `platform/streaming.py`
   - `platform/interrupts.py`
   - `platform/tracing.py`
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
   - `ConversationMemoryProvider`
   - `CheckpointProvider`

### 影响文件

- Create: `platform/*`
- Update: `runtime/task_runtime.py`
- Update: `runtime/task_dispatcher.py`
- Update: `main.py`
- Update: `storage/user_store_v2.py`（接口适配，非语义重写）
- Update: `runtime/conversation_context.py`（抽象提升）

### 验证

- `POST /tasks` 和 SSE 路径保持可用
- root graph 能跑 legacy `general_chat` / `deep_research`
- graph streaming 能转成现有 UI 事件
- LangSmith 能看到 root trace

### 风险控制

- 保留旧执行路径 behind flag
- 默认先只让少数任务类型走 root graph

---

## Phase 2: 统一流式协议和 HITL

### 目标

将 streaming / user reply / interrupt 统一为 LangGraph-native 语义。

### 交付物

- v2 `StreamPart` 到前端事件的稳定映射
- review/clarification interrupt 机制
- `reply_to_task` 与 `Command(resume=...)` 桥接
- LangGraph checkpointer 接管执行恢复

### 任务

1. 设计内部事件协议：
   - `updates`
   - `messages`
   - `custom`
   - `tasks`
   - `checkpoints`
2. 将 `runtime/task_interaction.py` 从“任意 handler 回调”升级为 graph interrupt bridge
3. 增加中断恢复测试：
   - 根图中断
   - 子图中断
   - 恢复后继续 streaming
4. 将当前 `waiting_for_user` 状态改为 graph interrupt 主导
5. 将业务层 checkpoint 从 agent memory 中剥离，切到 LangGraph checkpointer

### 影响文件

- Update: `runtime/task_runtime.py`
- Update: `runtime/task_interaction.py`
- Update: `platform/interrupts.py`
- Update: `platform/streaming.py`
- Update: `capabilities/memory.py`（checkpoint 职责下沉）

### 验证

- 用户追问/审批能中断并恢复
- 中断时前端能显示“待处理 gate”
- 恢复后 graph 从正确节点继续

### 风险控制

- 首先只让 research 域使用新的 interrupt 流程
- 兼容旧 `ask_user()` 直到所有路径迁完

---

## Phase 3: 重构 Deep Research 为第一个领域 Agent

### 目标

把 `deep_research` 从“当前系统里最复杂的 leaf agent”升级为新的 `domain_agents/research`。

### 交付物

- `domain_agents/research/`
- research artifact schema
- graph-native 并行 worker 方案
- 统一 evidence/citation 能力
- 保留并迁移的 research context memory 能力
- 共享 browser capability 的 research 域接入

### 任务

1. 创建 `domain_agents/research/`：
   - `agent.py`
   - `prompts.py`
   - `tools.py`
   - `schemas.py`
   - `reviewers.py`
   - `renderers.py`
2. 保留现有 `deep_research` 的有效资产：
   - 上下文管理
   - progress tracker
   - 工具选择策略
   - report rewrite
   - findings / summaries / final report 落盘语义
3. 把 parallel worker 从单节点 `asyncio.gather` 改成 graph-native fan-out/fan-in：
   - subgraphs 或 `Send`
   - `subgraphs=True` 可见
4. 引入统一 `EvidenceItem` / `CitationMap`
5. 在 research agent 内加入 review gate：
   - 结构完整性
   - 引用完整性
   - 研究缺口提示
6. 抽出共享 browser capability，并在 research 域优先接入

### 影响文件

- Create: `domain_agents/research/*`
- Update: `agents/deep_research/run.py` 作为兼容 shim
- Update: `agents/deep_research/graphs.py` 或逐步退役
- Update: `capabilities/` 下的 evidence/citation 能力
- Create: `capabilities/toolkits/browser_toolkit.py` 或 `tools/browser_navigate.py`
- Update: `core/models.py`

### 验证

- simple/hierarchical/parallel research 都能运行
- 并行子代理可在 stream 中单独可见
- 引用和 artifact 能结构化输出
- 旧 `deep_research.run()` 入口仍可用
- browser capability 仅在需要页面级交互时被触发

### 风险控制

- 先保留旧 graph，与新 research domain agent 并存
- 用 feature flag 切换新旧 research pipeline

---

## Phase 4: 引入 Deepagents Harness

### 目标

把 `deepagents` 引入 research 域内部，用来收敛 planning、subagent delegation、filesystem workspace 等通用 agent 样板。

### 交付物

- 一个 deepagents-backed research agent 版本
- deepagents 与现有工具/能力层的适配桥
- 使用规范：哪些能力由 deepagents 接管，哪些仍保留平台自定义
- workspace memory 与 product memory 的明确边界

### 任务

1. 在 `domain_agents/research/agent.py` 中实现 `create_deep_agent(...)` 版本
2. 定义 subagents：
   - `web_researcher`
   - `paper_researcher`
   - `evidence_synthesizer`
3. 定义 filesystem workspace 边界
4. 将现有搜索工具、研究笔记、citation manager 暴露为 deepagents 工具
5. 保持 domain artifact/review gate 在平台侧，不让 deepagents 吞掉产品层语义
6. 明确 memory 边界：
   - `MemoryStoreV2` 继续作为产品级用户记忆
   - `ConversationContextBuilder` 升级为会话记忆服务
   - deepagents 接管工作区 memory
   - LangGraph 接管 checkpoint

### 影响文件

- Update: `domain_agents/research/agent.py`
- Update: `pyproject.toml` 依赖约束与使用说明
- Update: `platform/root_graph.py`
- Update: `storage/user_store_v2.py`
- Update: `runtime/conversation_context.py`

### 验证

- deepagents 版 research 能完成复杂研究任务
- 仍能通过 root graph streaming / interrupt / tracing 工作
- workspace 与 artifact 目录可控

### 风险控制

- 维持一个非-deepagents fallback 实现
- 不在此阶段迁移 patent / zero_report

---

## Phase 5: 新建 Patent Agent

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

### 验证

- 技术交底输入能稳定产出 claim tree + 说明书草稿
- prior-art 引用清晰
- 人工 review gate 可插入

---

## Phase 6: 新建 Zero Report Agent

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

### 验证

- 输入事故材料后可输出时间线、根因树、整改矩阵、正式报告草稿
- 可在关键节点要求人工审批或补件

---

## Phase 7: 迁移 Orchestrator 语义到 Root Graph

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

---

## Phase 8: 统一 Artifact、Review、Tracing 与前端 UI

### 目标

完成产品层闭环，让前端真正消费新的流式事件和结构化 artifact。

### 交付物

- 统一 artifact 浏览器/下载
- review gate UI
- provenance/evidence 卡片
- LangSmith dashboard/alert/eval

### 任务

1. 前端消费新的流式事件协议
2. 新增 artifact 侧边栏和来源卡片
3. 新增 review gate 卡片和恢复入口
4. 建立 LangSmith 项目、dashboard、alert
5. 为三类领域 agent 建立评估集

### 验证

- 前端可显示：
  - 步骤树
  - token 流
  - 子代理进度
  - artifact 演化
  - review gate
  - 证据来源卡片

---

## 关键实施顺序

按优先级排序的真实落地顺序：

1. Phase 1: 平台骨架
2. Phase 2: 统一流式协议和 HITL
3. Phase 3: 重构 Deep Research
4. Phase 4: 引入 deepagents 到 research 域
5. Phase 5: 新建 Patent Agent
6. Phase 6: 新建 Zero Report Agent
7. Phase 7: 退役旧 orchestrator/scheduler 主路径
8. Phase 8: 完成前端和观测体系

这条顺序的理由：

- 先解决平台执行中心，再做领域迁移
- 先证明最复杂的 research 任务跑通，再复制到 patent / zero_report
- 最后才删旧 orchestrator，避免过早切断稳定路径

---

## 与当前文件的映射关系

| 当前模块 | 目标去向 |
|----------|----------|
| `runtime/task_runtime.py` | 保留为 API 壳，但内部执行切到 root graph |
| `runtime/task_dispatcher.py` | 收缩为 route alias 或兼容入口 |
| `orchestrator/runner.py` | 兼容 shim，逐步退役 |
| `orchestrator/scheduler.py` | 迁移完成后退役 |
| `orchestrator/templates.py` | 迁为 route presets / legacy aliases |
| `agents/deep_research/*` | 提炼资产后迁入 `domain_agents/research/` |
| `agents/general_chat.py` | 作为 root graph 的 direct-chat path 保留 |
| `storage/user_store_v2.py` | 保留并抽象为产品级用户记忆 |
| `runtime/conversation_context.py` | 保留策略，升级为会话记忆服务 |
| `capabilities/context_manager.py` | 迁入 research 域，保留为领域上下文压缩能力 |
| `capabilities/memory.py` | findings/artifact 语义保留；checkpoint 语义移除 |
| `agents/search_agent.py::browser_navigate` | 上收为共享 browser capability |
| `agents/deep_research/run.py::browser_navigate` | 上收为共享 browser capability |
| `core/models.py::get_browser_use_llm` | 保留，继续作为 browser capability 的统一模型适配层 |

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

## 非目标

以下内容不作为本轮核心目标：

- 追求一个“万能”通用 agent prompt
- 一次性重写所有前端页面
- 一次性替换所有 storage 方案
- 在第一阶段就让 patent / zero_report 达到 production quality
- 用 `AGENTS.md` 替换结构化产品记忆

---

## 结论

这不是一次“把代码搬进 LangGraph”的技术迁移，而是一次产品底座迁移：

> **先把运行时改成 graph-native，再把领域工作流做成产品化 agent。**

只要按上述阶段推进，风险可控，而且每个阶段都能交付可验证的中间成果，不需要等待“大重写结束”才能见效果。
