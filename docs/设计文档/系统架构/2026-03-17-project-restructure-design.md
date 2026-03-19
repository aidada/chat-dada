# 项目结构重组设计：按职责分层 + 为通用化留位

## 背景

项目经过多轮开发，当前 66 个 Python 文件的组织存在三个核心问题：

1. **根目录职能混杂**：12 个 .py 文件混合了全局基础设施（models, logger）、任务生命周期（task_runtime, task_dispatcher）和 deep_research 专属模块（context_manager, progress_tracker, research_memory, research_planner）
2. **Agent 文件臃肿**：deep_research.py 43KB/1088行，承担工具定义、状态管理、图构建、节点函数、入口函数 6 个职责
3. **层间依赖不清晰**：缺少明确的分层规则，tool 层（research_notes）通过 ContextVar 反向依赖 agent 层概念

## 目标

1. 按职责分层，建立清晰的依赖方向（core ← runtime ← capabilities ← agents）
2. deep_research 从单文件拆为包，每个文件 < 300 行
3. 为未来 capabilities 通用化留好位置（任何 agent 可选装上下文管理、进度追踪等能力）
4. 记忆体系区分任务记忆和用户记忆

## 设计

### 1. 目标目录结构

```
chat-dada/
├── main.py                      ← HTTP 入口（不变）
│
├── core/                        ← 全局基础设施
│   ├── __init__.py
│   ├── models.py                ← 原 models.py
│   ├── logger.py                ← 原 logger.py
│   ├── registry.py              ← 原 registry.py
│   └── content_utils.py         ← 原 content_utils.py
│
├── runtime/                     ← 任务执行生命周期
│   ├── __init__.py
│   ├── task_runtime.py          ← 原 task_runtime.py
│   ├── task_dispatcher.py       ← 原 task_dispatcher.py
│   └── task_interaction.py      ← 原 task_interaction.py
│
├── capabilities/                ← 可选装 agent 能力（未来通用化的位置）
│   ├── __init__.py
│   ├── context_manager.py       ← 原 context_manager.py，三层上下文管理
│   ├── progress_tracker.py      ← 原 progress_tracker.py，进度追踪 + token 统计
│   ├── planner.py               ← 原 research_planner.py，去掉 research_ 前缀
│   └── memory.py                ← 原 research_memory.py 的 agent 接口层
│
├── storage/                     ← 持久化层
│   ├── __init__.py
│   ├── task_store.py            ← 原 research_memory.py 的纯 I/O 部分
│   └── user_store.py            ← 原 memory/store.py
│
├── agents/                      ← Agent 层
│   ├── __init__.py
│   ├── deep_research/           ← 从单文件拆为包
│   │   ├── __init__.py          ← 只导出 run()
│   │   ├── config.py            ← ResearchState, ResearchConfig, ReportProfile
│   │   ├── prompts.py           ← system prompts + _build_research_messages
│   │   ├── graphs.py            ← 三种图构建 + 节点函数
│   │   ├── utils.py             ← _retry_async, _rewrite_final_report, helpers
│   │   └── run.py               ← run() 入口函数
│   ├── research_worker.py       ← 不变
│   ├── search_agent.py          ← 不变
│   ├── general_chat.py          ← 不变
│   ├── doc_agent.py             ← 不变
│   ├── data_analyst.py          ← 不变
│   └── writer_agent.py          ← 不变
│
├── tools/                       ← 工具层（不变）
│   ├── academic_search.py
│   ├── web_search.py
│   ├── brave_search.py
│   ├── research_notes.py
│   ├── code_executor.py
│   ├── image_gen.py
│   ├── image_to_diagram.py
│   ├── summarizer.py
│   └── translator.py
│
├── renderers/                   ← 渲染层（不变）
│   ├── word_renderer.py
│   ├── excel_renderer.py
│   ├── markdown_renderer.py
│   └── visio_renderer.py
│
├── orchestrator/                ← 编排层（不变）
│   ├── runner.py
│   ├── scheduler.py
│   ├── planner.py
│   └── templates.py
│
├── ppt_engine/                  ← PPT 引擎（不变）
│   ├── dsl_schema.py
│   └── renderer.py
│
├── tests/                       ← 测试（跟随源文件调整 import）
└── docs/plans/                  ← 设计文档
```

### 2. 分层依赖规则

```
依赖方向（只允许向下箭头）：

  main.py
     ↓
  runtime/    →  core/
     ↓
  orchestrator/  →  core/ + runtime/
     ↓
  agents/     →  core/ + capabilities/ + tools/
     ↓
  capabilities/  →  core/ + storage/
     ↓
  tools/      →  core/
     ↓
  renderers/  →  core/
     ↓
  storage/    →  core/
     ↓
  core/       →  (外部依赖 only)
```

**禁止的依赖：**
- core/ 不能 import 项目内任何其他层
- capabilities/ 不能 import agents/ 或 runtime/
- tools/ 不能 import agents/（research_notes.py 的 ContextVar 模式需要调整）
- storage/ 不能 import capabilities/ 或 agents/

### 3. deep_research 包拆分细节

| 文件 | 行数(估) | 内容 |
|------|----------|------|
| `config.py` | ~130 | ResearchState, ResearchConfig, ReportProfile, REPORT_PROFILES, aliases, keywords |
| `prompts.py` | ~80 | BASE_RESEARCH_SYSTEM, BASE_FINAL_REPORT_SYSTEM, _build_research_messages, _build_research_system, _build_final_report_system |
| `graphs.py` | ~400 | build_research_graph, build_hierarchical_research_graph, build_parallel_research_graph + 所有内部节点函数 |
| `utils.py` | ~100 | _retry_async, _rewrite_final_report, _synthesize_parallel_findings, _message_text, _truncate_text, _latest_tool_messages |
| `run.py` | ~100 | run() 入口 + 输入解析 + checkpoint resume + graph 选择 |
| `__init__.py` | ~5 | `from .run import run` |

### 4. 记忆体系

三种记忆的存储需求：

| 类型 | 生命周期 | 隔离键 | 存储位置 | 维护策略 |
|------|----------|--------|----------|----------|
| 任务记忆 | 一次任务 | task_id | storage/task_store.py | 任务完成后可清理 |
| 用户记忆 | 跨任务持久 | user_id | storage/user_store.py | 需要合并去重、过期淘汰 |
| 会话记忆 | 一次会话 | — | LangGraph state（内存） | 会话结束自动释放 |

`capabilities/memory.py` 是任务记忆的 **agent 接口层**：
- 提供 `save_checkpoint()`, `load_checkpoint()`, `save_finding()` 等高层 API
- 内部调用 `storage/task_store.py` 做实际文件 I/O
- 未来通用化时，任何 agent 通过 `capabilities.memory` 获取 checkpoint 能力

`storage/user_store.py` 是用户记忆的**持久化层**：
- 当前保持 `memory/store.py` 的功能不变
- 后续专项设计"合并-去重-过期"策略

### 5. import 路径迁移映射

| 原路径 | 新路径 |
|--------|--------|
| `from models import get_llm` | `from core.models import get_llm` |
| `from logger import log_async` | `from core.logger import log_async` |
| `from registry import get_tools_for_agent` | `from core.registry import get_tools_for_agent` |
| `from content_utils import ...` | `from core.content_utils import ...` |
| `from task_runtime import ...` | `from runtime.task_runtime import ...` |
| `from task_dispatcher import ...` | `from runtime.task_dispatcher import ...` |
| `from task_interaction import ask_user` | `from runtime.task_interaction import ask_user` |
| `from context_manager import ResearchContext` | `from capabilities.context_manager import ResearchContext` |
| `from progress_tracker import ProgressTracker` | `from capabilities.progress_tracker import ProgressTracker` |
| `from research_planner import ...` | `from capabilities.planner import ...` |
| `from research_memory import ResearchMemory` | `from capabilities.memory import ResearchMemory` |
| `from memory.store import ...` | `from storage.user_store import ...` |
| `from agents.deep_research import run` | `from agents.deep_research import run`（不变） |

### 6. tools/research_notes.py 的依赖修正

当前 `research_notes.py` 通过 ContextVar 依赖 `ResearchMemory`。移到 `capabilities/` 后：

```python
# tools/research_notes.py
from capabilities.memory import ResearchMemory  # 明确的依赖方向
```

这在分层规则里是 `tools/ → capabilities/` 的依赖。严格来说 tools 不应该依赖 capabilities，但 research_notes 本质上是 capabilities 的一个工具接口。

有两个选项：
1. **放宽规则**：允许 research_notes 依赖 capabilities（实用主义）
2. **移到 capabilities/**：research_notes 本质是 capability 的一部分，不是独立工具

建议选 1——这个 ContextVar 模式是合理的 IoC，不算真正的"反向依赖"。

## 实施顺序建议

```
Phase 1: 创建目录 + 移动文件（不改代码逻辑）
  - 创建 core/, runtime/, capabilities/, storage/ 目录
  - 移动文件到对应位置
  - 在原位置放置 re-export 兼容模块（保证旧 import 不断）

Phase 2: 更新所有 import 路径
  - 全局搜索替换 import 路径
  - 移除兼容模块
  - 运行全量测试

Phase 3: deep_research 包拆分
  - 拆 config.py, prompts.py, graphs.py, utils.py, run.py
  - 更新测试
  - 运行全量测试

Phase 4: 清理
  - 删除 old/ 目录
  - 删除空的 memory/ 目录
  - 更新 pyproject.toml（如有 package 配置）
```

## 注意事项

- Phase 1 中的"兼容模块"策略确保可以逐步迁移，不会一次性断裂所有 import
- 每个 Phase 结束后必须全量测试通过
- `old/` 目录中的 `orchestrator.py` 和 `agent.py` 确认不再需要后删除
- 用户记忆的"合并-去重-过期"策略是独立设计任务，不在本次重构范围
