# 机制 6：注意力操纵

> 优先级：P2 | 预计影响文件：agents/deep_research.py, 新增 attention_manager.py

## 1. 概述与目标

LLM 存在 "lost-in-the-middle" 问题——上下文中间的信息容易被忽视，模型倾向于关注开头（system prompt）和末尾（最近消息）。当研究进行到第 10+ 步时，原始目标和全局计划容易被遗忘，导致搜索偏离主题或重复已完成的工作。目标是引入主动的注意力操纵机制，确保全局目标和进度状态始终处于模型的高注意力区域。

## 2. 行业参考

### Manus 的 todo.md
- 不仅是进度跟踪，而是**刻意的注意力操纵**
- 通过不断重写 todo 列表，将目标"复述"到上下文末尾
- 将全局计划推入模型的**近期注意力范围**
- 对抗 lost-in-the-middle 问题

### Claude Code 的 Tasks API
- 任务持久化到文件系统，仅发送摘要给模型（节省 token）
- 跨会话共享：多个 Claude 会话可共享同一任务列表
- 上下文压实后任务状态依然存活

### AgentFold 的理论支撑
- 上下文分四层，"用户原始问题"作为**不变锚点**永远处于最顶层
- 多尺度状态摘要确保全局目标不被覆盖

### Manus 的错误保留策略
- 刻意在上下文中保留失败的动作和错误追踪信息
- 使模型"隐式更新内部信念"，避免重复相同错误

## 3. 当前代码诊断

| 位置 | 问题 |
|------|------|
| `_build_research_messages()` (L344-365) | 每步重复 query，但没有进度状态 |
| `BASE_RESEARCH_SYSTEM` (L193-207) | 系统指令在上下文开头，但研究进行中会被推到"注意力低谷" |
| 无 todo 跟踪 | agent 不知道自己已经完成了什么、还差什么 |
| 无错误保留 | 搜索失败后不保留失败信息，可能重复尝试同样的查询 |

### 核心症状
- agent 在第 10 步搜索的关键词和第 3 步一样（忘记了已经搜过）
- agent 偏离原始目标，开始搜索相关但不是核心的内容
- agent 在报告中遗漏了 query 的某个关键维度

## 4. 架构设计

### 4.1 研究进度追踪器

```python
# attention_manager.py（新增文件）

from dataclasses import dataclass, field


@dataclass
class ProgressTracker:
    """研究进度追踪——用于注意力操纵"""
    original_query: str
    clarified_goal: str = ""
    subtasks_status: list[dict] = field(default_factory=list)
    completed_searches: list[str] = field(default_factory=list)
    failed_searches: list[str] = field(default_factory=list)
    key_findings_so_far: list[str] = field(default_factory=list)
    remaining_gaps: list[str] = field(default_factory=list)

    def record_search(self, query: str, success: bool) -> None:
        """记录搜索历史"""
        if success:
            self.completed_searches.append(query)
        else:
            self.failed_searches.append(query)

    def record_finding(self, finding: str) -> None:
        """记录关键发现（1 句话摘要）"""
        self.key_findings_so_far.append(finding)
        # 限制数量，保持简洁
        if len(self.key_findings_so_far) > 10:
            self.key_findings_so_far = self.key_findings_so_far[-10:]

    def record_gap(self, gap: str) -> None:
        """记录信息缺口"""
        self.remaining_gaps.append(gap)

    def update_subtask(self, subtask_id: str, status: str) -> None:
        """更新子任务状态"""
        for st in self.subtasks_status:
            if st["id"] == subtask_id:
                st["status"] = status
                return
        self.subtasks_status.append({"id": subtask_id, "status": status})

    def build_attention_block(self) -> str:
        """生成注意力操纵块——插入到 prompt 末尾"""
        lines = []

        # 1. 原始目标（始终复述）
        lines.append(f"🎯 原始研究目标：{self.original_query}")
        if self.clarified_goal and self.clarified_goal != self.original_query:
            lines.append(f"   澄清后目标：{self.clarified_goal}")

        # 2. 子任务进度
        if self.subtasks_status:
            lines.append("\n📋 子任务进度：")
            for st in self.subtasks_status:
                icon = {"completed": "✅", "in_progress": "🔄", "pending": "⬜", "skipped": "⏭️"}
                lines.append(f"  {icon.get(st['status'], '❓')} {st['id']}: {st.get('topic', '')} [{st['status']}]")

        # 3. 已完成的搜索（防止重复）
        if self.completed_searches:
            lines.append(f"\n🔍 已完成搜索（{len(self.completed_searches)} 次）：")
            for q in self.completed_searches[-5:]:  # 最近 5 次
                lines.append(f"  - {q}")
            if len(self.completed_searches) > 5:
                lines.append(f"  ...（还有 {len(self.completed_searches) - 5} 次更早的搜索）")

        # 4. 失败的搜索（防止重复尝试）
        if self.failed_searches:
            lines.append(f"\n❌ 失败的搜索（不要重复）：")
            for q in self.failed_searches[-3:]:
                lines.append(f"  - {q}")

        # 5. 关键发现摘要
        if self.key_findings_so_far:
            lines.append(f"\n💡 已有关键发现：")
            for f in self.key_findings_so_far[-5:]:
                lines.append(f"  - {f}")

        # 6. 信息缺口（引导下一步）
        if self.remaining_gaps:
            lines.append(f"\n⚠️ 尚未填补的信息缺口：")
            for g in self.remaining_gaps[-3:]:
                lines.append(f"  - {g}")

        return "\n".join(lines)
```

### 4.2 注入位置策略

```
消息结构：
┌──────────────────────────────┐
│ SystemMessage                │  ← 研究指令 + 模板要求
│  (模型注意力高区)             │
├──────────────────────────────┤
│ HumanMessage                 │
│  1. 研究主题                  │
│  2. 当前研究笔记（压缩后）    │  ← 注意力低谷区（中间）
│  3. 下一步指导                │
│  4. ━━━━━━━━━━━━━━━━━━━━━   │
│  5. 🎯 注意力操纵块           │  ← 插入到末尾，注意力高区
│     - 原始目标复述            │
│     - 进度追踪               │
│     - 已完成搜索列表          │
│     - 信息缺口列表            │
└──────────────────────────────┘
```

### 4.3 集成到 `_build_research_messages()`

```python
def _build_research_messages(
    query: str,
    findings: str,
    report_profile: str = DEFAULT_REPORT_PROFILE,
    progress: ProgressTracker | None = None,
) -> list[BaseMessage]:
    notes = findings or "(暂无研究笔记)"
    profile = _get_report_profile(report_profile)
    section_requirements = "\n".join(f"   `{section}`" for section in profile.final_sections)

    prompt_parts = [
        f"研究主题：{query}\n",
        f"当前输出模板：{profile.name}\n",
        f"当前研究笔记（已压缩）：\n{notes}\n",
        "请基于当前笔记决定下一步：\n"
        "0. 如果研究目标存在关键歧义，先调用 ask_user_clarification；最多一次。\n"
        "1. 如果还缺少关键信息，继续调用最必要的 1-2 个工具。\n"
        "2. 如果信息已经足够，直接输出最终研究报告。\n"
        "3. 不要重复已经完成的搜索。\n"
        f"4. 最终报告至少要包含以下二级标题：\n{section_requirements}",
    ]

    # 注意力操纵块——插入到 prompt 末尾
    if progress:
        prompt_parts.append("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n")
        prompt_parts.append(progress.build_attention_block())

    return [
        SystemMessage(content=_build_research_system(report_profile)),
        HumanMessage(content="\n".join(prompt_parts)),
    ]
```

### 4.4 自动进度提取

```python
PROGRESS_EXTRACTION_SYSTEM = """从最近的 AI 助手回复中提取以下信息（用 JSON 回复）：
{
  "key_finding": "本轮最重要的发现（1 句话）或 null",
  "gap_identified": "发现的信息缺口（1 句话）或 null",
  "search_query_used": "本轮使用的搜索查询或 null"
}
只提取，不推理。"""


async def extract_progress_from_response(response_text: str) -> dict:
    """从 LLM 回复中自动提取进度信息"""
    llm = get_llm("summarizer")
    result = await llm.ainvoke([
        SystemMessage(content=PROGRESS_EXTRACTION_SYSTEM),
        HumanMessage(content=response_text),
    ])
    return _extract_json(extract_text_content(result))
```

### 4.5 轻量方案（无额外 LLM 调用）

如果不想为进度提取增加 LLM 调用开销，可以用规则提取：

```python
def extract_progress_rule_based(state: ResearchState, tool_messages: list) -> None:
    """基于规则的进度提取——零额外成本"""
    tracker = state.get("progress_tracker")
    if not tracker:
        return

    for msg in tool_messages:
        tool_name = getattr(msg, "name", "")
        content = str(getattr(msg, "content", ""))

        # 记录搜索查询
        if tool_name in ("web_search", "academic_search"):
            # 从工具调用参数中提取 query
            tracker.record_search(tool_name + ": " + content[:100], success=bool(content))

        # 提取 URL 作为来源记录
        urls = re.findall(r'https?://[^\s\]]+', content)
        if urls:
            tracker.record_finding(f"从 {tool_name} 获得 {len(urls)} 个来源")
```

## 5. 实现步骤

### Step 1：新增 `attention_manager.py`
- 实现 `ProgressTracker` 数据结构
- 实现 `build_attention_block()` 方法
- 实现基于规则的进度提取

### Step 2：扩展 `ResearchState`
```python
class ResearchState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    query: str
    step_count: int
    research_context: dict      # 机制 1
    research_plan: dict         # 机制 3
    current_subtask: dict       # 机制 3
    progress_tracker: dict      # 新增：注意力操纵
    report_profile: str
```

### Step 3：修改 `_build_research_messages()`
- 接收 `ProgressTracker` 参数
- 在 prompt 末尾插入注意力操纵块

### Step 4：在 `research_planner_node` 中更新进度
- 每步工具返回后调用 `extract_progress_rule_based()`
- 自动记录已完成搜索和失败搜索

### Step 5：在子任务切换时更新进度
- 子任务完成时调用 `tracker.update_subtask()`
- 在注意力块中显示全局进度

### Step 6：集成到外部记忆
- 每 5 步将 `ProgressTracker` 快照保存到检查点
- 从检查点恢复时恢复进度状态

## 6. 测试方案

```python
# tests/test_attention_manager.py

def test_attention_block_contains_original_query():
    """测试注意力块始终包含原始查询"""

def test_completed_searches_dedup():
    """测试已完成搜索记录不重复"""

def test_failed_searches_tracked():
    """测试失败搜索被记录"""

def test_key_findings_limited_to_10():
    """测试关键发现列表不超过 10 条"""

def test_subtask_progress_display():
    """测试子任务进度正确显示"""

def test_attention_block_format():
    """测试注意力块格式正确、可被 LLM 理解"""

def test_build_attention_block_empty():
    """测试空状态下注意力块不报错"""

def test_rule_based_extraction():
    """测试基于规则的进度提取"""
```

## 7. 验收标准

- [ ] 每步 prompt 末尾都包含原始目标的复述
- [ ] 已完成搜索列表可见，agent 不重复相同搜索
- [ ] 失败搜索被标记，agent 不重复尝试
- [ ] 子任务进度在注意力块中清晰展示
- [ ] 注意力块 token 消耗 < 500 token（保持轻量）
- [ ] 信息缺口引导 agent 的下一步搜索方向
- [ ] 从检查点恢复后进度状态完整
