# 项目结构重组实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将根目录 12 个 .py 文件按职责分层到 core/ runtime/ capabilities/ storage/，将 deep_research.py 拆为包，建立清晰的依赖方向。

**Architecture:** 四层分离（core ← runtime ← capabilities ← agents），每层只允许依赖下层。通过在旧路径放 re-export 兼容模块实现渐进迁移，避免一次性断裂所有 import。

**Tech Stack:** Python 3.13, pytest, git

---

## 重要前置说明

### 依赖图（models.py 被 18 个文件引用，logger.py 被 20 个文件引用）

迁移策略：
1. 每移动一个文件，在**原路径**创建 re-export shim（`from core.models import *`）
2. 批量更新所有 import 后再删除 shim
3. 每个 Task 结束跑 `python -m pytest tests/ -v` 确认不回归

### 文件移动命名映射

| 原路径 | 新路径 |
|--------|--------|
| `models.py` | `core/models.py` |
| `logger.py` | `core/logger.py` |
| `registry.py` | `core/registry.py` |
| `content_utils.py` | `core/content_utils.py` |
| `task_runtime.py` | `runtime/task_runtime.py` |
| `task_dispatcher.py` | `runtime/task_dispatcher.py` |
| `task_interaction.py` | `runtime/task_interaction.py` |
| `context_manager.py` | `capabilities/context_manager.py` |
| `progress_tracker.py` | `capabilities/progress_tracker.py` |
| `research_planner.py` | `capabilities/planner.py` |
| `research_memory.py` | `capabilities/memory.py` |
| `memory/store.py` | `storage/user_store.py` |

---

### Task 1: 创建目录结构 + 移动 core/ 文件

**Files:**
- Create: `core/__init__.py`
- Move: `logger.py` → `core/logger.py`
- Move: `registry.py` → `core/registry.py`
- Move: `content_utils.py` → `core/content_utils.py`
- Move: `models.py` → `core/models.py`
- Create shims: `logger.py`, `registry.py`, `content_utils.py`, `models.py` (re-export wrappers at original paths)

**Step 1: 创建 core/ 目录和 __init__.py**

```bash
mkdir -p core
touch core/__init__.py
```

**Step 2: 移动 logger.py 并创建 shim**

```bash
mv logger.py core/logger.py
```

创建 re-export shim `logger.py`（原路径）：

```python
"""Compatibility shim — real module at core/logger.py"""
from core.logger import *  # noqa: F401,F403
from core.logger import log_async, get_logger  # explicit re-exports for type checkers
```

注意：需要检查 `core/logger.py` 实际导出了哪些名称。shim 只需要 `from core.logger import *`，但显式 re-export 几个常用名称有利于 IDE 支持。

实际上更简单的做法——因为 logger.py 内部没有 import 项目其他文件，直接移动就行：

```bash
mv logger.py core/logger.py
cat > logger.py << 'EOF'
"""Compatibility shim — real module at core/logger.py"""
from core.logger import *  # noqa: F401,F403
EOF
```

**Step 3: 移动 registry.py 并创建 shim**

```bash
mv registry.py core/registry.py
cat > registry.py << 'EOF'
"""Compatibility shim — real module at core/registry.py"""
from core.registry import *  # noqa: F401,F403
EOF
```

**Step 4: 移动 content_utils.py 并创建 shim**

```bash
mv content_utils.py core/content_utils.py
cat > content_utils.py << 'EOF'
"""Compatibility shim — real module at core/content_utils.py"""
from core.content_utils import *  # noqa: F401,F403
EOF
```

**Step 5: 移动 models.py 并创建 shim**

`models.py` 内部不 import 项目其他文件（只 import logger），但 logger 现在在 `core/logger.py`。由于 shim 存在，`from logger import ...` 仍然能工作。

```bash
mv models.py core/models.py
cat > models.py << 'EOF'
"""Compatibility shim — real module at core/models.py"""
from core.models import *  # noqa: F401,F403
EOF
```

**Step 6: 修复 core/models.py 的内部 import**

`core/models.py` 内有 `from logger import ...`。需要改为 `from core.logger import ...`。

打开 `core/models.py`，找到：
```python
from logger import log_async
```
改为：
```python
from core.logger import log_async
```

（如果有多处 logger import，全部改）

**Step 7: 运行测试**

```bash
python -m pytest tests/ -v
```

Expected: ALL PASS（shim 保证旧 import 路径仍有效）

**Step 8: Commit**

```bash
git add core/ logger.py registry.py content_utils.py models.py
git commit -m "refactor: move core modules to core/ with compatibility shims"
```

---

### Task 2: 移动 runtime/ 文件

**Files:**
- Create: `runtime/__init__.py`
- Move: `task_interaction.py` → `runtime/task_interaction.py`
- Move: `task_dispatcher.py` → `runtime/task_dispatcher.py`
- Move: `task_runtime.py` → `runtime/task_runtime.py`
- Create shims at original paths

**Step 1: 创建 runtime/ 目录**

```bash
mkdir -p runtime
touch runtime/__init__.py
```

**Step 2: 移动 task_interaction.py（无内部依赖，最简单）**

```bash
mv task_interaction.py runtime/task_interaction.py
cat > task_interaction.py << 'EOF'
"""Compatibility shim — real module at runtime/task_interaction.py"""
from runtime.task_interaction import *  # noqa: F401,F403
EOF
```

**Step 3: 移动 task_dispatcher.py**

`task_dispatcher.py` 依赖 `orchestrator.runner`。这个 import 不需要改（orchestrator/ 不动）。

```bash
mv task_dispatcher.py runtime/task_dispatcher.py
cat > task_dispatcher.py << 'EOF'
"""Compatibility shim — real module at runtime/task_dispatcher.py"""
from runtime.task_dispatcher import *  # noqa: F401,F403
EOF
```

检查 `runtime/task_dispatcher.py` 的内部 import，如有 `from agents.general_chat import ...` 等需确认路径仍有效（agents/ 目录不动，所以 OK）。

**Step 4: 移动 task_runtime.py**

`task_runtime.py` 依赖 logger, models, task_dispatcher, task_interaction。shim 存在所以不需要改内部 import。

```bash
mv task_runtime.py runtime/task_runtime.py
cat > task_runtime.py << 'EOF'
"""Compatibility shim — real module at runtime/task_runtime.py"""
from runtime.task_runtime import *  # noqa: F401,F403
EOF
```

**Step 5: 修复 runtime/ 内的跨模块引用**

检查 `runtime/task_runtime.py`，如果有 `from task_dispatcher import ...`，改为 `from runtime.task_dispatcher import ...`。同理 `from task_interaction import ...` 改为 `from runtime.task_interaction import ...`。

也将 `from logger import ...` 改为 `from core.logger import ...`，`from models import ...` 改为 `from core.models import ...`。

**Step 6: 运行测试**

```bash
python -m pytest tests/ -v
```

Expected: ALL PASS

**Step 7: Commit**

```bash
git add runtime/ task_interaction.py task_dispatcher.py task_runtime.py
git commit -m "refactor: move runtime modules to runtime/ with compatibility shims"
```

---

### Task 3: 移动 capabilities/ 文件

**Files:**
- Create: `capabilities/__init__.py`
- Move: `context_manager.py` → `capabilities/context_manager.py`
- Move: `progress_tracker.py` → `capabilities/progress_tracker.py`
- Move: `research_planner.py` → `capabilities/planner.py`（重命名）
- Move: `research_memory.py` → `capabilities/memory.py`（重命名）
- Create shims at original paths

**Step 1: 创建 capabilities/ 目录**

```bash
mkdir -p capabilities
touch capabilities/__init__.py
```

**Step 2: 移动 context_manager.py**

```bash
mv context_manager.py capabilities/context_manager.py
cat > context_manager.py << 'EOF'
"""Compatibility shim — real module at capabilities/context_manager.py"""
from capabilities.context_manager import *  # noqa: F401,F403
EOF
```

**Step 3: 移动 progress_tracker.py**

```bash
mv progress_tracker.py capabilities/progress_tracker.py
cat > progress_tracker.py << 'EOF'
"""Compatibility shim — real module at capabilities/progress_tracker.py"""
from capabilities.progress_tracker import *  # noqa: F401,F403
EOF
```

**Step 4: 移动 research_planner.py → capabilities/planner.py（重命名）**

```bash
mv research_planner.py capabilities/planner.py
cat > research_planner.py << 'EOF'
"""Compatibility shim — real module at capabilities/planner.py"""
from capabilities.planner import *  # noqa: F401,F403
EOF
```

修复 `capabilities/planner.py` 内部 import：
```python
# 将 from models import ... 改为
from core.models import get_llm, response_text
```

**Step 5: 移动 research_memory.py → capabilities/memory.py（重命名）**

```bash
mv research_memory.py capabilities/memory.py
cat > research_memory.py << 'EOF'
"""Compatibility shim — real module at capabilities/memory.py"""
from capabilities.memory import *  # noqa: F401,F403
EOF
```

**Step 6: 运行测试**

```bash
python -m pytest tests/ -v
```

Expected: ALL PASS

**Step 7: Commit**

```bash
git add capabilities/ context_manager.py progress_tracker.py research_planner.py research_memory.py
git commit -m "refactor: move capability modules to capabilities/ with compatibility shims"
```

---

### Task 4: 移动 storage/ 文件

**Files:**
- Create: `storage/__init__.py`
- Move: `memory/store.py` → `storage/user_store.py`
- Create shim: `memory/__init__.py` 和 `memory/store.py`

**Step 1: 创建 storage/ 目录**

```bash
mkdir -p storage
touch storage/__init__.py
```

**Step 2: 移动 memory/store.py → storage/user_store.py**

```bash
cp memory/store.py storage/user_store.py
```

修复 `storage/user_store.py` 内部 import：
```python
# 将 from models import ... 改为
from core.models import get_llm, response_text
```

**Step 3: 更新 memory/__init__.py 为 shim**

查看当前 `memory/__init__.py` 的内容，改为指向新位置：

```python
"""Compatibility shim — real module at storage/user_store.py"""
from storage.user_store import *  # noqa: F401,F403
```

删除旧的 `memory/store.py`（或也改为 shim）：

```python
"""Compatibility shim — real module at storage/user_store.py"""
from storage.user_store import *  # noqa: F401,F403
```

**Step 4: 运行测试**

```bash
python -m pytest tests/ -v
```

Expected: ALL PASS

**Step 5: Commit**

```bash
git add storage/ memory/
git commit -m "refactor: move user store to storage/ with compatibility shim"
```

---

### Task 5: 全局 import 替换 — core/ 模块

现在所有 shim 都就位了，逐步将各文件的 import 从旧路径改为新路径。

**Step 1: 替换所有 `from models import` → `from core.models import`**

影响文件（18 个）：
- `agents/deep_research.py`
- `agents/research_worker.py`
- `agents/general_chat.py`
- `agents/doc_agent.py`
- `agents/data_analyst.py`
- `agents/search_agent.py`
- `agents/writer_agent.py`
- `tools/image_to_diagram.py`
- `tools/summarizer.py`
- `tools/translator.py`
- `orchestrator/planner.py`
- `orchestrator/runner.py`
- `runtime/task_runtime.py`（已在 Task 2 改过）

对每个文件，将 `from models import ...` 改为 `from core.models import ...`。

**Step 2: 替换所有 `from logger import` → `from core.logger import`**

影响文件（20 个）：同上 + `tools/code_executor.py`, `tools/brave_search.py`, `tools/image_gen.py`, `tools/academic_search.py`, `tools/web_search.py`, `main.py`, `orchestrator/scheduler.py`

**Step 3: 替换 `from registry import` → `from core.registry import`**

影响文件：`orchestrator/planner.py`, `orchestrator/scheduler.py`, `agents/deep_research.py`

**Step 4: 替换 `from content_utils import` → `from core.content_utils import`**

影响文件：`agents/deep_research.py`, `agents/doc_agent.py`, `agents/data_analyst.py`, `agents/search_agent.py`, `orchestrator/runner.py`, `orchestrator/scheduler.py`

**Step 5: 运行测试**

```bash
python -m pytest tests/ -v
```

Expected: ALL PASS

**Step 6: Commit**

```bash
git add agents/ tools/ orchestrator/ main.py
git commit -m "refactor: update all imports to use core/ module paths"
```

---

### Task 6: 全局 import 替换 — runtime/ 和 capabilities/ 模块

**Step 1: 替换 `from task_interaction import` → `from runtime.task_interaction import`**

影响文件：`agents/deep_research.py`

**Step 2: 替换 `from context_manager import` → `from capabilities.context_manager import`**

影响文件：`agents/deep_research.py`, `tests/test_context_manager.py`

**Step 3: 替换 `from progress_tracker import` → `from capabilities.progress_tracker import`**

影响文件：`agents/deep_research.py`, `tests/test_progress_tracker.py`

**Step 4: 替换 `from research_planner import` → `from capabilities.planner import`**

影响文件：`agents/deep_research.py`, `agents/research_worker.py`, `tests/test_research_planner.py`, `tests/test_research_worker.py`

**Step 5: 替换 `from research_memory import` → `from capabilities.memory import`**

影响文件：`agents/deep_research.py`, `tools/research_notes.py`, `tests/test_research_memory.py`, `tests/test_research_notes.py`

**Step 6: 替换 `from task_runtime import` / `from task_dispatcher import`**

影响文件：`main.py`, `tests/test_task_streaming.py`

**Step 7: 替换 `from memory import` / `from memory.store import`**

影响文件：`orchestrator/runner.py`（改为 `from storage.user_store import ...`）

**Step 8: 运行测试**

```bash
python -m pytest tests/ -v
```

Expected: ALL PASS

**Step 9: Commit**

```bash
git add agents/ tools/ orchestrator/ main.py tests/
git commit -m "refactor: update all imports to use runtime/, capabilities/, storage/ paths"
```

---

### Task 7: 删除所有 compatibility shims

**Step 1: 确认没有文件再 import 旧路径**

```bash
grep -rn "^from models import\|^from logger import\|^from registry import\|^from content_utils import\|^from task_runtime import\|^from task_dispatcher import\|^from task_interaction import\|^from context_manager import\|^from progress_tracker import\|^from research_planner import\|^from research_memory import" --include="*.py" . | grep -v "\.pyc" | grep -v "__pycache__" | grep -v "^./models.py\|^./logger.py\|^./registry.py\|^./content_utils.py\|^./task_runtime.py\|^./task_dispatcher.py\|^./task_interaction.py\|^./context_manager.py\|^./progress_tracker.py\|^./research_planner.py\|^./research_memory.py"
```

Expected: 只有 shim 文件自身引用旧路径，其他文件都已改为新路径。

**Step 2: 删除 shim 文件**

```bash
rm models.py logger.py registry.py content_utils.py
rm task_runtime.py task_dispatcher.py task_interaction.py
rm context_manager.py progress_tracker.py research_planner.py research_memory.py
```

**Step 3: 删除旧 memory/ 目录**

```bash
rm -rf memory/
```

**Step 4: 运行测试**

```bash
python -m pytest tests/ -v
```

Expected: ALL PASS

**Step 5: Commit**

```bash
git add -A
git commit -m "refactor: remove compatibility shims, migration complete"
```

---

### Task 8: 拆分 deep_research.py 为包

**Files:**
- Create: `agents/deep_research/__init__.py`
- Create: `agents/deep_research/config.py`
- Create: `agents/deep_research/prompts.py`
- Create: `agents/deep_research/utils.py`
- Create: `agents/deep_research/graphs.py`
- Create: `agents/deep_research/run.py`
- Delete: `agents/deep_research.py`（先备份）

这是最复杂的一步。需要先读当前 `agents/deep_research.py` 的完整内容，按职责拆分。

**Step 1: 备份当前文件**

```bash
cp agents/deep_research.py agents/deep_research.py.bak
```

**Step 2: 创建包目录**

```bash
mkdir -p agents/deep_research_pkg
```

（先用 `_pkg` 后缀避免和现有 `deep_research.py` 冲突，最后再 rename）

**Step 3: 创建 config.py**

从 `agents/deep_research.py` 提取：
- `ResearchState` TypedDict
- `CHECKPOINT_INTERVAL`, `SUMMARY_INTERVAL`
- `ResearchConfig` dataclass
- `DEFAULT_REPORT_PROFILE`, `ACADEMIC_PAPER_GUIDANCE_PROFILE`
- `ReportProfile` dataclass
- `REPORT_PROFILES` dict
- `REPORT_PROFILE_ALIASES` dict
- `ACADEMIC_PROFILE_KEYWORDS` tuple
- `CORE_TOOLS` list definition

写入 `agents/deep_research_pkg/config.py`。

**Step 4: 创建 prompts.py**

从 `agents/deep_research.py` 提取：
- `BASE_RESEARCH_SYSTEM`
- `BASE_FINAL_REPORT_SYSTEM`
- `_build_research_messages()`
- `_build_research_system()`
- `_build_final_report_system()`
- `_normalize_report_profile()`
- `_get_report_profile()`
- `_looks_like_academic_paper_task()`
- `_resolve_report_profile()`

写入 `agents/deep_research_pkg/prompts.py`。

**Step 5: 创建 utils.py**

从 `agents/deep_research.py` 提取：
- `_retry_async()`
- `_synthesize_parallel_findings()`
- `_generate_structured_summary()`
- `_rewrite_final_report()`
- `_message_text()`
- `_truncate_text()`
- `_latest_tool_messages()`

写入 `agents/deep_research_pkg/utils.py`。

**Step 6: 创建 graphs.py**

从 `agents/deep_research.py` 提取：
- `research_planner()` (standalone node)
- `research_tools()`
- `research_finish()`
- `research_should_continue()`
- `build_research_graph()`
- `build_hierarchical_research_graph()`
- `build_parallel_research_graph()`

写入 `agents/deep_research_pkg/graphs.py`。

**Step 7: 创建 run.py**

从 `agents/deep_research.py` 提取：
- `run()` function
- tool definitions (`web_search`, `academic_search`, `browser_navigate`, `ask_user_clarification`)

写入 `agents/deep_research_pkg/run.py`。

**Step 8: 创建 __init__.py**

```python
"""Deep Research Agent — multi-round research with web search + academic search."""
from agents.deep_research_pkg.run import run  # noqa: F401

__all__ = ["run"]
```

**Step 9: 删除旧文件并 rename 包**

```bash
rm agents/deep_research.py
mv agents/deep_research_pkg agents/deep_research
```

**Step 10: 修复所有 import**

所有 `from agents.deep_research import ...` 或 `from agents import deep_research` 需要确保仍然有效。由于 `__init__.py` 导出 `run`，`from agents.deep_research import run` 仍然工作。

但测试中有 `from agents import deep_research` 然后用 `deep_research._retry_async` 等内部函数。需要在 `__init__.py` 中额外导出测试需要的名称，或者测试改为 import 具体子模块。

建议在 `__init__.py` 中增加关键导出：

```python
from agents.deep_research.run import run  # noqa: F401
from agents.deep_research.config import ResearchConfig, ResearchState  # noqa: F401
from agents.deep_research.graphs import (  # noqa: F401
    build_research_graph,
    build_hierarchical_research_graph,
    build_parallel_research_graph,
    research_should_continue,
    research_finish,
)
from agents.deep_research.utils import _retry_async, _synthesize_parallel_findings  # noqa: F401
from agents.deep_research.prompts import _build_research_messages, _resolve_report_profile  # noqa: F401
```

**Step 11: 运行测试**

```bash
python -m pytest tests/ -v
```

Expected: ALL PASS

**Step 12: 删除备份**

```bash
rm agents/deep_research.py.bak
```

**Step 13: Commit**

```bash
git add agents/deep_research/
git commit -m "refactor: split deep_research.py into package (config/prompts/graphs/utils/run)"
```

---

### Task 9: 清理 + 最终验证

**Step 1: 删除 old/ 目录**

确认 `old/orchestrator.py` 和 `old/agent.py` 不被任何文件引用：

```bash
grep -rn "from old\.\|import old\." --include="*.py" .
```

Expected: No matches.

```bash
rm -rf old/
```

**Step 2: 全量回归测试**

```bash
python -m pytest tests/ -v
```

Expected: ALL PASS

**Step 3: 验证目录结构符合设计**

```bash
find . -name "*.py" -not -path "./.venv/*" -not -path "./__pycache__/*" -not -path "./old/*" | sort
```

Expected: 符合设计文档 `docs/plans/2026-03-17-project-restructure-design.md` 的目标结构。

**Step 4: 验证无残留 shim 文件**

```bash
grep -rn "Compatibility shim" --include="*.py" .
```

Expected: No matches.

**Step 5: Commit**

```bash
git add -A
git commit -m "chore: cleanup old/ directory, project restructure complete"
```
