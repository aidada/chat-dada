# Deep Thinking 成熟度判断与实现指导

## 目的

本文档用于回答两个问题：

1. 什么样的 agent 才能被称为“具备成熟的 deep thinking 能力”
2. 在当前 hard-task agent 平台重构中，应该如何把 deep thinking 从“多步 prompt 技巧”做成“可验证的运行时能力”

本文档不讨论单个模型是否“更聪明”，而讨论 **agent runtime 是否具备稳定完成高难任务的工程能力**。

## 核心判断

一个 agent 是否具备成熟的 deep thinking 能力，不能只看它是否：

- 会分步骤回答
- 会写较长的分析
- 会调用若干工具
- 会在回答里展示“推理痕迹”

真正的判断应当看它是否具备以下系统能力：

1. 能将复杂任务拆成稳定的执行单元
2. 能在长流程中管理上下文膨胀
3. 能保存并利用中间状态，而不是每轮重来
4. 能在关键节点做自校验、交叉验证与回退
5. 能在预算和失败条件下做可控决策
6. 能产出结构化 artifact，而不只是长文本
7. 能在必要时进行人工接管或审批

因此，结论是：

> **deep thinking 不是一个模型开关，而是一套运行时协议。**

## 成熟度分级

### Level 0: 单轮生成

特征：

- 单次 prompt 直接输出
- 无任务拆解
- 无持久状态
- 无工具编排

结论：

- 不属于 deep thinking agent

### Level 1: 多步工具调用 Agent

特征：

- 会调用多个工具
- 会做简单 route / plan
- 结果依赖 prompt 指导

不足：

- 缺少状态治理
- 缺少 verifier
- 缺少 artifact-first 设计

结论：

- 属于多步 agent，但不算成熟 deep thinking

### Level 2: 长流程任务 Agent

特征：

- 有规划、上下文压缩、记忆、工具链
- 能执行较长任务
- 有一定的人工交互能力

不足：

- verifier/reviewer 仍然偏弱
- budget 主要是统计，不是控制协议
- artifact 可能仍以文本为主

结论：

- 属于 deep thinking 原型

### Level 3: 产品级 Deep Thinking Agent

特征：

- 有统一 runtime
- 有显式 planner / executor / verifier / review gate
- 有分层 memory/context
- 有预算协议
- 有结构化 artifact
- 有 HITL / interrupt / resume
- 有评估与观测体系

结论：

- 可认为具备成熟 deep thinking 能力

## 成熟度判断标准

### 1. Runtime 标准

必须具备：

- 统一执行模型，而不是多套执行语义并存
- 明确的 route / task state / streaming / interrupt / checkpoint
- 长任务与短任务都受同一平台协议约束

反例：

- 外层自定义 scheduler，内层各 agent 再各自实现 graph

### 2. Planning 标准

必须具备：

- 任务拆解不是一次性 prompt 结果，而是可更新的执行计划
- 支持层级任务、并行子任务或显式工作包
- 计划状态能影响后续执行

反例：

- “先做 A 再做 B” 只是回答文本里写了，但 runtime 不理解

### 3. Memory / Context 标准

必须具备：

- 用户长期记忆
- 会话记忆
- 工作区记忆
- 领域上下文记忆
- artifact/evidence 记忆

并满足：

- 有裁剪策略
- 有来源保真
- 不把 checkpoint 混入业务 memory

### 4. Verifier / Reviewer 标准

必须具备：

- 完整性检查
- 证据充分性检查
- 矛盾检测
- 输出结构检查
- 失败后回退或重试路径

反例：

- 在 prompt 里写“请自我检查”，但 runtime 没有 reviewer 节点

### 5. Budget 标准

必须具备：

- token budget
- step budget
- browser/tool budget
- 并发预算
- 降级策略
- 超预算后的停止或人工接管策略

反例：

- 只记录 token，不影响决策

### 6. Artifact 标准

必须具备：

- 中间产物是结构化对象，而不是只存在 LLM 文本中
- 最终输出可回溯到中间产物和证据
- artifact 可渲染、可审阅、可版本化

### 7. HITL 标准

必须具备：

- 澄清
- 审批
- 回退
- 恢复

且这些能力是 runtime-native，而不是随意插一个回调函数。

### 8. Evaluation 标准

必须具备：

- 任务成功率评估
- 输出质量评估
- 证据/引用质量评估
- 延迟/成本/稳定性指标
- 领域基准任务集

没有评估体系，就不能证明“成熟”。

## 当前重构目标的判断

对照 `2026-03-20-hard-task-agent-platform-design.md` 的目标架构：

- **架构层面**：已经具备做成成熟 deep thinking agent 的骨架
- **实现层面**：当前仍处于“可实现成熟能力”的设计阶段，而非已验证成熟

更准确地说：

> 当前重构文档解决的是“如何搭建成熟 deep thinking agent 平台的结构”，而不是“已经证明该平台实现后天然成熟”。

成熟与否，取决于后续是否真正落地：

- verifier
- budget protocol
- artifact schema
- layered memory
- HITL runtime
- evaluation loop

## 制作标准

如果要把重构后的 agent 做到“成熟”，建议以以下制作标准约束实现。

### 标准 A: 先 runtime，后 prompt

优先交付：

- root graph
- stream protocol
- interrupt/resume
- checkpointer

而不是先写一套超长 domain prompt。

### 标准 B: 先 artifact，后文风

优先定义：

- `ResearchReportDraft`
- `ClaimTree`
- `PriorArtMatrix`
- `RootCauseTree`
- `ActionMatrix`

再去优化最终表达风格。

### 标准 C: 先 verifier，后并发

一个没有 reviewer 的多 agent 系统，只会更快地产生错误结果。

所以优先顺序应为：

1. planner
2. executor
3. verifier
4. parallel/subagents

### 标准 D: 先预算协议，后开放探索

先明确：

- 什么时候允许继续深挖
- 什么时候切换便宜模型
- 什么时候终止
- 什么时候交给人工

否则 deep thinking 会退化成 uncontrolled wandering。

### 标准 E: 先共享能力层，后复制到多领域

先把以下做成共享能力：

- evidence store
- citation manager
- browser capability
- review gates
- memory interfaces

然后再复制到 research / patent / zero_report。

### 标准 F: 先评估基线，后宣称成熟

成熟不是主观感觉，而要靠任务集证明。

建议每个领域至少有：

- 20 个标准任务
- 5 个高难任务
- 失败样本回放集
- reviewer 输出一致性检查

## 实现指导

### 第一阶段

把当前系统提升到 Level 2.5：

- 单根 root graph
- graph-native streaming
- interrupt / resume
- memory 分层接口
- research 领域 graph-native 化

### 第二阶段

把 research 提升到 Level 3：

- verifier/reviewer 节点
- artifact schema
- evidence/citation 闭环
- browser capability 结构化输出
- 预算协议
- 研究任务评估集

### 第三阶段

将 Level 3 方法复制到：

- patent
- zero_report

并保留每个领域自己的 artifact / reviewer / tool policy。

## 结论

重构后的目标架构 **可以** 支撑成熟 deep thinking agent，但只有在满足本文档的 runtime、memory、verifier、budget、artifact、HITL、evaluation 标准后，才能真正称为“成熟”。

换句话说：

> **成熟 deep thinking 能力的判据，不是“看起来很会想”，而是“能稳定、可控、可审计地完成高难任务”。**
