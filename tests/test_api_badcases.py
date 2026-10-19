"""M9-T07：Badcase API 契约、权限与导出测试。"""

from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI

from agent.tools import build_default_registry
from api.app import create_app
from api.deps import AppDeps
from config.settings import Settings
from governance.evaluation.dataset import load_dataset
from kingdee.client import KingdeeClient
from models.agent import AgentTrace
from models.user import UserContext
from observability.tracing import TraceStore
from tests.api_helpers import api_client, api_settings
from tests.fakes import FakeProvider

_ADMIN_KEY = "key-admin"
_MANAGER_KEY = "key-manager"
_VIEWER_KEY = "key-viewer"

_ADMIN = {"user_id": "admin", "role": "admin", "data_scope": {}}
_MANAGER = {"user_id": "mgr-a", "role": "manager", "data_scope": {}}
_VIEWER = {"user_id": "viewer", "role": "viewer", "data_scope": {}}

_KNOWN_TOOLS = frozenset(spec.name for spec in build_default_registry().all_specs())


def _settings(tmp_path: Path, base_url: str) -> Settings:
    return api_settings(
        base_url,
        tmp_path,
        {_ADMIN_KEY: _ADMIN, _MANAGER_KEY: _MANAGER, _VIEWER_KEY: _VIEWER},
    )


def _app(settings: Settings, client: KingdeeClient) -> FastAPI:
    return create_app(settings, provider=FakeProvider(), client=client)


def _headers(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


def _save_trace(store: TraceStore, question: str) -> str:
    trace = AgentTrace(
        trace_id=uuid4(),
        user_input=question,
        user_context=UserContext(user_id="alice", role="salesperson"),
        user_id="alice",
    )
    store.save(trace)
    return str(trace.trace_id)


async def test_admin_and_manager_can_mark_and_read(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)

    async with api_client(app) as http:
        deps: AppDeps = app.state.deps
        admin_trace = _save_trace(deps.trace_store, "客户 13800138000 的订单")
        manager_trace = _save_trace(deps.trace_store, "查一下客户 CUS002")

        admin = await http.post(
            "/v1/badcases",
            headers=_headers(_ADMIN_KEY),
            json={
                "trace_id": admin_trace,
                "expected_tool": "kingdee_query_customer",
                "reason": "手机 13800138000 应查客户却查了订单",
                "category": "badcase",
                "expected_behavior": "先换客户编码",
            },
        )
        manager = await http.post(
            "/v1/badcases",
            headers=_headers(_MANAGER_KEY),
            json={
                "trace_id": manager_trace,
                "expected_tool": "kingdee_query_customer",
                "reason": "选错工具",
            },
        )
        listing = await http.get("/v1/badcases", headers=_headers(_ADMIN_KEY))
        viewer = await http.get("/v1/badcases", headers=_headers(_VIEWER_KEY))

    assert admin.status_code == 200
    body = admin.json()
    assert body["status"] == "marked"
    assert body["question"] == "客户 138****8000 的订单"
    assert "13800138000" not in admin.text
    assert manager.status_code == 200
    assert manager.json()["created_by"] == "mgr-a"
    assert listing.status_code == 200
    assert {item["badcase_id"] for item in listing.json()["badcases"]} == {
        body["badcase_id"],
        manager.json()["badcase_id"],
    }
    assert viewer.status_code == 403
    assert viewer.json()["error"]["code"] == "forbidden"


async def test_mark_validation_and_missing_trace(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)

    async with api_client(app) as http:
        deps: AppDeps = app.state.deps
        trace_id = _save_trace(deps.trace_store, "你好，你是谁？")

        missing_tool = await http.post(
            "/v1/badcases",
            headers=_headers(_ADMIN_KEY),
            json={"trace_id": trace_id, "reason": "漏填期望工具"},
        )
        null_tool = await http.post(
            "/v1/badcases",
            headers=_headers(_ADMIN_KEY),
            json={"trace_id": trace_id, "expected_tool": None, "reason": "应纯聊天"},
        )
        unknown_tool = await http.post(
            "/v1/badcases",
            headers=_headers(_ADMIN_KEY),
            json={
                "trace_id": trace_id,
                "expected_tool": "kingdee_drop_database",
                "reason": "不存在的工具",
            },
        )
        missing_trace = await http.post(
            "/v1/badcases",
            headers=_headers(_ADMIN_KEY),
            json={
                "trace_id": "trace-not-exist",
                "expected_tool": None,
                "reason": "trace 不存在",
            },
        )

    assert missing_tool.status_code == 422
    assert null_tool.status_code == 200
    assert null_tool.json()["expected_tool"] is None
    # 同 trace 幂等：后续请求返回首条记录（expected_tool 保留 null）
    assert unknown_tool.status_code == 422
    assert unknown_tool.json()["error"]["code"] == "badcase_unknown_tool"
    assert missing_trace.status_code == 404
    assert missing_trace.json()["error"]["code"] == "badcase_not_found"


async def test_export_creates_loadable_dataset_and_is_idempotent(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)
    dataset_path = tmp_path / "eval_datasets" / "v1" / "tool_selection.json"

    async with api_client(app) as http:
        deps: AppDeps = app.state.deps
        trace_id = _save_trace(deps.trace_store, "客户 13800138000 的订单")
        marked = await http.post(
            "/v1/badcases",
            headers=_headers(_ADMIN_KEY),
            json={
                "trace_id": trace_id,
                "expected_tool": "kingdee_query_customer",
                "reason": "选错工具",
            },
        )
        badcase_id = marked.json()["badcase_id"]

        first = await http.post(
            f"/v1/badcases/{badcase_id}/export",
            headers=_headers(_ADMIN_KEY),
            json={"dataset_version": "v1"},
        )
        second = await http.post(
            f"/v1/badcases/{badcase_id}/export",
            headers=_headers(_ADMIN_KEY),
            json={"dataset_version": "v1"},
        )

    assert first.status_code == 200
    exported = first.json()
    assert exported["status"] == "exported"
    assert exported["exported_dataset"] == "v1/tool_selection.json"
    assert exported["exported_question_id"].startswith("BC-")
    assert second.status_code == 200
    assert second.json()["exported_question_id"] == exported["exported_question_id"]

    dataset = load_dataset(dataset_path, known_tools=_KNOWN_TOOLS)
    assert dataset.version == "v1"
    assert len(dataset.questions) == 1
    assert dataset.questions[0].question_id == exported["exported_question_id"]
    assert "13800138000" not in dataset_path.read_text(encoding="utf-8")


async def test_duplicate_question_and_tool_returns_conflict(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    app = _app(_settings(tmp_path, sandbox_server), client)

    async with api_client(app) as http:
        deps: AppDeps = app.state.deps
        first_trace = _save_trace(deps.trace_store, "查一下客户 CUS001")
        second_trace = _save_trace(deps.trace_store, "查一下客户 CUS001")
        payload = {
            "expected_tool": "kingdee_query_customer",
            "reason": "选错工具",
        }
        first = await http.post(
            "/v1/badcases",
            headers=_headers(_ADMIN_KEY),
            json={"trace_id": first_trace, **payload},
        )
        second = await http.post(
            "/v1/badcases",
            headers=_headers(_ADMIN_KEY),
            json={"trace_id": second_trace, **payload},
        )
        await http.post(
            f"/v1/badcases/{first.json()['badcase_id']}/export",
            headers=_headers(_ADMIN_KEY),
            json={},
        )
        duplicate = await http.post(
            f"/v1/badcases/{second.json()['badcase_id']}/export",
            headers=_headers(_ADMIN_KEY),
            json={},
        )

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "badcase_duplicate"
