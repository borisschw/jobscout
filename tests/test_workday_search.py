"""Tests for Workday search query building and filtering."""

import json

import httpx
import respx

from jobscout.config import CompanyConfig, FetchConfig
from jobscout.models import Job
from jobscout.search import normalize_search_terms
from jobscout.sources.workday import (
    WorkdaySource,
    build_workday_search_query,
    posting_matches_search_terms,
    public_workday_job_url,
    repair_workday_job,
    workday_url_needs_repair,
)


def test_build_workday_search_query_combines_with_or():
    assert build_workday_search_query(["embedded", "firmware"]) == "embedded OR firmware"
    assert build_workday_search_query(["linux"]) == "linux"
    assert build_workday_search_query([""]) == ""


def test_posting_matches_search_terms_uses_word_boundaries():
    posting = {"title": "Embedded Software Engineer", "locationsText": "Tel Aviv"}
    assert posting_matches_search_terms(posting, ["embedded"])
    assert not posting_matches_search_terms(posting, ["bed"])
    assert posting_matches_search_terms(posting, ["firmware", "embedded"])


@respx.mock
async def test_workday_uses_single_or_query_for_multiple_terms():
    company = CompanyConfig(
        name="Acme",
        source="workday",
        host="company.wd5.myworkdayjobs.com",
        tenant="company",
        site="careers",
        search_text=["embedded", "firmware"],
    )
    source = WorkdaySource(company, FetchConfig(retries=1))
    base = "https://company.wd5.myworkdayjobs.com"
    api = f"{base}/wday/cxs/company/careers/jobs"
    seen_queries: list[str] = []

    def listing(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_queries.append(body["searchText"])
        return httpx.Response(
            200,
            json={
                "total": 2,
                "jobPostings": [
                    {
                        "title": "Embedded Engineer",
                        "externalPath": "/job/A/A_R1",
                        "locationsText": "Tel Aviv",
                    },
                    {
                        "title": "Sales Manager",
                        "externalPath": "/job/B/B_R2",
                        "locationsText": "New York",
                    },
                ],
            },
        )

    respx.post(api).mock(side_effect=listing)
    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)

    assert seen_queries == ["embedded OR firmware"]
    assert len(jobs) == 1
    assert jobs[0].title == "Embedded Engineer"


def test_normalize_search_terms():
    assert normalize_search_terms(None) == [""]
    assert normalize_search_terms("linux") == ["linux"]
    assert normalize_search_terms([" embedded ", ""]) == ["embedded"]


def test_job_matches_search_terms_uses_title_and_description(make_job):
    from jobscout.search import job_matches_search_terms

    title_hit = make_job(title="Embedded Software Engineer")
    desc_hit = make_job(title="Software Engineer", description="Linux kernel work")
    miss = make_job(title="Sales Manager", description="Enterprise accounts")
    assert job_matches_search_terms(title_hit, ["embedded"])
    assert job_matches_search_terms(desc_hit, ["kernel"])
    assert not job_matches_search_terms(miss, ["embedded", "firmware"])


def test_workday_public_job_url_includes_site():
    company = CompanyConfig(
        name="NVIDIA",
        source="workday",
        host="nvidia.wd5.myworkdayjobs.com",
        tenant="nvidia",
        site="NVIDIAExternalCareerSite",
    )
    source = WorkdaySource(company, FetchConfig(retries=1))
    path = "/job/Israel-Tel-Aviv/Firmware-Engineer---Ethernet-Switching_JR2013842"
    expected = public_workday_job_url(
        "nvidia.wd5.myworkdayjobs.com",
        "NVIDIAExternalCareerSite",
        path,
    )
    assert source._public_job_url(path) == expected


def test_repair_workday_job_fixes_broken_url():
    host = "nvidia.wd5.myworkdayjobs.com"
    site = "NVIDIAExternalCareerSite"
    path = "/job/Israel-Tel-Aviv/Senior-Firmware-Engineer---NVLink-Switch_JR2016514"
    broken = f"https://{host}{path}"
    job = Job(
        company="NVIDIA",
        source="workday",
        external_id=path,
        title="Senior Firmware Engineer",
        url=broken,
    )
    assert workday_url_needs_repair(broken, host, site, path)
    fixed = repair_workday_job(job, host, site)
    assert fixed.url == public_workday_job_url(host, site, path)
    assert not workday_url_needs_repair(fixed.url, host, site, path)
