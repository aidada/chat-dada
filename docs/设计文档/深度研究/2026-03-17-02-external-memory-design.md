# 机制 2：外部记忆系统

> 优先级：P0 | 预计影响文件：新增 research_memory.py, agents/deep_research.py, content_utils.py

## 1. 概述与目标

当前所有研究发现只存在于 LangGraph 状态的内存字段中，一旦上下文被压缩或进程崩溃，数据不可恢复。目标是引入文件系统 + 结构化存储作为外部记忆，让 agent 可以按需读写研究笔记，上下文只保留引用指针。

## 2. 行业参考

### Manus
> "文件系统是终极上下文——大小无限、天然持久、可被 agent 直接操作。"
- 研究报告时为每个章节创建独立文件
- 中间结果保存到文件而非保持在上下文中
- 压缩策略始终设计为**可恢复的**：只要文件路径保留，内容可重新获取

### Devin
- 向量化快照存储代码库状态
- 完整回放时间线：每个命令、文件 diff 的操作记录
- 检查点回滚：回到时间线上的先前点

### AgeMem（学术前沿）
- 把记忆操作暴露为工具（存储/检索/更新/摘要/丢弃）
- 让 LLM 自主决定何时、存什么、如何管理记忆

## 3. 当前代码诊断

| 位置 | 问题 |
|------|------|
| `deep_research.py` ResearchState | `findings: str` 是纯内存字符串，无持久化 |
| `task_runtime.py` | 有 SQLite 持久化能力，但 deep_research 没有接入 |
| `memory/store.py` | 有用户记忆系统（profile/timeline），但没有任务级研究记忆 |
| 无检查点机制 | 第 14 步出错，前 13 步工作全丢 |

### 与机制 1 的关系
机制 1（上下文缩减）的压缩层需要一个地方存放被压缩的原始内容。外部记忆就是这个存放地——上下文中只保留压缩版本 + 文件路径引用，需要时可以从文件重新读取完整内容。

## 4. 架构设计

### 4.1 研究记忆存储结构

```
data/research/{task_id}/
├── meta.json              # 研究元数据（query, profile, 创建时间, 状态）
├── findings/
│   ├── step_01_web_search.md      # 每步工具结果的完整原始内容
│   ├── step_02_academic_search.md
│   ├── step_03_web_search.md
│   └── ...
├── summaries/
│   ├── summary_step_06.md         # 第 6 步时的全局摘要
│   ├── summary_step_12.md         # 第 12 步时的全局摘要
│   └── latest.md                  # 最新摘要（始终更新）
├── checkpoints/
│   ├── checkpoint_step_05.json    # 状态快照
│   └── checkpoint_step_10.json
└── final_report.md                # 最终报告
```

### 4.2 核心接口

```python
# research_memory.py（新增文件）

import json
from pathlib import Path
from dataclasses import dataclass, asdict


RESEARCH_BASE_DIR = Path("data/research")


@dataclass
class ResearchMeta:
    task_id: str
    query: str
    report_profile: str
    created_at: str
    status: str = "running"        # running / completed / failed
    total_steps: int = 0


class ResearchMemory:
    """任务级研究记忆——文件系统作为外部存储"""

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.base_dir = RESEARCH_BASE_DIR / task_id
        self.findings_dir = self.base_dir / "findings"
        self.summaries_dir = self.base_dir / "summaries"
        self.checkpoints_dir = self.base_dir / "checkpoints"

    def init(self, query: str, report_profile: str) -> None:
        """初始化研究目录结构"""
        for d in [self.findings_dir, self.summaries_dir, self.checkpoints_dir]:
            d.mkdir(parents=True, exist_ok=True)
        meta = ResearchMeta(
            task_id=self.task_id,
            query=query,
            report_profile=report_profile,
            created_at=_now_iso(),
        )
        self._write_json(self.base_dir / "meta.json", asdict(meta))

    def save_finding(self, step: int, tool_name: str, query: str,
                     content: str, urls: list[str] | None = None) -> str:
        """保存一条研究发现到文件，返回文件路径"""
        filename = f"step_{step:02d}_{tool_name}.md"
        filepath = self.findings_dir / filename
        header = f"# Step {step}: {tool_name}\n**Query**: {query}\n"
        if urls:
            header += "**Sources**: " + ", ".join(urls) + "\n"
        header += "\n---\n\n"
        filepath.write_text(header + content, encoding="utf-8")
        self._update_meta(total_steps=step)
        return str(filepath)

    def load_finding(self, step: int, tool_name: str) -> str | None:
        """从文件加载特定步骤的研究发现"""
        filename = f"step_{step:02d}_{tool_name}.md"
        filepath = self.findings_dir / filename
        if filepath.exists():
            return filepath.read_text(encoding="utf-8")
        return None

    def list_findings(self) -> list[str]:
        """列出所有 finding 文件路径"""
        if not self.findings_dir.exists():
            return []
        return sorted(str(p) for p in self.findings_dir.glob("step_*.md"))

    def save_summary(self, step: int, summary: str) -> str:
        """保存阶段性摘要"""
        filepath = self.summaries_dir / f"summary_step_{step:02d}.md"
        filepath.write_text(summary, encoding="utf-8")
        # 同时更新 latest
        latest = self.summaries_dir / "latest.md"
        latest.write_text(summary, encoding="utf-8")
        return str(filepath)

    def load_latest_summary(self) -> str:
        """加载最新摘要"""
        latest = self.summaries_dir / "latest.md"
        if latest.exists():
            return latest.read_text(encoding="utf-8")
        return ""

    def save_checkpoint(self, step: int, state: dict) -> str:
        """保存状态快照（用于恢复）"""
        filepath = self.checkpoints_dir / f"checkpoint_step_{step:02d}.json"
        self._write_json(filepath, {
            "step": step,
            "timestamp": _now_iso(),
            "state": _serialize_state(state),
        })
        return str(filepath)

    def load_checkpoint(self, step: int | None = None) -> dict | None:
        """加载检查点。step=None 时加载最新的"""
        if step is not None:
            filepath = self.checkpoints_dir / f"checkpoint_step_{step:02d}.json"
            if filepath.exists():
                return json.loads(filepath.read_text(encoding="utf-8"))
            return None
        # 找最新的
        checkpoints = sorted(self.checkpoints_dir.glob("checkpoint_step_*.json"))
        if not checkpoints:
            return None
        return json.loads(checkpoints[-1].read_text(encoding="utf-8"))

    def save_final_report(self, report: str) -> str:
        """保存最终报告"""
        filepath = self.base_dir / "final_report.md"
        filepath.write_text(report, encoding="utf-8")
        self._update_meta(status="completed")
        return str(filepath)

    def _update_meta(self, **kwargs) -> None:
        meta_path = self.base_dir / "meta.json"
        if meta_path.exists():
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta.update(kwargs)
            self._write_json(meta_path, meta)

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _serialize_state(state: dict) -> dict:
    """将 LangGraph state 序列化为可 JSON 化的 dict"""
    serialized = {}
    for key, value in state.items():
        if key == "messages":
            serialized[key] = [_serialize_message(m) for m in value[-10:]]  # 只保留最近 10 条
        elif isinstance(value, str):
            serialized[key] = value
        elif isinstance(value, (int, float, bool)):
            serialized[key] = value
        else:
            serialized[key] = str(value)
    return serialized


def _serialize_message(msg) -> dict:
    return {
        "type": type(msg).__name__,
        "content": str(getattr(msg, "content", ""))[:500],
    }
```

### 4.3 作为工具暴露给 agent（AgeMem 风格）

```python
# 在 deep_research.py 中新增工具

@tool
async def save_research_note(
    topic: str,
    content: str,
    evidence_strength: str = "moderate",
) -> str:
    """将重要研究发现保存到持久存储，防止上下文压缩时丢失。
    仅保存关键结论和证据，不要保存原始搜索结果。"""
    memory = _get_current_memory()  # 从 context var 获取
    step = _get_current_step()
    path = memory.save_finding(step, "note", topic, content)
    return f"已保存到 {path}"


@tool
async def recall_research_notes(topic: str = "") -> str:
    """回忆之前保存的研究笔记。可选按主题过滤。"""
    memory = _get_current_memory()
    findings = memory.list_findings()
    if not findings:
        return "暂无保存的研究笔记。"
    # 简单关键词匹配过滤
    results = []
    for path in findings:
        content = Path(path).read_text(encoding="utf-8")
        if not topic or topic.lower() in content.lower():
            results.append(content[:500])  # 每条限制 500 字符
    return "\n\n---\n\n".join(results[-5:])  # 最多返回最近 5 条
```

### 4.4 与上下文缩减的集成

```
工具返回结果
    ↓
save_finding() 写入文件（完整内容）
    ↓
FindingEntry.raw_content = 完整内容
FindingEntry.compact_content = 压缩版本
    ↓
build_prompt_context() 时：
  - 最近 2 步：读取 raw_content
  - 较早步骤：读取 compact_content + 文件路径引用
  - 需要回溯时：从文件路径重新加载 raw_content
```

## 5. 实现步骤

### Step 1：创建 `research_memory.py`
- 实现 `ResearchMemory` 类
- 实现目录初始化、finding 读写、summary 读写、checkpoint 读写

### Step 2：修改 `agents/deep_research.py` 的 `run()` 函数
```python
# run() 中初始化 memory
async def run(input_data) -> dict:
    ...
    task_id = input_data.get("task_id", str(uuid.uuid4())[:8])
    memory = ResearchMemory(task_id)
    memory.init(query, report_profile)
    ...
```

### Step 3：在 `research_planner_node` 中接入 memory
- 每次工具返回结果后，自动调用 `memory.save_finding()`
- 每 6 步自动调用 `memory.save_summary()`
- 每 5 步自动调用 `memory.save_checkpoint()`

### Step 4：添加 `save_research_note` 和 `recall_research_notes` 工具
- 注册到 `CORE_TOOLS`
- 让 agent 自主决定何时保存重要发现

### Step 5：修改 `research_finish` 使用 memory
- 最终报告写入 `memory.save_final_report()`
- 从 memory 而非内存 findings 生成报告

### Step 6：添加恢复机制
- `run()` 支持 `resume_from_checkpoint=True` 参数
- 加载最新 checkpoint 恢复状态
- 从断点继续研究

## 6. 测试方案

```python
# tests/test_research_memory.py

def test_init_creates_directory_structure():
    """测试初始化创建正确的目录结构"""

def test_save_and_load_finding():
    """测试保存和加载单条 finding"""

def test_list_findings_sorted():
    """测试 finding 列表按步骤排序"""

def test_save_and_load_summary():
    """测试摘要保存和 latest.md 更新"""

def test_save_and_load_checkpoint():
    """测试检查点保存和加载"""

def test_load_latest_checkpoint():
    """测试加载最新检查点"""

def test_save_final_report():
    """测试最终报告保存和状态更新"""

def test_serialize_state():
    """测试 LangGraph state 序列化"""

def test_finding_with_urls():
    """测试带 URL 的 finding 格式正确"""
```

## 7. 验收标准

- [ ] 每个研究步骤的完整工具输出都持久化到文件
- [ ] 上下文压缩后，可通过文件路径重新加载原始内容
- [ ] 进程崩溃后，可从最近的 checkpoint 恢复研究进度
- [ ] agent 可自主使用 save/recall 工具管理重要发现
- [ ] 最终报告持久化到文件系统
- [ ] data/research/ 目录结构符合设计规范
