"""M1-T05：端点描述符单测（纯元数据，零网络）。

断言的是白名单的**结构不变量**——这些不变量一旦破坏，M2 工具层与 M4 Guard 2
会静默拿到错误数据，故必须用测试锁死。
"""

from dataclasses import FrozenInstanceError

import pytest

from kingdee.endpoints.base import EndpointDescriptor
from kingdee.endpoints.sal_order import SAL_ORDER
from models.kingdee import BillQueryParams, DocumentStatus

_MIN_FIELD_COUNT = 15


# ── 描述符基本属性 ──


def test_form_id_and_label() -> None:
    assert SAL_ORDER.form_id == "SAL_Order"
    assert SAL_ORDER.label == "销售订单"


def test_field_count_covers_core_fields() -> None:
    assert len(SAL_ORDER.fields) >= _MIN_FIELD_COUNT


def test_no_duplicate_field_names() -> None:
    """重复字段名会让白名单出现歧义（同一字段两种标记）。"""
    names = [field.name for field in SAL_ORDER.fields]
    assert len(names) == len(set(names))


def test_every_field_has_description() -> None:
    """描述为空会让 M2 的 LLM 无从判断字段语义，生成错误的 FilterString。"""
    assert all(field.description.strip() for field in SAL_ORDER.fields)


# ── 集合不变量（白名单的核心契约）──


def test_filterable_is_proper_subset_of_selectable() -> None:
    """可过滤必须是可查的真子集：能过滤的字段必须能查，且存在仅可查的字段。"""
    selectable = SAL_ORDER.selectable_fields
    filterable = SAL_ORDER.filterable_fields
    assert filterable < selectable


def test_default_field_keys_subset_of_selectable() -> None:
    assert set(SAL_ORDER.default_field_keys) <= SAL_ORDER.selectable_fields


def test_default_sort_field_is_selectable() -> None:
    assert SAL_ORDER.default_sort in SAL_ORDER.selectable_fields


def test_derived_properties_are_frozensets() -> None:
    """派生集合必须只读，防止下游误改白名单。"""
    assert isinstance(SAL_ORDER.selectable_fields, frozenset)
    assert isinstance(SAL_ORDER.filterable_fields, frozenset)


# ── 「可查 ≠ 可过滤」的字段分档 ──


def test_related_field_names_use_dot_notation() -> None:
    """关联字段必须用点号形式（金蝶关联字段语法）。"""
    for field in SAL_ORDER.fields:
        if field.data_type == "related":
            assert "." in field.name, field.name


def test_customer_name_selectable_but_not_filterable() -> None:
    """名称可显示、不可过滤；过滤只走编码形式。"""
    assert "FCustomerID.FName" in SAL_ORDER.selectable_fields
    assert "FCustomerID.FName" not in SAL_ORDER.filterable_fields
    assert "FCustomerID.FNumber" in SAL_ORDER.filterable_fields


def test_primary_key_not_exposed_as_filterable() -> None:
    """内部主键不作为过滤条件暴露（字段粒度最小权限）。"""
    assert "FID" in SAL_ORDER.selectable_fields
    assert "FID" not in SAL_ORDER.filterable_fields


# ── 枚举联动 ──


def test_document_status_filterable_and_matches_enum() -> None:
    """FDocumentStatus 可过滤，其取值域与 DocumentStatus 枚举一致（单一真相源）。"""
    assert "FDocumentStatus" in SAL_ORDER.filterable_fields
    assert {member.value for member in DocumentStatus} == {"Z", "A", "B", "C", "D"}


# ── 与请求模型的接线 ──


def test_to_query_params_uses_defaults() -> None:
    params = SAL_ORDER.to_query_params()
    assert isinstance(params, BillQueryParams)
    assert params.form_id == "SAL_Order"
    assert params.field_keys == ",".join(SAL_ORDER.default_field_keys)
    assert params.order_string == "FBillNo"
    assert params.filter_string == ""
    assert params.start_row == 0
    assert params.limit == 100


def test_to_query_params_overrides() -> None:
    """显式传参覆盖默认值（M1-T06b 脚本与 M2 工具层按此调用）。"""
    params = SAL_ORDER.to_query_params(
        field_keys="FBillNo,FDocumentStatus",
        filter_string="FDocumentStatus = 'C'",
        order_string="FDate DESC",
        start_row=50,
        limit=200,
    )
    assert params.field_keys == "FBillNo,FDocumentStatus"
    assert params.filter_string == "FDocumentStatus = 'C'"
    assert params.order_string == "FDate DESC"
    assert params.start_row == 50
    assert params.limit == 200


def test_descriptor_is_frozen() -> None:
    """不可变值对象：下游不得篡改白名单。"""
    with pytest.raises(FrozenInstanceError):
        SAL_ORDER.form_id = "TAMPERED"  # type: ignore[misc]


def test_base_descriptor_is_exported() -> None:
    """基类可从包入口导入（后续 formId 照此模式扩展）。"""
    assert isinstance(SAL_ORDER, EndpointDescriptor)
