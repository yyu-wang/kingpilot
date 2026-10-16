"""测试包标记（M2-T01）。

加 ``__init__.py`` 的原因：``tests/fakes.py``（FakeProvider 测试替身）会被
各测试模块 import——无包标记时 mypy 会把测试文件双重推断为不同模块名
（同 M1-T07 在 scripts/ 踩过的坑）；包化后同时预防将来同名测试模块冲突。
"""
