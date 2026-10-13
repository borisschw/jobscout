"""Tests for Lever source adapter."""

import json
from pathlib import Path

import httpx
import respx

from jobscout.config import CompanyConfig, FetchConfig
from jobscout.sources.lever import LeverSource

FIXTURES = Path(__file__).parent / "fixtures"


@respx.mock
async def test_lever_fetches_us_board():
    company = CompanyConfig(name="Acme", source="lever", token="acme", region="us")
    source = LeverSource(company, FetchConfig(retries=1))
    data = json.loads((FIXTURES / "lever.json").read_text())
    respx.get("https://api.lever.co/v0/postings/acme?mode=json").mock(
        return_value=httpx.Response(200, json=data)
    )
    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)
    assert len(jobs) == 1
    assert jobs[0].title == "Platform Engineer"


@respx.mock
async def test_lever_uses_eu_host():
    company = CompanyConfig(name="Acme", source="lever", token="acme", region="eu")
    source = LeverSource(company, FetchConfig(retries=1))
    data = json.loads((FIXTURES / "lever.json").read_text())
    route = respx.get("https://api.eu.lever.co/v0/postings/acme?mode=json").mock(
        return_value=httpx.Response(200, json=data)
    )
    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)
    assert route.called
    assert len(jobs) == 1
