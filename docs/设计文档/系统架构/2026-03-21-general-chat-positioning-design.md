# General Chat 定位与路由语义设计

## 目的

本文档定义 `general_chat` 在 hard-task agent 平台中的目标定位，回答三个问题：

1. `general_chat` 是否是一个独立领域 agent
2. `route_domain` 在意图不明确时应如何处理
3. 本轮重构是否需要升级 `general_chat` 本体

本文档不把 `general_chat` 升级为本轮主线目标，而是为后续单独重构预留清晰边界。

## 当前状态

当前 [general_chat.py](/Users/luozhongxu/workspace/chat-dada/agents/general_chat.py) 的职责很简单：

- 接收 `query`
- 可消费外部注入的 `memory_context`
- 可消费外部注入的 `conversation_context`
- 直接进行流式或非流式问答

它当前不是“完全没有上下文能力”，而是：

- 自身不负责召回 memory
- 自身不负责领域判定
- 自身不负责多轮澄清后的再路由

因此，问题不在于它“不会聊天”，而在于重构文档若只把它写成 `direct-chat path`，会低估它在根图中的语义位置。

## 目标定位

`general_chat` 在目标架构中的定位应是：

1. `fallback path`
2. `conversation gateway`
3. `intent clarification entry`

它 **不是独立领域 agent**，也不应与 `research / patent / zero_report` 处于完全对称的位置。

更准确的职责是：

- 当用户只是寒暄、闲聊、快速问答时，作为终点路径
- 当用户意图不明确、信息不完整时，负责进行澄清性对话
- 当澄清后领域意图变清楚时，把控制权交回 `route_domain`

## Router 目标语义

`platform/router.py` 不应只返回“命中哪个领域”，而应支持至少三种结果：

1. `direct_chat`
2. `domain_selected`
3. `needs_clarification`

建议的语义如下：

### `direct_chat`

适用场景：

- 闲聊
- 简单问答
- 产品介绍类问题
- 与领域任务无关的轻量交互

执行方式：

- 直接进入 `run_general_chat`
- 不进入领域 agent

### `domain_selected`

适用场景：

- 已能明确判断为 `research`
- 已能明确判断为 `patent`
- 已能明确判断为 `zero_report`

执行方式：

- 进入对应领域 agent

### `needs_clarification`

适用场景：

- 用户需求尚未成形
- 同一句话可能落到多个领域
- 关键信息缺失，无法安全路由

执行方式：

1. 进入 `run_general_chat`
2. 使用澄清式对话补充信息
3. 将新对话上下文回写 `thread_context`
4. 再次进入 `route_domain`

## 根图中的建议路径

建议根图中允许以下链路：

```text
normalize_input
  -> route_domain
    -> direct_chat -> run_general_chat -> finish
    -> domain_selected -> run_domain_agent -> review/render/persist -> finish
    -> needs_clarification -> run_general_chat -> route_domain -> ...
```

这里的关键点是：

- `general_chat` 可以作为终点
- 也可以作为中间澄清节点
- 但不负责自己决定最终领域

## 与 Memory 的关系

`general_chat` 后续是否升级，可以分成两层看：

### 本轮重构要求

本轮只要求：

- `general_chat` 能消费 root graph 注入的 `memory_context`
- `general_chat` 能消费 root graph 注入的 `conversation_context`
- 澄清后的对话能回写到根图上下文，供下一次 `route_domain` 使用

### 后续可选升级

后续如果要单独重构 `general_chat`，可以增加：

- 更强的用户长期记忆召回策略
- 话题态跟踪
- 澄清模板
- 多轮意图收敛策略
- 与领域 agent 的 handoff 解释文案

但这不属于当前 hard-task 主线能力建设。

## 为什么不把它升为本轮主线

原因很简单：

1. 当前主线是 hard-task runtime、domain agent、artifact、review、budget
2. `general_chat` 目前已有可工作的流式问答实现
3. 当前缺口主要是“定位和路由语义”，不是“聊天质量”

因此，本轮只需要：

- 在设计文档中明确其定位
- 在 router 设计中引入 `needs_clarification`
- 在实施计划中加一个很小的 router 任务

而不需要把 `general_chat` 升级成完整的新项目。

## 对实施计划的影响

建议仅做轻量补充：

1. 在 `platform/router.py` 设计中加入 `direct_chat / domain_selected / needs_clarification`
2. 在 `RootState` 中增加可表达 router 决策与澄清状态的字段
3. 在 Phase 1 中增加一个小任务：
   - 定义 router 决策结果
   - 定义低置信度时进入 `general_chat` 澄清
4. 在映射表中将 `agents/general_chat.py` 的去向改成：
   - `conversation gateway / fallback path`

## 最终结论

`general_chat` 在目标架构里不应被当成与 `research / patent / zero_report` 并列的独立领域。

更准确的定义是：

> **它是 root graph 的 conversation gateway，既承担 fallback 终点，也承担 needs_clarification 的澄清入口。**

这意味着本轮不必重构 `general_chat` 本体，但必须在 router 和主设计文档中把它的语义写清楚。
