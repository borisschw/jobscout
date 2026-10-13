"""Tests for the LinkedIn guest jobs adapter."""

from pathlib import Path

import httpx
import respx

from jobscout.config import CompanyConfig, FetchConfig
from jobscout.sources.linkedin import LINKEDIN_429_ATTEMPTS, LinkedInSource, parse_listing_cards

FIXTURES = Path(__file__).parent / "fixtures"


@respx.mock
async def test_linkedin_lists_and_hydrates_jobs():
    company = CompanyConfig(
        name="Sandisk",
        source="linkedin",
        token="4251",
        country="IL",
        search_text=["embedded", "firmware", "linux", "kernel", "driver"],
    )
    source = LinkedInSource(company, FetchConfig(retries=1, concurrency=2))
    listing = (FIXTURES / "linkedin_list.html").read_text(encoding="utf-8")
    detail = (FIXTURES / "linkedin_detail.html").read_text(encoding="utf-8")

    def list_page(request: httpx.Request) -> httpx.Response:
        assert "f_C=4251" in str(request.url)
        assert "geoId=101620260" in str(request.url)
        start = request.url.params.get("start", "0")
        if start == "0":
            return httpx.Response(200, text=listing)
        return httpx.Response(200, text="<div></div>")

    respx.get(url__regex=r".*seeMoreJobPostings/search.*").mock(side_effect=list_page)
    respx.get("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/111").mock(
        return_value=httpx.Response(200, text=detail)
    )
    respx.get("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/222").mock(
        return_value=httpx.Response(200, text="<div></div>")
    )

    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.external_id == "111"
    assert job.title == "Linux Embedded Development Expert"
    assert "Israel" in job.location
    assert "kernel" in job.description.lower()
    assert job.url == "https://www.linkedin.com/jobs/view/111"


@respx.mock
async def test_linkedin_skips_detail_fetch_for_stored_job(store, make_job):
    company = CompanyConfig(
        name="Sandisk",
        source="linkedin",
        token="4251",
        country="IL",
        search_text=["embedded", "firmware", "linux", "kernel", "driver"],
    )
    source = LinkedInSource(company, FetchConfig(retries=3, concurrency=2))
    source.store = store
    stored = make_job(
        company="Sandisk",
        source="linkedin",
        external_id="111",
        title="Old title",
        url="https://www.linkedin.com/jobs/view/111",
        location="Kfar Saba, Israel",
        description="Linux kernel and embedded work",
    )
    store.upsert_job(stored)
    listing = (FIXTURES / "linkedin_list.html").read_text(encoding="utf-8")

    def list_page(request: httpx.Request) -> httpx.Response:
        start = request.url.params.get("start", "0")
        if start == "0":
            return httpx.Response(200, text=listing)
        return httpx.Response(200, text="<div></div>")

    respx.get(url__regex=r".*seeMoreJobPostings/search.*").mock(side_effect=list_page)
    detail_111 = respx.get("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/111").mock(
        return_value=httpx.Response(200, text="should not be fetched")
    )
    respx.get("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/222").mock(
        return_value=httpx.Response(200, text="<div></div>")
    )

    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)

    assert detail_111.call_count == 0
    assert len(jobs) == 1
    assert jobs[0].external_id == "111"
    assert jobs[0].title == "Linux Embedded Development Expert"
    assert jobs[0].description == "Linux kernel and embedded work"
    assert source.listing_complete is True
    assert source.resume_start is None


@respx.mock
async def test_linkedin_refetches_detail_when_stored_description_is_empty(store, make_job):
    company = CompanyConfig(name="Sandisk", source="linkedin", token="4251", country="IL")
    source = LinkedInSource(company, FetchConfig(retries=1))
    source.store = store
    store.upsert_job(
        make_job(
            company="Sandisk",
            source="linkedin",
            external_id="111",
            title="Linux Embedded Development Expert",
            url="https://www.linkedin.com/jobs/view/111",
            description="",
        )
    )
    listing = (FIXTURES / "linkedin_list.html").read_text(encoding="utf-8")
    detail = (FIXTURES / "linkedin_detail.html").read_text(encoding="utf-8")

    def list_page(request: httpx.Request) -> httpx.Response:
        start = request.url.params.get("start", "0")
        if start == "0":
            return httpx.Response(200, text=listing)
        return httpx.Response(200, text="<div></div>")

    respx.get(url__regex=r".*seeMoreJobPostings/search.*").mock(side_effect=list_page)
    detail_111 = respx.get("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/111").mock(
        return_value=httpx.Response(200, text=detail)
    )
    respx.get("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/222").mock(
        return_value=httpx.Response(200, text="<div></div>")
    )

    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)

    assert detail_111.call_count == 1
    assert jobs[0].description


def _rate_limit() -> httpx.Response:
    return httpx.Response(429, text="slow down", headers={"Retry-After": "0"})


@respx.mock
async def test_linkedin_retries_429_then_stops_and_keeps_page_offset():
    company = CompanyConfig(name="IAI", source="linkedin", token="100", country="IL")
    source = LinkedInSource(company, FetchConfig(retries=5))
    listing = respx.get(url__regex=r".*seeMoreJobPostings/search.*").mock(
        return_value=_rate_limit()
    )

    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)

    assert jobs == []
    assert listing.call_count == LINKEDIN_429_ATTEMPTS
    assert source.listing_complete is False
    assert source.resume_start == 0


@respx.mock
async def test_linkedin_continues_when_429_clears_on_retry():
    company = CompanyConfig(name="IAI", source="linkedin", token="100", country="IL")
    source = LinkedInSource(company, FetchConfig(retries=1))
    card = """
    <div class="base-card" data-entity-urn="urn:li:jobPosting:111">
      <h3 class="base-search-card__title">Embedded Engineer</h3>
      <span class="job-search-card__location">Yehud, Israel</span>
    </div>
    """
    calls = {"n": 0}

    def list_page(request: httpx.Request) -> httpx.Response:
        start = request.url.params.get("start", "0")
        if start != "0":
            return httpx.Response(200, text="<div></div>")
        calls["n"] += 1
        if calls["n"] < LINKEDIN_429_ATTEMPTS:
            return _rate_limit()
        return httpx.Response(200, text=card)

    respx.get(url__regex=r".*seeMoreJobPostings/search.*").mock(side_effect=list_page)
    respx.get("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/111").mock(
        return_value=httpx.Response(
            200,
            text=(
                "<h2 class='top-card-layout__title'>Embedded Engineer</h2>"
                "<div class='show-more-less-html__markup'><p>firmware</p></div>"
            ),
        )
    )

    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)

    assert calls["n"] == LINKEDIN_429_ATTEMPTS
    assert len(jobs) == 1
    assert source.listing_complete is True
    assert source.resume_start is None


@respx.mock
async def test_linkedin_stops_on_999_during_detail_and_does_not_fetch_the_rest():
    company = CompanyConfig(name="Sandisk", source="linkedin", token="4251", country="IL")
    source = LinkedInSource(company, FetchConfig(retries=5))
    listing = (FIXTURES / "linkedin_list.html").read_text(encoding="utf-8")
    respx.get(url__regex=r".*seeMoreJobPostings/search.*").mock(
        return_value=httpx.Response(200, text=listing)
    )
    detail_111 = respx.get("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/111").mock(
        return_value=httpx.Response(999, text="blocked")
    )
    detail_222 = respx.get("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/222").mock(
        return_value=httpx.Response(200, text="<div></div>")
    )

    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)

    assert jobs == []
    assert detail_111.call_count == 1
    assert detail_222.call_count == 0
    assert source.listing_complete is False
    assert source.resume_start == 0


@respx.mock
async def test_linkedin_resumes_from_saved_cursor(store):
    company = CompanyConfig(name="IAI", source="linkedin", token="100", country="IL")
    source = LinkedInSource(company, FetchConfig(retries=1))
    source.store = store
    store.set_linkedin_cursor("IAI", 10)
    seen_starts: list[str] = []

    def list_page(request: httpx.Request) -> httpx.Response:
        start = request.url.params.get("start", "0")
        seen_starts.append(start)
        return httpx.Response(200, text="<div></div>")

    respx.get(url__regex=r".*seeMoreJobPostings/search.*").mock(side_effect=list_page)

    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)

    assert jobs == []
    assert seen_starts == ["10"]
    assert source.listing_complete is False
    assert source.resume_start is None


@respx.mock
async def test_linkedin_page_cap_resumes_past_the_cap(monkeypatch):
    monkeypatch.setattr("jobscout.sources.linkedin.MAX_PAGES", 1)
    company = CompanyConfig(name="IAI", source="linkedin", token="100", country="IL")
    source = LinkedInSource(company, FetchConfig(retries=1))
    card = """
    <div class="base-card" data-entity-urn="urn:li:jobPosting:111">
      <h3 class="base-search-card__title">Embedded Engineer</h3>
      <span class="job-search-card__location">Yehud, Israel</span>
    </div>
    """
    respx.get(url__regex=r".*seeMoreJobPostings/search.*").mock(
        return_value=httpx.Response(200, text=card)
    )
    respx.get("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/111").mock(
        return_value=httpx.Response(
            200,
            text=(
                "<h2 class='top-card-layout__title'>Embedded Engineer</h2>"
                "<div class='show-more-less-html__markup'><p>firmware</p></div>"
            ),
        )
    )

    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)

    assert len(jobs) == 1
    assert source.listing_complete is False
    assert source.resume_start == 1


def test_parse_listing_cards_reads_urn_and_location():
    html = (FIXTURES / "linkedin_list.html").read_text(encoding="utf-8")
    cards = parse_listing_cards(html)
    assert [c["id"] for c in cards] == ["111", "222"]
    assert cards[0]["location"] == "Kfar Saba, Israel"
