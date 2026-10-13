"""Ollama local LLM provider."""

from __future__ import annotations

import httpx

from jobscout.config import LLMConfig
from jobscout.matching.providers.base import LLMError, LLMProvider


class OllamaProvider(LLMProvider):
    def __init__(self, config: LLMConfig) -> None:
        super().__init__(config)
        self._client = httpx.AsyncClient(base_url=config.base_url, timeout=config.timeout_seconds)

    async def complete(self, system: str, user: str) -> str:
        try:
            resp = await self._client.post(
                "/api/chat",
                json={
                    "model": self.config.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "stream": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            raise LLMError(f"Ollama API error: {e}") from e
        return data.get("message", {}).get("content", "")

    async def aclose(self) -> None:
        await self._client.aclose()
