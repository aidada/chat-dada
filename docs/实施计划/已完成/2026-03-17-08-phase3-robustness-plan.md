# Phase 3 实施计划：健壮性 + 可配置化 + 合成增强 + 可观测性 + 测试补全

## Context

Phase 2 已完成运行时状态（`ProgressTracker`）、检查点恢复、结构化摘要、记忆工具（`save_research_note` / `recall_research_notes`）、分层任务拆解（`ResearchPlan`）和多 Agent 并行（`coordinate_research`）。

当前遗留问题：

- `remaining_gaps` 是死代码，从未被 agent 调用
- 并行模式合成步只做拼接，无去重 / 矛盾检测
- 所有外部操作只 `try/except + log.warning`，无重试、无输入校验
- 序列化无版本字段，schema 变更会破坏 checkpoint 恢复
- 硬编码限制（步数 15、CHECKPOINT_INTERVAL 5、MAX_PARALLEL_WORKERS 3 等）不可配置
- `evidence_strength` 参数未落地存储
- 无 token 预算追踪
- 无结构化可观测性日志
- 多条关键路径缺乏测试覆盖

## 修改文件总览

| 文件                             | 操作     | 涉及步骤                       |
| -------------------------------- | -------- | ------------------------------ |
| `progress_tracker.py`            | **修改** | P0-1, P0-4, P1-2               |
| `tests/test_progress_tracker.py` | **修改** | P0-1, P1-2                     |
| `agents/deep_research.py`        | **修改** | P0-1/2/3/4/5, P1-1/2, P2-1/2/3 |
| `context_manager.py`             | **修改** | P0-4, P1-3                     |
| `research_planner.py`            | **修改** | P0-3/4, P1-1                   |
| `research_memory.py`             | **修改** | P0-4, P2-4                     |
| `agents/research_worker.py`      | **修改** | P0-2, P1-2, P2-1               |
| `tools/research_notes.py`        | **修改** | P1-4                           |
| `tests/test_deep_research.py`    | **修改** | 每步新增测试                   |
| `tests/test_context_manager.py`  | **修改** | P1-3                           |
| `tests/test_research_planner.py` | **修改** | P0-3, P2-5                     |
| `tests/test_research_worker.py`  | **修改** | P0-2, P2-5                     |
| `tests/test_research_notes.py`   | **修改** | P1-4                           |
| `tests/test_research_memory.py`  | **修改** | P2-4                           |

---

## P0 — 高优先级

---

### P0-1: `remaining_gaps` 自动填充

**现状**：`progress_tracker.py` 的 `record_gap()` 方法存在（L51-54），`build_attention_block()` 会展示 `remaining_gaps`（L82-83），但 `agents/deep_research.py` 中从未调用 `record_gap()`。

**方案**：在 `_generate_structured_summary()` 返回摘要后，用规则提取缺口；新增 `resolve_gap()` 在新发现覆盖已记录缺口时移除。

#### Step 1: 修改 `progress_tracker.py` — 新增 `extract_gaps_from_summary()` 和 `resolve_gap()`

```python
def extract_gaps_from_summary(summary: str) -> list[str]:
    """从结构化摘要中规则提取缺口描述。

    匹配模式：
    - "缺口" / "缺少" / "未覆盖" / "尚未" / "need" / "missing" 后的句子
    - "下一步" 段落中的建议
    每条缺口≤80字符，最多返回 5 条。
    """
```

实现逻辑：

1. 按行扫描 summary
2. 如果行包含关键词（`缺口|缺少|未覆盖|尚未|不足|need|missing|gap`），提取该行（去掉列表符号）
3. 如果当前处于"下一步建议"段落（`下一步|next step`），提取该段落的每行
4. 每条截断到 80 字符
5. 返回最多 5 条

新增 `ProgressTracker.resolve_gap()`:

```python
def resolve_gap(self, keyword: str) -> None:
    """移除包含 keyword 的缺口。"""
    self.remaining_gaps = [g for g in self.remaining_gaps if keyword.lower() not in g.lower()]
```

#### Step 2: 修改 `agents/deep_research.py` — 在摘要生成后填充 gaps

在 `research_planner_node()` 的 `# --- periodic structured summary ---` 块中（L402-410），summary 生成成功后追加：

```python
from progress_tracker import extract_gaps_from_summary

gaps = extract_gaps_from_summary(summary)
for gap in gaps:
    tracker.record_gap(gap)
```

#### Step 3: 新增测试

**`tests/test_progress_tracker.py`**：

- `test_extract_gaps_from_summary` — 验证从摘要提取缺口
- `test_extract_gaps_from_summary_empty` — 无关键词时返回空
- `test_resolve_gap` — 验证按关键词移除缺口

**`tests/test_deep_research.py`**：

- `test_gaps_populated_after_summary` — mock 摘要 LLM 返回含"缺少"的文本，验证 tracker.remaining_gaps 非空

#### 验证

```bash
python -m pytest tests/test_progress_tracker.py tests/test_deep_research.py -v
```

---

### P0-2: 并行模式合成步增强

**现状**：`build_parallel_research_graph()` 的 `parallel_research_node`（L663-684）把各 worker 结果用 `### {subtask_id}\n{findings}` 拼接后直接传给 `research_finish`，无去重、无矛盾检测、无结构化合并。

**方案**：新增 `_synthesize_parallel_findings()` 函数，用 orchestrator LLM 做合并。

#### Step 1: 新增 `_synthesize_parallel_findings()` — 在 `agents/deep_research.py`

```python
async def _synthesize_parallel_findings(
    query: str,
    subtask_results: dict[str, str],
    report_profile: str,
) -> str:
    """用 orchestrator LLM 合并多个子任务的发现。

    - 去重重叠内容
    - 标注矛盾发现
    - 按报告模板组织结构
    - 输出≤3000字
    """
    llm = get_llm("orchestrator")

    entries = []
    for sid, findings in subtask_results.items():
        entries.append(f"## 子任务 {sid}\n{findings[:1500]}")
    all_entries = "\n\n---\n\n".join(entries)

    profile = _get_report_profile(report_profile)
    sections = "\n".join(f"- {s}" for s in profile.final_sections)

    system_prompt = (
        "你是一个研究合成器。请把多个子任务的研究发现合并成一份结构化报告。\n\n"
        "要求：\n"
        "1. 去除重复内容，保留信息量最大的表述\n"
        "2. 如果子任务之间有矛盾发现，明确标注"[矛盾]"并列出各方证据\n"
        "3. 按以下报告结构组织：\n"
        f"{sections}\n"
        "4. 合并后总长度≤3000字\n"
        "5. 每个结论保留来源标注"
    )

    resp = await llm.ainvoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=f"研究主题：{query}\n\n子任务发现：\n{all_entries}"),
    ])
    return response_text(resp)
```

#### Step 2: 修改 `parallel_research_node`

替换当前的简单拼接逻辑（L673-679）：

```python
try:
    merged_findings = await _synthesize_parallel_findings(
        state["query"], results,
        state.get("report_profile", DEFAULT_REPORT_PROFILE),
    )
except Exception:
    log.warning("parallel synthesis failed, falling back to concatenation", exc_info=True)
    all_findings = [f"### {sid}\n{f}" for sid, f in results.items() if f]
    merged_findings = "\n\n".join(all_findings) if all_findings else state.get("findings", "")
```

#### Step 3: 新增测试

**`tests/test_deep_research.py`**：

- `test_synthesize_parallel_findings_basic` — mock LLM 返回合并文本
- `test_synthesize_parallel_findings_fallback` — LLM 抛异常时回退到拼接

**`tests/test_research_worker.py`**：

- `test_coordinate_research_with_deps` — 3 个子任务有 A→B→C 依赖链，验证按波次执行

#### 验证

```bash
python -m pytest tests/test_deep_research.py tests/test_research_worker.py -v
```

---

### P0-3: 输入校验

**现状**：`run()` 函数（L700-726）不校验 query 长度、report_profile 合法性、memory_context 大小。`generate_research_plan()`（research_planner.py L156）不校验空 query。

**方案**：在入口处加校验，不合法时返回 error 或截断。

#### Step 1: 修改 `agents/deep_research.py` — `run()` 函数

在 `report_profile = _resolve_report_profile(...)` 之后（L728），graph 选择之前，插入校验块：

```python
# --- input validation ---
if not query or not query.strip():
    return {"status": "error", "result": "研究查询不能为空。"}
query = query.strip()
if len(query) > 10000:
    query = query[:10000]
    log.warning("query truncated to 10000 chars")
if memory_context and len(memory_context) > 50000:
    memory_context = memory_context[:50000]
    log.warning("memory_context truncated to 50000 chars")
```

#### Step 2: 修改 `research_planner.py` — `generate_research_plan()`

在函数开头（L160 之后）：

```python
if not query or not query.strip():
    raise ValueError("Research query cannot be empty")
query = query.strip()
```

#### Step 3: 新增测试

**`tests/test_deep_research.py`**：

- `test_run_empty_query_returns_error` — 空字符串返回 `status: "error"`
- `test_run_long_query_truncated` — 超长 query 被截断，不报错
- `test_run_empty_dict_query_returns_error` — `{"query": ""}` 返回错误

**`tests/test_research_planner.py`**：

- `test_generate_plan_empty_query_raises` — 空 query 抛 ValueError

#### 验证

```bash
python -m pytest tests/test_deep_research.py tests/test_research_planner.py -v
```

---

### P0-4: 序列化版本字段

**现状**：`ProgressTracker.to_dict()`、`ResearchPlan.to_dict()`、`ResearchContext.to_dict()` 没有 `_version` 字段。未来 schema 变更会导致 `from_dict()` 静默产生错误数据。

**方案**：在所有 `to_dict()` 中加 `"_version": N`，在 `from_dict()` 中读取并在版本不匹配时 log warning。

#### Step 1: 修改 `progress_tracker.py`

```python
TRACKER_VERSION = 1

def to_dict(self) -> dict[str, Any]:
    return {
        "_version": TRACKER_VERSION,
        "original_query": self.original_query,
        ...
    }

@classmethod
def from_dict(cls, data: dict[str, Any]) -> ProgressTracker:
    version = data.get("_version", 0)
    if version != TRACKER_VERSION:
        log.warning("ProgressTracker version mismatch: expected %d, got %d", TRACKER_VERSION, version)
    return cls(...)
```

#### Step 2: 修改 `research_planner.py`

同理为 `ResearchSubtask` 和 `ResearchPlan` 添加 `_version` 字段。

```python
PLAN_VERSION = 1
SUBTASK_VERSION = 1
```

#### Step 3: 修改 `context_manager.py`

为 `FindingEntry` 和 `ResearchContext` 添加 `_version` 字段。

```python
FINDING_ENTRY_VERSION = 1
CONTEXT_VERSION = 1
```

#### Step 4: 修改 `research_memory.py` — checkpoint 保存/加载

```python
CHECKPOINT_VERSION = 1

def save_checkpoint(self, step, state_dict):
    state_dict.setdefault("_checkpoint_version", CHECKPOINT_VERSION)
    ...

def load_checkpoint(self, step=None):
    ...
    data = json.loads(...)
    version = data.get("_checkpoint_version", 0)
    if version != CHECKPOINT_VERSION:
        log.warning("Checkpoint version mismatch: expected %d, got %d", CHECKPOINT_VERSION, version)
    return data
```

#### Step 5: 新增测试

- `test_tracker_to_dict_includes_version`
- `test_tracker_from_dict_old_version_warns` — version=0 时 log warning 但不崩溃
- `test_plan_to_dict_includes_version`
- `test_context_to_dict_includes_version`
- `test_checkpoint_version_roundtrip`

#### 验证

```bash
python -m pytest tests/ -v
```

---

### P0-5: 重试机制

**现状**：所有外部操作只 `try/except + log.warning`，无重试。

**方案**：新增轻量级 `_retry_async()` 辅助函数，对可恢复错误自动重试。

#### Step 1: 在 `agents/deep_research.py` 中新增 `_retry_async()`

```python
import asyncio

async def _retry_async(coro_fn, *args, max_retries: int = 2, delay: float = 1.0, **kwargs):
    """重试异步函数调用，仅对可恢复错误重试。

    可恢复错误：OSError, TimeoutError, ConnectionError
    不可恢复错误：ValueError, TypeError, KeyError → 直接抛出
    """
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_fn(*args, **kwargs)
        except (OSError, TimeoutError, ConnectionError) as e:
            last_exc = e
            if attempt < max_retries:
                await asyncio.sleep(delay * (attempt + 1))
                log.info("Retrying %s (attempt %d/%d)", coro_fn.__name__, attempt + 2, max_retries + 1)
            continue
        except Exception:
            raise
    raise last_exc
```

#### Step 2: 在关键调用点使用

1. **`_generate_structured_summary()`** — LLM 调用加重试
2. **`generate_research_plan()`** — LLM 调用加重试
3. **`_synthesize_parallel_findings()`** — LLM 调用加重试

不对 `research_planner_node` 的主 LLM 调用加重试（已有 15 步上限保底）。

#### Step 3: 新增测试

- `test_retry_async_succeeds_first_try`
- `test_retry_async_succeeds_on_second_try` — 第一次 OSError，第二次成功
- `test_retry_async_raises_after_max_retries` — 全部 OSError 后抛出
- `test_retry_async_no_retry_on_value_error` — ValueError 直接抛出

#### 验证

```bash
python -m pytest tests/test_deep_research.py -v
```

---

## P1 — 中高优先级

---

### P1-1: 硬编码限制可配置化

**现状**：

- `agents/deep_research.py`: `step_count >= 15`（L306）, `CHECKPOINT_INTERVAL = 5`（L98）, `SUMMARY_INTERVAL = 6`（L99）
- `agents/research_worker.py`: `MAX_PARALLEL_WORKERS = 3`（L16）
- `context_manager.py`: `RAW_CONTENT_THRESHOLD = 8000`（L14）, `COMPACT_SNIPPET_LENGTH = 200`（L15）

**方案**：新增 `ResearchConfig` dataclass，通过 `input_data["config"]` 传入，不传用默认值。

#### Step 1: 新增 `ResearchConfig` — 在 `agents/deep_research.py`

```python
@dataclass
class ResearchConfig:
    max_steps: int = 15
    checkpoint_interval: int = 5
    summary_interval: int = 6
    max_parallel_workers: int = 3
    raw_content_threshold: int = 8000
    compact_snippet_length: int = 200

    @classmethod
    def from_dict(cls, data: dict) -> ResearchConfig:
        return cls(**{k: data[k] for k in data if k in cls.__dataclass_fields__})
```

#### Step 2: 修改 `run()` — 解析 config

```python
config = ResearchConfig()
if isinstance(input_data, dict) and input_data.get("config"):
    config = ResearchConfig.from_dict(input_data["config"])
```

将 `config` 传入各 graph builder。

#### Step 3: 替换硬编码引用

- `research_should_continue`: `>= 15` → `>= config.max_steps`（通过闭包捕获）
- `research_planner_node`: `CHECKPOINT_INTERVAL` → `config.checkpoint_interval`
- `research_planner_node`: `SUMMARY_INTERVAL` → `config.summary_interval`
- `coordinate_research`: `MAX_PARALLEL_WORKERS` → 从 config 传入

#### Step 4: 新增测试

- `test_config_from_dict_defaults` — 空 dict 用默认值
- `test_config_from_dict_custom` — 自定义 max_steps=5
- `test_run_with_custom_config` — 传入 `config: {"max_steps": 3}`，验证 step 上限

#### 验证

```bash
python -m pytest tests/test_deep_research.py -v
```

---

### P1-2: Token 预算追踪

**现状**：`_build_research_messages()` 不估算 prompt 大小，无累计 token 统计。

**方案**：在 `ProgressTracker` 中追踪 token 使用量，prompt 构建时估算大小并在接近上限时截断。

#### Step 1: 扩展 `ProgressTracker`

```python
total_input_tokens: int = 0
total_output_tokens: int = 0

def record_token_usage(self, input_tokens: int, output_tokens: int) -> None:
    self.total_input_tokens += input_tokens
    self.total_output_tokens += output_tokens
```

#### Step 2: 修改 `research_planner_node` — 记录 token 用量

```python
response = await llm.ainvoke(messages)
usage = getattr(response, "usage_metadata", None) or {}
if isinstance(usage, dict):
    tracker.record_token_usage(usage.get("input_tokens", 0), usage.get("output_tokens", 0))
```

#### Step 3: 修改 `_build_research_messages()` — prompt 超限压缩(在不改变原有语义情况下压缩 token 消耗)

#### Step 4: 在 `build_attention_block()` 展示 token 用量

```python
if self.total_input_tokens > 0:
    lines.append(f"累计 token: 输入 {self.total_input_tokens} / 输出 {self.total_output_tokens}")
```

#### Step 5: 新增测试

- `test_record_token_usage` — 累加 token
- `test_attention_block_shows_tokens` — token > 0 时在 block 中出现

#### 验证

```bash
python -m pytest tests/test_progress_tracker.py tests/test_deep_research.py -v
```

---

### P1-3: 自适应压缩

**现状**：`context_manager.py` 的 `trigger_compression()` 使用固定规则（step age ≥ 2, snippet 200 chars），不考虑 token 预算。

**方案**：支持 `token_budget` 参数，超预算时更激进压缩。

#### Step 1: 修改 `trigger_compression()` — 支持 budget 参数

```python
def trigger_compression(self, step: int, token_budget: int = 0) -> None:
    # 阶段 1：原规则（step age ≥ 2）
    total_raw = sum(len(e.raw_content) for e in self.entries)
    if total_raw > RAW_CONTENT_THRESHOLD:
        for entry in self.entries:
            if (step - entry.step) >= 2 and not entry.compact_content:
                self._compact_entry(entry)

    # 阶段 2：如果有 token_budget 且仍超预算
    if token_budget > 0:
        total = sum(len(e.raw_content) + len(e.compact_content) for e in self.entries)
        if total > token_budget:
            for entry in self.entries:
                if (step - entry.step) >= 1 and entry.raw_content:
                    self._compact_entry(entry, snippet_len=100)
```

提取 `_compact_entry(entry, snippet_len=COMPACT_SNIPPET_LENGTH)` 内部方法。

#### Step 2: 新增测试

**`tests/test_context_manager.py`**：

- `test_trigger_compression_with_budget` — 设置小 token_budget，验证更激进压缩
- `test_trigger_compression_budget_zero_no_change` — budget=0 行为不变
- `test_compact_entry_custom_snippet_len` — 验证 100 字符 snippet

#### 验证

```bash
python -m pytest tests/test_context_manager.py -v
```

---

### P1-4: `evidence_strength` 落地

**现状**：`save_research_note()` 接受 `evidence_strength` 参数（L28）但只在返回消息中展示，未写入文件。`recall_research_notes()` 不按证据强度排序。

**方案**：将 `evidence_strength` 写入文件 header，检索时按强度排序。

#### Step 1: 修改 `tools/research_notes.py` — `save_research_note`

```python
memory.save_finding(step, "note", topic,
    f"[evidence: {evidence_strength}]\n{content}", [])
```

#### Step 2: 修改 `recall_research_notes` — 按强度排序

```python
EVIDENCE_RANK = {"strong": 0, "moderate": 1, "weak": 2}

def _extract_evidence_strength(text: str) -> str:
    match = re.search(r"\[evidence:\s*(\w+)\]", text)
    return match.group(1) if match else "moderate"

# 在收集 results 后排序
results.sort(key=lambda r: EVIDENCE_RANK.get(_extract_evidence_strength(r), 2))
```

#### Step 3: 新增测试

**`tests/test_research_notes.py`**：

- `test_save_note_includes_evidence_strength` — 验证文件中包含 `[evidence: strong]`
- `test_recall_notes_sorted_by_evidence` — strong 排在 weak 前面

#### 验证

```bash
python -m pytest tests/test_research_notes.py -v
```

---

## P2 — 中优先级

---

### P2-1: 可观测性日志

**现状**：无工具调用耗时、上下文层使用量、计划生成 / 子任务路由等结构化日志。

**方案**：在关键节点添加结构化 `log.info`。

#### Step 1: 修改 `agents/deep_research.py`

在 `research_planner_node` 中（LLM 调用前后）：

```python
import time

start = time.monotonic()
response = await llm.ainvoke(messages)
elapsed = time.monotonic() - start

log.info(
    "research_step step=%d elapsed=%.1fs tool_calls=%d "
    "context_raw=%d context_compact=%d context_summary=%d "
    "completed_searches=%d failed_searches=%d findings=%d gaps=%d",
    step, elapsed,
    len(getattr(response, "tool_calls", []) or []),
    sum(len(e.raw_content) for e in ctx.entries),
    sum(len(e.compact_content) for e in ctx.entries),
    len(ctx.summary),
    len(tracker.completed_searches),
    len(tracker.failed_searches),
    len(tracker.key_findings_so_far),
    len(tracker.remaining_gaps),
)
```

在 `plan_generator_node` / `subtask_judge_node` / `coordinate_research` 中添加类似日志。

#### Step 2: 修改 `agents/research_worker.py` — Worker 级日志

```python
log.info("worker_completed subtask=%s elapsed=%.1fs findings_len=%d", ...)
```

#### Step 3: 无需功能测试，全量回归确认无 import 错误

```bash
python -m pytest tests/ -v
```

---

### P2-2: 分层模式 `is_plan_complete()` 集成

**现状**：`is_plan_complete()` 已导入（L23）但未在 `build_hierarchical_research_graph()` 中使用。`subtask_judge_node` 只截取 `findings[-500:]`。

#### Step 1: 修改 `subtask_judge_node`

- `findings[-500:]` → `findings[-2000:]`（保留更多内容）
- 子任务完成后用 `is_plan_complete()` 打日志

#### Step 2: 修改 `subtask_should_continue`

```python
def subtask_should_continue(state: ResearchState) -> str:
    if state.get("current_subtask"):
        return "planner"
    plan_dict = state.get("research_plan", {})
    if plan_dict:
        plan = ResearchPlan.from_dict(plan_dict)
        if is_plan_complete(plan):
            return "synthesize"
    return "synthesize"
```

#### Step 3: 新增测试

- `test_subtask_judge_findings_summary_limit` — 验证 ≤ 2000 字符
- `test_subtask_should_continue_checks_plan_complete`

#### 验证

```bash
python -m pytest tests/test_deep_research.py -v
```

---

### P2-3: Checkpoint 恢复校验

**现状**：`run()` 恢复 checkpoint 时（L736-754）不验证研究目标变更。

#### Step 1: 修改恢复逻辑

```python
if checkpoint:
    meta = memory.load_meta()
    old_query = (meta or {}).get("query", "")

    # 用户提供新 query 且与旧 query 不同 → 以新 query 为准
    if query and query != str(input_data) and old_query and query != old_query:
        log.warning("Resume query mismatch: checkpoint='%s', new='%s'. Using new query.",
            old_query[:50], query[:50])
    else:
        query = old_query or query
    ...
```

#### Step 2: 新增测试

- `test_run_resume_query_mismatch_uses_new_query`
- `test_run_resume_same_query_uses_checkpoint`

#### 验证

```bash
python -m pytest tests/test_deep_research.py -v
```

---

### P2-4: 研究记忆清理

**现状**：`research_memory.py` 无清理机制。

#### Step 1: 新增方法

```python
@classmethod
def list_tasks(cls, root=DEFAULT_RESEARCH_ROOT) -> list[dict]:
    """列出所有研究任务及其元数据。"""

def cleanup(self, keep_final_report: bool = True) -> None:
    """删除 findings 和 checkpoints，可选保留 final_report。"""

@classmethod
def cleanup_old_tasks(cls, max_age_days: int = 30, root=DEFAULT_RESEARCH_ROOT) -> int:
    """清理超过 max_age_days 天的研究任务。返回清理数量。"""
```

新增 `from datetime import timedelta` import。

#### Step 2: 新增测试

**`tests/test_research_memory.py`**：

- `test_list_tasks` — 创建 2 个任务，验证列表
- `test_cleanup_removes_findings` — findings 目录被删除
- `test_cleanup_keeps_final_report` — final_report 保留
- `test_cleanup_old_tasks` — 修改 created_at 为 31 天前，验证清理

#### 验证

```bash
python -m pytest tests/test_research_memory.py -v
```

---

### P2-5: 测试覆盖补全

**现状**：多条关键路径缺乏测试。

#### 新增测试列表

**`tests/test_deep_research.py`**：

1. `test_research_should_continue_non_ai_last_message` — 最后消息非 AIMessage 时返回 "finish"
2. `test_research_finish_no_messages` — 空消息列表不崩溃
3. `test_run_exception_in_memory_init_continues` — memory init 失败时 task_id 为空但不中断
4. `test_planner_node_checkpoint_saved_at_interval` — mock ResearchMemory，验证 step=5 时调用 save_checkpoint

**`tests/test_research_planner.py`**：5. `test_generate_plan_fallback_on_invalid_json` — LLM 返回非 JSON，验证回退到单子任务计划 6. `test_get_next_subtask_circular_deps` — 循环依赖（A→B→A），返回 None 7. `test_is_plan_complete_empty_subtasks` — 空列表返回 True

**`tests/test_research_worker.py`**：8. `test_worker_finish_empty_messages` — 空消息列表不崩溃 9. `test_coordinate_research_deep_deps` — A→B→C 三层依赖，按波次执行

**`tests/test_context_manager.py`**：10. `test_compression_already_compacted_skipped` — 已有 compact_content 不重复压缩 11. `test_build_prompt_context_large_summary_truncated` — 超长 summary 截断

#### 验证

```bash
python -m pytest tests/ -v
```

---

## 全局验证方案

```bash
# 1. P0 全部
python -m pytest tests/test_progress_tracker.py tests/test_deep_research.py tests/test_research_planner.py tests/test_research_worker.py -v

# 2. P1 全部
python -m pytest tests/test_context_manager.py tests/test_research_notes.py tests/test_deep_research.py tests/test_progress_tracker.py -v

# 3. P2 全部
python -m pytest tests/test_research_memory.py tests/test_deep_research.py tests/test_research_planner.py tests/test_research_worker.py tests/test_context_manager.py -v

# 4. 全量回归
python -m pytest tests/ -v
```

## 关键设计原则

1. **向后兼容**：所有 `_version` 字段用 `.get()` 默认 0；旧 checkpoint 仍可恢复，仅 log warning
2. **失败不阻塞**：重试机制只对可恢复错误生效；合成步有拼接 fallback
3. **可配置化**：`ResearchConfig` 通过 `input_data["config"]` 传入，不传则用默认值
4. **最小改动**：现有函数签名不变，通过新增参数（带默认值）扩展
5. **日志不测试**：可观测性日志只做 `log.info`，不需要功能测试

## 实施顺序建议

```
Batch 1: P0-1 + P0-3 + P0-4         （gaps 填充 + 输入校验 + 版本字段）
Batch 2: P0-2 + P0-5                 （合成增强 + 重试机制）
Batch 3: P1-1 + P1-2                 （可配置化 + token 追踪）
Batch 4: P1-3 + P1-4                 （自适应压缩 + evidence_strength）
Batch 5: P2-1 + P2-2 + P2-3 + P2-4  （可观测性 + 分层完善 + checkpoint 校验 + 清理）
Batch 6: P2-5                        （测试补全）
```
