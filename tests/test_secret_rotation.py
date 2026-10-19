"""M9-T10：轮换生效测试（LLM 按请求取 key / 金蝶按登录取 secret）。"""

import json

import httpx
import pytest
import respx
from pydantic import SecretStr

from agent.providers import OpenAICompatibleProvider
from config.model_registry import MODEL_REGISTRY
from config.settings import Settings
from infrastructure.exceptions import KingdeeAuthError, LLMError, SecretError
from infrastructure.secrets import SecretName, SecretProvider
from kingdee.auth import KingdeeAuth
from models.llm import LLMContext, LLMMessage

_LLM_URL = "https://api.deepseek.com/chat/completions"
_LOGIN_PATH = "/K3Cloud/Kingdee.BOS.WebApi.ServicesStub.AuthService.LoginBySign.common.kdsvc"
_LLM_BODY = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
}


class _MutableSecretProvider(SecretProvider):
    """测试用可变密钥源：直接改 dict 模拟轮换。"""

    def __init__(self, values: dict[SecretName, str]) -> None:
        self.values = values

    @property
    def source(self) -> str:
        return "test"

    def _load(self, name: SecretName) -> SecretStr:
        value = self.values.get(name)
        if value is None:
            raise SecretError("密钥未配置", code="secret_missing", secret=name.value)
        return SecretStr(value)

    def refresh(self) -> None:
        """测试替身无缓存。"""


@respx.mock
async def test_llm_uses_rotated_key_without_rebuild() -> None:
    route = respx.post(_LLM_URL).respond(json=_LLM_BODY)
    secrets = _MutableSecretProvider({SecretName.LLM_API_KEY: "test-key-a"})
    settings = Settings(_env_file=None)
    http = httpx.AsyncClient()
    provider = OpenAICompatibleProvider(
        settings,
        route=MODEL_REGISTRY["chat_default"],
        http=http,
        secrets=secrets,
    )
    context = LLMContext(messages=[LLMMessage(role="user", content="hi")])

    try:
        await provider.plan(context, tools=[])
        secrets.values[SecretName.LLM_API_KEY] = "test-key-b"
        await provider.plan(context, tools=[])
    finally:
        await http.aclose()

    headers = [call.request.headers["authorization"] for call in route.calls]
    assert headers == ["Bearer test-key-a", "Bearer test-key-b"]


def test_llm_missing_key_fails_at_construction() -> None:
    secrets = _MutableSecretProvider({})

    with pytest.raises(LLMError, match="密钥未配置"):
        OpenAICompatibleProvider(
            Settings(_env_file=None),
            route=MODEL_REGISTRY["chat_default"],
            secrets=secrets,
        )


@respx.mock
async def test_kingdee_uses_rotated_secret_on_relogin() -> None:
    route = respx.post("http://kd.test" + _LOGIN_PATH).respond(json={"LoginResultType": 1})
    secrets = _MutableSecretProvider({SecretName.KINGDEE_APP_SECRET: "test-secret-a"})
    settings = Settings(
        _env_file=None,
        kingdee_server_url="http://kd.test",
        kingdee_acct_id="acct",
        kingdee_user="user",
        kingdee_app_id="app",
    )
    http = httpx.AsyncClient()
    auth = KingdeeAuth(settings, http, clock=lambda: 1000.0, secrets=secrets)

    try:
        await auth.ensure_session()
        auth.invalidate()
        secrets.values[SecretName.KINGDEE_APP_SECRET] = "test-secret-b"
        await auth.ensure_session()
    finally:
        await http.aclose()

    signatures = [json.loads(call.request.content)["parameters"][4] for call in route.calls]
    assert len(signatures) == 2
    assert signatures[0] != signatures[1]


@respx.mock
async def test_kingdee_missing_secret_does_not_leak() -> None:
    respx.post("http://kd.test" + _LOGIN_PATH).respond(json={"LoginResultType": 1})
    secrets = _MutableSecretProvider({})
    settings = Settings(
        _env_file=None,
        kingdee_server_url="http://kd.test",
        kingdee_acct_id="acct",
        kingdee_user="user",
        kingdee_app_id="app",
    )
    async with httpx.AsyncClient() as http:
        auth = KingdeeAuth(settings, http, secrets=secrets)
        with pytest.raises(KingdeeAuthError) as exc:
            await auth.ensure_session()

    assert "kingdee_app_secret" in str(exc.value)
    assert "test-secret" not in str(exc.value)
