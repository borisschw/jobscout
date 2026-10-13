"""Tests for Workable, SuccessFactors, and Teamme adapters."""

import json
from pathlib import Path

import httpx
import respx

from jobscout.config import CompanyConfig, FetchConfig
from jobscout.sources.successfactors import SuccessFactorsSource
from jobscout.sources.teamme import TeammeSource
from jobscout.sources.workable import WorkableSource

FIXTURES = Path(__file__).parent / "fixtures"


@respx.mock
async def test_workable_paginates_and_fetches_details():
    company = CompanyConfig(name="Acme", source="workable", token="acme")
    source = WorkableSource(company, FetchConfig(retries=1, concurrency=2))
    page1 = json.loads((FIXTURES / "workable_list.json").read_text())
    page2 = json.loads((FIXTURES / "workable_list_p2.json").read_text())
    detail = json.loads((FIXTURES / "workable_detail.json").read_text())
    widget = {"name": "Acme", "jobs": page1["results"] + page2["results"]}

    respx.get("https://apply.workable.com/api/v1/widget/accounts/acme").mock(
        return_value=httpx.Response(200, json=widget)
    )
    respx.get("https://apply.workable.com/api/v2/accounts/acme/jobs/ABC111").mock(
        return_value=httpx.Response(200, json=detail)
    )
    respx.get("https://apply.workable.com/api/v2/accounts/acme/jobs/DEF222").mock(
        return_value=httpx.Response(404, text="missing")
    )

    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)

    assert {job.external_id for job in jobs} == {"ABC111", "DEF222"}
    fw = next(job for job in jobs if job.external_id == "ABC111")
    assert fw.location == "Tel Aviv, Israel"
    assert "embedded C firmware" in fw.description
    assert "Linux kernel" in fw.description
    linux = next(job for job in jobs if job.external_id == "DEF222")
    assert linux.title == "Linux Driver Engineer"


@respx.mock
async def test_workable_empty_board_returns_no_jobs():
    company = CompanyConfig(name="Vayyar", source="workable", token="vayyar")
    source = WorkableSource(company, FetchConfig(retries=1))
    respx.get("https://apply.workable.com/api/v1/widget/accounts/vayyar").mock(
        return_value=httpx.Response(200, json={"name": "Vayyar", "jobs": []})
    )
    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)
    assert jobs == []


@respx.mock
async def test_successfactors_parses_tiles_and_details():
    company = CompanyConfig(
        name="Stratasys",
        source="successfactors",
        careers_url="https://careers.example.com",
    )
    source = SuccessFactorsSource(company, FetchConfig(retries=1, concurrency=2))
    tiles = (FIXTURES / "successfactors_tiles.html").read_text()
    detail = (FIXTURES / "successfactors_detail.html").read_text()

    def tiles_page(request: httpx.Request) -> httpx.Response:
        if "startrow=0" in str(request.url):
            return httpx.Response(200, text=tiles)
        return httpx.Response(200, text="<!DOCTYPE html>")

    respx.get(url__regex=r".*tile-search-results.*").mock(side_effect=tiles_page)
    respx.get(url__regex=r".*/job/.*").mock(return_value=httpx.Response(200, text=detail))

    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)

    assert len(jobs) == 2
    by_id = {job.external_id: job for job in jobs}
    assert by_id["1411791700"].title == "Maintenance Electrician"
    assert by_id["1411791700"].location == "Kiryat Gat, IL"
    assert "embedded Linux" in by_id["1390554100"].description


@respx.mock
async def test_successfactors_falls_back_to_sitemap():
    company = CompanyConfig(
        name="Stratasys",
        source="successfactors",
        careers_url="https://careers.example.com",
    )
    source = SuccessFactorsSource(company, FetchConfig(retries=1, concurrency=2))
    sitemap = (FIXTURES / "successfactors_sitemap.xml").read_text()
    detail = (FIXTURES / "successfactors_detail.html").read_text()
    respx.get("https://careers.example.com/tile-search-results/?startrow=0").mock(
        return_value=httpx.Response(200, text="<html></html>")
    )
    respx.get("https://careers.example.com/sitemap.xml").mock(
        return_value=httpx.Response(200, text=sitemap)
    )
    respx.get("https://careers.example.com/job/Rehovot-Firmware-Engineer-IL/555/").mock(
        return_value=httpx.Response(200, text=detail)
    )
    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)
    assert len(jobs) == 1
    assert jobs[0].external_id == "555"
    assert "Firmware Engineer" in jobs[0].title


@respx.mock
async def test_teamme_reads_sitemap_and_json_ld():
    company = CompanyConfig(
        name="Quantum Machines",
        source="teamme",
        careers_url="https://qm.teamme.link",
    )
    source = TeammeSource(company, FetchConfig(retries=1, concurrency=2))
    sitemap = (FIXTURES / "teamme_sitemap.xml").read_text()
    page = (FIXTURES / "teamme_job.html").read_text()
    respx.get("https://qm.teamme.link/sitemap.xml").mock(
        return_value=httpx.Response(200, text=sitemap)
    )
    respx.get("https://qm.teamme.link/jobs/CC.F46").mock(
        return_value=httpx.Response(200, text=page)
    )
    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)
    assert len(jobs) == 1
    assert jobs[0].external_id == "CC.F46"
    assert jobs[0].title == "Compiler Engineer"
    assert jobs[0].location == "Tel Aviv, Israel"
    assert "quantum processor" in jobs[0].description
    assert "Linux kernel" in jobs[0].description
