# 用户记忆系统重设计

## 背景

当前 `MarkdownMemoryStore` 的记忆模式存在四个核心问题：

1. **Profile 假去重**：`_dedupe_items()` 只做字符串完全匹配去重。"用户在做 GNSS 论文" 和 "用户在写 GNSS NLOS 检测论文" 不会被合并，12 条上限被同义微变体占满
2. **Timeline 只 append 不归档**：每天一个 .md，一天 17 次交互就 21KB，`recall()` 搜索 12 天文件做 fuzzy match，随时间线性变慢
3. **项目没有生命周期**：Projects 只有文本列表，已完成和进行中的项目混在一起占用上限
4. **月度总结是流水账**：每条交互一行，无高层聚合
5. **离线回归无感知**：用户几个月不用后回来，看到的还是旧画像

## 设计

### 1. 核心模型：从文本列表变为结构化实体

#### 实体 1：UserFact（用户事实）

替代当前的 6 个扁平 section（Identity/Preferences/Projects/Working Style/Constraints/Open Loops）。

```python
@dataclass
class UserFact:
    id: str                          # uuid
    category: str                    # identity | preference | constraint | working_style
    content: str                     # "用户是博士生，研究方向是 GNSS NLOS 检测"
    confidence: float = 0.5          # 0.0-1.0，多次确认递增
    first_seen: str = ""             # ISO datetime
    last_confirmed: str = ""         # 每次被重新提取到时更新
    superseded_by: str | None = None # 被新 fact 取代时指向新 id
```

**状态流转**：
- 新 fact 创建时 confidence=0.5
- 被后续交互确认时 confidence += 0.15（上限 1.0）
- 用户长期不活跃时 confidence *= 0.7（软衰减）
- 被新 fact 语义取代时 superseded_by 指向新 fact

#### 实体 2：Project（用户项目）

从 Projects section 中独立出来，带生命周期。

```python
@dataclass
class Project:
    id: str                          # uuid
    name: str                        # "GNSS NLOS 检测论文"
    status: str = "active"           # active | stale | completed | paused | abandoned
    description: str = ""
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None
    related_tasks: list[str] = field(default_factory=list)  # task_id 列表
    key_findings: list[str] = field(default_factory=list)   # 最多 5 条
```

**自动衰减**：
```
active ──(14天不提及)──→ stale ──(再30天)──→ archived
  ↑                        ↑
  └──(用户重新提及)─────────┘
```

衰减在 `recall()` 时惰性检查，不需要后台任务。

### 2. Fact 合并策略：pending + 惰性合并

**写入路径**（每次对话后）：
1. LLM 从对话中提取新 facts（和当前一样的一次 LLM 调用）
2. 新 facts 追加到 `pending_facts` 文件（零额外开销）
3. **pending facts 立即参与 recall**——用户视角零延迟

**合并路径**（惰性触发）：
- 当 `pending_facts` 数量 > 8 时，recall() 前先触发 LLM 合并
- 合并操作：将 pending facts 和 confirmed facts 做**语义比较**
  - 语义相同 → 更新 `last_confirmed` + 提升 confidence
  - 同主题更新 → 标记旧 fact 的 `superseded_by`，新 fact 成为 confirmed
  - 全新信息 → 直接加入 confirmed

```python
def recall(self, user_id, task):
    confirmed = self._load_confirmed_facts(user_id)
    pending = self._load_pending_facts(user_id)

    if len(pending) > 8:
        confirmed = await self._merge_facts(confirmed, pending)
        self._save_confirmed_facts(user_id, confirmed)
        self._clear_pending(user_id)
        pending = []

    # 全部返回，用户视角无感
    all_facts = [f for f in confirmed if not f.superseded_by] + pending
    return all_facts
```

### 3. Timeline 分层归档

| 层级 | 保留期 | 粒度 | 格式 |
|------|--------|------|------|
| **Hot** | 最近 7 天 | 完整交互记录 | 当前 timeline 格式不变 |
| **Warm** | 7-90 天 | 每天 1 条摘要 | `YYYY-MM-DD: 做了X,Y,Z (N次交互)` |
| **Cold** | 90 天+ | 每月 1 段画像 | LLM 生成的月度总结 |

**归档触发**：`recall()` 时惰性检查 Hot 层文件日期。

**Hot → Warm**（规则提取，不用 LLM）：
```python
def _summarize_day(self, day_file: Path) -> str:
    """统计交互次数、提取 intent 列表、取用户消息前 30 字"""
    blocks = self._parse_timeline_blocks(day_file)
    intents = [b.intent for b in blocks]
    return f"{day_file.stem}: {', '.join(set(intents))} ({len(blocks)}次交互)"
```

**Warm → Cold**（月末 LLM 总结）：
```markdown
# 2026-03 月度总结

## 主要活动
- GNSS NLOS 检测论文：完成文献综述阶段，共 15 次深度研究
- 探索 PPT 自动生成功能

## 用户画像变化
- 从"探索 GNSS 多径"转向"写论文 introduction"

## 统计
- 总交互次数：28
- 深度研究：15 | 快速问答：10 | 文档生成：3
```

### 4. 离线回归检测

```python
def recall(self, user_id, task):
    last_seen = self._get_last_interaction_time(user_id)
    gap_days = (now - last_seen).days if last_seen else 0

    if gap_days > 14:
        # 1. 标记所有 active 项目为 stale
        for p in projects:
            if p.status == "active":
                p.status = "stale"

        # 2. 降低所有 fact 的 confidence
        for f in facts:
            f.confidence *= 0.7

        # 3. 注入 agent 提示（不是弹窗，而是系统上下文）
        recall_context.add_note(
            f"⚠ 用户已 {gap_days} 天未互动。"
            f"以下项目状态可能已过时：{stale_names}。"
            f"建议在合适时机确认用户当前工作重点。"
        )
```

**关键设计**：
- 不自动删除旧记忆，只降低 confidence 和标记 stale
- 通过 system prompt 提示 agent 自然地确认现状
- 用户重新活跃后，新交互自然恢复 confidence

### 5. 存储格式

从纯 markdown 改为 **JSON + markdown 混合**：

```
data/memory/<user_id>/
├── facts.json                    # confirmed UserFact 列表
├── pending_facts.json            # 待合并的新 facts
├── projects.json                 # Project 列表
├── meta.json                     # last_seen, interaction_count, merge_count
├── timeline/
│   ├── hot/                      # 最近 7 天，原格式 .md
│   │   ├── 2026-03-18.md
│   │   └── 2026-03-17.md
│   └── warm/                     # 7-90 天，每天一行
│       └── 2026-03.md
└── summaries/                    # 月度总结
    └── 2026-03.md
```

结构化数据（facts, projects, meta）用 JSON 存储，方便程序读写。
叙事性数据（timeline, summaries）用 Markdown 存储，方便人类阅读。

### 6. recall() 返回结构

```python
@dataclass
class MemoryRecall:
    facts: list[UserFact]            # confirmed + pending，按 confidence 降序
    projects: list[Project]          # active + stale
    recent_timeline: list[str]       # hot 层的最近交互片段
    monthly_summaries: list[str]     # 最近 2 个月的总结
    notes: list[str]                 # 系统提示（如离线回归警告）

    def to_prompt(self) -> str:
        """构建注入到 system prompt 的记忆上下文"""
        parts = []

        if self.notes:
            parts.append("## 注意\n" + "\n".join(self.notes))

        if self.facts:
            # 按 confidence 降序，只取 top 10
            top_facts = sorted(self.facts, key=lambda f: f.confidence, reverse=True)[:10]
            parts.append("## 用户画像\n" + "\n".join(f"- {f.content}" for f in top_facts))

        active = [p for p in self.projects if p.status == "active"]
        stale = [p for p in self.projects if p.status == "stale"]
        if active:
            parts.append("## 当前项目\n" + "\n".join(f"- {p.name}: {p.description}" for p in active))
        if stale:
            parts.append("## 可能已完成的项目\n" + "\n".join(f"- {p.name} (最后活跃: {p.updated_at[:10]})" for p in stale))

        if self.recent_timeline:
            parts.append("## 最近交互\n" + "\n".join(self.recent_timeline[:4]))

        return "\n\n".join(parts)
```

## 涉及文件

| 文件 | 改动 |
|------|------|
| `storage/user_store.py` | 重写核心类，新增 UserFact/Project 模型，分层 timeline，惰性合并 |
| `orchestrator/runner.py` | 适配新的 recall/remember 接口 |
| `tests/test_user_store.py` | 新建，覆盖 fact 合并、项目衰减、timeline 归档、离线回归 |

## 迁移策略

旧 profile.md 数据迁移：
1. 读取现有 6 个 section 的文本列表
2. 每条文本转为 UserFact（category 按原 section 映射）
3. Projects section 的条目转为 Project 实体
4. 旧 timeline 文件保留在 hot/ 目录
5. 旧 monthly summary 保留在 summaries/

## 关键设计原则

1. **写入快、合并懒**：每次对话只做 append，合并在 recall 时惰性触发
2. **软衰减不硬删**：旧记忆降低 confidence 而非删除，用户回归后自然恢复
3. **结构化实体**：UserFact 和 Project 取代扁平文本列表，支持语义合并和生命周期管理
4. **分层归档**：Hot/Warm/Cold 三级 timeline，控制存储增长
5. **agent 提示而非弹窗**：离线回归通过 system prompt 引导自然确认，不打断用户
