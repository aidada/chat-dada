# ResearchContext 重构：统一数据源 + 分级智能压缩

## 背景

当前系统有三套并行的"研究笔记"管道：

| 管道 | 数据 | 用途 |
|------|------|------|
| `findings: str` | 工具返回文本拼接，≤6000 字 | 遗留管道。送 `research_finish` 和 `_rewrite_final_report` |
| `ResearchContext` | 三层结构（raw + compact + summary） | prompt 构建管道。每轮给 LLM 的上下文 |
| `ResearchMemory` | 磁盘 .md 文件 | 持久化。checkpoint 恢复、recall_research_notes |

三个管道存储同一批工具返回的不同形态，导致：
1. 同一数据写入三次（`merge_tool_results` + `_merge_findings` + `save_finding`）
2. `findings` 的 6000 字截断丢失信息，且 ResearchContext 有内容时 findings 不参与 prompt
3. 压缩用前缀截断（`raw_content[:200]`），不保留关键数据

## 目标

1. 废弃 `findings` 字段，`ResearchContext` 成为唯一数据源
2. 压缩从"前缀截断"改为"规则摘要 + LLM 摘要分级"
3. 高价值 entry 延迟压缩，低价值 entry 优先压缩

## 设计

### 1. 废弃 findings，统一到 ResearchContext

**数据流变更：**

```
之前：  tool_msgs → findings (拼接)          ← 送 rewrite
        tool_msgs → ResearchContext (三层)   ← 送 prompt
        tool_msgs → ResearchMemory (磁盘)    ← 持久化

之后：  tool_msgs → ResearchContext (三层)   ← 唯一数据源
                 ├→ build_prompt_context()    ← 每轮 prompt
                 ├→ build_final_context()     ← 送 rewrite（新方法）
                 └→ ResearchMemory (磁盘)     ← 持久化（不变）
```

**具体改动：**

- `ResearchState` 移除 `findings: str` 字段
- 新增 `ResearchContext.build_final_context() -> str`：按步骤排序，输出所有 entry 的 compact_content 或 raw_content，用于最终报告改写
- `research_finish` 改为从 `state["research_context"]` 构建输出
- 删除 `_merge_findings()` 和 `_summarize_tool_messages()`
- `_rewrite_final_report` 接收 `ResearchContext.build_final_context()` 的输出

### 2. 分级智能压缩

替换当前的 `_compact_entry` 前缀截断。

**阶段 1：规则摘要（零 LLM 成本）**

提取结构化部分（标题、列表项、加粗文本、包含数据的句子），而非前缀截断：

```python
def _compact_entry(self, entry: FindingEntry, snippet_len: int = 300) -> None:
    text = entry.raw_content

    key_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if (stripped.startswith(('#', '-', '•', '*'))
            or '**' in stripped
            or re.search(r'\d+\.?\d*\s*(%|m|km|cm|ms|dB|accuracy)', stripped)):
            key_lines.append(stripped)

    if key_lines:
        snippet = '\n'.join(key_lines)[:snippet_len]
    else:
        snippet = text[:snippet_len]

    urls_str = ", ".join(entry.source_urls) if entry.source_urls else "(无来源)"
    entry.compact_content = f"{snippet}\n来源：{urls_str}"
    entry.raw_content = ""
```

**阶段 2：LLM 摘要（高价值 entry 专用）**

仅对 `evidence_strength == "strong"` 的 entry 使用 LLM 生成结构化摘要：

```python
async def _llm_compact_entry(self, entry: FindingEntry, query: str) -> None:
    llm = get_llm("orchestrator")
    resp = await _retry_async(llm.ainvoke, [
        SystemMessage(content="压缩为≤300字结构化摘要，保留所有数据、URL和关键结论。"),
        HumanMessage(content=f"研究主题：{query}\n\n原文：\n{entry.raw_content[:3000]}"),
    ])
    entry.compact_content = response_text(resp)
    entry.raw_content = ""
```

### 3. 动态阈值 + entry 保护

```python
async def trigger_compression(self, step: int, token_budget: int = 0, query: str = "") -> None:
    budget = token_budget or RAW_CONTENT_THRESHOLD
    total_raw = sum(len(e.raw_content) for e in self.entries)
    if total_raw <= budget:
        return

    # 按优先级排序压缩候选：低价值 + 旧的优先
    candidates = [
        e for e in self.entries
        if (step - e.step) >= 2 and e.raw_content and not e.compact_content
    ]
    candidates.sort(key=lambda e: (
        0 if e.evidence_strength == "strong" else 1,
        e.step,
    ), reverse=True)

    while total_raw > budget and candidates:
        entry = candidates.pop()
        old_len = len(entry.raw_content)
        if entry.evidence_strength == "strong":
            await self._llm_compact_entry(entry, query)
        else:
            self._compact_entry(entry)
        total_raw -= old_len
```

**保护规则：**

| 条件 | 行为 |
|------|------|
| `step - entry.step < 2` | 永远不压缩（最近 entry 保持完整） |
| `evidence_strength == "weak"` / 空 | 规则摘要，优先压缩 |
| `evidence_strength == "moderate"` | 规则摘要，正常压缩 |
| `evidence_strength == "strong"` | LLM 摘要，最后才压缩 |

### 4. build_final_context() 设计

```python
def build_final_context(self, max_chars: int = 12000) -> str:
    """构建最终报告输入，按步骤排序，尽可能保留完整内容。"""
    parts: list[str] = []

    if self.summary:
        parts.append(f"## 研究总结\n{self.summary}")

    for entry in sorted(self.entries, key=lambda e: e.step):
        content = entry.raw_content or entry.compact_content
        if not content:
            continue
        urls = f"\n来源：{', '.join(entry.source_urls)}" if entry.source_urls else ""
        parts.append(f"### [步骤{entry.step}] {entry.tool_name}\n{content}{urls}")

    result = "\n\n".join(parts)
    if len(result) > max_chars:
        result = result[:max_chars - 1] + "…"
    return result
```

## 涉及文件

| 文件 | 改动 |
|------|------|
| `context_manager.py` | 重写 `_compact_entry`, 新增 `_llm_compact_entry`, `build_final_context`, `trigger_compression` 改为 async |
| `agents/deep_research.py` | 移除 `findings` 相关逻辑，`research_finish` 改用 `ResearchContext`，删除 `_merge_findings` 和 `_summarize_tool_messages` |
| `ResearchState` | 移除 `findings: str` |
| `tests/test_context_manager.py` | 新增规则摘要、LLM 摘要、分级保护、build_final_context 测试 |
| `tests/test_deep_research.py` | 更新所有涉及 findings 的测试 |

## 注意事项

- `trigger_compression` 变为 async（因为 LLM 摘要），调用方需要 `await`
- `ResearchMemory` 不受影响——它已经独立持久化每个 finding
- `recall_research_notes` 不受影响——它读磁盘，不读 ResearchContext
- checkpoint 中 `findings` 字段保留为空字符串以兼容旧 checkpoint 加载
