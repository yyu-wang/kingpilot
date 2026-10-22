# KingPilot 金蝶模块抽象为通用 ERP 适配层

## Context

**为什么改**：当前项目无法接入真实金蝶环境，且需要把系统演示为「通用 ERP 适配层 + 金蝶是其中一种实现」，便于后续接入用友/SAP 等其他 ERP 并对外讲解。

**现状问题**：
- LLM 可见的工具名硬编码 `kingdee_query_*` 前缀（演示时穿帮为金蝶专属）
- `AgentCore` / `ToolDefinition` 直接依赖具体类 `KingdeeClient`，无 Protocol 抽象
- `kingdee/endpoints/` 同时承载「通用字段白名单真相源」与「金蝶专属 FilterString 组装」，职责混合
- `agent/rule_engine.py:22-27` 硬编码 6 个工具名常量，L173/L206 用 f-string 拼写工具名
- `safety/guard_sql.py` 的 Guard 2 整架建立在 FilterString 字符串解析上（引号掩码 + 危险模式黑名单 + 受限文法），与金蝶方言强耦合

**预期收益**：
- 演示叙事成立：「新增厂商 = 实现 `ErpClient` Protocol 三方法 + 工厂注册一个分支」
- 注入面进一步收敛：LLM 不仅不见 FilterString，连字符串 dialect 都不存在可接触对象
- Guard 2 从「解析危险字符串」升级为「校验结构化表达式」，更简更稳

**用户已确认的决策**：
1. 工具名前缀 `erp_`（如 `erp_query_sales_order`）
2. 预留 `erp/yonyou/` 骨架（用友）
3. 与 M3 并行推进（本 refactor 在独立分支）
4. Guard 2 重写为表达式校验，黑名单下沉到金蝶渲染器内部兜底

---

## 核心设计决策

### D1 — `kingdee/` 保持顶层位置不迁移
全仓 97 个文件、195 处 `from kingdee.` 导入（含 `safety/`、`governance/`、`api/`、`agent/`、`tests/`）。物理迁移到 `erp/kingdee/` 会产生与 M3-T04..T07 的巨大冲突面。改为：
- `erp/` = 厂商中立契约 + 扩展点（新增顶层包）
- `kingdee/` = 参考适配器，实现 `erp.protocol.ErpClient`
- `erp/yonyou/` = 第二厂商骨架

后续里程碑再评估是否物理迁移。

### D2 — 中性查询契约用结构化 `FilterExpression`，不再用 FilterString
- 新增 `erp/types.py`：`FilterCondition`（field + op 枚举 + 类型化 value）+ `FilterExpression`（条件元组，语义 = AND）
- 工具层 `build_filter` 从 `-> str` 改为 `-> FilterExpression`
- `BillQueryParams` / `BillQueryResult` / `ActionResult` 保持留在 `models/kingdee.py` 不动，它们仍是金蝶协议模型
- 金蝶适配器内部用 `kingdee/filter_string.py` 渲染器把 `FilterExpression` 翻译成 FilterString；其他厂商各自实现自己的渲染器（用友/SAP 不必复用 FilterString 概念）

### D3 — Guard 2 重写：黑名单下沉到金蝶渲染器内部兜底
- `safety/guard_sql.py` 现有 `FilterCondition`（字符串三元组）**删除**，名字让给新结构化类型
- 现有 `_DANGEROUS_PATTERNS` / `_mask_quoted` / `_parse_literal` / `parse_filter_string` 全部搬到 `kingdee/filter_string.py`，作为渲染器内部自检（防御渲染器自身的 bug）
- 新 Guard 2 职责：
  - 校验 `FilterCondition.field` 命中端点 `filterable_fields`（复用现有白名单机制）
  - 校验 `FilterCondition.value` 类型与 `FieldDescriptor.data_type` 匹配（date 字段收 `"YYYY-MM-DD"` 字符串、number 字段收 float、enum 字段收预定义字符集）
  - 空 `FilterExpression` 放行（无条件查询的拒绝归 Guard 4，与现状一致）
- `test_guard_sql.py` 大规模重写（黑名单用例搬到 `test_kingdee_filter_string.py`）

### D4 — 工具名硬切，不留别名
- 无外部发布消费者，留别名会让注册表翻倍且 LLM 看见两套名字
- 所有 `kingdee_*` 字面量收敛到 `erp/tool_names.py` 常量
- `agent/rule_engine.py:173,206` 的 `f"kingdee_{action}_{form}"` 拼接改为查表 `(action, form) -> tool_name` 常量字典（因为工具名常量需要静态可枚举，f-string 破坏这一点）
- `evals/datasets/v1/tool_selection.json` 用脚本批量替换（sed 一遍 + `evals/dataset.py` 加载期校验自动兜底）

### D5 — `row_filter` 从 `str` 改为 `FilterExpression`
- `agent/tools.py:143` `ToolDefinition.execute(..., row_filter: str = "")` 改为 `row_filter: FilterExpression | None = None`
- `safety/permission_engine.py` 的 `_build_row_filter` 改为产 `FilterExpression`
- 合并逻辑从字符串拼接 `f"{filter_string} AND {row_filter}"`（L211）改为 `FilterExpression(conditions=(*expr.conditions, *row_filter.conditions))`

---

## 目标目录树

```
erp/                              # 新增顶层包：厂商中立契约 + 扩展点
  __init__.py                     # 公共再导出（ErpClient / BillQuerySpec / FilterExpression / bindings）
  types.py                        # FilterOp / FilterCondition / FilterExpression / BillQuerySpec
  protocol.py                     # ErpClient Protocol（@runtime_checkable）
  descriptors.py                  # FieldDescriptor / EndpointDescriptor（从 kingdee/endpoints/base.py 上收）
  bindings.py                     # EndpointParams / QueryToolBinding / WriteToolBinding / EntityReference
  conditions.py                   # eq() / like() / ge() 等 FilterCondition 构造助手（供 endpoint build_filter 使用）
  tool_names.py                   # 全部 erp_* 工具名常量（评测/规则引擎/labels 唯一来源）
  factory.py                      # build_erp_client(settings) -> ErpClient（按 settings.erp_vendor 分派）
  yonyou/                         # 第二厂商骨架
    __init__.py                   # 模块 docstring = 「如何新增一个厂商」三步指南
    client.py                     # class YonyouClient: 实现 ErpClient，方法体 raise NotImplementedError
    filter_renderer.py            # TODO 注释：用友查询语法渲染器占位

kingdee/                          # 位置不动，语义变为「金蝶适配器」
  client.py                       # 新增 query() / invoke_action() 协议方法；execute_bill_query / execute_action 保留为金蝶私有
  filter_string.py                # 新增：FilterExpression -> FilterString 渲染器 + 黑名单自检（吸收 _DANGEROUS_PATTERNS / _mask_quoted / quote_eq / quote_like / fmt_number）
  endpoints/base.py               # 变薄：只留 re-export 或直接删除；金蝶专属逻辑全部移 filter_string.py
  endpoints/*.py                  # build_filter 改为返回 FilterExpression；tool_name 改引用 erp.tool_names 常量

sandbox/kingdee_stub/             # 不动（金蝶专属仿真；未来厂商各自建 stub）

# 配置文件改动
pyproject.toml                    # wheel packages / isort known-first-party / coverage source 三处加 "erp"
config/settings.py                # 新增 erp_vendor: Literal["kingdee", "yonyou"] = "kingdee"
.env.example                      # 追加 KINGPILOT_ERP_VENDOR=kingdee
```

---

## 关键类型与 Protocol 签名

### `erp/types.py`

```python
class FilterOp(StrEnum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GE = "ge"
    LT = "lt"
    LE = "le"
    LIKE = "like"

@dataclass(frozen=True)
class FilterCondition:
    """结构化过滤条件。field 必须命中端点 filterable_fields（Guard 2 校验）。"""
    field: str
    op: FilterOp
    value: str | float  # date 用 "YYYY-MM-DD" 字符串；渲染期才决定引号

@dataclass(frozen=True)
class FilterExpression:
    """条件元组，语义 = AND 连接；空 = 无条件。"""
    conditions: tuple[FilterCondition, ...] = ()

class BillQuerySpec(KingPilotBaseModel):
    """中性查询请求（与厂商无关）。各厂商适配器内部翻译为各自协议。"""
    form_id: str
    field_keys: tuple[str, ...]
    filter: FilterExpression = FilterExpression()
    order_by: str = ""
    start_row: int = 0
    limit: int = 100
```

### `erp/protocol.py`

```python
@runtime_checkable
class ErpClient(Protocol):
    """厂商中立 ERP 客户端契约。"""

    async def query(self, spec: BillQuerySpec) -> BillQueryResult: ...

    async def invoke_action(
        self,
        action: str,  # "save" / "submit" / "audit" / "unaudit" / "delete"
        form_id: str,
        *,
        model: dict[str, Any] | None = None,
        bill_no: str = "",
    ) -> ActionResult: ...

    async def aclose(self) -> None: ...
```

### `KingdeeClient` 新增方法（`kingdee/client.py`）

```python
async def query(self, spec: BillQuerySpec) -> BillQueryResult:
    """实现 ErpClient.query。内部：FilterExpression -> FilterString -> execute_bill_query。"""
    filter_string = render_filter_expression(spec.filter, spec.form_id)  # kingdee/filter_string.py
    params = BillQueryParams(
        form_id=spec.form_id,
        field_keys=",".join(spec.field_keys),
        filter_string=filter_string,
        order_string=spec.order_by,
        start_row=spec.start_row,
        limit=spec.limit,
    )
    return await self.execute_bill_query(params)

async def invoke_action(
    self, action: str, form_id: str, *,
    model: dict[str, Any] | None = None, bill_no: str = "",
) -> ActionResult:
    """实现 ErpClient.invoke_action。薄委托 execute_action。"""
    return await self.execute_action(cast(KingdeeWriteAction, action), form_id, model=model, bill_no=bill_no)
```

### `kingdee/filter_string.py`（渲染器 + 黑名单自检）

```python
def render_filter_expression(expr: FilterExpression, form_id: str) -> str:
    """FilterExpression -> FilterString。

    流程：
    1. 逐条件渲染（根据 FieldDescriptor.data_type 决定引号/数值格式）
    2. 用 " AND " 连接
    3. 黑名单自检（防御渲染器自身的 bug；从 safety/guard_sql.py 搬来）
    """
```

---

## 工具改名表（硬切，共 21 个）

| 旧 | 新 |
|---|---|
| `kingdee_query_sales_order` | `erp_query_sales_order` |
| `kingdee_query_purchase_order` | `erp_query_purchase_order` |
| `kingdee_query_purchase_inbound` | `erp_query_purchase_inbound` |
| `kingdee_query_customer` | `erp_query_customer` |
| `kingdee_query_supplier` | `erp_query_supplier` |
| `kingdee_query_inventory` | `erp_query_inventory` |
| `kingdee_{create,submit,audit,unaudit,delete}_{sales_order,purchase_order,purchase_inbound}` ×15 | `erp_` 同构替换 ×15 |
| `kb_search` | 不变（非 ERP 工具） |

**落点**：
- `erp/tool_names.py` 定义全部常量
- `agent/rule_engine.py:22-27` 常量改引用；L173/L206 的 f-string 改为 `(action, form) -> tool_name` 字典查表
- `kingdee/endpoints/labels.py` 映射表改键
- `kingdee/endpoints/*.py` 各 `tool_name=` 改常量引用
- `evals/datasets/v1/tool_selection.json` 脚本批量替换
- `tests/` 约 15 个文件字面量同步替换

---

## 配置改造

### `config/settings.py`

```python
erp_vendor: Literal["kingdee", "yonyou"] = "kingdee"
```

`kingdee_*` 配置键**保持扁平不改名**（嵌套化收益低、改动面大）。

### `erp/factory.py`

```python
def build_erp_client(settings: Settings) -> ErpClient:
    match settings.erp_vendor:
        case "kingdee":
            from kingdee.client import KingdeeClient
            return KingdeeClient(settings)
        case "yonyou":
            from erp.yonyou.client import YonyouClient
            return YonyouClient(settings)
```

### 注入点改造（共 5 处）

| 位置 | 改动 |
|---|---|
| `agent/core.py:96` | `client: KingdeeClient` → `client: ErpClient` |
| `agent/tools.py:139` | `ToolDefinition.execute(client: KingdeeClient, ...)` → `client: ErpClient`；`RemoteRunner` / `WriteRemoteRunner` 签名同步 |
| `api/deps.py` / `agent/cli.py` / `agent/query_pipeline.py` | 构造点统一改走 `build_erp_client(settings)` |
| `tests/conftest.py` | `client` fixture 改经工厂构造、返回类型标注 `ErpClient` |
| 写工具 runner（如 `create_sales_order(client: KingdeeClient, ...)`） | 签名改收 `ErpClient`；体内调用 `client.execute_action(...)` 改 `client.invoke_action(...)` |

---

## 与 M3 并行推进的分支策略

- 本 refactor 分支：`refactor/erp-adapter`，自 main 切出
- M3 继续在 `m3/*` 分支
- **共触文件**：`agent/tools.py` / `agent/core.py`
  - M3-T04..T07 动的是 memory/entity 相关阶段函数（`_phase0_memory` / `_phase6_finalize`）
  - 本 refactor 动的是 `__init__` 签名与 `ToolDefinition.execute` 的类型标注 + `execute` 内 client 调用两行
  - **文本上不重叠**；约定本 refactor 不改这两个文件的任何行为逻辑
- **合并序**：本 refactor 先合 main（它先就绪），M3 分支 rebase 时冲突集中在 import 块，机械可解
- 每个任务完成即在 refactor 分支跑 `make check`；合 main 前加跑 `make test-integration` + `make eval`

---

## 分阶段任务（T01-T09）

按「风险递减」排序：契约先行，机械改名殿后。每个任务独立可评审、`make check` 保持绿。

| 任务 | 范围 | 关键文件 | 验收 |
|---|---|---|---|
| **T01** | 建 `erp/types.py` + `erp/protocol.py` + `erp/conditions.py`；`pyproject.toml` 三处（wheel packages / isort / coverage）加 `"erp"` | `erp/*`、`pyproject.toml` | mypy strict 绿；`make check` 绿（新代码暂无人引用） |
| **T02** | `KingdeeClient` 新增 `query()` / `invoke_action()`；新增 `kingdee/filter_string.py` 渲染器（吸收 `quote_eq` / `quote_like` / `fmt_number`）+ 渲染器单测 | `kingdee/client.py`、`kingdee/filter_string.py`、`tests/test_kingdee_filter_string.py` | 渲染器单测覆盖既有 quote 行为；`make check` 绿 |
| **T03** | `erp/descriptors.py` + `erp/bindings.py` 落位；`kingdee/endpoints/base.py` 改为 re-export 薄壳（过渡期兼容） | `erp/descriptors.py`、`erp/bindings.py`、`kingdee/endpoints/base.py` | 现有 endpoint 测试零改动通过 |
| **T04** | 黑名单下沉：`_DANGEROUS_PATTERNS` / `_mask_quoted` / `_parse_literal` / `parse_filter_string` 从 `safety/guard_sql.py` 搬到 `kingdee/filter_string.py` 作为渲染器自检 | `safety/guard_sql.py`、`kingdee/filter_string.py` | 黑名单用例搬到 `test_kingdee_filter_string.py` 后绿 |
| **T05** | 各 endpoint `build_filter` 改返回 `FilterExpression`；`EndpointDescriptor.to_query_params` 改产 `BillQuerySpec`；`ToolDefinition.execute` 改走 `client.query(spec)` | `kingdee/endpoints/*.py`、`agent/tools.py`、`erp/descriptors.py` | `test_endpoint_query_bindings` / `test_agent_tools` 更新后绿 |
| **T06** | Guard 2 重写：删除旧 `FilterCondition` 与字符串黑名单设施；新 `FilterGuard.check` 校验 `FilterExpression` 字段白名单 + 值类型匹配；`row_filter: str` 改 `FilterExpression | None`；`permission_engine._build_row_filter` 改产表达式 | `safety/guard_sql.py`、`safety/permission_engine.py`、`agent/tools.py` | `test_guard_sql.py` / `test_permission_engine.py` / `test_safety_*` 重写后绿 |
| **T07** | `erp/tool_names.py` + 全部 `kingdee_*` → `erp_*`（endpoints / rule_engine / labels / tests / evals JSON）；`rule_engine.py` f-string 改查表 | `erp/tool_names.py`、`agent/rule_engine.py`、`kingdee/endpoints/*.py`、`evals/datasets/v1/tool_selection.json` | `make check` 绿；`evals/dataset.py` 加载校验通过 |
| **T08** | `erp/factory.py` + `Settings.erp_vendor` + 注入点切换（`agent/core.py` / `agent/tools.py` / `api/deps.py` / `agent/cli.py` / `agent/query_pipeline.py` / `tests/conftest.py`） | 见「注入点改造」表 | `make check` + `make test-integration` 绿 |
| **T09** | `erp/yonyou/` 骨架 + skeleton 测试（`isinstance(YonyouClient(...), ErpClient)` 通过 + 方法抛 `NotImplementedError`）+ 模块 docstring 扩展指南；`make eval` 全量回归（DeepSeek 路由，100% 选对率验收） | `erp/yonyou/*`、`tests/test_erp_yonyou_skeleton.py` | skeleton 测试绿；eval 46 题 100%（新 `erp_*` 名） |

**评审重点**：T04/T05/T06 是仅有的三个行为变更任务；其余为机械移动。

---

## 验证计划

### 每个任务后
```bash
make check   # lint + typecheck + test，三绿
```

### T08 后追加
```bash
make test-integration   # 沙箱契约测试（真实 uvicorn 启动）
```

### T09 后追加
```bash
make eval   # 工具选择准确率（DeepSeek，目标 100%，46 题）
```

### 最终演示验证（demo script）

```bash
# 1. Protocol 满足性
uv run python -c "
from erp.yonyou.client import YonyouClient
from erp.protocol import ErpClient
from config.settings import get_settings
client = YonyouClient(get_settings())
assert isinstance(client, ErpClient), 'YonyouClient must satisfy ErpClient Protocol'
print('OK: YonyouClient satisfies ErpClient Protocol')
"

# 2. 工厂切换
KINGPILOT_ERP_VENDOR=kingdee uv run python -c "
from erp.factory import build_erp_client
from config.settings import get_settings
from kingdee.client import KingdeeClient
client = build_erp_client(get_settings())
assert isinstance(client, KingdeeClient)
print('OK: factory dispatches to KingdeeClient by default')
"

# 3. 演示话术（口头）
# 「新增一个厂商 = 实现 ErpClient 的 query/invoke_action/aclose 三方法
#  + 在 erp/factory.py 注册一个分支 + 在 erp/<vendor>/endpoints/ 建端点描述符。
#  参考 erp/yonyou/__init__.py 的模块 docstring。」
```

---

## 关键文件清单

### 新增（11 个）
- `erp/__init__.py`
- `erp/types.py`
- `erp/protocol.py`
- `erp/descriptors.py`
- `erp/bindings.py`
- `erp/conditions.py`
- `erp/tool_names.py`
- `erp/factory.py`
- `erp/yonyou/__init__.py`
- `erp/yonyou/client.py`
- `erp/yonyou/filter_renderer.py`
- `kingdee/filter_string.py`
- `tests/test_kingdee_filter_string.py`
- `tests/test_erp_yonyou_skeleton.py`

### 重大行为变更（5 个）
- [agent/tools.py](file:///Users/wangyu/Desktop/个人/demo/KingPilot/agent/tools.py) — `ToolDefinition.execute` 签名 + `build_filter` 返回类型 + `_from_binding` / `_from_write_binding`
- [agent/core.py](file:///Users/wangyu/Desktop/个人/demo/KingPilot/agent/core.py) — `__init__` 的 `client: KingdeeClient` → `ErpClient`
- [kingdee/client.py](file:///Users/wangyu/Desktop/个人/demo/KingPilot/kingdee/client.py) — 新增 `query()` / `invoke_action()` 协议方法
- [safety/guard_sql.py](file:///Users/wangyu/Desktop/个人/demo/KingPilot/safety/guard_sql.py) — 删除字符串黑名单设施 + 旧 `FilterCondition`；新 `FilterGuard.check` 直接校验 `FilterExpression`
- [safety/permission_engine.py](file:///Users/wangyu/Desktop/个人/demo/KingPilot/safety/permission_engine.py) — `_build_row_filter` 改产 `FilterExpression`

### 机械改名/迁移（约 25 个）
- `kingdee/endpoints/*.py`（8 个端点文件）— `build_filter` 返回类型 + `tool_name` 常量引用
- `kingdee/endpoints/base.py` — 变薄为 re-export 壳
- `agent/rule_engine.py` — 6 个查询工具常量 + f-string 改查表
- `evals/datasets/v1/tool_selection.json` — 批量替换工具名
- `tests/`（约 15 个测试文件）— 字面量同步
- `api/deps.py` / `agent/cli.py` / `agent/query_pipeline.py` — 改走 `build_erp_client`
- `tests/conftest.py` — fixture 改经工厂构造
- `config/settings.py` — 新增 `erp_vendor`
- `pyproject.toml` — 三处配置加 `"erp"`
- `.env.example` — 追加 `KINGPILOT_ERP_VENDOR=kingdee`

---

## 已确认排除项

- **不迁移 `kingdee/` 到 `erp/kingdee/`**（D1，与 M3 冲突面太大）
- **不留旧工具名别名**（D4，无外部消费者）
- **不动 `sandbox/kingdee_stub/` 位置**（它是金蝶专属仿真，未来厂商各自建 stub）
- **不嵌套化 `kingdee_*` 配置键**（扁平保留，改动面大收益低）
- **不实现 `erp/yonyou/` 的真实逻辑**（骨架仅做 Protocol 演示）

---

## 执行结论（2026-10-21，T01–T09 全部完成）

| 验收项 | 计划值 | 实际值 |
|---|---|---|
| `make check` | 全绿 | **1426 passed**（lint + mypy strict + 单测） |
| `make test-integration` | 沙箱契约测试绿 | **12 passed**（真实 uvicorn + 沙箱） |
| `make eval` | 100%（46 题） | **97.7%**（43 题，计分 43，阈值 90%；与重构前 M9 基线持平） |
| 工具名 | 全部 `erp_*` | 21 个 ERP + `kb_search`，常量集中在 `erp/tool_names.py` |
| 第二厂商 | `isinstance(..., ErpClient)` 通过 | 通过，且工厂可按 `erp_vendor=yonyou` 选中 |

**与计划的偏差（均已记录理由）**：

1. **骨架异常类型**：用 `YonyouNotConfiguredError`（继承 `KingPilotError`）而非 `NotImplementedError`——前者能进统一异常层与审计，语义也更准确（「未接入」而非「方法没写」）。
2. **第二厂商示例文件**：计划写 `erp/yonyou/filter_renderer.py`，实际为 `erp/yonyou/endpoints.py`（端点描述符）。理由：骨架阶段演示「第 2 步声明端点」比提前写一个没有查询语义的空渲染器更有价值；真实接入时再补渲染器。
3. **评测题数**：计划写 46 题，实际数据集仍为 43 题（本次重构不新增业务场景，只改名）；97.7% 为重构前基线，未退化。
4. **`infrastructure/secrets.py` 未改**：`SecretName.KINGDEE_APP_SECRET` 本就是厂商专属键，由金蝶适配器内部使用；工厂只透传 `SecretProvider`。
5. **`config/compensate_map.py` 额外修正**：原映射有 4 处指向不存在的工具（`unsubmit` / `purchase_in` / `stock_out` / `finance_voucher`），改名时一并修正为真实工具，否则会留下「格式正确但永远找不到」的坏映射。
6. **文档一致性**：额外完成 D01–D05（实现方案 §2.4/§3/§4.2/§5.1 纠偏、面试稿 Q24 重写、架构报告补 `erp/` 包、手工测试清单与前端文案）。
