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
DEFAULT_SCENARIO = "default"

CASE_TASK_QUEUE = "dme-cases"
VOICE_CALL_TASK_QUEUE = "voice-calls"

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_MODEL = "baidu/ernie-5.5-flash-preview:free"
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


def temporal_profile() -> str:
    return os.getenv("TEMPORAL_PROFILE", DEFAULT_TEMPORAL_PROFILE)


def llm_settings() -> LlmSettings:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise ConfigError("OPENROUTER_API_KEY is not set. Export it or add it to a local .env file.")
    return LlmSettings(api_key=api_key, model=os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL))


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
