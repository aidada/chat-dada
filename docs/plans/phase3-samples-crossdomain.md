# Phase 3 Cross-Domain Sample Inputs

**日期**: 2026-04-06
**用途**: Task 6 E2E 跨领域任务测试、Task 8 单技能延迟基准测试、Task 9 事件序列兼容性验证

---

## 样本列表（12个）

| ID | 目标描述 | 预期模式 | 预期技能 |
|----|---------|---------|---------|
| cd01 | 研究量子计算最新进展，并基于研究结果撰写专利申请 | DAG | do_research → do_patent |
| cd02 | 调研竞品技术方案，生成PPT演示文稿并附上分析报告 | DAG | do_research → do_ppt |
| cd03 | 分析最新AI论文，生成深度研究报告 | single_skill | do_research |
| cd04 | 调研氢能源技术发展现状，写一份完整的技术专利布局分析 | DAG | do_research → do_patent |
| cd05 | 帮我制作一个介绍公司新产品的PPT，包含产品功能演示和竞争优势分析 | single_skill | do_ppt |
| cd06 | 分析近期数据中心故障事件，生成零报告（事故分析报告） | single_skill | do_zero_report |
| cd07 | 研究脑机接口技术最新进展，生成综述报告，并制作配套PPT | DAG | do_research → do_ppt |
| cd08 | 调研竞争对手的AI产品功能，生成对比分析报告和专利侵权分析 | DAG | do_research → do_patent |
| cd09 | 帮我写一个关于区块链在供应链应用的技术专利 | single_skill | do_patent |
| cd10 | 分析电动汽车续航技术突破，生成行业研究报告，并制作CEO汇报PPT | DAG | do_research → do_ppt |
| cd11 | 调研量子机器学习的研究现状和商业化前景，生成完整研究报告 | single_skill | do_research |
| cd12 | 对最近的网络安全事件进行根因分析，生成零报告和改进建议 | single_skill | do_zero_report |

---

## 单技能样本（5个，用于延迟基准测试）

| ID | 目标描述 | 推断技能 |
|----|---------|---------|
| ss01 | 研究量子计算的最新进展 | do_research |
| ss02 | 帮我写一个软件专利申请 | do_patent |
| ss03 | 制作一个介绍AI技术的PPT | do_ppt |
| ss04 | 分析这次系统故障，生成零报告 | do_zero_report |
| ss05 | 调研区块链在金融领域的应用 | do_research |

---

## 跨领域任务说明

**DAG模式任务**（cd01, cd02, cd04, cd07, cd08, cd10）:
- 包含2个技能串行依赖：上游技能结果作为下游技能输入
- DAG结构：t1(上游技能) → t2(下游技能)

**single_skill模式任务**（cd03, cd05, cd06, cd09, cd11, cd12）:
- 单一技能即可完成
- Coordinator understand_goal 判断后直接调用技能，无DAG开销

---

## 测试方法

1. **E2E跨领域测试**: 通过 `tests/test_coordinator_phase3_crossdomain.py` 执行
2. **延迟基准测试**: 通过 `tests/test_coordinator_phase3_singleskill_latency.py` 执行
3. **事件序列测试**: 通过 `tests/test_coordinator_phase3_event_sequence.py` 执行

所有测试使用mock LLM和mock skill adapter，结果数据输出到对应报告文件。
