# 文档一致性与缺陷修复计划（ERP 适配层配套）

## Context

T01–T06 已把 `kingdee/` 抽象为 `erp/` 契约 + 金蝶适配器，`build_filter` 从返回 FilterString 字符串改为返回结构化 `FilterExpression`，Guard 2 从「字符串受限文法解析」重写为「结构化条件白名单 + 值类型校验」。

但三份对外材料仍按**旧架构**描述，且不是「陈旧」而是**讲反了**：

| 材料 | 错误表述 | 与现状的关系 |
|---|---|---|
| `docs/企业级Agent实现方案.md` §3.1/§3.2/§3.3 | `FilterString` 是 LLM 直接填写的工具参数 | 完全相反：LLM 只见结构化字段，FilterString 由适配器内部渲染 |
| 同上 §4.2 | Tool Schema 示例含 `FilterString` 且为 required | 现状参数模型里根本不存在该字段 |
| 同上 §5.1 支柱二 | Guard 2 = FilterString AST 解析 + SQL 黑名单 | 已重写；黑名单下沉到渲染器自检，Guard 不再接触字符串 |
| `kingpilot-architecture.html` L1303/L1884 | Guard 2 做 FilterString 受限文法校验 | 同上 |
| `kingpilot-architecture.html` 包表 | **没有 `erp/` 包** | 整条适配层主线在架构图里不存在 |
| `kingpilot-agent-interview.html` Q24 | 整题答案讲「受限文法 + 引号掩码」 | 该方案已被更彻底的方案取代（不产生字符串），答案需换 |

这直接伤害两个场景：**演示时被追问会自相矛盾**，**面试稿背的是已废弃的实现**。

处置原则：
1. **活文档与演示物必须与代码一致**；历史存档不回改（避免伪造历史）。
2. 涉及工具名的改动依赖 T07，不在本轮；涉及语义的改动可立即做。
3. 代码内注释随 T07 顺带清理，不单独成任务。

---

## 任务拆分

### D01 方案文档纠偏（`docs/企业级Agent实现方案.md`）✅ 已完成

**前置依赖**：无（本文档不含真实工具名，仅含 `query_sales_orders` 这类示意名）。

| 位置 | 现状 | 改为 |
|---|---|---|
| §三 标题（L276） | 「自然语言到**金蝶** API 的映射设计」 | 「自然语言到 ERP 查询的映射设计」 |
| §3.1（L280-289） | 直接给金蝶参数结构，未说明谁能产生 | 标题改「厂商查询协议参数（以金蝶 ExecuteBillQuery 为例）」，正文加一句：**这是适配器内部形态，LLM 不可见** |
| §3.2 Step 3（L309-315） | 输出含 `FilterString` 的 API 参数 | 输出中性 `BillQuerySpec`：`form_id` + `field_keys` + `filter: FilterExpression` + `limit` |
| §3.3 规则 2（L326） | 「FilterString 使用 SQL WHERE 语法，字段名参考工具描述」 | 「只填结构化字段（单号/编码/日期/金额区间）；禁止输出任何查询方言字符串」 |
| §3.3 规则 3（L327-331） | 教 LLM 写 `FDate >= CURDATE()` 这类 SQL 表达式 | 改为「日期只填 `YYYY-MM-DD` 字符串，范围查询由端点参数模型决定」 |
| §4.2 Tool 示例（L369-392） | `FilterString` 为 required 参数 | 换成真实参数模型形态：`bill_no` / `customer_number` / `min_amount` / `order_date` / `limit`（1-100），并注明「Schema 由 `model_json_schema()` 生成」 |
| §4.2 末尾（L394） | 5 个示意工具名 | 改为指向 `erp/tool_names.py` 与当前 21 个 ERP 工具 + 1 个知识工具 |
| §5.1 支柱二流程（L489） | 「Guard 2: FilterString AST 解析 —— 字段白名单 / 防全表 / 禁危险模式」 | 「Guard 2: FilterExpression 校验 —— 字段白名单 / 值类型匹配 / LIKE 限制」 |
| §5.1 FilterString 实现（L497-535） | 整段 `sqlparse` + 黑名单代码 | 替换为新实现：构造期单引号 invariant（`erp/conditions.py`）→ Guard 2 白名单 + 类型匹配（`safety/guard_sql.py`）→ 金蝶渲染器内部黑名单自检（`kingdee/filter_string.py`）三层，并说明「纵深防御，各层职责不重叠」 |
| 新增 §2.x | 无 | 「ERP 适配层」小节：`erp/` 契约（`ErpClient`/`BillQuerySpec`/`FilterExpression`/`EndpointDescriptor`）、`kingdee/` 参考适配器、**新增厂商三步**（实现 `ErpClient` 三方法 → 声明端点描述符 → 工厂注册） |

**验收**：全文 grep 不再出现「LLM 输出/填写 FilterString」「Guard 2 解析 FilterString」类表述；§4.2 的 Schema 示例能直接对上 `kingdee/endpoints/sal_order.py` 的 `SalesOrderParams`；新增小节读完能照着接一个新厂商。

---

### D02 架构报告更新（`kingpilot-architecture/kingpilot-architecture.html`）✅ 已完成（含 T07 后的工具表）

**前置依赖**：工具名部分依赖 T07；语义与结构部分可立即做。

**语义/结构（现在做）**
- L984-986 端点节点：「金蝶端点描述符」→「ERP 端点描述符（金蝶适配）」；chips 的 `FilterString` → `FilterExpression`，并补 `erp/descriptors.py`
- L1301-1305 Guard 2 卡片：改为「对工具组装出的结构化条件做字段白名单与值类型校验；数值/日期/LIKE 的类型误配直接拦截；危险模式黑名单下沉到适配器渲染器」
- L1337 行级权限行：「执行前追加到 FilterString」→「按 AND 语义合并进 FilterExpression，在 Guard 之后注入」
- L1745 前后包表：**新增 `erp/` 行**（契约 + 条件助手 + 端点描述符 + 工具绑定 + 工厂），`kingdee/` 职责改为「金蝶参考适配器：签名客户端、FilterString 渲染、幂等/Saga/快照」
- 8.5 文件索引（L1771 附近）：**新增 `erp/` 折叠块**，逐文件列出 `types.py` / `protocol.py` / `conditions.py` / `descriptors.py` / `bindings.py`
- L1884 `guard_sql.py` 描述：「FilterString 受限文法 + SQL 黑名单 + 字段白名单」→「FilterExpression 字段白名单 + 值类型匹配」
- L2008 沙箱 `filter_parser.py`：「与 Guard 2 形成双重拒绝」→「校验适配器渲染出的 FilterString（渲染器自检的对外兜底）」
- L2182 Boundary 01：保留「LLM 永远不接触 FilterString」，补一句「且 LLM 侧根本不产生字符串——工具层产出结构化条件，方言只在适配器边界出现」
- L2261-L2285 工具表：21 个 ERP 工具名统一改 `erp_*`（**T07 后做**）

**统计数字（现在做，用脚本重算）**
- 包表的模块数/行数、`22 tools`、`tests/` 文件数与行数均已过时
- 做法：写一次性统计脚本（不入库）→ 用真实数值替换

**验收**：架构图里能看出「LLM → 工具层 → erp 契约 → 厂商适配器 → ERP」这条链路；包表数值与仓库实际一致；Guard 2 描述与 `safety/guard_sql.py` 的 docstring 一致。

---

### D03 面试稿纠偏与叙事升级（`kingpilot-agent-interview/kingpilot-agent-interview.html`）✅ 已完成

**前置依赖**：无（本节不含真实工具名）。

这是**收益最高**的一处：Q24 现在的答案讲的是已被取代的方案，而它的「边界」段落最后一句正好写着「更彻底的做法是把 FilterString 解析成 AST，再按白名单渲染」——现在的实现比它更进一步（**根本不产生字符串**）。这是一个天然的加分升级点。

| 位置 | 改动 |
|---|---|
| Q24 标题（L1168） | 「FilterString 校验为什么不用 sqlparse？」→「注入防护为什么最终不做字符串校验？」 |
| Q24 结论（L1173） | 「定义受限文法 + 引号掩码」→「三重收敛：构造期不产生危险值 → Guard 只校验结构化条件 → 方言渲染器自检」 |
| Q24 要点（L1174-1179） | 重写为：①`FilterCondition` 只有 field/op/value 三要素，无语法表达力 ②构造期拒绝单引号（不等到渲染） ③Guard 校验字段白名单 + 值类型（数值字段不收字符串、LIKE 不可用于数值、日期必须 `YYYY-MM-DD`） ④黑名单下沉渲染器，只防自身 bug ⑤「可查 ≠ 可过滤」两套字段集 |
| Q24 边界（L1181） | 更新为：LIKE 的 `%`/`_` 仍可扩大匹配范围（非注入）；白名单需随端点维护；**升级路径**——从「解析字符串」到「不产生字符串」，注入面在类型层消失 |
| Q24 bonus（L1182） | 保留（结论依然成立且更有力） |
| Q14 要点（L984） | 「端点层 `build_filter` 负责拼 FilterString」→「返回 `FilterExpression`；FilterString 只在金蝶适配器内部渲染」 |
| Q14 边界（L988） | 「依赖权限引擎和 `quote_eq` 拒绝单引号」→「依赖权限引擎构造 + 构造期 invariant；行过滤按 AND 合并进表达式而非拼字符串」 |
| L736 / L789 / L1482 | 表述微调，与上述口径一致 |

**验收**：Q24 的新答案能对应到真实文件与函数（`erp/conditions.py::_check_value`、`safety/guard_sql.py::_check_value_type`、`kingdee/filter_string.py`）；Q14 与 Q24 口径一致，不出现「解析字符串」作为主防线的说法。

---

### D04 开发计划追加 M10 与历史标注（`docs/开发计划.md`）✅ 已完成

**前置依赖**：无（M10 看板可先建，状态如实标 pending）。

- 在 M9 小结之后新增 **M10：ERP 适配层重构** 小节，落 T01–T09 看板与验收基线（T01–T06 已完成，T07–T09 待做）
- M10 小结记录核心决策：`erp/` 顶层新增不迁移 `kingdee/`、工具名 `erp_` 前缀、Guard 2 重写、`row_filter` 表达式化
- L489（M4-T03）、L494（M4-T05）验收描述已过时：**不回改原文**，改为在其下加一行引用块「> 本节描述的实现已于 M10-T05 被结构化表达式校验取代，见 §M10」
- 变更记录追加一行 v2.15，记录本次文档一致性修复

**验收**：`docs/开发计划.md` 能查到 M10 看板；M4 任务卡与现状的差异有明确指针，不产生「文档自相矛盾」。

---

### D05 流程交互文件与前端文案（T07 后执行）✅ 已完成

**前置依赖**：T07（工具名切换）。先改文档会导致文档比代码超前。

| 文件 | 改动 |
|---|---|
| `docs/全流程手工测试提问清单.md` L110 / L361 | 工具名 → `erp_query_sales_order`；「金蝶响应」→「ERP 响应」 |
| 同上 L49 | `KINGPILOT_KINGDEE_*` 说明补「`ERP_VENDOR=kingdee` 时生效」 |
| `docs/WebUI-全流程测试问题清单.md` L113 / L319 | `kingdee_query_*` → `erp_query_*` |
| 同上 L18 | 「未配置金蝶凭证」→「未配置当前 ERP 厂商凭证」 |
| `api/static/index.html` L14 | 「企业级金蝶 ERP Agent 控制台」→「企业级 ERP Agent 控制台」 |
| `api/static/admin/admin.js` L530 | 「金蝶响应」→「ERP 响应」 |

**验收**：两份手工测试清单可直接照着念且与 LLM 实际可见工具名一致；两个页面不出现厂商名（除非上下文明确是金蝶适配器）。

---

### 随 T07 顺带清理的代码注释（不单独成任务）

| 文件 | 过时内容 |
|---|---|
| `governance/admin_console.py` L4/L8 | 仍写 `KingdeeClient.execute_bill_query`、`原始 FilterString` |
| `models/kingdee.py` L49 | 「M4 Guard 2（AST 白名单）」 |
| `kingdee/endpoints/base.py` L13-15 | re-export 过渡期说明，T07 后应删除 |
| `kingdee/endpoints/sal_order.py` L135 | 「结构化参数 + FilterString 组装」→「结构化参数 + FilterExpression 组装」 |
| `erp/types.py` L78 | `BillQueryParams + FilterString` → 明确「由适配器渲染为厂商方言」 |

---

## 执行顺序

```
现在：D01（方案文档）→ D03（面试稿）→ D04（开发计划）→ D02 语义/结构/统计
 ↓
T07 工具名切换（代码）
 ↓
D02 工具名部分 + D05（清单与前端）+ 代码注释清理
 ↓
T08 工厂化 → T09 用友骨架 → 文档补工厂/用友章节
```

理由：D01/D03/D04 不含真实工具名，先做能立刻消除「讲反了」的矛盾；D02/D05 的工具名部分必须等 T07，否则文档说完 `erp_*` 而代码仍是 `kingdee_*`。

---

## 明确不做

- **不回改历史存档**：`docs/M9-T01..T13-详细设计.md`、`docs/回归报告-M8.md`。它们是当时的事实记录，改了反而失真；只在开发计划里给一条指针。
- **不做工具名以外的重命名**：`kingdee_id`（记忆实体字段）、`trace.kingdee_api_calls` 属跨模块数据契约，另立任务评估。
- **不重写文档结构**：只做纠偏与增补，保持原有章节编号与阅读顺序，降低 diff 噪音。

---

## 总验收

| 检查 | 方法 |
|---|---|
| 无「LLM 产出 FilterString」类表述 | `grep -rn "LLM 输出.*FilterString\|FilterString 使用 SQL" docs/ kingpilot-architecture/ kingpilot-agent-interview/` 零命中 |
| 架构图含适配层 | 架构 HTML 中出现 `erp/` 包行与折叠块 |
| 面试稿可对照代码 | Q24 提到的每个机制都能在仓库找到实现 |
| 开发计划自洽 | M10 看板存在；M4 过时描述有 superseded 指针 |
| 手工清单可照念 | 清单里的工具名与 `build_default_registry()` 实际输出一致 |
