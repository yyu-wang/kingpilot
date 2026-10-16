"""可导入脚本集合（M1-T06b）。

加 ``__init__.py`` 的原因：集成测试直接 ``import scripts.query_sal_orders``
做冒烟验证；无此文件时 mypy 会把脚本同时推断为 ``query_sal_orders`` 与
``scripts.query_sal_orders`` 两个模块名而报错（PEP 420 隐式命名空间的双重推断）。

仍不属于 wheel 打包范围（见 pyproject ``[tool.hatch.build.targets.wheel]``）：
脚本面向开发机/CI 终端使用，不随生产包分发。
"""
