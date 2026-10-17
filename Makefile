# ══════════════════════════════════════════════════════════════════════════
# KingPilot —— 开发命令唯一入口
# 依据：开发计划 §3.10 工具链与质量门禁 / M0 模块规范 4
# 所有命令统一走 uv（无需手动 activate venv，版本由 uv.lock 锁定）
# ══════════════════════════════════════════════════════════════════════════

UV     ?= uv
PYTEST := $(UV) run pytest
RUFF   := $(UV) run ruff

.DEFAULT_GOAL := help
.PHONY: help lint typecheck test test-integration cov audit check sandbox query-demo eval

help:  ## 显示可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

lint:  ## ruff check + format 校验
	$(RUFF) check .
	$(RUFF) format --check .

typecheck:  ## mypy 严格类型检查
	$(UV) run mypy .

# 说明：`|| [ $? -eq 5 ]` 把退出码 5（"未收集到用例"）视为通过
# 卡片要求「test 可为空收集」。守卫必须内联在各目标里，不可抽成变量——
# 变量展开后 `A || B -m integration` 会把参数喂给 B 而非 pytest。

test:  ## 单元测试（默认排除 integration；无用例时视为通过）
	$(PYTEST) || [ $$? -eq 5 ]

test-integration:  ## 集成测试（需真实金蝶沙箱/LLM，-m integration；无用例时视为通过）
	$(PYTEST) -m integration || [ $$? -eq 5 ]

cov:  ## 覆盖率报告（不含 integration）
	$(PYTEST) --cov --cov-report=term-missing

audit:  ## 依赖漏洞扫描（联网查 OSV 漏洞库）
	$(UV) run pip-audit

check: lint typecheck test  ## 完成门禁：lint + typecheck + test 三绿

# ── M1-T06b 开发辅助：仿真沙箱体验（不属于六门禁，仅本地手工验收用）──

SANDBOX_PORT ?= 8000

sandbox:  ## 前台启动本地 K3Cloud 仿真沙箱（Ctrl+C 停止；另开终端跑 query-demo）
	$(UV) run python -m sandbox.kingdee_stub --port $(SANDBOX_PORT)

query-demo:  ## 硬编码查询脚本：经 KingdeeClient 查仿真沙箱的销售订单（需先 make sandbox）
	$(UV) run python scripts/query_sal_orders.py --url http://127.0.0.1:$(SANDBOX_PORT)

# ── M2-T05 开发辅助：Tool 选对率评测（真 LLM，不属于六门禁）──
# ROUTE 未传时为空串 → 脚本回退读 .env 的 llm_route（make 未定义变量展开即空）

eval:  ## Tool 选对率评测（31 题，读 .env 路由；评测务必 ROUTE=chat_default 用 DeepSeek）
	$(UV) run python scripts/eval_tool_selection.py --route "$(ROUTE)"
