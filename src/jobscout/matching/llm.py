"""Job scoring via LLM."""

from __future__ import annotations

import asyncio
from typing import Any

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from jobscout.config import AppConfig
from jobscout.matching.prompts import SYSTEM_PROMPT, build_user_prompt, prompt_fingerprint
from jobscout.matching.providers.base import (
    LLMError,
    LLMProvider,
    build_provider,
    parse_json_object,
)
from jobscout.models import Job, Verdict, VerdictLabel
from jobscout.progress import RunProgress
from jobscout.store import Store


class JobJudge:
    def __init__(self, config: AppConfig, provider: LLMProvider | None = None) -> None:
        self.config = config
        self.provider = provider or build_provider(config.settings.llm)
        self.prompt_version = prompt_fingerprint(config.profile, config.preferences)
        self.model = config.settings.llm.model
        self._semaphore = asyncio.Semaphore(config.settings.llm.concurrency)
        self._call_lock = asyncio.Lock()
        self._calls_made = 0
        self._max_calls = config.settings.llm.max_calls

    async def aclose(self) -> None:
        await self.provider.aclose()

    async def score_jobs(
        self,
        jobs: list[Job],
        store: Store,
        *,
        rescore: bool = False,
        max_llm_calls: int | None = None,
        progress: RunProgress | None = None,
    ) -> tuple[list[tuple[Job, Verdict]], int]:
        effective_max = max_llm_calls if max_llm_calls is not None else self._max_calls
        to_score: list[Job] = []

        for job in jobs:
            if rescore or store.needs_scoring(job, self.prompt_version):
                cached = store.latest_verdict(
                    job.job_key, job.content_hash, self.prompt_version
                )
                if cached and not rescore:
                    continue
                to_score.append(job)

        if not to_score:
            return [], self._calls_made

        try:
            self.provider.ensure_ready()
        except LLMError as e:
            return [(job, Verdict.errored(str(e))) for job in to_score], 0

        results: list[tuple[Job, Verdict]] = []
        total = len(to_score)
        completed = 0
        progress_lock = asyncio.Lock()
        log_every = 1 if progress and progress.verbose else max(1, total // 20)

        async def score_one(job: Job) -> tuple[Job, Verdict]:
            nonlocal completed
            async with self._semaphore:
                async with self._call_lock:
                    if effective_max > 0 and self._calls_made >= effective_max:
                        return job, Verdict.skipped("LLM call budget reached")
                    self._calls_made += 1

                verdict = await self._score_with_retry(job)

            if progress:
                async with progress_lock:
                    completed += 1
                    if completed == total or completed % log_every == 0:
                        progress.log(f"  scored {completed}/{total}")

            return job, verdict

        scored = await asyncio.gather(*[score_one(j) for j in to_score])
        results.extend(scored)
        return results, self._calls_made

    async def _score_with_retry(self, job: Job) -> Verdict:
        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=20),
            retry=retry_if_exception_type(LLMError),
            reraise=True,
        )
        async def _call() -> Verdict:
            return await self._score_one(job)

        try:
            return await _call()
        except LLMError as e:
            return Verdict.errored(str(e))
        except Exception as e:
            return Verdict.errored(str(e))

    async def _score_one(self, job: Job) -> Verdict:
        user = build_user_prompt(
            self.config.profile,
            self.config.preferences,
            job.title,
            job.location,
            job.department,
            job.description,
        )
        raw = await self.provider.complete(SYSTEM_PROMPT, user)
        data = parse_json_object(raw)
        return self._parse_verdict(data)

    def _parse_verdict(self, data: dict[str, Any]) -> Verdict:
        score = _coerce_score(data.get("score", 0))

        verdict_str = data.get("verdict", "")
        try:
            verdict = VerdictLabel(verdict_str)
        except ValueError:
            verdict = VerdictLabel.NO

        if verdict_str not in ("strong", "possible", "no") or (
            (verdict == VerdictLabel.STRONG and score < 80)
            or (verdict == VerdictLabel.POSSIBLE and score < 50)
            or (verdict == VerdictLabel.NO and score >= 50)
        ):
            if score >= 80:
                verdict = VerdictLabel.STRONG
            elif score >= 50:
                verdict = VerdictLabel.POSSIBLE
            else:
                verdict = VerdictLabel.NO

        return Verdict(
            score=score,
            verdict=verdict,
            why=str(data.get("why", "")),
            seniority_fit=str(data.get("seniority_fit", "")),
            location_fit=str(data.get("location_fit", "")),
            matched_skills=self._parse_list(data.get("matched_skills")),
            gaps=self._parse_list(data.get("gaps")),
            model=self.model,
            prompt_version=self.prompt_version,
        )

    @staticmethod
    def _parse_list(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(v) for v in value[:5]]
        if isinstance(value, str) and value:
            return [value][:5]
        return []


def _coerce_score(score_raw: Any) -> int:
    if isinstance(score_raw, str):
        score_raw = score_raw.strip().rstrip("%")
    try:
        score_f = float(score_raw)
    except (TypeError, ValueError):
        return 0
    if 0 < score_f < 1:
        score_f *= 100
    return max(0, min(100, int(round(score_f))))
