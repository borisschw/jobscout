"""Shared test helpers (non-fixture)."""

from __future__ import annotations

from jobscout.matching.providers.base import LLMProvider
from jobscout.config import LLMConfig


class FakeProvider(LLMProvider):
    def __init__(self, responses: list[str | Exception]) -> None:
        super().__init__(LLMConfig())
        self.responses = list(responses)
        self.calls = 0

    async def complete(self, system: str, user: str) -> str:
        if not self.responses:
            raise RuntimeError("No more fake responses")
        item = self.responses[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


GOOD_LLM_RESPONSE = """{
  "score": 85,
  "verdict": "strong",
  "why": "Strong Python backend match.",
  "seniority_fit": "Senior IC",
  "location_fit": "Tel Aviv matches",
  "matched_skills": ["Python", "APIs"],
  "gaps": ["Go"]
}"""
