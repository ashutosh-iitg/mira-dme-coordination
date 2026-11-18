import json
import logging
import re
from typing import TypeVar

from openai import AsyncOpenAI, BadRequestError, OpenAIError
from pydantic import BaseModel, ValidationError

from mira.config import llm_settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

MAX_SCHEMA_REPAIR_ATTEMPTS = 2
CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class LlmError(RuntimeError):
    pass


def _client() -> AsyncOpenAI:
    settings = llm_settings()
    return AsyncOpenAI(base_url=settings.base_url, api_key=settings.api_key)


def _parse(content: str, schema: type[T]) -> T:
    return schema.model_validate(json.loads(CODE_FENCE.sub("", content.strip())))


async def _create(client: AsyncOpenAI, model: str, messages: list[dict], response_format: dict | None) -> str:
    kwargs = {"response_format": response_format} if response_format else {}
    response = await client.chat.completions.create(model=model, messages=messages, temperature=0, **kwargs)
    if not response.choices:
        raise LlmError(f"Model provider returned no choices for model {model}")
    return response.choices[0].message.content or ""


async def complete_structured(system_prompt: str, user_prompt: str, schema: type[T]) -> T:
    """Ask the model for JSON matching `schema`; re-prompt with the validation error if it drifts.

    The schema is also embedded in the prompt because not every model honours
    `response_format`; if the provider rejects it we fall back to prompt-only JSON.
    """
    schema_json = json.dumps(schema.model_json_schema())
    messages = [
        {"role": "system", "content": f"{system_prompt}\n\nJSON schema:\n{schema_json}"},
        {"role": "user", "content": user_prompt},
    ]
    response_format: dict | None = {
        "type": "json_schema",
        "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()},
    }
    client = _client()
    model = llm_settings().model
    last_error: Exception | None = None

    for attempt in range(1, MAX_SCHEMA_REPAIR_ATTEMPTS + 1):
        try:
            content = await _create(client, model, messages, response_format)
        except BadRequestError as exc:
            if response_format is None:
                raise LlmError(f"Model provider rejected the request: {exc}") from exc
            logger.info("Model %s rejected response_format; retrying with prompt-only JSON", model)
            response_format = None
            content = await _create(client, model, messages, None)
        except OpenAIError as exc:
            raise LlmError(f"Model provider request failed: {exc}") from exc

        try:
            return _parse(content, schema)
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            logger.warning("LLM output failed %s validation (attempt %d): %s", schema.__name__, attempt, exc)
            messages = [
                *messages,
                {"role": "assistant", "content": content},
                {"role": "user", "content": f"That did not match the schema: {exc}. Reply with corrected JSON only."},
            ]

    raise LlmError(f"Model never produced valid {schema.__name__}: {last_error}")
