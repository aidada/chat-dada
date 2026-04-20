# Office PPT Visual Quality — Design Spec

**Status**: Proposed
**Date**: 2026-04-20
**Scope**: Option C (full) — 批 1 + 批 2 + 批 3，估 1-2 周
**Supersedes**: n/a (扩展现有 `2026-04-16-office-ppt-staged-execution-design.md` 与 `2026-04-17-office-reference-driven-strategy-design.md` 的 QA / planner 语义)

---

## 1. Problem

生成的 PPT 在结构上通过 `validate`、`view stats`、`view annotated`，但视觉上明显粗糙。参考 benchmark deck `fishing-benefits-for-men.pptx` 观察到的症状：

- `ppt/media/` 为空，10 页全部 `pics=0`、`graphicFrame=0`
- 部分页同时存在 placeholder title 和手工 manual textbox title，标题重复
- 字体家族漂移：Calibri / Georgia / Microsoft YaHei / Arial 混用
- 色块 + 文本框堆出来的"伪视觉"过关；QA 认为 `visual_slide_count` 达标
- 用户要求"配图"时没有真图，管线也不强制 `image_gen`

### 根因

1. **QA 真值源错误**：`strategies/ppt.py` 的 `evaluate_quality_stats` 直接从模型最终 JSON 的 `stats` 字段读数字，模型可以幻觉或敷衍，硬门槛形同虚设。
2. **视觉定义过宽**：`workflow.py` 把"卡片、色块、流程、KPI 数字、图片"都算视觉元素，模型走成本最低路径 = 色块 + 文本框。
3. **图片能力是"可选"**：`workflow.py:259-262` 说"配图时可以调 `image_gen`"，没有"没真图必须生成"。
4. **Plan 和 QA 未绑定**：`_slide_visual_requirements` 在 planner 写好 `picture` / `hero-shape-background` 等期望，但 QA 只看 deck 聚合 stats，单页期望是否命中无法验证。
5. **无 style preset**：没有色板合同、字体对合同、vertical 风格合同；每份 deck 的视觉系统靠模型自由发挥。
6. **无模板路由**：`skills/officecli/morph-ppt*` / `officecli-pitch-deck` / `officecli-presentation-quality` 已存在，但 strategy 没有路由逻辑调用它们，所有 deck 都走 blank-deck 堆对象的路径。

---

## 2. Non-Goals

明确不做：

- **不换渲染引擎**。不迁移到 PptxGenJS（MiniMax 方案）、不迁移到从零 python-pptx 路线。
- **不做渲染后截图 QA**。desktop 自动开窗做视觉检查在 v1 太脆，defer 到未来。
- **不建平行 XML unpack → edit → repack lane**。officecli 结构化 verb 已覆盖。
- **不做 18 套完整色板**。v1 只做 4-5 个 style preset。
- **不做动画/转场美学检查**。transition 只保"存在"，不保"审美"。
- **不做 markitdown-only QA**。我们现有 `view stats` / `view annotated` 比它强。

---

## 3. Architecture Overview

保持现有 LangGraph 节点拓扑：
`analyze → preflight → resolve_reference_inputs → select_strategy → planning → build ⇄ qa_fix → finalize`

三批改动分布：

- **批 1（真值化 + 硬门槛）**：新增 `agent/workflows/office/strategies/ppt_stats_reader.py`；`strategies/ppt.py::evaluate_quality_stats` 签名和实现改造；`workflow.py` 的 format guidance 与 `_OFFICE_SYSTEM` prompt 段更新。
- **批 2（taxonomy + preset）**：`goal_contract.py` 扩 `GoalProfile.style_preset`；`goal_normalizer.py` 增加 vertical 推断；`strategies/ppt.py` 的 `build_plan` / `validate_plan` 扩 slide schema；新增 `agent/workflows/office/style_presets.py`；`preflight_node` 在 `confidence == "low"` 时通过 interrupt 澄清 vertical。
- **批 3（模板优先 lane）**：新增 `agent/workflows/office/template_router.py` + `strategies/ppt_template.py`；在 `planning_node` 之前路由到模板 lane 或 blank lane；批 3 的 QA 复用批 1 + 2 的 gate 集合。

---

## 4. Batch 1 — Ground-Truth Stats + Hard Gates

### 4.1 真值化 stats

**问题**：QA 读 `meta["stats"]`，模型可以撒谎。

**做法**：

1. Build 阶段结尾强制执行 `officecli(verb="view", mode="stats")` 和 `officecli(verb="view", mode="annotated")`，把原始工具输出作为 `intermediate_results` 的专用字段 `ground_truth_stats_raw` 回传，而不是只看模型压缩后的 JSON。
2. 新增 `agent/workflows/office/strategies/ppt_stats_reader.py`，负责解析 `view stats` / `view annotated` 的原始文本/JSON 输出，产出结构化 `GroundTruthStats`：

   ```python
   class GroundTruthStats:
       slide_count: int
       per_slide: list[SlidePhysicalStats]   # 每页对象统计
       unique_font_families: set[str]
       theme_colors: list[str]
       placeholder_remnant_hits: list[tuple[int, str]]  # (slide_idx, matched_pattern)

   class SlidePhysicalStats:
       index: int
       picture_count: int
       chart_count: int
       table_count: int
       smartart_count: int
       shape_count: int
       text_box_count: int
       distinct_title_objects: int   # placeholder_title + manual_textbox_title 同页计数
       layout_signature: str          # 由形状类型序列 hash 而来（见 4.2 G1-consecutive-layout）
       has_notes: bool
       has_transition: bool
   ```

3. `strategies/ppt.py::evaluate_quality_stats` 签名改为接收 `GroundTruthStats` 而不是 `dict`；模型自报 stats 仅作为次要对照（不一致时以真值为准，模型的 stats 不再能让 QA pass）。

### 4.2 新增 6 个硬 gate（所有都必须基于 `GroundTruthStats`）

| Gate | 判定 | Severity |
|---|---|---|
| G1-font-family | `len(unique_font_families) > 2` → fail | error |
| G1-duplicate-title | 任意 slide 的 `distinct_title_objects > 1` → fail | error |
| G1-placeholder-remnant | 任意 slide 文本命中 `{xxxx, lorem ipsum, TODO, FIXME, 占位, placeholder}` → fail | error |
| G1-picture-threshold | 用户 goal 含配图词 → `sum(s.picture_count) < ceil(slide_count * 0.3)` → fail | error |
| G1-consecutive-layout | 相邻两页 `layout_signature` 相同 → fail | error |
| G1-decorative-only-cap | `decorative_only_slide_count / content_slide_count > 0.5` → fail | error |

**说明：批 1 不做色板门槛**。理由：benchmark failure 的根因是字体漂 + 缺图 + 重复标题，色板漂通常和字体漂一起出现，字体门槛已覆盖 ~80% case；真正的"使用中"颜色扫描需要遍历所有 `ppt/slides/slide*.xml` 抽 `srgbClr` + `schemeClr`，工程成本高、边缘 case 多（填充/边框/文字/渐变各处都有颜色）。批 1 先不做，等批 2 观察字体门槛拦下来的 case 之后再评估是否有必要补。

**Layout signature 定义**（用于 G1-consecutive-layout）：每页按 `annotated` 输出顺序取 shape 类型序列（`Title`, `TextBox`, `Picture`, `Chart`, `Table`, `SmartArt`, `Shape` 等），join 成 `T-TB-TB-P` 形式的字符串，作为该页 layout 的签名。不依赖 officecli 输出 slideLayout 名称——当前 `view annotated` 不报这个字段，自己算 signature 足以拦"连续两页结构相同"。

"配图词"定义（用于触发 G1-picture-threshold）：`{配图, 插图, 加图, 配一些图, 要图, 附图, with images, with pictures}`，任意命中即触发。判定来自 `GoalProfile.quality_profile.visuals=true` 或 goal 原文匹配。

"Decorative-only" 定义：单页 `picture_count == 0 and chart_count == 0 and table_count == 0 and smartart_count == 0`，即没有真视觉，只靠 shape/textbox 堆。

### 4.3 Prompt 强化

`workflow.py::_build_format_specific_guidance` 中的图片段落改写：

- 当前："如果没有现成图片素材，不允许交付纯文字 deck；改用 shapes / cards / chart / process flow 做视觉表达。"
- 改为：
  - "用户要求配图或 deck 类型是营销/案例/生活方式：**必须**让 `picture_count ≥ ceil(slide_count × 0.3)`。"
  - "检查 `list_user_images`；命中用用户图；**没命中必须走 `image_gen`，不允许省略**。"
  - "封面 slide 必须有 picture 或 full-bleed hero shape，不允许只有文字 + 色块。"

`_OFFICE_SYSTEM` 增加 "交付前 QA" 段：

- 模型自报的 `stats` 仅作参考；以 `view stats` / `view annotated` 工具真值为准
- 字体家族限制为 {YaHei, Arial} 两种（中文 + 英文），禁止 Georgia / Calibri / Cambria 混入
- 色板软约束写入 prompt（建议只用 preset 里声明的主题色），但批 1 QA 不做硬检查

### 4.4 QA fix-loop

现有 `qa_fix_round` 机制不变；fail 的 gate 写入 `qa_feedback`，repair_mode 再过 build。模型必须针对 gate 做定点修复，不能重建整套 deck。

### 4.5 Acceptance Criteria

- 重放 `fishing-benefits-for-men.pptx` 的 goal，生成的 deck 必须在 QA 被至少两个 gate 拦下（G1-font-family + G1-picture-threshold）。
- 现有所有 pass 的 case 必须继续 pass（在 `tests/test_office_domain.py` 和 `test_office_workflow_prompt.py` 增加 golden case）。
- `GroundTruthStats` 解析覆盖率：对 10 份历史 deck 的真值抽取，per_slide 字段不能有 `None`。

---

## 5. Batch 2 — Per-Slide Taxonomy + Style Preset + Vertical 澄清

### 5.1 Page taxonomy（借 MiniMax ppt-orchestra）

Plan schema 扩展，新增每 slide 字段：

```python
# agent/workflows/office/strategies/ppt.py::_build_slide
{
    "index": int,
    "title": str,
    "role": str,                    # 保留现有: cover|agenda|content|summary
    "page_type": str,               # 新: cover|toc|section_divider|content|summary
    "content_subtype": str | None,  # 新: text|mixed|data_viz|comparison|timeline|image_showcase
    "layout_type": str,             # 保留现有
    "takeaway": str,
    "visual_requirements": list[str],
    "requires_real_picture": bool,  # 新: 判定该页是否必须有真图 (picture_count >= 1)
    "max_text_blocks": int,         # 新: 该页 text_box_count 上限
    "typography_pair": dict,        # 新: {"header_font": ..., "body_font": ...}
    "theme_ref": str,               # 新: 关联到 style_preset 的某条主题色
    "transition_required": bool,
    "notes_required": bool,
}
```

`requires_real_picture=True` 的默认规则：

- `page_type == "cover"` → True
- `content_subtype in {image_showcase, timeline, comparison}` → True（timeline / comparison 在没有图时至少要有 chart）
- 其余页 → False（但受 deck 级 picture 门槛约束）

### 5.2 Per-slide QA

`evaluate_quality_stats` 增加 per-slide pass：

- 遍历 `GroundTruthStats.per_slide` 与 `plan.slides`
- 每页检查：
  - `slide.requires_real_picture == True and per_slide_stats.picture_count == 0` → 单页 fail
  - `per_slide_stats.text_box_count > slide.max_text_blocks` → 单页 fail
  - `per_slide_stats.font_families not ⊆ plan.typography_pair` → 单页 fail
- 单页 fail 进入 `qa_feedback`，`qa_fix_node` 把具体页号和违规原因传给 build repair

### 5.2.1 色板门槛是否在批 2 补（延后决策）

批 1 先跑一段时间，观察字体门槛拦截后剩余 case 中色板漂是否还是主要失败原因。若是，批 2 再补"使用中色板门槛"：扫所有 `ppt/slides/slide*.xml`，聚合 `srgbClr` + `schemeClr` 出现的颜色，去重后若超过 preset 允许的 5 种则 fail。若不是，批 2 不做该项，把成本留给模板 lane。判定在批 1 完成后、批 2 启动前做。

### 5.3 Style preset 系统

新增文件 `agent/workflows/office/style_presets.py`，定义 5 个 preset：

```python
STYLE_PRESETS: dict[str, StylePreset] = {
    "business_formal": StylePreset(
        name="business_formal",
        theme={"primary": "1F3864", "secondary": "2E75B6", "accent": "C55A11", "light": "F2F2F2", "bg": "FFFFFF"},
        typography=TypographyPair(header="Microsoft YaHei", body="Microsoft YaHei"),
        corner_radius=0.05,   # Sharp & Compact (借 MiniMax design-style)
        layout_rotation=["two-column", "cards-grid", "big-number", "timeline"],
        hero_style="shape_divider",
    ),
    "marketing": StylePreset(...),     # Rounded & Spacious, 暖色调
    "product_launch": StylePreset(...),# Pill & Airy, 高对比
    "course_training": StylePreset(...),# Soft & Balanced, 冷色调
    "lifestyle": StylePreset(...),      # Rounded & Spacious, 图片主导
}
```

Preset 用途：

1. 注入 `_OFFICE_SYSTEM` 作为视觉合同（"你本次只能用这 5 个 theme color、这 2 种字体"）
2. `build_plan` 使用 preset 的 `layout_rotation` 生成每页 `layout_type`
3. QA 用 preset 的 `theme` / `typography` 作为硬门槛的数据源

### 5.4 Vertical 推断 + 低置信度澄清（B 方案）

`GoalProfile` 扩字段：

```python
class GoalProfile(BaseModel):
    ...
    style_preset: Literal["business_formal","marketing","product_launch","course_training","lifestyle"] | None = None
    style_preset_confidence: GoalConfidence = "low"
```

`goal_normalizer.py::normalize_goal_profile` 增加 vertical inference 步骤：

- LLM prompt 扫描 goal 语义，输出 `style_preset` + `confidence`
- 关键词辅助：
  - "商务/汇报/季度/董事会" → business_formal (high)
  - "营销/推广/campaign/发布会" → marketing (high 或 product_launch)
  - "产品/发布/launch" → product_launch (high)
  - "培训/教程/课程/workshop" → course_training (high)
  - "生活/旅行/美食/钓鱼/健身" → lifestyle (high)
  - 命中多个或全无 → low

`preflight_node` 行为：

- `style_preset_confidence in {"high", "medium"}` → 直接用
- `style_preset_confidence == "low"` → 通过 `request_interrupt` 发澄清，placeholder: `"例如：商务汇报 / 营销推广 / 产品发布 / 培训课程 / 生活方式"`
- 用户回答被 re-normalize

### 5.5 Acceptance Criteria

- 5 个 preset 各生成一份 sample deck，所有 deck 通过批 1 + 批 2 所有 gate
- "钓鱼好处" goal 在 preflight 被判为 lifestyle (high) 或触发澄清；不再出现 Calibri/Georgia 混入
- Per-slide QA 的失败反馈能够在一次 repair round 内被模型修复（抽样 10 个 case，至少 7 个一次修复成功）

---

## 6. Batch 3 — Template-First Lane

### 6.1 路由触发条件

新增 `agent/workflows/office/template_router.py::decide_template_lane`。用用户的**显式信号**触发，不用启发式。

触发任一即走模板 lane：

- **用户自带模板**：`operation == "create"` 且 `reference_files` 中存在 `.pptx` 文件。直接用那份 .pptx 作模板。
- **用户点名项目内置模板**：goal 文本中命中内置模板名，例如 `{pitch deck 模板, pitch-deck, 营销模板, morph, morph-ppt, 路演模板}`。按命中的名字映射到对应 bundled skill 目录。

**其他所有情况**（纯文字 goal，无 reference，没命中内置模板名）→ blank lane（批 1 + 批 2 已改好的新管线）。

**边界说明**：`operation == "edit"` 走的是对 `source_files` 原地编辑的现有路径，不是模板 lane；模板 lane 只在 create 时触发。这两条路径在现有 workflow 中已分开，路由不会冲突。

### 6.2 模板 lane pipeline

新增 `strategies/ppt_template.py`，实现 `OfficeFormatStrategy` 协议。核心步骤（借 MiniMax ppt-editing-skill 的 sequencing）：

1. **Copy**：复制模板 .pptx 到目标文件名
2. **Plan mapping**：`content slide 需求` vs `template slide layouts`；输出"哪些 layout 用、哪些要删、哪些要复制"
3. **Structure phase**：通过 `officecli_batch` 先完成所有 `slide delete` / `slide duplicate` / `slide reorder`
4. **Content phase**：在结构稳定后，逐页填入 takeaway / bullet / picture（用 `list_user_images` 或 `image_gen`）
5. **Cleanup**：删除未用的 placeholder / orphaned media
6. **QA**：走批 1 + 批 2 的完整 gate 集合（**不绕过**）

硬规则（borrowed from MiniMax）：

- Structure phase 完成前禁止做任何文本替换
- 结构修改完成后 slide 数必须等于 plan 要求
- 模板原图在 replace 时必须走 `officecli edit ... --type picture --prop src=<new>`，不允许删除 + 新增（会丢坐标）

### 6.3 Bundled skill 接线

模板 lane 的 `build_plan` 会根据 `style_preset` 挑选对应 skill 目录作为 reference：

- `product_launch` → `skills/officecli/officecli-pitch-deck`
- `marketing` → `skills/officecli/morph-ppt`
- `business_formal` → `skills/officecli/officecli-presentation-quality`（作为 QA skill，不是模板）
- 其他 → blank lane

Skill 目录内容通过现有 `officecli_skill_loader` 注入 system prompt。

### 6.4 Acceptance Criteria

- 模板 lane 生成的 deck 同样通过批 1 + 批 2 的所有 gate（不许绕过）
- Structure → content 顺序违反时 planner 阶段直接失败（而不是运行时崩）
- 5 个 preset 各至少有一条模板 lane 成功路径

---

## 7. State / Schema 改动清单

| 文件 | 改动类型 | 说明 |
|---|---|---|
| `agent/workflows/office/core/state.py` | 扩字段 | 加 `ground_truth_stats: GroundTruthStats \| None`, `style_preset: str \| None`, `template_lane: bool` |
| `agent/workflows/office/goal_contract.py` | 扩字段 | `GoalProfile` 加 `style_preset`, `style_preset_confidence` |
| `agent/workflows/office/goal_normalizer.py` | 扩 prompt | vertical inference 步骤 |
| `agent/workflows/office/strategies/ppt.py` | 重写 `evaluate_quality_stats`、扩 slide schema、所有 gate | 单文件最大改动 |
| `agent/workflows/office/strategies/ppt_stats_reader.py` | 新增 | `view stats` / `view annotated` 解析器 |
| `agent/workflows/office/strategies/ppt_template.py` | 新增 | 批 3 的模板 lane |
| `agent/workflows/office/style_presets.py` | 新增 | 5 个 preset 定义 |
| `agent/workflows/office/template_router.py` | 新增 | 批 3 路由 |
| `agent/workflows/office/workflow.py` | 改 prompt、planning 路由 | |
| `tests/test_office_domain.py` | 增 case | 真值化 stats、每个 gate、preset 选择 |
| `tests/test_office_workflow_prompt.py` | 增 case | 图片硬要求的 prompt 段 |
| `tests/test_office_goal_normalizer.py` | 增 case | vertical inference |
| `tests/test_ppt_stats_reader.py` | 新增 | 真值解析覆盖率 |
| `tests/test_ppt_template_lane.py` | 新增 | 批 3 lane |

---

## 8. Risks & Resolved Decisions

### 风险

1. **`view stats` / `view annotated` 输出格式不稳定**。如果 officecli 升级改了字段名，解析器会静默漂。
   - 缓解：parser 里每个字段都打 debug log；CI 跑一份 golden deck 的真值对账。
2. **per-slide font detection 依赖 `view annotated`**。已在本 spec 确认该输出每个 Text Box 会标 `← <Font> <size>pt`，可逐页聚合字体家族。若 shape 是 picture/chart 不报字体，视为对门槛无贡献。
3. **Vertical inference 误判**。"钓鱼好处" 可能被推成 lifestyle 也可能被推成 course_training。
   - 缓解：B 方案的澄清 interrupt 接住 low confidence 情况。
4. **模板 lane 的 structure phase 失败会让用户等很久**。
   - 缓解：structure phase 限最多 2 轮，失败即退回 blank lane。
5. **Preset 主题色和用户上传图片色调不匹配**。
   - v1 不解决；用户不满可手动指定 preset。

### Resolved Decisions（取代原 Open Questions）

- **D1（原 Q1）—— 连续 layout 检测方案**：不依赖 officecli 报 slideLayout 名。自己算 `layout_signature` = 每页 annotated 输出的 shape 类型序列（如 `T-TB-TB-P`），相邻两页签名相同即 fail。零外部依赖。
- **D2（原 Q2）—— 色板门槛方案**：批 1 不做色板硬 gate。benchmark failure 的主因是字体漂，字体门槛（`≤ 2 family`）已覆盖大部分 case。批 1 上线后若观察到剩余 failure 中色板漂仍然显著，批 2 再补"使用中颜色数量上限"（扫 `ppt/slides/slide*.xml` 抽 `srgbClr` + `schemeClr`）。
- **D3（原 Q3）—— 模板 lane 触发条件**：用显式信号，不用启发式。`operation == "create"` 且 `reference_files` 含 `.pptx` → 用用户模板；或 goal 文本命中内置模板名 → 用对应 bundled skill；否则 blank lane。

---

## 9. Rollout

- **Batch 1 单独 merge**：改动面小、风险低、直接解决当前最严重的丑。
- **Batch 2 需要伴随 Batch 1**：preset 不单独有意义，必须和真值化 stats 一起。
- **Batch 3 独立**：路由默认 off，通过 config flag `OFFICE_PPT_TEMPLATE_LANE_ENABLED` 控制 v1 是否启用。

每批 merge 前：

- 全量单测通过
- `tests/test_office_domain.py` 的 golden decks 重新生成，人眼过一遍
- 批 1 需要 benchmark 10 个历史 goal 的 QA 拦截率（期望：历史上 30% 以丑结尾的 deck 在批 1 后至少被 1 个 gate 拦）

---

## 10. Appendix — Codex 建议 vs 本 spec

| Codex 提议 | 本 spec 处理 |
|---|---|
| picture_count 硬门槛 | 采纳（G1-picture-threshold）+ 真值化前置 |
| 视觉元素分级（decorative vs real） | 采纳（G1-decorative-only-cap + per-slide stats）|
| Page taxonomy（封面/目录/分节/内容/总结 + 内容子类）| 采纳（借 MiniMax 5+6 分类法）|
| 3-5 套模板 | 采纳但改形态（5 个 style preset + 现有 skill 接线，而非新建 5 个 .pptx）|
| 单页文本对象上限 / 重复标题 / 字体家族上限 / 连续 layout 检测 | 采纳（G1-* 系列）|
| 渲染后截图 QA | 明确拒绝（v1 不做）|
| 切 pptx-plugin / PptxGenJS | 明确拒绝 |

Codex 未提、本 spec 新增：

- QA 真值化（最关键的前置，其他 gate 的基础）
- 5 键 theme color 合同（借 MiniMax color-font-skill）
- Vertical 推断 + 低置信度 interrupt（批 2 的交互契约）
- Template lane 的 structure-before-content sequencing（借 MiniMax ppt-editing-skill）
