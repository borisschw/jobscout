"""Tests for store.py."""

from jobscout.models import Verdict, VerdictLabel
from jobscout.store import Store


def test_upsert_job_returns_new_for_first_insert(store, make_job):
    assert store.upsert_job(make_job()) == "new"


def test_upsert_job_returns_unchanged_when_content_same(store, make_job):
    job = make_job()
    store.upsert_job(job)
    assert store.upsert_job(job) == "unchanged"


def test_upsert_job_returns_changed_when_description_differs(store, make_job):
    store.upsert_job(make_job(description="v1"))
    assert store.upsert_job(make_job(description="v2")) == "changed"


def test_mark_missing_deletes_after_grace_period(store, make_job):
    job = make_job(external_id="1")
    store.upsert_job(job)
    store.save_verdict(
        job, Verdict(score=80, verdict=VerdictLabel.STRONG, model="m", prompt_version="1")
    )
    store.ignore([job.job_key])
    store.mark_missing("Acme", set())
    assert store.get_job(job.job_key) is not None
    assert store.is_ignored(job.job_key)
    removed = store.mark_missing("Acme", set())
    assert removed == 1
    assert store.get_job(job.job_key) is None
    assert store.is_ignored(job.job_key) is False
    verdicts = store._conn.execute(
        "SELECT 1 FROM verdicts WHERE job_key = ?", (job.job_key,)
    ).fetchone()
    assert verdicts is None


def test_mark_missing_does_not_close_seen_jobs(store, make_job):
    job = make_job(external_id="1")
    store.upsert_job(job)
    store.mark_missing("Acme", {job.job_key})
    row = store._conn.execute(
        "SELECT status, missing_runs FROM jobs WHERE job_key = ?", (job.job_key,)
    ).fetchone()
    assert row["status"] == "open"
    assert row["missing_runs"] == 0


def test_mark_missing_scopes_to_source(store, make_job):
    comeet = make_job(source="comeet", external_id="c1", title="Comeet Role")
    linkedin = make_job(source="linkedin", external_id="l1", title="LinkedIn Role")
    store.upsert_job(comeet)
    store.upsert_job(linkedin)
    store.mark_missing("Acme", set(), source="linkedin")
    store.mark_missing("Acme", set(), source="linkedin")
    comeet_row = store._conn.execute(
        "SELECT status, missing_runs FROM jobs WHERE job_key = ?", (comeet.job_key,)
    ).fetchone()
    linkedin_row = store._conn.execute(
        "SELECT status, missing_runs FROM jobs WHERE job_key = ?", (linkedin.job_key,)
    ).fetchone()
    assert comeet_row["status"] == "open"
    assert comeet_row["missing_runs"] == 0
    assert linkedin_row is None


def test_mark_missing_deletes_already_closed_jobs(store, make_job):
    job = make_job(external_id="1")
    store.upsert_job(job)
    store.ignore([job.job_key])
    store._conn.execute(
        "UPDATE jobs SET status = 'closed' WHERE job_key = ?", (job.job_key,)
    )
    removed = store.mark_missing("Acme", set())
    assert removed == 1
    assert store.get_job(job.job_key) is None
    assert store.is_ignored(job.job_key) is False


def test_an_ignored_posting_is_never_scored_again(store, make_job):
    job = make_job()
    store.upsert_job(job)
    store.ignore([job.job_key])
    assert store.needs_scoring(job, "1-abc") is False


def test_needs_scoring_true_for_unknown_posting(store, make_job):
    assert store.needs_scoring(make_job()) is True


def test_needs_scoring_false_when_scored_hash_matches(store, make_job):
    job = make_job()
    store.upsert_job(job)
    store.mark_scored(job)
    assert store.needs_scoring(job) is False


def test_needs_scoring_true_when_content_changes(store, make_job):
    job = make_job(description="v1")
    store.upsert_job(job)
    store.mark_scored(job)
    updated = make_job(description="v2")
    store.upsert_job(updated)
    assert store.needs_scoring(updated) is True


def test_needs_scoring_respects_prompt_version_cache(store, make_job):
    job = make_job()
    store.upsert_job(job)
    verdict = Verdict(
        score=80,
        verdict=VerdictLabel.STRONG,
        model="m",
        prompt_version="1-old",
    )
    store.save_verdict(job, verdict)
    store.mark_scored(job)
    assert store.needs_scoring(job, "1-new") is True
    assert store.needs_scoring(job, "1-old") is False


def test_needs_scoring_false_when_no_verdict_and_prompt_version_set(store, make_job):
    job = make_job()
    store.upsert_job(job)
    store.mark_scored(job)
    assert store.needs_scoring(job, "1-abc") is False


def test_save_and_latest_verdict(store, make_job):
    job = make_job()
    store.upsert_job(job)
    v = Verdict(score=75, verdict=VerdictLabel.POSSIBLE, model="m", prompt_version="1-x")
    store.save_verdict(job, v)
    latest = store.latest_verdict(job.job_key)
    assert latest is not None
    assert latest.score == 75


def test_scored_jobs_filters_by_min_score(store, make_job):
    job = make_job(external_id="1")
    store.upsert_job(job)
    store.save_verdict(
        job, Verdict(score=90, verdict=VerdictLabel.STRONG, model="m", prompt_version="1")
    )
    job2 = make_job(external_id="2", title="Junior")
    store.upsert_job(job2)
    store.save_verdict(
        job2, Verdict(score=30, verdict=VerdictLabel.NO, model="m", prompt_version="1")
    )
    results = store.scored_jobs(min_score=60)
    assert len(results) == 1
    assert results[0].verdict.score == 90


def test_resolve_job_keys_by_prefix_and_url(store, make_job):
    job = make_job(url="https://example.com/jobs/123")
    store.upsert_job(job)
    found, unmatched = store.resolve_job_keys([job.job_key[:8]])
    assert len(found) == 1
    assert unmatched == []

    found2, _ = store.resolve_job_keys(["https://example.com/jobs/123"])
    assert found2 == [job.job_key]


def test_ignore_and_unignore(store, make_job):
    job = make_job()
    store.upsert_job(job)
    assert store.ignore([job.job_key]) == 1
    assert store.is_ignored(job.job_key)
    assert store.unignore([job.job_key]) == 1
    assert not store.is_ignored(job.job_key)


def test_list_ignored_includes_title_and_company(store, make_job):
    job = make_job(company="Mobileye", title="Embedded Integrator")
    store.upsert_job(job)
    store.ignore([job.job_key])
    rows = store.list_ignored()
    assert len(rows) == 1
    assert rows[0]["job_key"] == job.job_key
    assert rows[0]["company"] == "Mobileye"
    assert rows[0]["title"] == "Embedded Integrator"
    assert rows[0]["ignored_at"]


def test_applied_jobs_are_kept_out_of_later_matches(store, make_job):
    job = make_job()
    store.upsert_job(job)
    store.save_verdict(
        job, Verdict(score=90, verdict=VerdictLabel.STRONG, model="m", prompt_version="1")
    )
    assert store.mark_applied([job.job_key]) == 1
    assert store.is_applied(job.job_key)
    matches = store.scored_jobs(min_score=0)
    assert [item.job.job_key for item in matches] == [job.job_key]
    assert matches[0].applied is True
    assert store.needs_scoring(job, "1-abc") is False

    assert store.unapply([job.job_key]) == 1
    assert not store.is_applied(job.job_key)
    assert store.scored_jobs(min_score=0)


def test_applied_and_ignored_are_exclusive(store, make_job):
    job = make_job()
    store.upsert_job(job)
    store.ignore([job.job_key])
    store.mark_applied([job.job_key])
    assert store.is_applied(job.job_key)
    assert not store.is_ignored(job.job_key)

    store.ignore([job.job_key])
    assert store.is_ignored(job.job_key)
    assert not store.is_applied(job.job_key)


def test_clear_applied(store, make_job):
    job = make_job(company="Mobileye", title="Embedded Integrator")
    store.upsert_job(job)
    store.mark_applied([job.job_key])
    rows = store.list_applied()
    assert len(rows) == 1
    assert rows[0]["company"] == "Mobileye"
    assert rows[0]["title"] == "Embedded Integrator"
    assert store.clear_applied() == 1
    assert store.list_applied() == []


def test_clear_ignored(store, make_job):
    job = make_job()
    store.upsert_job(job)
    store.ignore([job.job_key])
    assert store.clear_ignored() == 1
    assert store.list_ignored() == []


def test_delete_company_jobs_removes_verdicts_and_ignores(store, make_job):
    kept = make_job(company="Acme", external_id="keep")
    gone = make_job(company="GoneCo", external_id="gone")
    store.upsert_job(kept)
    store.upsert_job(gone)
    store.save_verdict(
        gone, Verdict(score=90, verdict=VerdictLabel.STRONG, model="m", prompt_version="1")
    )
    applied = make_job(company="GoneCo", external_id="applied")
    store.upsert_job(applied)
    store.ignore([gone.job_key])
    store.mark_applied([applied.job_key])
    store.mark_applied([kept.job_key])

    assert store.delete_company_jobs("GoneCo") == 2
    assert store.get_job(gone.job_key) is None
    assert store.latest_verdict(gone.job_key) is None
    assert not store.is_ignored(gone.job_key)
    assert not store.is_applied(applied.job_key)
    assert store.is_applied(kept.job_key)
    assert store.get_job(kept.job_key) is not None


def test_linkedin_cursor_round_trip(store):
    assert store.linkedin_cursor("IAI") == 0
    store.set_linkedin_cursor("IAI", 200)
    assert store.linkedin_cursor("IAI") == 200
    store.set_linkedin_cursor("IAI", 210)
    assert store.linkedin_cursor("IAI") == 210
    store.clear_linkedin_cursor("IAI")
    assert store.linkedin_cursor("IAI") == 0


def test_delete_unlisted_companies_keeps_configured_names(store, make_job):
    acme = make_job(company="Acme", external_id="1")
    other = make_job(company="OtherCo", external_id="2")
    store.upsert_job(acme)
    store.upsert_job(other)
    dropped = store.delete_unlisted_companies({"acme"})
    assert dropped == 1
    assert store.get_job(acme.job_key) is not None
    assert store.get_job(other.job_key) is None
    assert store.companies_in_store() == ["Acme"]
