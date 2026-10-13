"""Tests for runner.py."""

import json
from pathlib import Path

import httpx
import respx

from jobscout.config import load_config
from jobscout.matching.llm import JobJudge
from jobscout.models import Verdict, VerdictLabel
from jobscout.runner import RunOptions, run_pipeline
from jobscout.sources.linkedin import LINKEDIN_429_ATTEMPTS
from jobscout.store import Store
from tests.helpers import GOOD_LLM_RESPONSE, FakeProvider


@respx.mock
async def test_runner_fetches_and_scores_new_jobs(app_config, project):
    data = {
        "jobs": [{
            "id": 1,
            "title": "Senior Backend Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
            "location": {"name": "Tel Aviv, Israel"},
            "departments": [{"name": "Engineering"}],
            "content": "Python backend work",
            "updated_at": "2024-01-01",
        }]
    }
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true").mock(
        return_value=httpx.Response(200, json=data)
    )
    store = Store(project / "data" / "test.db")
    provider = FakeProvider([GOOD_LLM_RESPONSE])
    judge = JobJudge(app_config, provider)
    summary = await run_pipeline(
        app_config, store, RunOptions(max_llm_calls=5), judge=judge
    )
    assert summary.fetched == 1
    assert summary.new == 1
    assert summary.llm_calls == 1
    store.close()


@respx.mock
async def test_runner_seed_marks_scored_without_llm(app_config, project):
    data = {
        "jobs": [{
            "id": 1,
            "title": "Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
            "location": {"name": "Remote"},
            "content": "Work",
            "updated_at": "2024-01-01",
        }]
    }
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true").mock(
        return_value=httpx.Response(200, json=data)
    )
    store = Store(project / "data" / "test.db")
    summary = await run_pipeline(app_config, store, RunOptions(seed=True))
    assert summary.llm_calls == 0
    assert summary.fetched == 1
    store.close()


@respx.mock
async def test_runner_dry_run_does_not_mark_scored(app_config, project, make_job):
    data = {
        "jobs": [{
            "id": 1,
            "title": "Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
            "location": {"name": "Tel Aviv"},
            "content": "Work",
            "updated_at": "2024-01-01",
        }]
    }
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true").mock(
        return_value=httpx.Response(200, json=data)
    )
    store = Store(project / "data" / "test.db")
    await run_pipeline(app_config, store, RunOptions(dry_run=True))
    rows = store._conn.execute("SELECT scored_hash FROM jobs").fetchall()
    assert all(r["scored_hash"] is None for r in rows)
    store.close()


@respx.mock
async def test_runner_does_not_mark_missing_on_fetch_failure(app_config, project):
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true").mock(
        return_value=httpx.Response(500, text="error")
    )
    store = Store(project / "data" / "test.db")
    from jobscout.models import Job

    job = Job(
        company="Acme", source="greenhouse", external_id="old",
        title="Old Job", url="https://example.com/1", location="Remote",
        description="desc",
    )
    store.upsert_job(job)
    summary = await run_pipeline(app_config, store, RunOptions())
    assert summary.failures
    row = store._conn.execute(
        "SELECT status FROM jobs WHERE job_key = ?", (job.job_key,)
    ).fetchone()
    assert row["status"] == "open"
    store.close()


@respx.mock
async def test_runner_counts_llm_errors(app_config, project):
    from jobscout.matching.providers.base import LLMError

    data = {
        "jobs": [{
            "id": 1,
            "title": "Senior Backend Engineer",
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
            "location": {"name": "Tel Aviv, Israel"},
            "departments": [{"name": "Engineering"}],
            "content": "Python backend work",
            "updated_at": "2024-01-01",
        }]
    }
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true").mock(
        return_value=httpx.Response(200, json=data)
    )
    store = Store(project / "data" / "test.db")
    provider = FakeProvider([LLMError("missing binary")])
    judge = JobJudge(app_config, provider)
    summary = await run_pipeline(
        app_config, store, RunOptions(max_llm_calls=5), judge=judge
    )
    assert summary.llm_error_count == 1
    assert summary.scored == 0
    job_key = store._conn.execute("SELECT job_key FROM jobs").fetchone()["job_key"]
    assert store.latest_verdict(job_key) is None
    store.close()


@respx.mock
async def test_runner_enriches_jobs_from_auto_companies(project):
    (project / "config" / "companies.yaml").write_text(
        "companies:\n"
        "  - name: ComeetCo\n    source: auto\n"
        "    careers_url: https://www.comeet.com/jobs/acme/18.007\n",
        encoding="utf-8",
    )
    config = load_config(project)
    html = '{"token": "abc123", "company-uid": "18.007"}'
    respx.get("https://www.comeet.com/jobs/acme/18.007").mock(
        return_value=httpx.Response(200, text=html)
    )
    jobs = json.loads((Path(__file__).parent / "fixtures" / "comeet.json").read_text())
    jobs[0]["name"] = "Embedded Linux Engineer"
    jobs[0]["location"] = {"city": "Tel Aviv", "country": "Israel"}
    jobs[0]["details"] = {"description": "<p>Linux kernel and firmware</p>"}
    respx.get(
        "https://www.comeet.co/careers-api/2.0/company/18.007/positions"
        "?token=abc123&details=true"
    ).mock(return_value=httpx.Response(200, json=jobs))

    store = Store(project / "data" / "test.db")
    provider = FakeProvider([GOOD_LLM_RESPONSE])
    judge = JobJudge(config, provider)
    summary = await run_pipeline(
        config, store, RunOptions(max_llm_calls=5), judge=judge
    )
    assert not summary.failures
    assert summary.fetched == 1
    assert summary.llm_calls == 1
    store.close()


def _greenhouse_jobs_url(token: str) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def _empty_board() -> dict:
    return {"jobs": []}


def _scored(store: Store, job, score: int = 90) -> None:
    store.upsert_job(job)
    store.save_verdict(
        job,
        Verdict(score=score, verdict=VerdictLabel.STRONG, model="m", prompt_version="1"),
    )
    store.mark_scored(job)


@respx.mock
async def test_runner_deletes_jobs_when_company_removed_from_list(app_config, project, make_job):

    store = Store(project / "data" / "test.db")
    gone = make_job(company="GoneCo", external_id="g1", title="Embedded Linux Engineer")
    _scored(store, gone)
    respx.get(_greenhouse_jobs_url("acme")).mock(
        return_value=httpx.Response(200, json=_empty_board())
    )
    summary = await run_pipeline(app_config, store, RunOptions())
    assert summary.removed == 1
    assert store.get_job(gone.job_key) is None
    assert all(m.job.company != "GoneCo" for m in summary.matches)
    store.close()


@respx.mock
async def test_runner_company_flag_filters_report_without_deleting_others(
    project, make_job
):

    (project / "config" / "companies.yaml").write_text(
        "companies:\n"
        "  - name: Acme\n    source: greenhouse\n    token: acme\n"
        "  - name: Mobileye\n    source: greenhouse\n    token: mobileye\n",
        encoding="utf-8",
    )
    config = load_config(project)
    store = Store(project / "data" / "test.db")
    acme_job = make_job(company="Acme", external_id="a1", title="Acme Engineer")
    mobileye_job = make_job(
        company="Mobileye", external_id="m1", title="Mobileye Engineer"
    )
    _scored(store, acme_job)
    _scored(store, mobileye_job)

    respx.get(_greenhouse_jobs_url("acme")).mock(
        return_value=httpx.Response(200, json=_empty_board())
    )
    respx.get(_greenhouse_jobs_url("mobileye")).mock(
        return_value=httpx.Response(200, json=_empty_board())
    )
    summary = await run_pipeline(
        config, store, RunOptions(only_companies=["Mobileye"])
    )
    assert summary.removed == 0
    assert store.get_job(acme_job.job_key) is not None
    assert store.get_job(mobileye_job.job_key) is not None
    assert [m.job.company for m in summary.matches] == ["Mobileye"]
    store.close()


@respx.mock
async def test_runner_dry_run_does_not_delete_unlisted_companies(
    app_config, project, make_job
):
    store = Store(project / "data" / "test.db")
    gone = make_job(company="GoneCo", external_id="g1")
    store.upsert_job(gone)
    respx.get(_greenhouse_jobs_url("acme")).mock(
        return_value=httpx.Response(200, json=_empty_board())
    )
    summary = await run_pipeline(app_config, store, RunOptions(dry_run=True))
    assert summary.removed == 0
    assert store.get_job(gone.job_key) is not None
    store.close()


def _cursor(store: Store, company: str) -> int | None:
    row = store._conn.execute(
        "SELECT next_start FROM linkedin_cursors WHERE company = ?",
        (company,),
    ).fetchone()
    return None if row is None else int(row["next_start"])


@respx.mock
async def test_linkedin_run_resumes_after_rate_limit_without_dropping_unseen_jobs(
    project, make_job
):
    (project / "config" / "companies.yaml").write_text(
        "companies:\n"
        "  - name: IAI\n    source: linkedin\n    token: '100'\n    country: IL\n",
        encoding="utf-8",
    )
    config = load_config(project)
    store = Store(project / "data" / "test.db")
    old = make_job(
        company="IAI",
        source="linkedin",
        external_id="old",
        title="Stored embedded role",
        url="https://www.linkedin.com/jobs/view/old",
        description="firmware already saved",
    )
    store.upsert_job(old)
    card = """
    <div class="base-card" data-entity-urn="urn:li:jobPosting:111">
      <h3 class="base-search-card__title">Embedded Engineer</h3>
      <span class="job-search-card__location">Yehud, Israel</span>
    </div>
    """
    detail = (
        "<h2 class='top-card-layout__title'>Embedded Engineer</h2>"
        "<div class='show-more-less-html__markup'><p>Real-time firmware</p></div>"
    )
    later_pages = {"n": 0}

    def list_page(request: httpx.Request) -> httpx.Response:
        start = request.url.params.get("start", "0")
        if start == "0":
            return httpx.Response(200, text=card)
        later_pages["n"] += 1
        if later_pages["n"] <= LINKEDIN_429_ATTEMPTS:
            return httpx.Response(429, text="slow down", headers={"Retry-After": "0"})
        return httpx.Response(200, text="<div></div>")

    respx.get(url__regex=r".*seeMoreJobPostings/search.*").mock(side_effect=list_page)
    detail_route = respx.get(
        "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/111"
    ).mock(return_value=httpx.Response(200, text=detail))

    await run_pipeline(config, store, RunOptions(dry_run=True))
    assert _cursor(store, "IAI") == 1
    assert store.find_job("IAI", "linkedin", "111") is not None
    assert store._conn.execute(
        "SELECT missing_runs FROM jobs WHERE job_key = ?", (old.job_key,)
    ).fetchone()["missing_runs"] == 0
    assert detail_route.call_count == 1

    await run_pipeline(config, store, RunOptions(dry_run=True))
    assert _cursor(store, "IAI") is None
    assert store._conn.execute(
        "SELECT missing_runs FROM jobs WHERE job_key = ?", (old.job_key,)
    ).fetchone()["missing_runs"] == 0

    await run_pipeline(config, store, RunOptions(dry_run=True))
    assert _cursor(store, "IAI") is None
    assert detail_route.call_count == 1
    assert store._conn.execute(
        "SELECT missing_runs FROM jobs WHERE job_key = ?", (old.job_key,)
    ).fetchone()["missing_runs"] == 1
    assert store.find_job("IAI", "linkedin", "111") is not None
    store.close()

