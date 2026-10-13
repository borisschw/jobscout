"""Tests for the Elbit Systems jobs.json adapter."""

import json
from pathlib import Path

import httpx
import pytest
import respx

from jobscout.config import CompanyConfig, FetchConfig
from jobscout.sources.base import SourceError
from jobscout.sources.elbit import ElbitSource

FIXTURES = Path(__file__).parent / "fixtures"


@respx.mock
async def test_elbit_parses_jobs_json():
    company = CompanyConfig(
        name="Elbit Systems",
        source="elbit",
        careers_url="https://elbitsystemscareer.com",
        search_text=["embedded", "firmware", "linux", "kernel", "driver"],
    )
    source = ElbitSource(company, FetchConfig(retries=1))
    data = json.loads((FIXTURES / "elbit.json").read_text(encoding="utf-8"))
    respx.get("https://elbitsystemscareer.com/cron/jobs.json").mock(
        return_value=httpx.Response(200, json=data)
    )
    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.external_id == "21001"
    assert job.title == "Embedded Software Engineer"
    assert job.url == "https://elbitsystemscareer.com/job?jid=21001"
    assert job.location == "Haifa, Israel"
    assert "firmware" in job.description.lower()
    assert "linux kernel" in job.description.lower()
    assert "<div>" not in job.description


@respx.mock
async def test_elbit_empty_board_returns_no_jobs():
    company = CompanyConfig(
        name="Elbit Systems",
        source="elbit",
        careers_url="https://elbitsystemscareer.com",
    )
    source = ElbitSource(company, FetchConfig(retries=1))
    respx.get("https://elbitsystemscareer.com/cron/jobs.json").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)
    assert jobs == []


@respx.mock
async def test_elbit_rejects_non_list_payload():
    company = CompanyConfig(
        name="Elbit Systems",
        source="elbit",
        careers_url="https://elbitsystemscareer.com",
    )
    source = ElbitSource(company, FetchConfig(retries=1))
    respx.get("https://elbitsystemscareer.com/cron/jobs.json").mock(
        return_value=httpx.Response(200, json={"jobs": []})
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(SourceError, match="Unexpected Elbit"):
            await source.fetch_jobs(client)
