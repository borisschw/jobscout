"""Tests for LLM matching layer."""

import pytest

from jobscout.matching.llm import JobJudge
from jobscout.matching.prompts import prompt_fingerprint
from jobscout.matching.providers.base import LLMError, parse_json_object
from jobscout.models import VerdictLabel
from tests.helpers import GOOD_LLM_RESPONSE, FakeProvider


def test_parse_json_object_from_fence():
    data = parse_json_object('```json\n{"score": 80}\n```')
    assert data["score"] == 80


def test_parse_json_object_from_bare_braces():
    data = parse_json_object('Here is the result: {"score": 70, "verdict": "possible"} end')
    assert data["score"] == 70


def test_parse_json_object_raises_on_invalid():
    with pytest.raises(LLMError):
        parse_json_object("no json here")


def test_prompt_fingerprint_is_stable(app_config):
    fp1 = prompt_fingerprint(app_config.profile, app_config.preferences)
    fp2 = prompt_fingerprint(app_config.profile, app_config.preferences)
    assert fp1 == fp2
    assert fp1.startswith("1-")


async def test_judge_scores_job_and_parses_verdict(app_config, store, make_job):
    provider = FakeProvider([GOOD_LLM_RESPONSE])
    judge = JobJudge(app_config, provider)
    job = make_job()
    store.upsert_job(job)
    results, calls = await judge.score_jobs([job], store)
    assert calls == 1
    assert results[0][1].score == 85
    assert results[0][1].verdict == VerdictLabel.STRONG


async def test_judge_error_verdict_is_not_cached(app_config, store, make_job):
    provider = FakeProvider([LLMError("fail")])
    judge = JobJudge(app_config, provider)
    job = make_job()
    store.upsert_job(job)
    results, _ = await judge.score_jobs([job], store)
    assert results[0][1].verdict == VerdictLabel.ERROR
    assert store.latest_verdict(job.job_key) is None


async def test_judge_respects_max_calls_budget(app_config, store, make_job):
    provider = FakeProvider([GOOD_LLM_RESPONSE, GOOD_LLM_RESPONSE])
    judge = JobJudge(app_config, provider)
    jobs = [make_job(external_id=str(i)) for i in range(2)]
    for j in jobs:
        store.upsert_job(j)
    results, calls = await judge.score_jobs(jobs, store, max_llm_calls=1)
    assert calls == 1
    skipped = [v for _, v in results if v.verdict == VerdictLabel.SKIPPED]
    assert len(skipped) == 1


async def test_judge_repairs_contradictory_verdict_from_score(app_config, store, make_job):
    bad = '{"score": 90, "verdict": "no", "why": "x", "matched_skills": [], "gaps": []}'
    provider = FakeProvider([bad])
    judge = JobJudge(app_config, provider)
    job = make_job()
    results, _ = await judge.score_jobs([job], store)
    assert results[0][1].verdict == VerdictLabel.STRONG
