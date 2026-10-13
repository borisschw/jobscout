"""Tests for Greenhouse source adapter."""

import json
from pathlib import Path

import httpx
import pytest
import respx

from jobscout.config import CompanyConfig, FetchConfig
from jobscout.sources.greenhouse import GreenhouseSource

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def greenhouse_source():
    company = CompanyConfig(name="Acme", source="greenhouse", token="acme")
    return GreenhouseSource(company, FetchConfig(retries=1))


@respx.mock
async def test_greenhouse_fetches_and_normalizes_jobs(greenhouse_source):
    data = json.loads((FIXTURES / "greenhouse.json").read_text())
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true").mock(
        return_value=httpx.Response(200, json=data)
    )
    async with httpx.AsyncClient() as client:
        jobs = await greenhouse_source.fetch_jobs(client)
    assert len(jobs) == 1
    assert jobs[0].title == "Senior Backend Engineer"
    assert jobs[0].company == "Acme"
    assert jobs[0].location == "Tel Aviv, Israel"
    assert "Python" in jobs[0].description


@respx.mock
async def test_greenhouse_filters_jobs_by_search_text():
    company = CompanyConfig(
        name="Acme",
        source="greenhouse",
        token="acme",
        search_text=["embedded", "firmware"],
    )
    source = GreenhouseSource(company, FetchConfig(retries=1))
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 1,
                        "title": "Embedded Linux Engineer",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                        "location": {"name": "Tel Aviv"},
                        "content": "Board bring-up",
                    },
                    {
                        "id": 2,
                        "title": "Account Executive",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
                        "location": {"name": "Tel Aviv"},
                        "content": "Sell software",
                    },
                    {
                        "id": 3,
                        "title": "Software Engineer",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/3",
                        "location": {"name": "Tel Aviv"},
                        "content": "Write firmware for NICs",
                    },
                ]
            },
        )
    )
    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)
    assert [job.title for job in jobs] == [
        "Embedded Linux Engineer",
        "Software Engineer",
    ]


@respx.mock
async def test_greenhouse_raises_on_404(greenhouse_source):
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true").mock(
        return_value=httpx.Response(404, text="Not found")
    )
    async with httpx.AsyncClient() as client:
        from jobscout.sources.base import SourceError

        with pytest.raises(SourceError, match="404"):
            await greenhouse_source.fetch_jobs(client)
