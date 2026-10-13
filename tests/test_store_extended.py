"""Extended store tests."""

import pytest

from jobscout.models import Verdict, VerdictLabel


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "New Title"),
        ("location", "Berlin"),
        ("department", "Product"),
        ("description", "New desc"),
    ],
)
def test_content_hash_changes_on_field_update(make_job, field, value):
    base = make_job()
    kwargs = {field: value}
    updated = make_job(**kwargs)
    assert base.content_hash != updated.content_hash


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("url", "https://other.com/job"),
        ("posted_at", "2099-12-31"),
        ("external_id", "999"),
        ("company", "Other"),
        ("source", "lever"),
    ],
)
def test_content_hash_ignores_non_content_fields(make_job, field, value):
    base = make_job()
    kwargs = {field: value}
    updated = make_job(**kwargs)
    assert base.content_hash == updated.content_hash


def test_transaction_rolls_back_on_error(store, make_job):
    job = make_job()
    try:
        with store.transaction():
            store.upsert_job(job)
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert store.get_job(job.job_key) is None


def test_save_run_persists_counts(store):
    run_id = store.save_run(
        started_at="2024-01-01T00:00:00Z",
        finished_at="2024-01-01T00:01:00Z",
        fetched=10,
        new=3,
        changed=1,
        closed=2,
        scored=2,
        llm_calls=2,
    )
    assert run_id > 0
    latest = store.latest_run()
    assert latest["fetched"] == 10


def test_scored_jobs_excludes_closed_by_default(store, make_job):
    job = make_job()
    store.upsert_job(job)
    store.save_verdict(
        job, Verdict(score=80, verdict=VerdictLabel.STRONG, model="m", prompt_version="1")
    )
    store._conn.execute(
        "UPDATE jobs SET status = 'closed' WHERE job_key = ?", (job.job_key,)
    )
    results = store.scored_jobs(min_score=0)
    assert len(results) == 0


def test_scored_jobs_include_closed_when_requested(store, make_job):
    job = make_job()
    store.upsert_job(job)
    store.save_verdict(
        job, Verdict(score=80, verdict=VerdictLabel.STRONG, model="m", prompt_version="1")
    )
    store._conn.execute(
        "UPDATE jobs SET status = 'closed' WHERE job_key = ?", (job.job_key,)
    )
    results = store.scored_jobs(min_score=0, include_closed=True)
    assert len(results) == 1


def test_scored_jobs_respects_company_filter(store, make_job):
    j1 = make_job(company="Acme", external_id="1")
    j2 = make_job(company="Beta", external_id="2")
    for j in (j1, j2):
        store.upsert_job(j)
        store.save_verdict(
            j, Verdict(score=80, verdict=VerdictLabel.STRONG, model="m", prompt_version="1")
        )
    results = store.scored_jobs(min_score=0, company="Acme")
    assert len(results) == 1
    assert results[0].job.company == "Acme"
