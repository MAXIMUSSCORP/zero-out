import json
import logging
import time
from typing import Any, Callable

import openai

from .config import Settings

log = logging.getLogger(__name__)
MAX_TRIES = 3
BASE_DELAY = 2.0
JSON_NUDGE = {"role": "system", "content": "Return ONLY valid JSON."}


def _retry_after(e: openai.RateLimitError, default: float) -> float:
    try:
        return max(0.0, float(e.response.headers["retry-after"]))
    except (KeyError, ValueError, AttributeError, TypeError):
        return default


def _parse(content: str | None) -> dict | None:
    try:
        out = json.loads(content or "")
    except (json.JSONDecodeError, TypeError):
        return None
    return out if isinstance(out, dict) else None


def chat_json(
    settings: Settings,
    model: str,
    messages: list[dict],
    *,
    client: Any = None,
    temperature: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict | None:
    client = client or openai.OpenAI(base_url=settings.tf_base_url, api_key=settings.tf_api_key, timeout=60)
    msgs = list(messages)
    extra = {} if temperature is None else {"temperature": temperature}
    json_retried = False
    delay = BASE_DELAY
    tries = 0
    while True:
        try:
            resp = client.chat.completions.create(
                model=model, messages=msgs, response_format={"type": "json_object"}, **extra
            )
        except openai.RateLimitError as e:
            tries += 1
            if tries >= MAX_TRIES:
                return None
            sleep(_retry_after(e, delay))
            delay *= 2
            continue
        except openai.APIError as e:  # includes connection and timeout errors
            log.warning("model call failed: %s", type(e).__name__)
            return None
        out = _parse(resp.choices[0].message.content)
        if out is not None:
            return out
        if json_retried:
            return None
        json_retried = True
        msgs = msgs + [JSON_NUDGE]
