"""Tests for html_generic source."""

import httpx
import pytest
import respx

from jobscout.config import CompanyConfig, FetchConfig
from jobscout.sources.html_generic import HtmlGenericSource


JSON_LD_PAGE = """
<html><head>
<script type="application/ld+json">
{
  "@graph": [
    {"@type": "Organization", "name": "Acme"},
    {"@type": "JobPosting", "title": "Backend Engineer", "url": "/jobs/1",
     "description": "Build systems", "jobLocation": {"address": {"addressLocality": "Tel Aviv"}}}
  ]
}
</script>
</head><body></body></html>
"""


@respx.mock
async def test_html_generic_parses_json_ld_breadth_first():
    company = CompanyConfig(
        name="Acme", source="html", careers_url="https://example.com/careers"
    )
    source = HtmlGenericSource(company, FetchConfig(retries=1, allow_browser=False))
    respx.get("https://example.com/careers").mock(
        return_value=httpx.Response(200, text=JSON_LD_PAGE)
    )
    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)
    assert len(jobs) == 1
    assert jobs[0].title == "Backend Engineer"
    assert jobs[0].location == "Tel Aviv"


ANCHOR_PAGE = """
<html><body>
<a href="/careers/jobs/backend-engineer">Senior Backend Engineer</a>
<a href="/about">About Us</a>
</body></html>
"""


@respx.mock
async def test_html_generic_parses_anchor_heuristics():
    company = CompanyConfig(
        name="Acme", source="html", careers_url="https://example.com/careers"
    )
    source = HtmlGenericSource(company, FetchConfig(retries=1, allow_browser=False))
    respx.get("https://example.com/careers").mock(
        return_value=httpx.Response(200, text=ANCHOR_PAGE)
    )
    async with httpx.AsyncClient() as client:
        jobs = await source.fetch_jobs(client)
    assert len(jobs) == 1
    assert "Backend" in jobs[0].title


@respx.mock
async def test_html_generic_reports_browser_not_allowed():
    company = CompanyConfig(
        name="Acme", source="html", careers_url="https://example.com/careers"
    )
    source = HtmlGenericSource(company, FetchConfig(retries=1, allow_browser=False))
    respx.get("https://example.com/careers").mock(
        return_value=httpx.Response(200, text="<html><body>No jobs</body></html>")
    )
    async with httpx.AsyncClient() as client:
        from jobscout.sources.base import SourceError

        with pytest.raises(SourceError, match="browser rendering not allowed"):
            await source.fetch_jobs(client)
