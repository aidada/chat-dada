# 机制 5：检索锚定（RAG 增强）

> 优先级：P2 | 预计影响文件：agents/deep_research.py, 新增 verification.py, tools/web_search.py

## 1. 概述与目标

当前 deep_research agent 有搜索能力，但**不验证搜索结果的质量和一致性**。LLM 可能把摘要级证据写成强结论，或在多轮搜索后产生与早期发现矛盾的主张。目标是引入 Perplexity 风格的检索锚定机制：每个主张都必须有来源支撑，多来源交叉验证，证据强度显式标注。

## 2. 行业参考

### Perplexity
- 核心原则："你不应该说任何你没有检索到的内容"
- 执行 20-50 次定向查询，一份报告可能引用超过 200 个来源
- 自动交叉验证：通常需要 2+ 个独立来源才能标记主题完成
- 每个主张都附带精确引用和 URL

### OpenAI Deep Research
- 过程监督（process supervision）——对每个中间推理步骤提供反馈
- 遇到矛盾数据时自动转向或深入挖掘
- 扩展思维链中保持专注（通过专门训练实现）

### 防幻觉学术研究
- **PLR+FAE**：每个推理步骤都必须建立在带时间戳的证据之上
- **Model-First Reasoning**：先构建问题的显式模型再推理
- **自洽性检查**：多次推理取交集
- **推理模型比基础模型更容易产生幻觉**（重要警告）

## 3. 当前代码诊断

| 位置 | 问题 |
|------|------|
| `web_search()` (L31-37) | 返回原始结果，无质量评估或来源标注 |
| `academic_search()` (L41-45) | 返回原始结果，无引用格式化 |
| `BASE_RESEARCH_SYSTEM` (L193-207) | 提到"关键结论后要给来源 URL"但无强制机制 |
| `_rewrite_final_report()` (L463-484) | 重写时可能引入未经检索的主张（LLM 自由发挥） |
| `_summarize_tool_messages()` (L417-428) | 压缩工具消息时可能丢失来源 URL |

### 核心风险
1. **幻觉放大**：agent 搜索到 A，摘要写成 A+B，后续搜索基于 A+B 搜，B 被当作已有证据
2. **证据强度不透明**：用户无法区分哪些结论有实验数据支撑，哪些只是观点
3. **来源丢失**：多轮压缩后 URL 可能被截断

## 4. 架构设计

### 4.1 引用追踪系统

```python
# verification.py（新增文件）

from dataclasses import dataclass, field


@dataclass
class Citation:
    """单条引用"""
    id: str                        # 如 "[1]", "[2]"
    url: str
    title: str = ""
    source_type: str = ""          # paper / web / official_doc / news
    accessed_step: int = 0         # 在哪一步获取的
    snippet: str = ""              # 原文关键段落（≤200 字符）


@dataclass
class Claim:
    """单个主张及其证据"""
    statement: str                 # 主张内容
    citations: list[str] = field(default_factory=list)  # Citation id 列表
    evidence_strength: str = "unverified"  # strong / moderate / weak / abstract_only / unverified
    cross_validated: bool = False   # 是否有 2+ 独立来源
    contradictions: list[str] = field(default_factory=list)  # 矛盾的引用 id


@dataclass
class CitationRegistry:
    """全局引用注册表"""
    citations: dict[str, Citation] = field(default_factory=dict)
    claims: list[Claim] = field(default_factory=list)
    _next_id: int = 1

    def add_citation(self, url: str, title: str = "", source_type: str = "",
                     step: int = 0, snippet: str = "") -> str:
        """注册一条引用，返回引用 id"""
        # 去重：同一 URL 不重复注册
        for cid, c in self.citations.items():
            if c.url == url:
                return cid
        cid = f"[{self._next_id}]"
        self._next_id += 1
        self.citations[cid] = Citation(
            id=cid, url=url, title=title, source_type=source_type,
            accessed_step=step, snippet=snippet,
        )
        return cid

    def add_claim(self, statement: str, citation_ids: list[str],
                  evidence_strength: str = "unverified") -> Claim:
        """注册一个主张"""
        cross_validated = len(set(citation_ids)) >= 2
        claim = Claim(
            statement=statement,
            citations=citation_ids,
            evidence_strength=evidence_strength,
            cross_validated=cross_validated,
        )
        self.claims.append(claim)
        return claim

    def format_references(self) -> str:
        """生成参考文献列表"""
        lines = ["## 参考来源\n"]
        for cid, c in sorted(self.citations.items(), key=lambda x: x[0]):
            line = f"{cid} "
            if c.title:
                line += f"{c.title}. "
            line += c.url
            if c.source_type:
                line += f" ({c.source_type})"
            lines.append(line)
        return "\n".join(lines)

    def get_unverified_claims(self) -> list[Claim]:
        """获取未验证的主张"""
        return [c for c in self.claims if c.evidence_strength == "unverified"]

    def get_weak_claims(self) -> list[Claim]:
        """获取证据不足的主张"""
        return [c for c in self.claims if c.evidence_strength in ("weak", "abstract_only")]
```

### 4.2 工具输出的来源提取

```python
# 增强 web_search 工具的返回格式

@tool
async def web_search(query: str) -> str:
    """用 Tavily 搜索互联网，返回结果含完整来源信息。"""
    if HAS_TAVILY:
        search = TavilySearchResults(max_results=5)
        results = await search.ainvoke(query)
        formatted = []
        for r in results:
            formatted.append(
                f"来源: {r['url']}\n"
                f"标题: {r.get('title', '未知')}\n"
                f"内容: {r['content']}\n"
                f"---"
            )
        return "\n\n".join(formatted)
    return f"(TAVILY_API_KEY not configured, skipping '{query}')"
```

### 4.3 证据强度自动评估

```python
EVIDENCE_ASSESSMENT_SYSTEM = """评估以下研究发现的证据强度。

证据强度分级：
- strong：有实验数据、公式推导、或权威机构的量化结论
- moderate：有论据和逻辑推理，但缺少量化数据
- weak：仅有观点、类比、或个人经验
- abstract_only：仅有论文摘要，未见正文数据

对每个关键主张，输出：
主张：...
证据强度：strong/moderate/weak/abstract_only
支撑来源：[URL1], [URL2]
是否需要补充验证：是/否
"""


async def assess_evidence_strength(findings: str) -> str:
    """用 LLM 评估 findings 中各主张的证据强度"""
    llm = get_llm("summarizer")  # 用小模型评估
    response = await llm.ainvoke([
        SystemMessage(content=EVIDENCE_ASSESSMENT_SYSTEM),
        HumanMessage(content=findings),
    ])
    return extract_text_content(response)
```

### 4.4 交叉验证机制

```python
async def cross_validate_claim(claim: str, existing_sources: list[str]) -> str:
    """对关键主张进行交叉验证搜索"""
    # 构造验证查询：原始主张 + 反面关键词
    validation_query = f"{claim} evidence OR disprove OR limitation"
    result = await web_search.ainvoke(validation_query)
    return result
```

### 4.5 最终报告的引用注入

```python
FINAL_REPORT_WITH_CITATIONS = """你是研究报告编辑。重写报告时必须遵守以下引用规则：

1. 每个事实性陈述后必须附引用编号，如 [1]、[2]
2. 如果某个主张只有单一来源，标注"(单一来源)"
3. 如果某个主张缺少量化数据，标注"(仅摘要级证据)"
4. 如果多个来源有矛盾，必须如实呈现矛盾并标注各来源
5. 报告末尾必须有完整的参考来源列表
6. 不要添加任何没有来源支撑的事实性主张
"""
```

## 5. 实现步骤

### Step 1：新增 `verification.py`
- 实现 `Citation`、`Claim`、`CitationRegistry` 数据结构
- 实现 `format_references()`、`get_unverified_claims()`

### Step 2：增强工具返回格式
- 修改 `web_search()` 返回结构化的来源信息（URL + 标题 + 内容）
- 修改 `academic_search()` 返回 DOI + 作者 + 年份

### Step 3：在 `research_planner_node` 中接入引用追踪
- 每次工具返回后，自动提取 URL 注册到 `CitationRegistry`
- 在 findings 中嵌入引用编号

### Step 4：添加证据强度评估步骤
- 每个子任务完成后（机制 3/4 的 worker 完成时），自动运行 `assess_evidence_strength()`
- 评估结果写入外部记忆

### Step 5：修改 `_rewrite_final_report()`
- 使用 `FINAL_REPORT_WITH_CITATIONS` 作为 system prompt
- 将 `CitationRegistry.format_references()` 附加到报告末尾
- 在 prompt 中注入证据强度评估结果

### Step 6：添加交叉验证（可选增强）
- 识别 `evidence_strength == "weak"` 或 `cross_validated == False` 的关键主张
- 自动发起验证搜索
- 更新证据强度

## 6. 测试方案

```python
# tests/test_verification.py

def test_citation_dedup():
    """测试相同 URL 不重复注册"""

def test_citation_auto_increment_id():
    """测试引用编号自增"""

def test_claim_cross_validation():
    """测试 2+ 来源时 cross_validated=True"""

def test_format_references():
    """测试参考文献列表格式"""

def test_get_unverified_claims():
    """测试过滤未验证主张"""

def test_evidence_strength_categories():
    """测试四级证据强度分类"""

async def test_assess_evidence_integration():
    """集成测试：LLM 证据评估输出格式"""
```

## 7. 验收标准

- [ ] 最终报告中每个事实性陈述都附有引用编号
- [ ] 报告末尾有完整的参考来源列表（URL + 标题）
- [ ] 证据强度透明标注（strong/moderate/weak/abstract_only）
- [ ] 单一来源的主张有特殊标记
- [ ] 工具消息压缩时来源 URL 不丢失
- [ ] 多来源矛盾时如实呈现双方观点
