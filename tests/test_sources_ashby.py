"""Tests for Ashby source adapter."""

import json
from pathlib import Path

import httpx
import respx

from jobscout.config import CompanyConfig, FetchConfig
from jobscout.sources.ashby import AshbySource

FIXTURES = Path(__file__).parent / "fixtures"


@respx.mock
async def test_ashby_fetches_jobs():
    company = CompanyConfig(name="Acme", source="ashby", token="acme")
    source = AshbySource(company, FetchConfig(retries=1))
    data = json.loads((FIXTURES / "ashby.json").read_text())
    respx.get(
        "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true"
    ).mock(return_value=httpx.Response(200, json=data))
    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)
    assert len(jobs) == 1
    assert jobs[0].source == "ashby"
