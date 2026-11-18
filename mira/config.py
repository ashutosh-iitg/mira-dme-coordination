import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
RUNTIME_DIR = PROJECT_ROOT / "runtime"

SUPPLIER_DIRECTORY_PATH = PROJECT_ROOT / "sample_dir.csv"
CASE_PATH = DATA_DIR / "case.json"
SCENARIOS_DIR = DATA_DIR / "scenarios"
EVENT_LOG_PATH = RUNTIME_DIR / "events.jsonl"
ESCALATIONS_PATH = RUNTIME_DIR / "escalations.json"
DEFAULT_SCENARIO = "default"

CASE_TASK_QUEUE = "dme-cases"
VOICE_CALL_TASK_QUEUE = "voice-calls"

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_OPENROUTER_MODEL = "baidu/ernie-5.5-flash-preview:free"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
DEFAULT_SECONDS_PER_DAY = 5.0

load_dotenv(PROJECT_ROOT / ".env")


class ConfigError(RuntimeError):
    pass


PHONE_LINES = 2
DEFAULT_TEMPORAL_PROFILE = "cloud-setup"


@dataclass(frozen=True)
class LlmSettings:
    api_key: str
    model: str
    provider: str
    base_url: str


def temporal_profile() -> str:
    return os.getenv("TEMPORAL_PROFILE", DEFAULT_TEMPORAL_PROFILE)


def llm_settings() -> LlmSettings:
    provider = os.getenv("MODEL_PROVIDER", "openrouter").strip().lower()
    providers = {
        "openrouter": ("OPENROUTER_API_KEY", "OPENROUTER_MODEL", OPENROUTER_BASE_URL, DEFAULT_OPENROUTER_MODEL),
        "groq": ("GROQ_API_KEY", "GROQ_MODEL", GROQ_BASE_URL, DEFAULT_GROQ_MODEL),
    }
    if provider not in providers:
        raise ConfigError(f"Unknown MODEL_PROVIDER {provider!r}; choose groq or openrouter")
    key_name, model_name, base_url, default_model = providers[provider]
    api_key = os.getenv(key_name, "").strip()
    if not api_key:
        raise ConfigError(f"{key_name} is not set. Export it or add it to a local .env file.")
    model = os.getenv(model_name, default_model).strip()
    if not model:
        raise ConfigError(f"{model_name} must not be empty")
    return LlmSettings(api_key=api_key, model=model, provider=provider, base_url=base_url)


def scenario_path() -> Path:
    name = os.getenv("SCENARIO", DEFAULT_SCENARIO)
    path = SCENARIOS_DIR / f"{name}.json"
    if not path.is_file():
        raise ConfigError(f"Scenario {name!r} not found at {path}")
    return path


def seconds_per_day() -> float:
    raw = os.getenv("SECONDS_PER_DAY", str(DEFAULT_SECONDS_PER_DAY))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"SECONDS_PER_DAY must be a number, got {raw!r}") from exc
    if value <= 0:
        raise ConfigError("SECONDS_PER_DAY must be positive")
    return value
