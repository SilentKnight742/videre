import pytest
from pydantic import ValidationError

from videre.config import Settings

SANDBOX = "https://api-m.sandbox.paypal.com"


def make(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def test_defaults_are_safe() -> None:
    s = make()
    assert s.rail == "mock"
    assert s.paypal_base_url == SANDBOX
    assert s.demo_replay is False


def test_paypal_rail_accepts_the_sandbox() -> None:
    s = make(rail="paypal", paypal_client_id="id", paypal_client_secret="shh")
    assert s.rail == "paypal"


@pytest.mark.parametrize(
    "url",
    [
        "https://api-m.paypal.com",
        "https://api.paypal.com",
        "http://api-m.sandbox.paypal.com",
        "https://api-m.sandbox.paypal.com.evil.example",
        "https://evil.example/api-m.sandbox.paypal.com",
    ],
)
def test_paypal_rail_refuses_non_sandbox_hosts(url: str) -> None:
    with pytest.raises(ValidationError, match="sandbox only"):
        make(rail="paypal", paypal_base_url=url, paypal_client_id="id", paypal_client_secret="shh")


def test_paypal_rail_requires_credentials() -> None:
    with pytest.raises(ValidationError, match="PAYPAL_CLIENT_ID"):
        make(rail="paypal")


def test_mock_rail_does_not_need_paypal_settings() -> None:
    assert make(rail="mock", paypal_base_url="https://api-m.paypal.com").rail == "mock"


def test_secrets_never_appear_in_repr() -> None:
    s = make(paypal_client_secret="shh-paypal", gemini_api_key="shh-gemini")
    assert "shh-" not in repr(s)
    assert "shh-" not in str(s.model_dump())


def test_prices_are_integer_micro_usd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRICE_IN_PER_MTOK", "750000")
    assert make().price_in_per_mtok == 750_000
    monkeypatch.setenv("PRICE_IN_PER_MTOK", "0.75")
    with pytest.raises(ValidationError):
        make()


def test_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_REPLAY", "1")
    monkeypatch.setenv("GEMINI_MODEL", "some-model")
    s = make()
    assert s.demo_replay is True
    assert s.gemini_model == "some-model"
