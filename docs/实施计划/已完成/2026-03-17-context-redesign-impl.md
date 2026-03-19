# ResearchContext 重构实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 废弃 `findings` 冗余管道，统一到 `ResearchContext`；用规则摘要+LLM摘要替换前缀截断压缩。

**Architecture:** `ResearchContext` 成为唯一工具输出数据源，通过 `build_prompt_context()` 供每轮 prompt、`build_final_context()` 供最终报告改写。压缩分两级：规则提取结构化行（零成本）用于普通 entry，LLM 摘要用于 strong entry。

**Tech Stack:** Python, LangChain/LangGraph, unittest

---

### Task 1: context_manager.py — 智能规则摘要 + build_final_context

**Files:**
- Modify: `context_manager.py:156-183` (`_compact_entry` + `trigger_compression`)
- Modify: `context_manager.py:190-232` (after `build_prompt_context`, add `build_final_context`)
- Test: `tests/test_context_manager.py`

**Step 1: Write failing tests for smart _compact_entry**

在 `tests/test_context_manager.py` 的 `ExtractUrlsTests` class 中添加：

```python
def test_compact_entry_extracts_structured_lines(self) -> None:
    """Smart compaction should extract headings, list items, and data lines."""
    ctx = ResearchContext()
    raw = (
        "Some navigation text\n"
        "Cookie policy notice\n\n"
        "# GNSS Accuracy Study\n"
        "- Open sky accuracy: 3.2m\n"
        "- Urban canyon: 15.7m\n"
        "More filler text that is not important\n"
        "**Key conclusion: multipath degrades accuracy by 5x**\n"
    )
    entry = FindingEntry(step=1, tool_name="web_search", query="GNSS",
                         raw_content=raw, source_urls=["https://example.com"])
    ctx.add_entry(entry)
    ctx._compact_entry(entry)
    # Should contain the structured lines, not the filler
    self.assertIn("GNSS Accuracy Study", entry.compact_content)
    self.assertIn("3.2m", entry.compact_content)
    self.assertIn("multipath", entry.compact_content)
    self.assertNotIn("Cookie policy", entry.compact_content)
    self.assertEqual(entry.raw_content, "")

def test_compact_entry_fallback_to_prefix(self) -> None:
    """When no structured lines found, fall back to prefix truncation."""
    ctx = ResearchContext()
    entry = FindingEntry(step=1, tool_name="t", query="q",
                         raw_content="plain text without any structure " * 20)
    ctx.add_entry(entry)
    ctx._compact_entry(entry, snippet_len=100)
    self.assertTrue(len(entry.compact_content) > 0)
    self.assertEqual(entry.raw_content, "")
```

**Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_context_manager.py::ExtractUrlsTests::test_compact_entry_extracts_structured_lines tests/test_context_manager.py::ExtractUrlsTests::test_compact_entry_fallback_to_prefix -v
```

Expected: FAIL — 当前 `_compact_entry` 不做结构化提取。

**Step 3: Implement smart _compact_entry**

替换 `context_manager.py` 中的 `_compact_entry` 方法（当前在 ~line 156）：

```python
_DATA_PATTERN = re.compile(r'\d+\.?\d*\s*(%|m|km|cm|mm|ms|s|dB|Hz|MHz|GHz|accuracy|精度|误差)')

def _compact_entry(self, entry: FindingEntry, snippet_len: int = 300) -> None:
    """Compress entry using smart extraction of structured lines."""
    text = entry.raw_content

    key_lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if (stripped.startswith(('#', '-', '•', '*', '>', '|'))
            or '**' in stripped
            or _DATA_PATTERN.search(stripped)):
            key_lines.append(stripped)

    if key_lines:
        snippet = '\n'.join(key_lines)[:snippet_len]
    else:
        snippet = text[:snippet_len]

    urls_str = ", ".join(entry.source_urls) if entry.source_urls else "(无来源)"
    entry.compact_content = f"{snippet}\n来源：{urls_str}"
    entry.raw_content = ""
```

同时在文件顶部常量区（~line 26-29之后）加上 `_DATA_PATTERN` 编译好的正则。

**Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_context_manager.py -v
```

Expected: ALL PASS

**Step 5: Write failing test for build_final_context**

在 `tests/test_context_manager.py` 的 `ResearchContextTests` class 中添加：

```python
def test_build_final_context_includes_all_entries(self) -> None:
    ctx = ResearchContext()
    ctx.update_summary("研究总结内容")
    ctx.add_entry(FindingEntry(step=1, tool_name="web_search", query="q",
                               raw_content="", compact_content="压缩后的第一步",
                               source_urls=["https://a.com"]))
    ctx.add_entry(FindingEntry(step=3, tool_name="academic_search", query="q",
                               raw_content="第三步的完整内容"))
    output = ctx.build_final_context()
    self.assertIn("研究总结内容", output)
    self.assertIn("压缩后的第一步", output)
    self.assertIn("第三步的完整内容", output)
    self.assertIn("https://a.com", output)

def test_build_final_context_respects_max_chars(self) -> None:
    ctx = ResearchContext()
    ctx.add_entry(FindingEntry(step=1, tool_name="t", query="q",
                               raw_content="A" * 20000))
    output = ctx.build_final_context(max_chars=500)
    self.assertLessEqual(len(output), 500)

def test_build_final_context_sorted_by_step(self) -> None:
    ctx = ResearchContext()
    ctx.add_entry(FindingEntry(step=3, tool_name="t", query="q", raw_content="step3"))
    ctx.add_entry(FindingEntry(step=1, tool_name="t", query="q", raw_content="step1"))
    output = ctx.build_final_context()
    self.assertLess(output.find("step1"), output.find("step3"))
```

**Step 6: Run tests to verify they fail**

```bash
python -m pytest tests/test_context_manager.py::ResearchContextTests::test_build_final_context_includes_all_entries -v
```

Expected: FAIL — `build_final_context` 不存在。

**Step 7: Implement build_final_context**

在 `context_manager.py` 的 `ResearchContext` class 中，`build_prompt_context` 方法之后添加：

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

**Step 8: Run all context_manager tests**

```bash
python -m pytest tests/test_context_manager.py -v
```

Expected: ALL PASS

**Step 9: Commit**

```bash
git add context_manager.py tests/test_context_manager.py
git commit -m "feat: smart rule-based compaction + build_final_context for ResearchContext"
```

---

### Task 2: context_manager.py — 分级压缩（async trigger_compression + LLM 摘要）

**Files:**
- Modify: `context_manager.py:163-183` (`trigger_compression` → async, 分级逻辑)
- Test: `tests/test_context_manager.py`

**Step 1: Write failing tests for priority-based compression**

```python
def test_trigger_compression_strong_entry_preserved(self) -> None:
    """Strong evidence entries should be compressed last."""
    ctx = ResearchContext()
    # Add a weak entry at step 1 (should be compressed first)
    ctx.add_entry(FindingEntry(step=1, tool_name="t", query="q",
                               raw_content="W" * 5000, evidence_strength="weak"))
    # Add a strong entry at step 1 (should be preserved longer)
    ctx.add_entry(FindingEntry(step=1, tool_name="t", query="q",
                               raw_content="S" * 5000, evidence_strength="strong"))
    # Budget that only allows compressing one entry
    # total raw = 10000, budget = 6000 → need to free ~4000
    import asyncio
    asyncio.run(ctx.trigger_compression(step=5, token_budget=6000, query="test"))
    # Weak should be compressed, strong should still have raw
    self.assertEqual(ctx.entries[0].raw_content, "")  # weak compressed
    self.assertTrue(ctx.entries[0].compact_content)
    self.assertTrue(len(ctx.entries[1].raw_content) > 0)  # strong preserved
```

注意：由于 `trigger_compression` 变为 async，测试中需要 `asyncio.run()` 或者改用 `IsolatedAsyncioTestCase`。将 `ResearchContextTests` 改为继承 `unittest.IsolatedAsyncioTestCase`，然后现有的同步测试保持不变（unittest 允许 sync 方法在 IsolatedAsyncioTestCase 中运行），新测试用 async：

```python
async def test_trigger_compression_priority_order(self) -> None:
    """Weak entries compressed before strong when over budget."""
    ctx = ResearchContext()
    ctx.add_entry(FindingEntry(step=1, tool_name="t", query="q",
                               raw_content="W" * 5000, evidence_strength="weak"))
    ctx.add_entry(FindingEntry(step=1, tool_name="t", query="q",
                               raw_content="S" * 5000, evidence_strength="strong"))
    await ctx.trigger_compression(step=5, token_budget=6000, query="test")
    self.assertEqual(ctx.entries[0].raw_content, "")
    self.assertTrue(len(ctx.entries[1].raw_content) > 0)
```

**Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_context_manager.py::ResearchContextTests::test_trigger_compression_priority_order -v
```

Expected: FAIL — `trigger_compression` 不是 async，没有 priority 逻辑。

**Step 3: Implement async trigger_compression with priority**

替换 `context_manager.py` 中的 `trigger_compression`：

```python
async def trigger_compression(self, step: int, token_budget: int = 0, query: str = "") -> None:
    """Compress old entries when total raw content exceeds threshold.

    Priority: weak/empty entries first, strong entries last (use LLM summary).
    Entries within 2 steps of current are never compressed.
    """
    budget = token_budget or RAW_CONTENT_THRESHOLD
    total_raw = sum(len(e.raw_content) for e in self.entries)
    if total_raw <= budget:
        return

    # Collect compressible candidates (age >= 2, has raw, not yet compacted)
    candidates = [
        e for e in self.entries
        if (step - e.step) >= 2 and e.raw_content and not e.compact_content
    ]
    # Sort: strong last (will be popped last from reversed list)
    candidates.sort(key=lambda e: (
        0 if e.evidence_strength == "strong" else 1,
        e.step,
    ), reverse=True)

    while total_raw > budget and candidates:
        entry = candidates.pop()
        old_len = len(entry.raw_content)
        if entry.evidence_strength == "strong":
            try:
                await self._llm_compact_entry(entry, query)
            except Exception:
                self._compact_entry(entry)
        else:
            self._compact_entry(entry)
        total_raw -= old_len
```

同时添加 `_llm_compact_entry`：

```python
async def _llm_compact_entry(self, entry: FindingEntry, query: str) -> None:
    """Compress a high-value entry using LLM summarization."""
    from models import get_llm, response_text
    from langchain_core.messages import SystemMessage, HumanMessage

    llm = get_llm("orchestrator")
    resp = await llm.ainvoke([
        SystemMessage(content="请把以下工具返回内容压缩为≤300字的结构化摘要，保留所有数据、URL和关键结论。"),
        HumanMessage(content=f"研究主题：{query}\n\n原文：\n{entry.raw_content[:3000]}"),
    ])
    entry.compact_content = response_text(resp)
    entry.raw_content = ""
```

**Step 4: Update existing sync callers temporarily**

现有的 `trigger_compression` 调用点都在 async 函数中（`research_planner_node`, `hierarchical_planner_node` 等），所以只需加 `await`。但先不改 deep_research.py（那是 Task 3 的事）。先确保 context_manager.py 自身测试通过。

**Step 5: Update existing tests that call trigger_compression**

现有测试中同步调用 `ctx.trigger_compression(step=3)` 需要改为 async。将 `ResearchContextTests` 改为 `unittest.IsolatedAsyncioTestCase`，将涉及 `trigger_compression` 的测试方法加上 `async`：

- `test_trigger_compression_compacts_old` → `async def`，加 `await ctx.trigger_compression(step=3)`
- `test_trigger_compression_noop_below_threshold` → `async def`，加 `await`
- `test_trigger_compression_with_budget` → `async def`，加 `await`
- `test_trigger_compression_budget_zero_no_change` → `async def`，加 `await`

**Step 6: Run all context_manager tests**

```bash
python -m pytest tests/test_context_manager.py -v
```

Expected: ALL PASS

**Step 7: Commit**

```bash
git add context_manager.py tests/test_context_manager.py
git commit -m "feat: async priority-based compression with LLM summary for strong entries"
```

---

### Task 3: deep_research.py — 移除 findings 管道，统一到 ResearchContext

**Files:**
- Modify: `agents/deep_research.py` (多处)
- Test: `tests/test_deep_research.py`

这是最大的改动。分步骤执行：

**Step 1: 修改 ResearchState — 移除 findings**

`agents/deep_research.py` ~line 87-97，移除 `findings: str` 行：

```python
class ResearchState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    query: str
    step_count: int
    report_profile: str
    research_context: dict      # serialized ResearchContext — 唯一数据源
    task_id: str
    progress: dict
    research_plan: dict
    current_subtask: dict
```

**Step 2: 修改 research_planner (standalone, ~line 248-298)**

删除 findings 相关逻辑：

```python
async def research_planner(state: ResearchState) -> dict:
    llm = get_llm("deep_research").bind_tools(CORE_TOOLS)
    tracker = ProgressTracker.from_dict(state.get("progress", {})) if state.get("progress") else ProgressTracker(original_query=state["query"])
    tool_msgs = _latest_tool_messages(state["messages"])
    step = state["step_count"]

    prev_ai = None
    for msg in reversed(state["messages"]):
        if isinstance(msg, AIMessage):
            prev_ai = msg
            break

    completed_qs, findings_extracted, failed_qs = extract_progress_from_tool_results(tool_msgs, prev_ai)
    for q in completed_qs:
        tracker.record_search(q, success=True)
    for q in failed_qs:
        tracker.record_search(q, success=False)
    for f in findings_extracted:
        tracker.record_finding(f)
    attention_block = tracker.build_attention_block()

    ctx = ResearchContext.from_dict(state.get("research_context", {})) if state.get("research_context") else ResearchContext()
    ctx.merge_tool_results(tool_msgs, step)
    await ctx.trigger_compression(step)
    prompt_context = ctx.build_prompt_context()

    messages = _build_research_messages(
        state["query"],
        prompt_context,
        state.get("report_profile", DEFAULT_REPORT_PROFILE),
        attention_block=attention_block,
    )
    response = await llm.ainvoke(messages)
    return {
        "messages": [response],
        "step_count": step + 1,
        "research_context": ctx.to_dict(),
        "progress": tracker.to_dict(),
    }
```

**Step 3: 修改 research_finish (~line 305-318)**

改为从 `research_context` 构建输出：

```python
def research_finish(state: ResearchState) -> dict:
    # Try to find a textual AIMessage (LLM's final answer)
    for msg in reversed(state["messages"]):
        if not isinstance(msg, AIMessage):
            continue
        text = normalize_markdown_report(extract_text_content(msg))
        if text:
            return {"research_context": state.get("research_context", {}), "_final_text": text}

    # Fallback: build from ResearchContext
    ctx = ResearchContext.from_dict(state.get("research_context", {}))
    fallback = ctx.build_final_context()
    if fallback:
        log.warning("research_finish: no textual AIMessage, using ResearchContext fallback")
    else:
        log.warning("research_finish: no content found")
    return {"research_context": state.get("research_context", {}), "_final_text": normalize_markdown_report(fallback)}
```

注意：`_final_text` 是临时键，只在 `run()` 中使用。

**Step 4: 修改 build_research_graph 中的 research_planner_node (~line 431-542)**

同理删除 findings 逻辑，添加 `await ctx.trigger_compression(step, query=state["query"])`，返回值不含 findings。

所有 `_merge_findings` / `_summarize_tool_messages` 调用删除。保留 checkpoint 中存 `research_context`。

**Step 5: 修改 hierarchical 相关节点**

- `hierarchical_planner_node`：同理删除 findings
- `subtask_judge_node`：`st.findings_summary` 改为从 `ResearchContext.build_final_context()[:2000]` 获取

**Step 6: 修改 parallel_research_node**

`_synthesize_parallel_findings` 的结果存入 `ResearchContext` 而非 `findings`：

```python
async def parallel_research_node(state: ResearchState) -> dict:
    from agents.research_worker import coordinate_research
    plan = ResearchPlan.from_dict(state.get("research_plan", {}))
    task_id = state.get("task_id", "")
    memory = ResearchMemory(task_id) if task_id else None
    results = await coordinate_research(plan, all_tools, memory)

    # Build ResearchContext from parallel results
    ctx = ResearchContext.from_dict(state.get("research_context", {})) if state.get("research_context") else ResearchContext()
    for sid, worker_findings in results.items():
        if worker_findings:
            ctx.add_entry(FindingEntry(
                step=0, tool_name=f"worker_{sid}", query=sid,
                raw_content=worker_findings,
            ))

    # Try LLM synthesis for the summary
    try:
        synthesis = await _synthesize_parallel_findings(
            state["query"], results,
            state.get("report_profile", DEFAULT_REPORT_PROFILE),
        )
        ctx.update_summary(synthesis)
    except Exception:
        log.warning("parallel synthesis failed", exc_info=True)

    return {
        "research_context": ctx.to_dict(),
        "research_plan": plan.to_dict(),
    }
```

**Step 7: 修改 run() (~line 782-943)**

- 初始 state 不含 `findings`
- 结果提取改为：

```python
result = await graph.ainvoke(state)
# Extract final text from result
final_text = result.get("_final_text", "")
if not final_text:
    ctx = ResearchContext.from_dict(result.get("research_context", {}))
    final_text = ctx.build_final_context()
final_text = extract_result_text(final_text)
if final_text:
    final_text = await _rewrite_final_report(query, final_text, report_profile)
```

- checkpoint resume 中 `findings` 字段忽略（兼容旧 checkpoint）

**Step 8: 删除 _merge_findings 和 _summarize_tool_messages**

删除这两个函数（~line 1021-1042）。

**Step 9: 更新 _build_research_messages 签名**

`findings` 参数重命名为 `context`（语义更准确），但保持功能不变：

```python
def _build_research_messages(query: str, context: str, report_profile: str = DEFAULT_REPORT_PROFILE, attention_block: str = "") -> list[BaseMessage]:
    notes = context or "(暂无研究笔记)"
    ...
```

**Step 10: Run existing tests, expect many failures**

```bash
python -m pytest tests/test_deep_research.py -v 2>&1 | head -80
```

Expected: 多个涉及 `findings` 的测试失败。

**Step 11: Update test_deep_research.py**

需要更新的测试（移除 `findings` 相关断言，改为 `research_context` 断言）：

- `test_research_planner_uses_deep_research_role`: state 移除 findings
- `test_research_planner_builds_compact_prompt_from_findings`: 移除 `result["findings"]` 断言
- `test_research_finish_*`: 改为检查 `_final_text` 或 `research_context`
- `test_run_*`: FakeGraph 返回值改为含 `research_context`，不含 `findings`
- `test_run_resume_from_checkpoint`: checkpoint 数据移除 findings

每个测试的具体改动较多，在执行时逐个调整。

**Step 12: Run full test suite**

```bash
python -m pytest tests/ -v
```

Expected: ALL PASS

**Step 13: Commit**

```bash
git add agents/deep_research.py tests/test_deep_research.py
git commit -m "refactor: unify data source to ResearchContext, remove findings pipeline"
```

---

### Task 4: 全量回归 + 清理

**Files:**
- All test files
- `agents/deep_research.py` (cleanup)

**Step 1: Run full regression**

```bash
python -m pytest tests/ -v
```

Expected: ALL PASS

**Step 2: Verify no remaining references to deleted functions**

```bash
grep -rn "_merge_findings\|_summarize_tool_messages" agents/ tests/ context_manager.py
```

Expected: No matches.

**Step 3: Verify findings field is gone from ResearchState**

```bash
grep -n "findings" agents/deep_research.py
```

Expected: Only `_final_text`, `findings_summary` (in subtask), `key_findings_so_far` (in progress tracker) — no `findings: str` in ResearchState.

**Step 4: Commit**

```bash
git add -A
git commit -m "chore: cleanup after ResearchContext unification"
```
