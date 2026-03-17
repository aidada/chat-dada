# TODO

## 已完成

- [x] 增加基于 `user_id` 的分层记忆骨架，存储格式为 markdown + 时间分层目录。
- [x] Web 端自动生成稳定的浏览器侧 `user_id` 并通过 WebSocket 上送。
- [x] 在编排入口增加记忆召回，并把召回内容注入规划与回答链路。
- [x] 在任务完成后写入记忆，包括原始时间线、月度摘要、长期画像。

## 当前记忆设计

- 长期画像：`data/memory/<user_id>/profile.md`
- 月度摘要：`data/memory/<user_id>/summaries/YYYY/YYYY-MM.md`
- 原始时间线：`data/memory/<user_id>/timeline/YYYY/MM/YYYY-MM-DD.md`

这套设计对应三层记忆：

- `Profile Memory`：稳定身份、偏好、项目、工作方式、约束、未完成事项
- `Episodic Summary`：按月滚动摘要，方便低成本回忆近期上下文
- `Raw Timeline`：按天记录完整交互，保留最细粒度证据

## 调研结论

截至 2026-03-13，对照生产级 agent 项目的官方资料，memory 通常不是单一“聊天历史”，而是至少拆成下面几类：

- 短期会话记忆：保存当前线程状态，支持多轮对话连续性。参考 OpenAI Agents SDK Sessions 和 LangGraph 的 thread/checkpoint 设计。
- 长期语义记忆：保存稳定事实、偏好、项目背景、用户画像，避免每轮都重新告诉 agent。
- 情节记忆：保存过去做过什么、什么时候做的、结果如何，便于召回历史决策和上下文。
- 可恢复执行状态：不仅记文本，还记 run/thread/checkpoint，支持中断后恢复。

参考资料：

- OpenAI Agents SDK Sessions: https://openai.github.io/openai-agents-python/sessions/
- LangGraph Memory / Durable Execution: https://docs.langchain.com/oss/python/langgraph/durable-execution
- Mem0 Docs: https://docs.mem0.ai/
- Letta Memory Concepts: https://docs.letta.com/home/concepts/memory

## 这次实现的取舍

- 先不用向量库，优先把“可落盘、可读、可排查”的 memory 基线建起来。
- 召回策略先采用 `长期画像 + 月度摘要 + 最近相关时间线片段`，避免一步到位做复杂 RAG。
- 长期画像优先用 LLM 提取，失败时退回正则启发式，避免记忆链路阻塞主流程。
- 当前轮请求始终优先于旧记忆，避免历史偏好压过实时指令。

## 仍然缺的部分

- [ ] 真正的 session/thread 概念。现在是按 `user_id` 聚合，不区分多个并行会话。
- [ ] 记忆冲突解决。旧偏好与新偏好冲突时，目前没有版本化和置信度策略。
- [ ] 删除与纠错能力。用户还不能显式要求“忘记某条记忆”或修正画像。
- [ ] 更好的召回排序。当前是轻量关键词匹配，还不是 embedding / hybrid retrieval。
- [ ] 更细的结构化写入。现在摘要和时间线是 markdown，后续可增加 frontmatter / metadata。
- [ ] 记忆预算控制。超长 profile 和 timeline 需要压缩、归档、淘汰策略。
- [ ] 记忆可观测性。需要知道每次召回了什么、命中率如何、是否误召回。

## 对整个项目的结论

项目现在已经不是纯 demo，已经有：

- registry 驱动的能力注册
- planner / scheduler / runner 的基本编排层
- 多 agent / tool / renderer 分层
- FastAPI + WebSocket 的交互入口

但它仍然缺少生产级控制面，这部分仍然是后续主线：

- [ ] 持久化 run/session/checkpoint，而不只是内存态执行
- [ ] tracing / structured logging / token-cost-latency 观测
- [ ] auth / rate limit / 审计
- [ ] 更安全的代码执行沙箱，避免本机直接子进程执行
- [ ] 测试体系、CI、回归评测集
- [ ] guardrails / human-in-the-loop / approval flow
- [ ] 配置治理和多环境部署骨架

## 建议的下一步

- [ ] 把 `user_id` 扩展为 `user_id + thread_id`，补上真正的线程级短期记忆
- [ ] 给 memory 文件增加 metadata 头，记录 intent、source、tags、importance
- [ ] 为 memory recall / save 加结构化日志，便于后续做 tracing
- [ ] 加一个“查看记忆”和“清除记忆”的 API
- [ ] 把长期画像提取从自由 JSON 改成严格 schema 校验
- [ ] 再决定是否引入向量库或外部 memory 服务
