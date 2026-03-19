# 设计文档：Deep Research 搜索工具智能选择策略

## 背景

deep_research agent 现有 4 种搜索工具：
- `web_search`（Tavily）：通用网页搜索，~1s，低成本
- `brave_search`：快速网页发现，~1s，低成本
- `academic_search`：Semantic Scholar + arXiv，~2s，免费
- `exa_deep_search`（Exa）：AI 深度语义搜索，5-60s，高成本

**问题**：当前 system prompt 只提到 3 个工具（web_search/academic_search/browser_navigate），LLM 缺乏选择指导，可能浪费高成本工具或错过最佳工具。

## 方案：Prompt 引导 + 动态工具筛选

采用混合策略：代码层根据三个维度做硬性工具过滤（控制成本），prompt 层在可用工具内做偏好引导（保持灵活）。

### 筛选维度

1. **查询特征（学术 vs 通用）**：复用 `_looks_like_academic_paper_task()` 检测学术关键词
2. **研究阶段（step_count）**：前期用快速工具建基础，后期开放深度搜索填缺口
3. **研究进度（progress/gaps）**：根据已有发现和缺口动态调整

### 工具可用性矩阵

| 工具 | Phase 1 (steps 0-3) | Phase 2 (steps 4+) | gaps 驱动提前开放 |
|------|---------------------|---------------------|-------------------|
| web_search | 始终可用 | 始终可用 | — |
| brave_search | 始终可用 | findings>=8 时移除 | — |
| academic_search | 学术查询可用 | 学术查询可用 | gaps 含论文/数据关键词时开放 |
| exa_deep_search | 不可用 | 可用 | gaps 驱动下 step>=2 时提前开放 |

## 修改文件

### 1. `agents/deep_research/graphs.py`

**新增函数** `_select_search_tools(state, all_search_tools) -> list`：

```python
from agents.deep_research.prompts import _looks_like_academic_paper_task

_SEARCH_TOOL_NAMES = {"web_search", "brave_search", "academic_search", "exa_deep_search"}
_ACADEMIC_GAP_KEYWORDS = ("论文", "paper", "实验", "experiment", "数据", "data", "baseline", "ablation")

def _select_search_tools(state: dict, all_search_tools: list) -> list:
    step = state.get("step_count", 0)
    query = state.get("query", "")
    progress = state.get("progress", {})
    is_academic = _looks_like_academic_paper_task(query)

    available = {"web_search", "brave_search"}

    if is_academic:
        available.add("academic_search")

    if step >= 4:
        available.add("exa_deep_search")

    gaps = progress.get("gaps", [])
    findings = progress.get("findings", [])
    gap_text = " ".join(gaps).lower() if gaps else ""

    if any(kw in gap_text for kw in _ACADEMIC_GAP_KEYWORDS):
        available.add("academic_search")
        if step >= 2:
            available.add("exa_deep_search")

    if len(findings) >= 8:
        available.discard("brave_search")

    return [t for t in all_search_tools if t.name in available]
```

**修改 planner 节点**（`research_planner_node`、`hierarchical_planner_node`、`parallel_worker_node`）：

在 `bind_tools` 之前分离搜索工具和非搜索工具，动态筛选后合并：

```python
search_tools = [t for t in all_tools if t.name in _SEARCH_TOOL_NAMES]
non_search_tools = [t for t in all_tools if t.name not in _SEARCH_TOOL_NAMES]
selected_search = _select_search_tools(state, search_tools)
step_tools = non_search_tools + selected_search

llm = get_llm("deep_research").bind_tools(step_tools)
```

### 2. `agents/deep_research/prompts.py`

**更新** `BASE_RESEARCH_SYSTEM` 策略第 3 条：

```
3. 搜索工具选择策略：
   - web_search（Tavily）：通用网页搜索，返回较完整摘要，速度快成本低，适合初步信息收集
   - brave_search：快速发现候选网页和来源列表，适合广撒网摸底
   - academic_search：搜索 Semantic Scholar + arXiv 论文，免费，适合找特定论文/作者/引用
   - exa_deep_search：AI 深度语义搜索，返回全文和要点提取，延迟高(5-60s)成本高，
     仅在快速搜索无法满足时使用，适合填补关键证据缺口或寻找深度分析文章
   - browser_navigate：浏览器抓取具体页面，适合动态内容或需要多步交互
   选择原则：先用快速工具(web_search/brave_search)建立信息基础，
   再用 academic_search 补充学术文献，最后用 exa_deep_search 填补关键缺口。
   避免在信息已充足时调用高成本工具。
```

## 验证

1. 单元测试：新增 `_select_search_tools()` 的测试用例（不同 step/query/progress 组合）
2. 集成测试：发送学术查询，验证 step 0-3 不出现 exa_deep_search 调用，step 4+ 出现
3. 现有测试：`pytest tests/test_deep_research.py` 全部通过

## 不修改的文件

- `tools/*.py` — 工具实现不变
- `core/registry.py` — 注册不变
- `agents/deep_research/run.py` — CORE_TOOLS 不变
