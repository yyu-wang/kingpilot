"""M1-T06a：仿真沙箱登录（LoginBySign）端点单测。

锚点：签名用官方算法在测试侧独立算一遍（不调用被测实现的私有方法），
确保「实现怎么算签名」与「测试怎么验签名」不是同义反复。
"""

import hashlib
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from sandbox.kingdee_stub.app import (
    LOGIN_PATH,
    SANDBOX_ACCT_ID,
    SANDBOX_APP_ID,
    SANDBOX_APP_SECRET,
    SANDBOX_USER,
    SESSION_COOKIE_NAME,
    app,
    reset_sessions,
)

_LC_ID_ZH_CN = 2052
_TIMESTAMP = 1_760_000_000


@pytest.fixture(autouse=True)
def _clean_sessions() -> Iterator[None]:
    """会话表是模块级可变状态，每个用例前后都复位，避免用例互相污染。"""
    reset_sessions()
    yield
    reset_sessions()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _sign(acct: str, user: str, app_id: str, secret: str, timestamp: int) -> str:
    """按官方算法独立计算 LoginBySign 签名（与实现侧算法互为交叉验证）。"""
    parts = [acct, user, app_id, secret, str(timestamp)]
    return hashlib.sha256("".join(sorted(parts)).encode("utf-8")).hexdigest()


def _login_payload(
    *,
    acct: str = SANDBOX_ACCT_ID,
    user: str = SANDBOX_USER,
    app_id: str = SANDBOX_APP_ID,
    secret: str = SANDBOX_APP_SECRET,
    timestamp: int = _TIMESTAMP,
) -> dict[str, list[object]]:
    return {
        "parameters": [
            acct,
            user,
            app_id,
            timestamp,
            _sign(acct, user, app_id, secret, timestamp),
            _LC_ID_ZH_CN,
        ]
    }


def test_login_valid_credentials_sets_session_cookie(client: TestClient) -> None:
    response = client.post(LOGIN_PATH, json=_login_payload())

    assert response.status_code == 200
    assert response.json()["LoginResultType"] == 1
    assert SESSION_COOKIE_NAME in response.cookies


def test_login_wrong_secret_is_rejected(client: TestClient) -> None:
    response = client.post(LOGIN_PATH, json=_login_payload(secret="wrong_secret"))

    assert response.status_code == 200
    assert response.json()["LoginResultType"] == -1
    assert SESSION_COOKIE_NAME not in response.cookies


def test_login_wrong_account_is_rejected(client: TestClient) -> None:
    response = client.post(LOGIN_PATH, json=_login_payload(acct="other_acct"))

    assert response.json()["LoginResultType"] == -1


def test_login_tampered_signature_is_rejected(client: TestClient) -> None:
    """凭证正确但签名被篡改：必须失败（防止只校凭证不校签名）。"""
    payload = _login_payload()
    payload["parameters"][4] = "0" * 64
    response = client.post(LOGIN_PATH, json=payload)

    assert response.json()["LoginResultType"] == -1


def test_login_malformed_parameters_is_rejected(client: TestClient) -> None:
    response = client.post(LOGIN_PATH, json={"parameters": [SANDBOX_ACCT_ID, SANDBOX_USER]})

    assert response.status_code == 200
    assert response.json()["LoginResultType"] == -1
