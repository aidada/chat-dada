# Research Workflow Worker 收敛修复实施计划

**Goal:** 修复 research workflow 在 `optimize_modules` 阶段出现的“重复检索、空草稿 finish、依赖链卡死、评审无法进入下一轮”的结构性问题，使科研任务能够稳定走完 `评审 -> 定向修订 -> 再评审 -> 最终整合` 闭环。

**Scope:** 仅覆盖 research domain 的 worker 执行结构、review/replan 路由、检索去重，以及 SSE/DB 生命周期的回归加固与可观测性；不改动 patent / zero_report 的业务语义，但要抽出可复用的 worker 机制以便后续迁移。

**Tech Stack:** Python 3.13, FastAPI, LangGraph, LangSmith, PostgreSQL, Redis, filesystem memory, Exa / academic_search / Tavily / Brave

当前代码基线：

- `domain_agents/research/worker.py`
- `domain_agents/research/workflow.py`
- `domain_agents/research/prompts.py`
- `domain_agents/research/config.py`
- `domain_agents/research/tools.py`
- `tools/academic_search.py`
- `apps/web/deps/auth.py`
- `apps/web/routers/tasks.py`
- `apps/web/runtime.py`
- `chat-dada-front/src/App.tsx`
- `chat-dada-front/src/styles.css`

---

## 问题摘要

当前 research workflow 存在以下结构性问题：

1. `optimize_modules` 进入后，worker 经常反复执行 `planner -> tools -> planner`，但无法收束成模块正文。
2. worker 的工具结果没有进入下一轮 prompt，导致模型看不到已检索证据，重复发出近义查询。
3. `problem_definition` 被分配给 `citation_worker`，角色目标偏“继续找文献”，而不是“先收束问题定义”。
4. worker 到达 `max_worker_rounds` 后会被硬截断，出现 `finish -> findings: ""`。
5. 空 `findings` 会让根模块长期卡在 `needs_revision`，其依赖模块无法进入后续 wave。
6. `evaluate_draft` 的评审摘要与 revision targets 虽然生成了，但没有稳定形成“完成修订后再评审”的闭环。
7. SSE 长连接相关问题已经通过短生命周期鉴权修复，但仍需要回归加固、补观测和防回退测试。

---

## 设计原则

### 1. 检索与写作必须分阶段

worker 不能继续沿用单一 `planner` 节点同时承担：

- 决定搜什么
- 执行工具
- 整理证据
- 输出模块草稿

必须显式拆成“检索阶段”和“起草阶段”。

### 2. 空草稿不是完成态

`finish -> findings: ""` 不能再被视为合法结束。  
worker 的结束态必须是：

- `completed`: 有非空模块正文
- `blocked`: 无法继续，但输出结构化 blocker
- `error`: 执行失败

### 3. 依赖链阻塞必须显式暴露

当根模块无法收敛时，应把 blocker 上浮到 workflow，而不是让下游模块静默等待。

### 4. review / replan 信号不能在 checkpoint 被弱化

如果 evaluator 判定 `needs_replan = true`，后续 checkpoint 不能被一句“继续”覆盖。

### 5. 优先做结构修复，再做策略调优

当前问题首先是执行结构错误，而不是 prompt 文案不够强。  
先修结构，再谈搜索策略和 prompt 优化。

---

## 总体方案

将 research worker 从当前的三节点循环：

`planner -> tools -> finish`

升级为五阶段执行器：

`plan_search -> run_tools -> integrate_evidence -> draft_module -> validate_module`

关键变化：

1. 工具结果不再只存在 `messages` 中，而要沉淀到结构化 `evidence_pack`
2. `draft_module` 必须读取 `evidence_pack`
3. `validate_module` 必须明确判断：
   - 是否有非空草稿
   - 是否满足最低引用/结构要求
   - 是否需要 `blocked`
4. `optimize_modules` 只在上游模块真正完成后才解锁依赖模块

---

## Phase 1: Worker 状态机重构

### 目标

让单个模块 worker 能稳定从“检索”进入“起草”，避免无限重复搜索。

### 任务

1. 重构 [domain_agents/research/worker.py](../../../domain_agents/research/worker.py) 的 `WorkerState`：
   - 新增 `evidence_pack`
   - 新增 `search_history`
   - 新增 `query_fingerprints`
   - 新增 `draft_status`
   - 新增 `blocker_reason`
2. 将当前 `planner` 节点拆分为：
   - `plan_search`
   - `draft_module`
   - `validate_module`
3. 保留 `run_tools`，但 tools 的输出必须在 `integrate_evidence` 中被结构化整理。
4. `draft_module` 输入必须包含：
   - `brief`
   - `module_plan`
   - `dependency_context`
   - `existing_draft`
   - `revision_instructions`
   - `evidence_pack`
5. `validate_module` 明确输出：
   - `completed`
   - `blocked`
   - `needs_more_evidence`

### 验收

1. worker 可以在 1-2 轮检索后进入起草
2. `draft_module` 的 prompt 中可看到结构化证据摘要
3. `finish -> findings: ""` 不再作为正常结束路径

---

## Phase 2: 角色语义与模块分配修正

### 目标

让不同模块由语义匹配的 worker 处理，减少天然的“过度检索”倾向。

### 任务

1. 先将 `problem_definition` 从 `citation_worker` 改给 `argument_worker`，复用现有角色体系验证是否足以稳定收束问题定义。
2. 仅当 `argument_worker` 仍无法稳定收敛时，再评估是否新增 `problem_worker`。
3. 保持：
   - `related_work` -> `citation_worker`
   - `argument_map / contributions / limitations` -> `argument_worker`
   - `method_candidates / experiment_design` -> `method_worker`
4. 为不同 worker 配置不同检索预算：
   - `problem_definition` 所属角色：1-2 轮
   - `citation_worker`: 2 轮
   - `argument_worker`: 1 轮
   - `method_worker`: 2 轮，可一次全文核查

### 验收

1. `problem_definition` 不再主要表现为连续文献搜索
2. `related_work` 仍可保留足够的检索深度
3. `argument_map` 和 `limitations` 可以基于已有证据收束成正文

---

## Phase 3: 检索去重与 fallback 策略

### 目标

减少 Exa/academic_search 的重复调用，并在失败后自动切换到下一层可用工具。

### 任务

1. 在 research worker 层实现 query fingerprint：
   - 维度包括 `tool + query + mode + category + summary_query`
2. 对命中的重复 query 直接复用 `evidence_pack`，不再次调用工具
3. 对 academic_search 建立 fallback 链：
   - `academic_search`
   - `exa_deep_search` 精确 paper query
   - 必要时 `browser_navigate`
4. 记录每轮检索的：
   - 成功结果数
   - 新增证据数
   - 重复命中数

### 验收

1. 同模块内近义重复查询显著下降
2. academic_search 429 或无结果后不再原地重试
3. LangSmith trace 中能看到“重复命中缓存”与“fallback 发生”的证据

---

## Phase 4: Review / Replan 闭环修复

### 目标

让 `evaluate_draft -> checkpoint_b -> optimize_modules -> evaluate_draft` 真正闭环，而不是半途停在优化阶段。

### 任务

1. 保持 evaluator 的 `needs_replan` 信号，不允许 checkpoint 清零
2. 在 workflow 中引入模块级 blocker 上浮：
   - 如果某根模块 `blocked`
   - 直接进入 review 或人工确认
   - 不再让下游依赖模块无限等待
3. 每轮评审保存结构化 diff：
   - 维度分数变化
   - revision targets 变化
   - changed_modules
   - unchanged_modules
4. 将每轮 `aggregate_draft`、`evaluation`、`module_outputs` 全量落盘

### 验收

1. 第 2 轮及以后评审可以明确回答“这一轮具体优化了什么”
2. 如果优化没有推进，系统会显式显示 blocker
3. 不再出现“优化阶段长时间运行但没有进入下一轮评审”

---

## Phase 5: SSE 与连接生命周期修复

### 目标

把已修复的长连接 SSE 鉴权改动纳入回归加固，避免后续重构再次占住 DB session、拖垮连接池。

### 任务

1. 保留并固定已实现的短生命周期鉴权入口：
   - [apps/web/deps/auth.py](../../../apps/web/deps/auth.py)
   - [apps/web/routers/tasks.py](../../../apps/web/routers/tasks.py)
   - [apps/web/runtime.py](../../../apps/web/runtime.py)
2. 检查其他长生命周期响应路径，确保不再复用 request-scoped DB session
3. 增加连接池与 SSE 订阅数量的观测：
   - 活跃 SSE 数
   - DB pool in-use 数
   - 鉴权查询耗时

### 验收

1. `/tasks/{task_id}/events` 不再长期持有 request-scoped session
2. 连接池不会因多个 SSE 订阅而耗尽
3. 不再出现 GC 清理未归还连接的告警

---

## Phase 6: UI 可视化与可运营性

### 目标

让用户和开发者都能直接看见“当前卡在哪个模块、修订推进到哪里”。

### 任务

1. 前端 task panel 中展示结构化 review：
   - 当前轮 summary
   - revision targets
   - 已完成模块
   - 阻塞模块
2. 在详情弹窗中支持：
   - 评审摘要
   - 待修订模块
   - 当前草稿摘录
   - blocker 原因
3. 新增任务 trace API 字段：
   - `revision_round`
   - `active_modules`
   - `blocked_modules`
   - `last_evaluation_diff`

### 验收

1. 用户能在 UI 中看到“为什么还在跑”
2. 开发者不必只靠 LangSmith 才能判断流程卡点

---

## 文件级任务清单

### 需要修改

- [domain_agents/research/worker.py](../../../domain_agents/research/worker.py)
  - worker state
  - 子图结构
  - 完成态/阻塞态定义
  - evidence pack 集成
- [domain_agents/research/prompts.py](../../../domain_agents/research/prompts.py)
  - 新增 search/draft/validate prompt builder
  - 调整 `problem_definition` 对应角色的 prompt 约束
- [domain_agents/research/workflow.py](../../../domain_agents/research/workflow.py)
  - blocker 上浮
  - 评审 diff 落盘
  - optimize/review 闭环控制
- [domain_agents/research/config.py](../../../domain_agents/research/config.py)
  - 不同 worker 的检索预算配置
- [domain_agents/research/tools.py](../../../domain_agents/research/tools.py)
  - academic fallback 编排
- [tools/academic_search.py](../../../tools/academic_search.py)
  - 失败语义与 fallback 协议
- [core/logger.py](../../../core/logger.py)
  - 记录 worker 收束状态、重复 query 命中、usage availability
- [apps/web/routers/tasks.py](../../../apps/web/routers/tasks.py)
  - trace/review API 字段补充
- [apps/web/runtime.py](../../../apps/web/runtime.py)
  - SSE metadata 扩展
- `chat-dada-front/src/App.tsx`
  - review / blocker 展示
- `chat-dada-front/src/styles.css`
  - task panel / modal 布局

### 需要新增

- `capabilities/retrieval_cache.py`
- `tests/test_research_worker_convergence.py`
- `tests/test_research_review_diff.py`
- `tests/test_task_sse_auth_lifecycle.py`

---

## 最小上线顺序

1. Phase 1 + Phase 2
2. Phase 3
3. Phase 4
4. Phase 5
5. Phase 6

原因：

- Phase 1/2 解决“写不出来”
- Phase 3 解决“搜太多”
- Phase 4 解决“评审闭环不成立”
- Phase 5 解决“长连接拖垮基础设施”
- Phase 6 解决“不可观测”

---

## 当前任务 `task_16e59f56d9d8` 的处理建议

不要继续沿当前 in-flight trace 运行。

推荐操作：

1. 先完成 Phase 1 + Phase 2 的结构修复
2. 保留当前已确认的：
   - brief
   - clarification_history
   - 第 1 轮 `revision_targets`
3. 基于这些状态重新启动优化流程
4. 重新跑的目标路径应为：
   - `problem_definition`
   - `related_work`
   - `argument_map`
   - `limitations`
   - `aggregate_draft`
   - `evaluate_draft`

成功信号：

- `problem_definition` 首次产出非空草稿
- 后续 3 个依赖模块开始进入 wave
- 第二轮 `evaluate_draft` 被真正触发

---

## 完成定义

这项修复完成，至少要满足：

1. `problem_definition` 不再长时间停在重复检索循环
2. `finish -> findings: ""` 不再出现在正常完成路径
3. `related_work / argument_map / limitations` 能在依赖满足后依次推进
4. 第 2 轮及以后 `evaluate_draft` 能给出可比较的修订差异
5. SSE 长连接不再引发 DB 连接池耗尽
