"""Extended matching layer tests."""

import pytest

from jobscout.matching.llm import JobJudge
from jobscout.matching.prompts import build_user_prompt
from jobscout.models import VerdictLabel
from tests.helpers import FakeProvider


def test_build_user_prompt_truncates_description(app_config, make_job):
    job = make_job(description="x" * 10000)
    prompt = build_user_prompt(
        app_config.profile,
        app_config.preferences,
        job.title,
        job.location,
        job.department,
        job.description,
    )
    assert len(prompt) < 11000


@pytest.mark.parametrize(
    ("score", "expected"),
    [(85, VerdictLabel.STRONG), (65, VerdictLabel.POSSIBLE), (30, VerdictLabel.NO)],
)
async def test_judge_maps_score_to_verdict(app_config, store, make_job, score, expected):
    response = (
        f'{{"score": {score}, "verdict": "no", "why": "test", '
        f'"matched_skills": [], "gaps": []}}'
    )
    provider = FakeProvider([response])
    judge = JobJudge(app_config, provider)
    job = make_job()
    results, _ = await judge.score_jobs([job], store)
    if score >= 80:
        assert results[0][1].verdict == VerdictLabel.STRONG
    elif score >= 50:
        assert results[0][1].verdict == VerdictLabel.POSSIBLE
    else:
        assert results[0][1].verdict == VerdictLabel.NO


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0.85, 85), ("85%", 85), ("70", 70), (1, 1), (0, 0)],
)
async def test_judge_coerces_fractional_and_percent_scores(
    app_config, store, make_job, raw, expected
):
    score_json = f'"{raw}"' if isinstance(raw, str) else str(raw)
    response = (
        f'{{"score": {score_json}, "verdict": "possible", "why": "test", '
        f'"matched_skills": [], "gaps": []}}'
    )
    provider = FakeProvider([response])
    judge = JobJudge(app_config, provider)
    results, _ = await judge.score_jobs([make_job()], store)
    assert results[0][1].score == expected


async def test_judge_skips_llm_when_provider_is_not_ready(app_config, store, make_job):
    from jobscout.matching.providers.base import LLMError

    class MissingProvider(FakeProvider):
        def ensure_ready(self) -> None:
            raise LLMError("Cursor Agent CLI 'cursor-agent' was not found on PATH.")

        async def complete(self, system: str, user: str) -> str:
            raise AssertionError("complete should not be called")

    provider = MissingProvider(["should-not-use"])
    judge = JobJudge(app_config, provider)
    results, calls = await judge.score_jobs([make_job()], store)
    assert calls == 0
    assert results[0][1].verdict == VerdictLabel.ERROR
    assert "cursor-agent" in results[0][1].why


async def test_judge_accepts_string_list_fields(app_config, store, make_job):
    response = (
        '{"score": 70, "verdict": "possible", "why": "ok", '
        '"matched_skills": "Python", "gaps": "Kubernetes"}'
    )
    provider = FakeProvider([response])
    judge = JobJudge(app_config, provider)
    job = make_job()
    results, _ = await judge.score_jobs([job], store)
    assert results[0][1].matched_skills == ["Python"]
    assert results[0][1].gaps == ["Kubernetes"]
