"""``FilterCondition`` 构造助手（endpoint ``build_filter`` 的调用面）。

Why 提供助手而非让 endpoint 直接构造 ``FilterCondition``：
- 构造点集中——invariant 只需改一处而非 8 个 endpoint 文件
- 函数名对齐 ``FilterOp`` 枚举值，endpoint 代码可读性更高
  （``eq("FCustomerID.FNumber", "CUS001")`` vs
  ``FilterCondition(field="FCustomerID.FNumber", op=FilterOp.EQ, value="CUS001")``）

Why 在构造期拒绝单引号（而非留到各厂商渲染期）：
- 单引号是所有类 SQL 查询方言的公共注入面——值里带引号几乎必然意味着
  注入尝试或脏数据；早拒绝 = 错误信息离用户输入更近（纵深防御，
  各厂商渲染器的字面量校验是主防线，两者不互相替代）
- 若未来某厂商支持安全的转义规则，放宽本 invariant 需安全评审
"""

from erp.types import FilterCondition, FilterOp

__all__ = ["eq", "ge", "gt", "le", "like", "lt", "ne"]


def _check_value(value: str | float) -> None:
    """构造期 invariant：字符串值不允许单引号（公共注入面，见模块 docstring）。"""
    if isinstance(value, str) and "'" in value:
        raise ValueError(f"过滤值不能包含单引号: {value!r}")


def eq(field: str, value: str | float) -> FilterCondition:
    """等值条件（``=``）。"""
    _check_value(value)
    return FilterCondition(field=field, op=FilterOp.EQ, value=value)


def ne(field: str, value: str | float) -> FilterCondition:
    """不等条件（``<>``）。"""
    _check_value(value)
    return FilterCondition(field=field, op=FilterOp.NE, value=value)


def gt(field: str, value: str | float) -> FilterCondition:
    """大于（``>``）。"""
    _check_value(value)
    return FilterCondition(field=field, op=FilterOp.GT, value=value)


def ge(field: str, value: str | float) -> FilterCondition:
    """大于等于（``>=``）。"""
    _check_value(value)
    return FilterCondition(field=field, op=FilterOp.GE, value=value)


def lt(field: str, value: str | float) -> FilterCondition:
    """小于（``<``）。"""
    _check_value(value)
    return FilterCondition(field=field, op=FilterOp.LT, value=value)


def le(field: str, value: str | float) -> FilterCondition:
    """小于等于（``<=``）。"""
    _check_value(value)
    return FilterCondition(field=field, op=FilterOp.LE, value=value)


def like(field: str, value: str) -> FilterCondition:
    """模糊匹配（``LIKE '%value%'``）。仅支持字符串值。"""
    _check_value(value)
    return FilterCondition(field=field, op=FilterOp.LIKE, value=value)
