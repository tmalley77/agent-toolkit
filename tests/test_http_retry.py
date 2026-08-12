import httpx
import pytest

from agent_toolkit.http_retry import retry_http


def test_retries_on_connect_error_then_succeeds():
    calls = {"n": 0}

    @retry_http
    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectError("boom")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 2


def test_does_not_retry_on_other_exceptions():
    calls = {"n": 0}

    @retry_http
    def always_value_error():
        calls["n"] += 1
        raise ValueError("not a transport error")

    with pytest.raises(ValueError):
        always_value_error()
    assert calls["n"] == 1
