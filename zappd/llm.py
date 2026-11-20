"""The ONE place that knows how to reach Claude.

Every LLM call in the project goes through `get_client()` and these constants,
so swapping models, adding logging, or mocking in tests touches one file.
"""

import os
from functools import lru_cache

import anthropic
from dotenv import load_dotenv

MODEL = "claude-opus-5-5"

# If Claude's safety classifier declines a request, the API can retry it on a
# fallback model server-side instead of returning an empty answer.
# "default" lets Anthropic pick the fallback model by refusal category.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACKS = "default"

# Rough Opus 5.5 prices, used only for the cost estimates the evals print.
PRICE_PER_M_INPUT_USD = 4.00
PRICE_PER_M_OUTPUT_USD = 20.00


class LLMUnavailableError(RuntimeError):
    """Raised when no API key is configured."""


@lru_cache
def get_client() -> anthropic.Anthropic:
    load_dotenv()  # reads ANTHROPIC_API_KEY from .env into the environment
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise LLMUnavailableError("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key.")
    # The SDK retries 429/5xx/connection errors with backoff (2 retries by default).
    return anthropic.Anthropic(timeout=60.0)


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return input_tokens / 1e6 * PRICE_PER_M_INPUT_USD + output_tokens / 1e6 * PRICE_PER_M_OUTPUT_USD
