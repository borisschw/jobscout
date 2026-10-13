"""OpenAI LLM provider."""

from __future__ import annotations

from openai import AsyncOpenAI

from jobscout.config import LLMConfig
from jobscout.matching.providers.base import LLMError, LLMProvider


class OpenAIProvider(LLMProvider):
    def __init__(self, config: LLMConfig) -> None:
        super().__init__(config)
        self._client = AsyncOpenAI(timeout=config.timeout_seconds)

    async def complete(self, system: str, user: str) -> str:
        try:
            resp = await self._client.chat.completions.create(
                model=self.config.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except Exception as e:
            raise LLMError(f"OpenAI API error: {e}") from e
        return resp.choices[0].message.content or ""

    async def aclose(self) -> None:
        await self._client.close()
