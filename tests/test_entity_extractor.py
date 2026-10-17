"""M3-T04：实体抽取器 + 实体图查询接口单测（零网络）。

覆盖：
- 各表单抽取：客户/供应商/订单/物料，id+name+attributes 正确
- 跨实体：SAL_Order 一行同时产出 order + customer
- 跳过规则：id 和 name 都空不建实体；未知 form_id 返回空
- 查询接口：find_entity（id/名称）、entities_mentioned_after、entity_anchors
"""

from agent.entity_extractor import extract_entities
from agent.memory import MemoryStore
from models.memory import EntityRef

# ── 抽取器 ──


def test_extract_customer_from_bd_customer() -> None:
    """BD_Customer：id=FNumber、name=FName、FShortName 入 attributes。"""
    result = {
        "form_id": "BD_Customer",
        "field_keys": ["FNumber", "FName", "FShortName"],
        "rows": [{"FNumber": "CUS001", "FName": "测试客户", "FShortName": "测试"}],
    }
    entities = extract_entities(result, round_no=3)
    assert len(entities) == 1
    assert entities[0] == EntityRef(
        entity_type="customer",
        display_name="测试客户",
        kingdee_id="CUS001",
        last_mentioned_in_round=3,
        attributes={"FShortName": "测试"},
    )


def test_extract_supplier_from_bd_supplier() -> None:
    result = {
        "form_id": "BD_Supplier",
        "field_keys": ["FNumber", "FName"],
        "rows": [{"FNumber": "SUP001", "FName": "供应商A"}],
    }
    entities = extract_entities(result, round_no=1)
    assert entities[0].entity_type == "supplier"
    assert entities[0].kingdee_id == "SUP001"
    assert entities[0].display_name == "供应商A"


def test_sal_order_yields_order_and_customer() -> None:
    """SAL_Order 一行同时产订单实体 + 关联客户实体（跨实体抽取的核心价值）。"""
    result = {
        "form_id": "SAL_Order",
        "field_keys": ["FBillNo", "FCustomerID.FNumber", "FCustomerID.FName", "FDate"],
        "rows": [
            {
                "FBillNo": "SO000008",
                "FCustomerID.FNumber": "CUS001",
                "FCustomerID.FName": "测试客户",
                "FDate": "2026-03-05",
            }
        ],
    }
    entities = extract_entities(result, round_no=2)
    types = {e.entity_type for e in entities}
    assert types == {"order", "customer"}
    order = next(e for e in entities if e.entity_type == "order")
    assert order.kingdee_id == "SO000008"
    assert order.attributes["FDate"] == "2026-03-05"
    customer = next(e for e in entities if e.entity_type == "customer")
    assert customer.kingdee_id == "CUS001"
    assert customer.display_name == "测试客户"


def test_skip_row_when_id_and_name_both_empty() -> None:
    """id 和 name 字段都取不到值 → 不建实体（无法锚定）。"""
    result = {
        "form_id": "BD_Customer",
        "field_keys": ["FNumber", "FName"],
        "rows": [{"FNumber": None, "FName": ""}, {"FNumber": "CUS002", "FName": "客户B"}],
    }
    entities = extract_entities(result, round_no=1)
    assert len(entities) == 1
    assert entities[0].kingdee_id == "CUS002"


def test_name_fallback_when_id_missing() -> None:
    """id 空但有 name → 用 name 作 display_name（名称占位，等后续补内码）。"""
    result = {
        "form_id": "BD_Customer",
        "field_keys": ["FNumber", "FName"],
        "rows": [{"FNumber": None, "FName": "未编码客户"}],
    }
    entity = extract_entities(result, round_no=1)[0]
    assert entity.kingdee_id == ""
    assert entity.display_name == "未编码客户"


def test_unknown_form_returns_empty() -> None:
    """不在映射表的 form_id → 空（保守不抽，避免误建实体）。"""
    result = {"form_id": "SOME_OtherForm", "rows": [{"FBillNo": "X"}]}
    assert extract_entities(result, round_no=1) == []


def test_malformed_result_returns_empty() -> None:
    """缺 form_id / rows 字段 → 空。"""
    assert extract_entities({}, round_no=1) == []
    assert extract_entities({"form_id": "BD_Customer"}, round_no=1) == []


def test_material_from_inventory() -> None:
    result = {
        "form_id": "STK_Inventory",
        "field_keys": ["FMaterialId.FNumber", "FMaterialId.FName", "FBaseQty"],
        "rows": [{"FMaterialId.FNumber": "MAT001", "FMaterialId.FName": "物料A", "FBaseQty": 100}],
    }
    entity = extract_entities(result, round_no=1)[0]
    assert entity.entity_type == "material"
    assert entity.kingdee_id == "MAT001"
    assert entity.attributes["FBaseQty"] == 100


# ── 查询接口 ──


def _store_with_entities() -> MemoryStore:
    store = MemoryStore(session_id="s1")
    store.upsert_entity(
        EntityRef(
            entity_type="customer",
            display_name="测试客户",
            kingdee_id="CUS001",
            last_mentioned_in_round=2,
        )
    )
    store.upsert_entity(
        EntityRef(
            entity_type="order",
            display_name="SO000008",
            kingdee_id="SO000008",
            last_mentioned_in_round=3,
        )
    )
    store.upsert_entity(
        EntityRef(
            entity_type="customer",
            display_name="另一客户",
            kingdee_id="",
            last_mentioned_in_round=5,
        )
    )
    return store


def test_find_entity_by_kingdee_id() -> None:
    store = _store_with_entities()
    found = store.find_entity("customer", kingdee_id="CUS001")
    assert found is not None
    assert found.display_name == "测试客户"


def test_find_entity_by_display_name() -> None:
    store = _store_with_entities()
    found = store.find_entity("customer", display_name="另一客户")
    assert found is not None
    assert found.kingdee_id == ""


def test_find_entity_no_match_returns_none() -> None:
    store = _store_with_entities()
    assert store.find_entity("customer", kingdee_id="NOPE") is None
    assert store.find_entity("supplier", display_name="测试客户") is None


def test_entities_mentioned_after() -> None:
    store = _store_with_entities()
    recent = store.entities_mentioned_after(3)
    assert len(recent) == 1
    assert recent[0].display_name == "另一客户"


def test_entity_anchors_format() -> None:
    store = _store_with_entities()
    anchors = store.entity_anchors()
    assert "customer:测试客户(CUS001)" in anchors
    assert "order:SO000008(SO000008)" in anchors
    # 无内码实体不带括号
    assert "customer:另一客户" in anchors
    assert not any("另一客户(" in a for a in anchors)
