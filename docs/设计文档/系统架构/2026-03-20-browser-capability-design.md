# Browser Capability 设计

## 目标

将当前分散在 `search_agent` 与 `deep_research` 内部的 `browser_use` 封装为共享 browser capability，供 `research`、`patent` 等领域 agent 按需调用。

该能力的目标不是替代搜索引擎，而是处理以下高成本网页任务：

- 动态网页抓取
- 多步点击/展开/翻页
- 页面级证据采集
- 截图与可审计执行过程保留

## 设计原则

1. **显式启用**
   - 只有在需要页面级交互时才调用 browser capability
2. **结构化输出**
   - 不返回纯字符串，统一返回结构化结果
3. **可审计**
   - 保留访问 URL、截图、提取证据、执行日志
4. **可约束**
   - 支持域名白名单、最大步数、超时、截图开关
5. **领域无关**
   - capability 本身不理解 research/patent 语义，只负责浏览器执行和证据采集

## 目标位置

建议实现路径：

```text
capabilities/
└── toolkits/
    └── browser_toolkit.py
```

由各领域 agent 通过共享 toolkit 使用，而不是继续内嵌 `browser_navigate`。

## 接口草图

### 高层调用接口

```python
async def run_browser_task(request: BrowserTaskRequest) -> BrowserTaskResult:
    ...
```

### 便捷包装接口

```python
async def browse_for_summary(
    goal: str,
    *,
    start_url: str | None = None,
    allowed_domains: list[str] | None = None,
) -> BrowserTaskResult:
    ...


async def browse_for_evidence(
    goal: str,
    *,
    start_url: str | None = None,
    allowed_domains: list[str] | None = None,
) -> BrowserTaskResult:
    ...
```

## Schema 草图

### 请求模型

```python
from typing import Literal
from pydantic import BaseModel, Field


class BrowserTaskRequest(BaseModel):
    goal: str = Field(..., description="浏览器任务目标，面向 agent 的操作说明")
    start_url: str | None = Field(default=None, description="任务起始 URL")
    allowed_domains: list[str] = Field(default_factory=list, description="允许访问的域名白名单")
    max_steps: int = Field(default=10, ge=1, le=50, description="最大浏览器操作步数")
    timeout_seconds: int = Field(default=120, ge=10, le=900, description="任务超时时间")
    need_login: bool = Field(default=False, description="是否需要登录态或受保护页面")
    headless: bool = Field(default=True, description="是否以无头模式运行")
    capture_screenshots: bool = Field(default=True, description="是否保存截图 artifact")
    capture_html: bool = Field(default=False, description="是否保存页面 HTML")
    extract_mode: Literal["summary", "evidence", "structured"] = Field(
        default="summary",
        description="提取模式：摘要、证据、结构化"
    )
    domain_tag: str = Field(default="generic", description="调用方领域标签，如 research/patent/zero_report")
    task_id: str | None = Field(default=None, description="平台任务 ID")
    thread_id: str | None = Field(default=None, description="平台线程 ID")
```

### 页面 artifact

```python
class BrowserPageArtifact(BaseModel):
    url: str
    title: str = ""
    screenshot_path: str | None = None
    html_path: str | None = None
    text_excerpt: str = ""
    timestamp: str = ""
```

### 证据项

```python
class BrowserEvidenceItem(BaseModel):
    type: Literal["quote", "fact", "table", "screenshot", "link"]
    content: str
    source_url: str
    locator: str = ""
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
```

### 执行日志

```python
class BrowserExecutionLogItem(BaseModel):
    step: int
    action: str
    target: str = ""
    status: Literal["ok", "error", "skipped"] = "ok"
    note: str = ""
```

### 返回模型

```python
class BrowserTaskResult(BaseModel):
    status: Literal["ok", "partial", "error"]
    summary: str = ""
    final_url: str = ""
    visited_urls: list[str] = Field(default_factory=list)
    page_artifacts: list[BrowserPageArtifact] = Field(default_factory=list)
    extracted_evidence: list[BrowserEvidenceItem] = Field(default_factory=list)
    execution_log: list[BrowserExecutionLogItem] = Field(default_factory=list)
    error: str = ""
```

## 运行语义

### `extract_mode="summary"`

用于快速浏览并总结页面内容：

- 优先返回 `summary`
- 可保留少量 `page_artifacts`
- `extracted_evidence` 可为空或很少

### `extract_mode="evidence"`

用于需要证据链的任务：

- 强制提取 `extracted_evidence`
- 推荐开启截图
- 要求记录 `source_url` 和 `locator`

### `extract_mode="structured"`

用于页面可解析成更强结构的场景：

- 例如专利详情页、事故工单页、表格型页面
- 后续可在 `BrowserTaskResult` 外再包一层领域专用 parser

## 与领域 agent 的关系

### Research

适用：

- 候选网页深入阅读
- 需要展开/点击后才可见的研究资料
- 截图与网页证据采集

### Patent

适用：

- 专利详情页核实
- 技术资料页面证据补充

限制：

- 不替代 prior-art 主检索路径

## 与现有实现的映射

当前复用来源：

- `agents/search_agent.py::browser_navigate`
- `agents/deep_research/run.py::browser_navigate`
- `core/models.py::get_browser_use_llm`

目标迁移：

1. 保留 `get_browser_use_llm()` 作为底层模型适配
2. 把两处 `browser_navigate` 上收为共享 toolkit
3. 旧 agent 改为调用共享 capability
4. 逐步将返回值从字符串升级为 `BrowserTaskResult`

## 非目标

以下内容不在 browser capability 本身负责：

- 领域级结论生成
- artifact 渲染
- 用户审批逻辑
- 平台级 checkpoint 恢复
- 网页内容的最终学术/专利/归零报告解释

## 结论

browser capability 的目标不是“多一个工具”，而是：

> **一个可约束、可审计、可结构化输出的共享网页执行能力层。**

只有把它从当前的私有字符串工具升级为结构化 capability，前端的证据卡、页面溯源卡、浏览器进度条和 LangSmith 观测才有稳定基础。
