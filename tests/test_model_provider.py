import pytest

from mira.config import ConfigError, GROQ_BASE_URL, OPENROUTER_BASE_URL, llm_settings
from mira.coordinator.agent import coordinator_model
from mira.llm import _client


@pytest.fixture(autouse=True)
def isolate_model_environment(monkeypatch):
    for key in ("MODEL_PROVIDER", "GROQ_API_KEY", "GROQ_MODEL", "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "COORDINATOR_MODEL"):
        monkeypatch.delenv(key, raising=False)


def test_groq_uses_only_its_own_key_model_and_endpoint(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "unrelated-openrouter-model")
    settings = llm_settings()
    assert settings.provider == "groq"
    assert settings.api_key == "test-groq-key"
    assert settings.model == "openai/gpt-oss-120b"
    assert settings.base_url == GROQ_BASE_URL
    model = coordinator_model()
    assert model.model_name == settings.model
    assert model.openai_api_base == GROQ_BASE_URL


async def test_extraction_client_also_uses_selected_provider(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    async with _client() as client:
        assert str(client.base_url).rstrip("/") == GROQ_BASE_URL
        assert client.api_key == "test-groq-key"


def test_openrouter_remains_available_and_is_the_unconfigured_default(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-router-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "router-model")
    settings = llm_settings()
    assert settings.provider == "openrouter"
    assert settings.base_url == OPENROUTER_BASE_URL
    assert settings.model == "router-model"


def test_provider_model_and_coordinator_override(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", " GROQ ")
    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    monkeypatch.setenv("GROQ_MODEL", "test-provider-model")
    monkeypatch.setenv("COORDINATOR_MODEL", "test-coordinator-model")
    assert llm_settings().model == "test-provider-model"
    assert coordinator_model().model_name == "test-coordinator-model"


def test_missing_selected_key_does_not_fall_back_to_another_provider(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "groq")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-router-key")
    with pytest.raises(ConfigError, match="GROQ_API_KEY"):
        llm_settings()


def test_unknown_provider_fails_with_clear_error(monkeypatch):
    monkeypatch.setenv("MODEL_PROVIDER", "unknown")
    with pytest.raises(ConfigError, match="Unknown MODEL_PROVIDER"):
        llm_settings()
