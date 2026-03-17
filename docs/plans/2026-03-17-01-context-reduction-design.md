# 机制 1：分阶段上下文缩减

> 优先级：P0 | 预计影响文件：agents/deep_research.py, content_utils.py, 新增 context_manager.py

## 1. 概述与目标

当前 deep_research agent 用固定 6000 字符尾部截断管理 findings，工具消息截断到 700 字符——这是最粗暴的"一刀切"策略。目标是引入 Manus 风格的三级上下文缩减：**原始 > 压缩 > 摘要**，让信息按重要性和时间分级衰减，而非简单丢弃。

## 2. 行业参考

### Manus 的做法
- **压缩（可逆）**：网页内容删除但保留 URL，工具结果替换为紧凑版本（仅含引用路径），需要时可重新获取
- **摘要（有损）**：在上下文达 128K token 时触发；摘要时保留最近几次工具调用的完整原始格式；摘要模型接收完整原始版本
- **优先级**：原始 > 压缩 > 摘要

### Claude Code 的做法
- **微压实**：过大的工具输出保存到磁盘，上下文只留引用指针
- **自动压实**：上下文达 95% 时自动触发
- **手动压实**：将对话总结为结构化"工作状态"，然后重新注入关键文件

### AgentFold（学术前沿）
- 上下文分四层：用户原始问题（不变锚点）、工具定义、多尺度状态摘要（长期记忆）、最近交互（工作记忆）
- 均匀全历史摘要中每次迭代 1% 的细节损失，步骤 1 信息到步骤 100 存活概率仅 36.6%
- 关键细节需豁免重处理

## 3. 当前代码诊断

### `agents/deep_research.py`

| 位置 | 问题 |
|------|------|
| `_merge_findings()` (L431-438) | 固定 6000 字符尾部截断，早期发现被无差别丢弃 |
| `_summarize_tool_messages()` (L417-428) | 每条工具消息截断到 700 字符，最多保留 2 条，大量搜索结果直接丢失 |
| `_truncate_text()` (L456-460) | 纯字符级截断，不区分结论性内容和背景噪声 |
| `_build_research_messages()` (L344-365) | 每步都把完整 findings 塞入 prompt，无分级策略 |
| `research_planner()` (L218-230) | 每步都全量合并 findings，无增量更新概念 |

### 核心矛盾
15 步 × 每步 1-2 个工具调用 × 每个工具返回数千字 = 潜在几万字的研究材料，但只有 6000 字符的缓冲区。后期搜索无法参考早期结论，导致重复搜索和信息断层。

## 4. 架构设计

### 4.1 三级缩减模型

```
┌─────────────────────────────────────────────────┐
│                  原始层 (Raw)                     │
│  最近 2 步的工具调用和结果，完整保留               │
│  容量：不限（受 LLM 上下文窗口约束）               │
├─────────────────────────────────────────────────┤
│                  压缩层 (Compact)                 │
│  步骤 3~N 的工具结果，去除冗余但保留引用           │
│  网页内容 → 摘要 + URL                            │
│  论文结果 → 标题 + 结论 + DOI                     │
│  容量上限：4000 token                             │
├─────────────────────────────────────────────────┤
│                  摘要层 (Summary)                  │
│  所有已积累发现的结构化摘要                        │
│  按研究子主题分段，每段标注证据强度                 │
│  容量上限：2000 token                             │
└─────────────────────────────────────────────────┘
```

### 4.2 核心数据结构

```python
# context_manager.py（新增文件）

from dataclasses import dataclass, field
from enum import Enum


class ReductionLevel(Enum):
    RAW = "raw"           # 完整原始内容
    COMPACT = "compact"   # 去冗余，保留引用
    SUMMARY = "summary"   # 结构化摘要


@dataclass
class FindingEntry:
    """单条研究发现"""
    step: int                      # 产生该发现的步骤号
    tool_name: str                 # 来源工具
    query: str                     # 原始查询
    raw_content: str               # 完整原始内容
    compact_content: str = ""      # 压缩版本
    source_urls: list[str] = field(default_factory=list)
    evidence_strength: str = ""    # strong/moderate/weak/abstract-only
    key_claims: list[str] = field(default_factory=list)


@dataclass
class ResearchContext:
    """分阶段上下文管理器"""
    entries: list[FindingEntry] = field(default_factory=list)
    summary: str = ""              # 累积摘要
    current_step: int = 0

    def add_entry(self, entry: FindingEntry) -> None:
        self.entries.append(entry)

    def build_prompt_context(self, max_raw_steps: int = 2,
                              max_compact_tokens: int = 4000,
                              max_summary_tokens: int = 2000) -> str:
        """构建分级上下文供 LLM 使用"""
        sections = []

        # 摘要层：全局研究摘要
        if self.summary:
            sections.append(f"## 研究摘要\n{self._truncate_to_tokens(self.summary, max_summary_tokens)}")

        # 压缩层：较早步骤的压缩版本
        older = [e for e in self.entries if e.step <= self.current_step - max_raw_steps]
        if older:
            compact_parts = []
            for e in older:
                content = e.compact_content or self._auto_compact(e)
                compact_parts.append(f"### [{e.tool_name}] {e.query}\n{content}")
            compact_text = "\n\n".join(compact_parts)
            sections.append(f"## 已压缩的早期发现\n{self._truncate_to_tokens(compact_text, max_compact_tokens)}")

        # 原始层：最近几步的完整内容
        recent = [e for e in self.entries if e.step > self.current_step - max_raw_steps]
        if recent:
            raw_parts = []
            for e in recent:
                raw_parts.append(f"### [{e.tool_name}] {e.query}\n{e.raw_content}")
            sections.append(f"## 最近研究结果（完整）\n{''.join(raw_parts)}")

        return "\n\n".join(sections)

    def update_summary(self, new_summary: str) -> None:
        """更新累积摘要（由 LLM 生成）"""
        self.summary = new_summary

    @staticmethod
    def _auto_compact(entry: FindingEntry) -> str:
        """自动压缩：保留来源 URL + 关键主张"""
        parts = []
        if entry.key_claims:
            parts.append("关键发现：" + "；".join(entry.key_claims))
        if entry.source_urls:
            parts.append("来源：" + ", ".join(entry.source_urls))
        if entry.evidence_strength:
            parts.append(f"证据强度：{entry.evidence_strength}")
        return "\n".join(parts) if parts else entry.raw_content[:200] + "…"

    @staticmethod
    def _truncate_to_tokens(text: str, max_tokens: int) -> str:
        """粗略 token 估算截断（1 token ≈ 1.5 中文字符 / 4 英文字符）"""
        estimated_chars = max_tokens * 2  # 中英混合的粗略估算
        if len(text) <= estimated_chars:
            return text
        return text[:estimated_chars - 1].rstrip() + "…"
```

### 4.3 压缩触发策略

```python
def should_compress(context: ResearchContext) -> bool:
    """判断是否需要触发压缩"""
    total_raw_chars = sum(len(e.raw_content) for e in context.entries)
    return total_raw_chars > 8000  # 约 4000 token

def should_summarize(context: ResearchContext) -> bool:
    """判断是否需要触发摘要"""
    return len(context.entries) >= 6  # 每 6 步做一次全局摘要
```

### 4.4 LLM 辅助压缩（可选增强）

```python
COMPACT_SYSTEM = """你是研究笔记压缩器。将以下工具返回结果压缩为结构化摘要：
1. 提取 3-5 个关键主张（key claims）
2. 标注证据强度：strong（有实验数据）/ moderate（有论据但缺数据）/ weak（仅观点）/ abstract-only
3. 保留所有来源 URL 和 DOI
4. 删除重复背景信息和无关内容
输出格式：
关键主张：
- ...
证据强度：...
来源：...
"""

async def llm_compact(raw_content: str, query: str) -> str:
    """用小模型压缩工具输出"""
    llm = get_llm("summarizer")  # 用便宜的小模型
    response = await llm.ainvoke([
        SystemMessage(content=COMPACT_SYSTEM),
        HumanMessage(content=f"查询：{query}\n\n原始内容：\n{raw_content}")
    ])
    return extract_text_content(response)
```

## 5. 实现步骤

### Step 1：新增 `context_manager.py`
- 实现 `FindingEntry`、`ResearchContext` 数据结构
- 实现 `build_prompt_context()` 分级组装方法
- 实现 `_auto_compact()` 基于规则的自动压缩

### Step 2：修改 `agents/deep_research.py` 的 ResearchState
```python
# 修改 ResearchState，用 ResearchContext 替代 findings: str
class ResearchState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    query: str
    step_count: int
    research_context: dict       # ResearchContext 序列化后的 dict
    report_profile: str
```

### Step 3：修改 `research_planner` / `research_planner_node`
- 用 `ResearchContext.build_prompt_context()` 替代直接拼接 findings
- 每步调用 `add_entry()` 记录新发现
- 达到压缩阈值时触发 `_auto_compact()` 或 `llm_compact()`

### Step 4：修改 `_build_research_messages()`
- 接收 `ResearchContext` 而非 `str`
- 将三层上下文分别注入 prompt 的不同位置

### Step 5：修改 `_merge_findings()` → 重构为 `ResearchContext.merge()`
- 删除旧的尾部截断逻辑
- 新逻辑：添加条目 → 检查压缩阈值 → 必要时压缩早期条目

### Step 6：添加周期性摘要机制
- 每 6 步（可配置）调用 LLM 生成全局研究摘要
- 摘要作为"长期记忆"持续保留在最顶层

## 6. 测试方案

```python
# tests/test_context_manager.py

def test_finding_entry_creation():
    """测试 FindingEntry 创建和字段"""

def test_auto_compact_with_urls():
    """测试自动压缩保留 URL 和关键主张"""

def test_build_prompt_context_layering():
    """测试三级分层：最近 2 步原始 + 早期压缩 + 摘要"""

def test_context_truncation():
    """测试各层不超过 token 上限"""

def test_compression_trigger():
    """测试压缩触发条件"""

def test_summary_trigger():
    """测试摘要触发条件（每 6 步）"""

def test_empty_context():
    """测试空上下文不报错"""

async def test_llm_compact_integration():
    """集成测试：LLM 压缩输出格式正确"""
```

## 7. 验收标准

- [ ] 15 步研究后，步骤 1 的关键结论仍可在最终报告中被引用
- [ ] 上下文总 token 数不超过 8000 token（约 16000 字符）
- [ ] 压缩后的条目保留所有来源 URL
- [ ] 最近 2 步的工具结果始终完整可见
- [ ] 摘要层能正确反映全局研究进展
- [ ] 所有现有测试通过，无回归
