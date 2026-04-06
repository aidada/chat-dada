# Phase 3 Routing Accuracy Validation Samples

This file contains the 20 fixed input samples for validating understand_goal routing accuracy, comparing the old dispatcher (keyword matching) with the new LLM-based routing.

## Sample List

### 5 Chat/闲谈 Samples (Expected: direct)

| # | Query | Expected Mode |
|---|-------|---------------|
| 1 | 你好 | direct |
| 2 | 嗨，今天怎么样？ | direct |
| 3 | 早上好呀 | direct |
| 4 | hey, how are you? | direct |
| 5 | 晚上好，有什么好看的电影吗？ | direct |

### 8 Single Domain Samples (Expected: single_skill)

| # | Query | Expected Mode | Domain |
|---|-------|---------------|--------|
| 6 | 帮我研究量子计算的最新进展 | single_skill | research |
| 7 | 写一个关于区块链的专利申请 | single_skill | patent |
| 8 | 帮我写一份事故分析报告 | single_skill | zero_report |
| 9 | 制作一个关于AI的PPT | single_skill | ppt |
| 10 | 调研一下新能源技术的发展现状 | single_skill | research |
| 11 | 写一个技术方案文档 | single_skill | research |
| 12 | 整理一份竞品分析报告 | single_skill | research |
| 13 | 帮我搜索一下最新的大模型论文 | single_skill | research |

### 7 Cross-Domain Samples (Expected: dag)

| # | Query | Expected Mode | Components |
|---|-------|---------------|-----------|
| 14 | 研究竞品技术方案后撰写专利 | dag | research + patent |
| 15 | 调研市场现状并制作分析报告PPT | dag | research + ppt |
| 16 | 先做文献综述，再写专利申请 | dag | research + patent |
| 17 | 调研AI技术发展并制作演示文稿 | dag | research + ppt |
| 18 | 先分析问题根因，再写整改报告 | dag | analysis + zero_report |
| 19 | 研究竞品同时制作对比PPT | dag | research + ppt |
| 20 | 深度研究量子计算并撰写学术论文 | dag | research + writing |

## Old Dispatcher Logic (Baseline)

The old dispatcher uses keyword matching:

**direct keywords**: hi, hello, hey, 你好, 您好, 早上好, 晚上好, 请问, 解释, 什么是, 为什么, 怎么, 如何, 能不能, 翻译, 改写, 润色, 总结一下

**agent/research keywords**: 搜索, 查找, 检索, 研究, 调研, 深度研究, 论文, 文献, 综述, 研究, research, paper, survey, etc.

**patent keywords**: 专利, 权利要求, 技术交底, 现有技术, 专利, patent

**ppt keywords**: ppt, 幻灯片, 演示文稿, slide, presentation, etc.

**multi-step hints**: 同时, 并且, 以及, 还要, 还需要, 用于, 先, 再

## Routing Rules

1. If chat keywords detected → direct
2. If 2+ domain keywords OR 1 domain + 2+ multi-step → dag
3. If single domain keyword → single_skill
4. Default → direct