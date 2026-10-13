"""Tests for remaining source adapters."""

import json
from pathlib import Path

import httpx
import respx

from jobscout.config import CompanyConfig, FetchConfig
from jobscout.sources.amazon import AmazonSource
from jobscout.sources.comeet import ComeetSource
from jobscout.sources.smartrecruiters import SmartRecruitersSource
from jobscout.sources.base import RetryableSourceError, retryable_wait_seconds
from jobscout.sources.workday import WorkdaySource

FIXTURES = Path(__file__).parent / "fixtures"


@respx.mock
async def test_smartrecruiters_paginates_and_fetches_details():
    company = CompanyConfig(name="Acme", source="smartrecruiters", token="Acme")
    source = SmartRecruitersSource(company, FetchConfig(retries=1, concurrency=2))
    listing = json.loads((FIXTURES / "smartrecruiters_list.json").read_text())
    detail = json.loads((FIXTURES / "smartrecruiters_detail.json").read_text())
    respx.get("https://api.smartrecruiters.com/v1/companies/Acme/postings/sr-1").mock(
        return_value=httpx.Response(200, json=detail)
    )
    respx.get(url__regex=r".*smartrecruiters.com/v1/companies/Acme/postings(\?|$)").mock(
        return_value=httpx.Response(200, json=listing)
    )
    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)
    assert len(jobs) == 1
    assert "CI/CD" in jobs[0].description


@respx.mock
async def test_workday_paginates_listings_only():
    company = CompanyConfig(
        name="Acme",
        source="workday",
        host="company.wd5.myworkdayjobs.com",
        tenant="company",
        site="careers",
    )
    source = WorkdaySource(company, FetchConfig(retries=1, concurrency=2))
    listing = json.loads((FIXTURES / "workday_list.json").read_text())
    base = "https://company.wd5.myworkdayjobs.com"
    respx.post(f"{base}/wday/cxs/company/careers/jobs").mock(
        return_value=httpx.Response(200, json=listing)
    )
    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)
    assert len(jobs) == 1
    assert jobs[0].title == "Cloud Engineer"
    assert jobs[0].description == ""
    assert jobs[0].url == f"{base}/careers/job/Tel-Aviv/Cloud-Engineer_R123"


@respx.mock
async def test_workday_enrich_job_uses_cxs_detail_api():
    company = CompanyConfig(
        name="Acme",
        source="workday",
        host="company.wd5.myworkdayjobs.com",
        tenant="company",
        site="careers",
    )
    source = WorkdaySource(company, FetchConfig(retries=1))
    detail = json.loads((FIXTURES / "workday_detail.json").read_text())
    base = "https://company.wd5.myworkdayjobs.com"
    path = "/job/Tel-Aviv/Cloud-Engineer_R123"
    respx.get(f"{base}/wday/cxs/company/careers{path}").mock(
        return_value=httpx.Response(200, json=detail)
    )
    job = source._make_job(
        external_id=path,
        title="Cloud Engineer",
        url=f"{base}{path}",
        location="Tel Aviv",
    )
    async with httpx.AsyncClient() as client:
        enriched = await source.enrich_job(client, job)
    assert "AWS and Kubernetes" in enriched.description


@respx.mock
async def test_workday_paginates_when_later_pages_return_zero_total():
    company = CompanyConfig(
        name="Acme",
        source="workday",
        host="company.wd5.myworkdayjobs.com",
        tenant="company",
        site="careers",
    )
    source = WorkdaySource(company, FetchConfig(retries=1, concurrency=2))
    base = "https://company.wd5.myworkdayjobs.com"
    api = f"{base}/wday/cxs/company/careers/jobs"

    def listing(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        offset = body["offset"]
        if offset == 0:
            return httpx.Response(
                200,
                json={
                    "total": 40,
                    "jobPostings": [
                        {
                            "title": "Job A",
                            "externalPath": "/job/A/A_R1",
                            "locationsText": "Tel Aviv",
                        }
                    ],
                },
            )
        if offset == 20:
            return httpx.Response(
                200,
                json={
                    "total": 0,
                    "jobPostings": [
                        {
                            "title": "Job B",
                            "externalPath": "/job/B/B_R2",
                            "locationsText": "Haifa",
                        }
                    ],
                },
            )
        return httpx.Response(200, json={"total": 0, "jobPostings": []})

    respx.post(api).mock(side_effect=listing)
    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)
    assert len(jobs) == 2


@respx.mock
async def test_workday_detail_404_falls_back_to_summary():
    company = CompanyConfig(
        name="Acme",
        source="workday",
        host="company.wd5.myworkdayjobs.com",
        tenant="company",
        site="careers",
    )
    source = WorkdaySource(company, FetchConfig(retries=1))
    listing = json.loads((FIXTURES / "workday_list.json").read_text())
    base = "https://company.wd5.myworkdayjobs.com"
    respx.post(f"{base}/wday/cxs/company/careers/jobs").mock(
        return_value=httpx.Response(200, json=listing)
    )
    path = "/job/Tel-Aviv/Cloud-Engineer_R123"
    respx.get(f"{base}/wday/cxs/company/careers{path}").mock(
        return_value=httpx.Response(404, text="not found")
    )
    job = source._make_job(
        external_id=path,
        title="Cloud Engineer",
        url=f"{base}{path}",
        location="Tel Aviv",
    )
    async with httpx.AsyncClient() as client:
        enriched = await source.enrich_job(client, job)
    assert enriched.title == "Cloud Engineer"
    assert enriched.description == ""


@respx.mock
async def test_amazon_paginates_jobs():
    company = CompanyConfig(name="Amazon", source="amazon", country="ISR")
    source = AmazonSource(company, FetchConfig(retries=1))
    data = json.loads((FIXTURES / "amazon.json").read_text())
    respx.get(url__regex=r".*amazon.jobs/en/search.json.*").mock(
        return_value=httpx.Response(200, json=data)
    )
    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)
    assert len(jobs) == 1
    assert jobs[0].company == "Amazon"
    assert jobs[0].department == "AWS"


@respx.mock
async def test_amazon_parses_jobs_list_and_paginates():
    company = CompanyConfig(name="Amazon", source="amazon", country="ISR")
    source = AmazonSource(company, FetchConfig(retries=1))

    def _page(request: httpx.Request) -> httpx.Response:
        offset = int(httpx.URL(str(request.url)).params.get("offset", "0"))
        if offset == 0:
            body = {
                "error": None,
                "hits": 2,
                "jobs": [
                    {
                        "id": "uuid-1",
                        "id_icims": "111",
                        "title": "Embedded Linux Engineer",
                        "job_path": "/en/jobs/111/embedded-linux-engineer",
                        "city": "Tel Aviv",
                        "location": "IL, Tel Aviv",
                        "country_code": "ISR",
                        "description": "Work on <b>kernel</b> drivers.",
                        "basic_qualifications": "C and Linux",
                        "preferred_qualifications": "Yocto",
                        "job_category": "Software Development",
                        "business_category": "aws",
                        "job_schedule_type": "Full time",
                        "posted_date": "September 22, 2026",
                    }
                ],
            }
        else:
            body = {
                "error": None,
                "hits": 2,
                "jobs": [
                    {
                        "id": "uuid-2",
                        "id_icims": "222",
                        "title": "SDE",
                        "job_path": "/en/jobs/222/sde",
                        "city": "Haifa",
                        "location": "IL, Haifa",
                        "country_code": "ISR",
                        "description": "Build services.",
                        "job_category": "Software Development",
                    }
                ],
            }
        return httpx.Response(200, json=body)

    respx.get(url__regex=r".*amazon.jobs/en/search.json.*").mock(side_effect=_page)
    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)
    assert [j.external_id for j in jobs] == ["111", "222"]
    assert jobs[0].url == "https://www.amazon.jobs/en/jobs/111/embedded-linux-engineer"
    assert jobs[0].location == "IL, Tel Aviv"
    assert jobs[0].department == "Software Development"
    assert "kernel" in jobs[0].description
    assert "C and Linux" in jobs[0].description
    assert "Yocto" in jobs[0].description


@respx.mock
async def test_comeet_filters_internal_and_parses_jobs():
    company = CompanyConfig(
        name="Acme", source="comeet", token="tok", company_uid="18.007"
    )
    source = ComeetSource(company, FetchConfig(retries=1))
    data = json.loads((FIXTURES / "comeet.json").read_text())
    respx.get(url__regex=r".*comeet.co/careers-api/2.0/company/18.007/positions.*").mock(
        return_value=httpx.Response(200, json=data)
    )
    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)
    assert len(jobs) == 1
    assert jobs[0].title == "Full Stack Developer"


@respx.mock
async def test_comeet_handles_null_location_and_list_details():
    company = CompanyConfig(
        name="Silk", source="comeet", token="tok", company_uid="F6.00C"
    )
    source = ComeetSource(company, FetchConfig(retries=1))
    listing = [
        {
            "uid": "pos-1",
            "name": "Platform Engineer",
            "url_active_page": "https://www.comeet.co/jobs/silk/pos-1",
            "is_internal": False,
            "location": None,
            "details": [
                {"name": "Description", "value": "Build storage systems."},
            ],
        }
    ]
    respx.get(url__regex=r".*comeet.co/careers-api/2.0/company/F6.00C/positions.*").mock(
        return_value=httpx.Response(200, json=listing)
    )
    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)
    assert len(jobs) == 1
    assert jobs[0].title == "Platform Engineer"
    assert "Build storage systems." in jobs[0].description
    assert jobs[0].location == ""


def test_rate_limit_wait_uses_retry_after_then_backoff():
    limited = RetryableSourceError("HTTP 429", status_code=429, retry_after=12)
    assert retryable_wait_seconds(limited, 1) == 12
    assert retryable_wait_seconds(limited, 4) == 12

    capped = RetryableSourceError("HTTP 429", status_code=429, retry_after=120)
    assert retryable_wait_seconds(capped, 1) == 60

    plain = RetryableSourceError("HTTP 429", status_code=429)
    assert retryable_wait_seconds(plain, 1) == 5
    assert retryable_wait_seconds(plain, 2) == 10
    assert retryable_wait_seconds(plain, 4) == 30

    server = RetryableSourceError("HTTP 503", status_code=503)
    assert retryable_wait_seconds(server, 1) is None
