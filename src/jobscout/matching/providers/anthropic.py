"""Anthropic LLM provider."""

from __future__ import annotations

from anthropic import AsyncAnthropic

from jobscout.config import LLMConfig
from jobscout.matching.providers.base import LLMError, LLMProvider


class AnthropicProvider(LLMProvider):
    def __init__(self, config: LLMConfig) -> None:
        super().__init__(config)
        self._client = AsyncAnthropic(timeout=config.timeout_seconds)

    async def complete(self, system: str, user: str) -> str:
        try:
            resp = await self._client.messages.create(
                model=self.config.model,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as e:
            raise LLMError(f"Anthropic API error: {e}") from e
        blocks = resp.content
        return blocks[0].text if blocks else ""

    async def aclose(self) -> None:
        await self._client.close()
