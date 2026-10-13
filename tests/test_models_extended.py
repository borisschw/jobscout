"""Extended model tests."""

import pytest

from jobscout.models import FetchResult, RunSummary, ScoredJob, Verdict, VerdictLabel


def test_fetch_result_defaults():
    r = FetchResult(company="A", source="gh")
    assert r.jobs == []
    assert r.error is None


def test_run_summary_failures_filters_errors():
    s = RunSummary(
        fetch_results=[
            FetchResult(company="A", source="gh", error="fail"),
            FetchResult(company="B", source="gh", jobs=[]),
        ]
    )
    assert len(s.failures) == 1
    assert s.failures[0].company == "A"


def test_scored_job_is_new_flag(make_job):
    sj = ScoredJob(
        job=make_job(),
        verdict=Verdict(score=80, verdict=VerdictLabel.STRONG),
        first_seen="2024-01-01",
        is_new=True,
    )
    assert sj.is_new is True


@pytest.mark.parametrize("score", [0, 50, 100])
def test_verdict_score_range(score):
    v = Verdict(score=score, verdict=VerdictLabel.POSSIBLE)
    assert 0 <= v.score <= 100
