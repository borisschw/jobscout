"""LLM provider ABC and utilities."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod

from jobscout.config import LLMConfig


class LLMError(Exception):
    """Retryable LLM failure."""


class LLMProvider(ABC):
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    @abstractmethod
    async def complete(self, system: str, user: str) -> str:
        raise NotImplementedError

    def ensure_ready(self) -> None:
        """Raise LLMError before any calls if the provider cannot run."""
        return

    async def aclose(self) -> None:
        pass


def parse_json_object(text: str) -> dict:
    cleaned = text.strip()
    fence = re.match(r"```(?:json)?\s*\n?(.*?)\n?```", cleaned, re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            result = json.loads(cleaned[start : end + 1])
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    raise LLMError(f"Could not parse JSON from LLM response: {text[:200]}")


def build_provider(config: LLMConfig) -> LLMProvider:
    if config.provider == "cursor":
        from jobscout.matching.providers.cursor import CursorProvider

        return CursorProvider(config)
    if config.provider == "openai":
        from jobscout.matching.providers.openai import OpenAIProvider

        return OpenAIProvider(config)
    if config.provider == "anthropic":
        from jobscout.matching.providers.anthropic import AnthropicProvider

        return AnthropicProvider(config)
    if config.provider == "ollama":
        from jobscout.matching.providers.ollama import OllamaProvider

        return OllamaProvider(config)
    raise ValueError(f"Unknown LLM provider: {config.provider}")
