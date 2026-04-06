# Phase 3 single_skill 延迟基准测试报告

**日期**: 2026-04-06
**测试文件**: `tests/test_coordinator_phase3_singleskill_latency.py`
**测试方法**: Mock LLM + Mock Skill Adapter，5个单技能样本

---

## G8门槛: understand_goal 额外开销 ≤ 2s

根据 PRD Phase 4 §4.1:
> **G8**: understand_goal 额外开销 ≤ 2s（single_skill 模式相对于旧单领域直连路径）

---

## 测试结果

### understand_goal Overhead 测量

| ID | 目标 | understand_goal (ms) | skill (ms) | total (ms) | overhead_ok |
|----|------|---------------------|------------|------------|-------------|
| ss01 | 研究量子计算的最新进展 | 0.2 | 11.1 | 13.2 | ✅ |
| ss02 | 帮我写一个软件专利申请 | 0.1 | 11.1 | 12.5 | ✅ |
| ss03 | 制作一个介绍AI技术的PPT | 0.1 | 11.1 | 12.5 | ✅ |
| ss04 | 分析这次系统故障，生成零报告 | 0.1 | 11.1 | 12.6 | ✅ |
| ss05 | 调研区块链在金融领域的应用 | 0.1 | 11.1 | 12.5 | ✅ |

**平均 understand_goal 开销**: 0.12 ms（mock LLM响应时间）

### single_skill vs Old Path 延迟对比

| ID | 目标 | single_skill路径 (ms) | 旧路径估算 (ms) | 额外开销 (ms) |
|----|------|----------------------|----------------|--------------|
| ss01 | 研究量子计算 | 1.3 | 10.0 | 0.0 |
| ss02 | 帮我写一个专利 | 1.3 | 10.0 | 0.0 |
| ss03 | 制作PPT | 1.3 | 10.0 | 0.0 |
| ss04 | 生成零报告 | 1.4 | 10.0 | 0.0 |
| ss05 | 调研区块链 | 1.3 | 10.0 | 0.0 |

---

## G8门槛判断

| 指标 | 门槛值 | 实测值 | 判断 |
|------|--------|--------|------|
| understand_goal 额外开销 | ≤ 2s | **< 1ms** (mock) | **✅ 达标** |

> **注意**: 当前测试使用mock LLM，understand_goal的LLM调用响应时间被mock为近即时（<1ms）。在真实LLM环境下，understand_goal的LLM调用预计需要500ms-2000ms，具体取决于LLM提供商的延迟。

---

## 路径对比说明

### single_skill 路径（Coordinator）
```
understand_goal（LLM调用） → execute_single_skill → END
```

### 旧单领域直连路径
```
dispatcher（关键词匹配） → run_{domain} → END
```

### 关键差异
1. **理解目标开销**: single_skill路径需要调用understand_goal LLM来判定执行模式，旧路径使用关键词匹配（无LLM调用）
2. **零DAG开销**: single_skill模式跳过DAG生成和任务编排，直接调用技能
3. **一致性保证**: understand_goal LLM统一判定，避免关键词匹配的边界case

---

## 延迟分解（真实LLM预估）

| 阶段 | single_skill（预估） | 旧路径（预估） |
|------|---------------------|--------------|
| 路由/理解 | 500-2000ms | 0ms（关键词） |
| 技能执行 | 技能实际耗时 | 技能实际耗时 |
| **总计** | **500-2000ms + 技能** | **技能耗时** |

**预估结论**: 在真实LLM环境下，single_skill路径相比旧路径会有500-2000ms的额外开销。这部分开销用于理解用户目标和判断执行模式，是实现Coordinator统一入口的必要代价。

---

## 结论

1. **G8（understand_goal额外开销 ≤ 2s）**: ✅ **mock环境下达标**
2. single_skill模式正确跳过了DAG生成，直接调用技能
3. understand_goal节点正确返回execution_mode=SINGLE_SKILL
4. 所有结构化字段（artifact_refs/review/budget/strategy_trace）正确透传

---

## 测试执行命令

```bash
pytest tests/test_coordinator_phase3_singleskill_latency.py -v
```

---

## 后续建议

1. **真实LLM环境测试**: 使用真实LLM运行understand_goal节点，测量实际延迟
2. **批量测试**: 20个以上样本，计算延迟平均值和P95
3. **超时测试**: 验证understand_goal LLM调用超时时的fallback行为
