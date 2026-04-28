"""Root Graph — 单一编排控制面 (depth=0)。

负责理解目标、生成 AgentPlan、调度 Sub Graph 执行、汇总结果。
通过策略驱动调度创建 Sub Graphs (depth=1)。
"""
