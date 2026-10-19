"""M9-T11：多租户隔离测试（五类存储 + API 越权 + 预算）。"""

import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import SecretStr

from agent.tools import build_default_registry
from api.app import create_app
from api.auth import ApiKeyDirectory
from api.deps import AppDeps
from config.model_registry import MODEL_REGISTRY
from config.settings import Settings
from config.tenants import TENANT_PROFILES, TenantProfile, tenant_profile
from governance.cost_engine import CostEngine
from governance.online_sampler import OnlineSampler
from governance.prompt_registry import PromptRegistry
from infrastructure.exceptions import TenantError
from infrastructure.secrets import EnvSecretProvider
from infrastructure.tenancy import current_tenant, tenant_paths, tenant_scope, validate_tenant_id
from kingdee.client import KingdeeClient
from ledger.write_ledger import SqliteWriteLedgerStore, WriteLedger
from models.agent import AgentResponse, AgentTrace
from models.audit import AuditEvent
from models.ledger import ProvenanceChain
from models.llm import TokenUsage
from models.tenant import TenantContext
from models.user import UserContext
from observability.audit import AuditLogger, JsonlAuditSink
from tests.api_helpers import api_client, api_settings
from tests.fakes import FakeProvider

_ALPHA_KEY = "alpha-key"
_BETA_KEY = "beta-key"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        session_store_dir=tmp_path / "sessions",
        trace_store_dir=tmp_path / "traces",
        audit_log_path=tmp_path / "audit.jsonl",
        ledger_db_path=tmp_path / "ledger.sqlite3",
        online_sample_dir=tmp_path / "samples",
        approval_store_dir=tmp_path / "approvals",
        badcase_store_dir=tmp_path / "badcases",
        llm_api_key=SecretStr("test-key"),
    )


def _api_settings(base_url: str, tmp_path: Path) -> Settings:
    return api_settings(
        base_url,
        tmp_path,
        {
            _ALPHA_KEY: {"user_id": "alice", "tenant_id": "alpha", "role": "admin"},
            _BETA_KEY: {"user_id": "bob", "tenant_id": "beta", "role": "admin"},
        },
    )


# ────────────────────────── 路径与上下文 ──────────────────────────


def test_default_tenant_keeps_legacy_paths(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    paths = tenant_paths(settings, "default")

    assert paths.session_root == settings.session_store_dir
    assert paths.trace_dir == settings.trace_store_dir
    assert paths.audit_path == settings.audit_log_path
    assert paths.ledger_path == settings.ledger_db_path
    assert paths.sample_dir == settings.online_sample_dir


def test_named_tenant_paths_are_namespaced(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    paths = tenant_paths(settings, "alpha")

    assert paths.session_root == settings.session_store_dir / "tenants" / "alpha"
    assert paths.trace_dir == settings.trace_store_dir / "tenants" / "alpha"
    assert paths.audit_path == tmp_path / "tenants" / "alpha" / "audit.jsonl"
    assert paths.ledger_path == tmp_path / "tenants" / "alpha" / "ledger.sqlite3"
    assert paths.sample_dir == settings.online_sample_dir / "tenants" / "alpha"


def test_invalid_tenant_rejected(tmp_path: Path) -> None:
    with pytest.raises(TenantError):
        validate_tenant_id("../evil")
    with pytest.raises(TenantError):
        tenant_paths(_settings(tmp_path), "")


def test_tenant_scope_is_restored() -> None:
    assert current_tenant() == "default"
    with tenant_scope("alpha"):
        assert current_tenant() == "alpha"
    assert current_tenant() == "default"


def test_tenant_context_from_user() -> None:
    user = UserContext(user_id="u001", role="viewer")
    assert TenantContext.from_user(user).tenant_id == "default"


# ────────────────────────── API 越权 ──────────────────────────


async def test_cross_tenant_session_and_trace_are_404(
    tmp_path: Path, sandbox_server: str, client: KingdeeClient
) -> None:
    settings = _api_settings(sandbox_server, tmp_path)
    app = create_app(settings, provider=FakeProvider(response_queue=["你好"]), client=client)
    alpha_headers = {"X-API-Key": _ALPHA_KEY}
    beta_headers = {"X-API-Key": _BETA_KEY}

    async with api_client(app) as http:
        response = await http.post("/v1/chat", headers=alpha_headers, json={"message": "你好"})
        assert response.status_code == 200
        body = response.json()
        session_id = body["session_id"]
        trace_id = body["trace_id"]

        assert (
            await http.get(f"/v1/sessions/{session_id}", headers=alpha_headers)
        ).status_code == 200
        assert (await http.get(f"/v1/traces/{trace_id}", headers=alpha_headers)).status_code == 200
        assert (
            await http.get(f"/v1/sessions/{session_id}", headers=beta_headers)
        ).status_code == 404
        assert (await http.get(f"/v1/traces/{trace_id}", headers=beta_headers)).status_code == 404
        assert (await http.get("/v1/sessions", headers=beta_headers)).json()["sessions"] == []

    alpha_session = (
        settings.session_store_dir / "tenants" / "alpha" / "users" / "alice" / f"{session_id}.json"
    )
    beta_session = (
        settings.session_store_dir / "tenants" / "beta" / "users" / "bob" / f"{session_id}.json"
    )
    assert alpha_session.exists()
    assert not beta_session.exists()


# ────────────────────────── 五类存储 ──────────────────────────


def test_audit_events_route_by_tenant(tmp_path: Path) -> None:
    base_path = tmp_path / "audit.jsonl"
    audit = AuditLogger(JsonlAuditSink(base_path, tenant_aware=True))

    with tenant_scope("alpha"):
        audit.emit(AuditEvent(event_type="guard.blocked"))
    with tenant_scope("beta"):
        audit.emit(AuditEvent(event_type="permission.denied"))

    alpha = json.loads(
        (tmp_path / "tenants" / "alpha" / "audit.jsonl").read_text(encoding="utf-8").strip()
    )
    beta = json.loads(
        (tmp_path / "tenants" / "beta" / "audit.jsonl").read_text(encoding="utf-8").strip()
    )
    assert alpha["tenant_id"] == "alpha"
    assert beta["tenant_id"] == "beta"
    assert not base_path.exists()


def test_ledger_is_tenant_namespaced(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    alpha = WriteLedger(SqliteWriteLedgerStore(tenant_paths(settings, "alpha").ledger_path))
    beta = WriteLedger(SqliteWriteLedgerStore(tenant_paths(settings, "beta").ledger_path))

    alpha.record_write(
        trace_id="trace-alpha",
        form_id="SAL_Order",
        business_key="SO000001",
        action="save",
        provenance=ProvenanceChain(user_id="alice"),
    )

    assert len(alpha.find_by_trace_id("trace-alpha")) == 1
    assert beta.find_by_trace_id("trace-alpha") == []


def test_sample_is_tenant_namespaced(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    alpha_paths = tenant_paths(settings, "alpha")
    beta_paths = tenant_paths(settings, "beta")
    sampler = OnlineSampler(alpha_paths.sample_dir, sample_rate=1.0, max_per_day=10)
    trace = AgentTrace(
        user_input="你好",
        user_context=UserContext(user_id="alice", tenant_id="alpha", role="viewer"),
        user_id="alice",
    )
    response = AgentResponse(content="你好", trace_id=trace.trace_id, trace=trace)

    path = sampler.maybe_sample(response, session_id="s1")

    assert path is not None
    assert path.parent == alpha_paths.sample_dir
    assert not beta_paths.sample_dir.exists()


# ────────────────────────── 预算与资源 ──────────────────────────


def test_cost_engines_are_per_tenant() -> None:
    route = MODEL_REGISTRY["chat_default"]
    alpha = CostEngine()
    beta = CostEngine()

    alpha.record(route, TokenUsage(prompt_tokens=1000, completion_tokens=1000))

    assert alpha.total_cost_usd > 0
    assert beta.total_cost_usd == 0


def test_tenant_profile_budget_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        TENANT_PROFILES, "alpha", TenantProfile(tenant_id="alpha", llm_budget_usd=0.01)
    )

    assert tenant_profile("alpha").llm_budget_usd == 0.01
    assert tenant_profile("unknown").llm_budget_usd is None


def test_appdeps_resources_are_per_tenant(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    deps = AppDeps(
        settings=settings,
        client=cast(KingdeeClient, object()),
        registry=build_default_registry(),
        audit=AuditLogger(JsonlAuditSink(tmp_path / "audit.jsonl")),
        api_keys=ApiKeyDirectory({}),
        prompts=PromptRegistry(Path(__file__).resolve().parents[1] / "prompts"),
        secrets=EnvSecretProvider(settings),
        provider_override=FakeProvider(),
    )

    alpha = deps.resources_for("alpha")
    beta = deps.resources_for("beta")

    assert alpha is not beta
    assert alpha.rollout_key == "alpha"
    assert beta.rollout_key == "beta"
    assert alpha.cost_engine is not beta.cost_engine
    assert alpha.trace_store.root != beta.trace_store.root
    assert alpha.ledger is not beta.ledger
