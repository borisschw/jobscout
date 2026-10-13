"""Generic HTML career page fallback."""

from __future__ import annotations

import re
from collections import deque
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from jobscout.models import Job
from jobscout.sources.base import Source, SourceError, html_to_text

NON_JOB_LINK_TEXT = {
    "about us", "about", "careers", "contact", "home", "privacy", "terms",
    "cookie", "cookies", "login", "sign in", "sign up", "apply now",
    "view all", "see all", "back", "next", "previous", "share",
}

POSTING_URL_RE = re.compile(
    r"/(?:job|jobs|career|careers|position|positions|opening|vacancy)/[^/?#]+",
    re.IGNORECASE,
)
POSTING_QUERY_RE = re.compile(
    r"[?&](?:job|jobId|gh_jid|posting|position)=",
    re.IGNORECASE,
)


class HtmlGenericSource(Source):
    async def fetch_jobs(self, client: httpx.AsyncClient) -> list[Job]:
        url = self.company.careers_url or ""
        if self.company.source == "browser":
            allow_browser = True
        else:
            allow_browser = (
                self.company.allow_browser
                if self.company.allow_browser is not None
                else self.fetch.allow_browser
            )

        html = await self._fetch_html(client, url, allow_browser=False)
        jobs = self._parse_html(html, url)

        if not jobs and allow_browser:
            html = await self._fetch_html(client, url, allow_browser=True)
            jobs = self._parse_html(html, url)

        if not jobs:
            if not allow_browser:
                raise SourceError(
                    f"No jobs found at {url}; browser rendering not allowed "
                    "(set fetch.allow_browser or company.allow_browser)"
                )
            raise SourceError(f"No jobs found at {url} even with browser rendering")

        return self._apply_job_limit(jobs)

    async def _fetch_html(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        allow_browser: bool,
    ) -> str:
        if allow_browser:
            return await self._render_with_browser(url)
        resp = await self._request(client, "GET", url)
        return resp.text

    async def _render_with_browser(self, url: str) -> str:
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise SourceError(
                "Playwright not installed; install with: uv sync --extra browser"
            ) from e

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=60000)
            html = await page.content()
            await browser.close()
            return html

    def _parse_html(self, html: str, base_url: str) -> list[Job]:
        jobs = self._parse_json_ld(html, base_url)
        if jobs:
            return jobs
        return self._parse_anchor_heuristics(html, base_url)

    def _parse_json_ld(self, html: str, base_url: str) -> list[Job]:
        tree = HTMLParser(html)
        jobs: list[Job] = []
        seen_urls: set[str] = set()

        for script in tree.css('script[type="application/ld+json"]'):
            text = script.text()
            if not text:
                continue
            try:
                import json

                data = json.loads(text)
            except Exception:
                continue

            queue: deque[Any] = deque([data])
            while queue:
                node = queue.popleft()
                if isinstance(node, list):
                    queue.extend(node)
                    continue
                if not isinstance(node, dict):
                    continue

                node_type = node.get("@type", "")
                if isinstance(node_type, list):
                    types = node_type
                else:
                    types = [node_type]

                if any(t == "JobPosting" for t in types):
                    job = self._job_from_json_ld(node, base_url)
                    if job and job.url not in seen_urls:
                        seen_urls.add(job.url)
                        jobs.append(job)
                    continue

                for key in ("@graph", "itemListElement", "item", "mainEntity", "hasPart"):
                    val = node.get(key)
                    if val is not None:
                        queue.append(val)

        return jobs

    def _job_from_json_ld(self, node: dict[str, Any], base_url: str) -> Job | None:
        title = node.get("title", "")
        if not title:
            return None
        url = node.get("url", "") or node.get("sameAs", "")
        if url:
            url = urljoin(base_url, url)
        else:
            url = base_url

        location = ""
        loc = node.get("jobLocation") or node.get("applicantLocationRequirements")
        if isinstance(loc, dict):
            addr = loc.get("address", loc)
            if isinstance(addr, dict):
                parts = [addr.get("addressLocality", ""), addr.get("addressCountry", "")]
                location = ", ".join(p for p in parts if p)
            else:
                location = str(addr)
        elif isinstance(loc, list) and loc:
            location = str(loc[0])

        desc = node.get("description", "") or ""
        if desc and "<" in desc:
            desc = html_to_text(desc)

        return self._make_job(
            external_id=url,
            title=title,
            url=url,
            location=location,
            description=desc,
            posted_at=node.get("datePosted", "") or "",
            employment_type=node.get("employmentType", "") or "",
        )

    def _parse_anchor_heuristics(self, html: str, base_url: str) -> list[Job]:
        tree = HTMLParser(html)
        jobs: list[Job] = []
        seen_urls: set[str] = set()

        for anchor in tree.css("a[href]"):
            href = anchor.attributes.get("href", "")
            if not href or href.startswith("#"):
                continue
            full_url = urljoin(base_url, href)
            parsed = urlparse(full_url)

            if not POSTING_URL_RE.search(parsed.path) and not POSTING_QUERY_RE.search(
                parsed.query
            ):
                continue

            title = anchor.text(strip=True)
            if not title or len(title) < 3 or len(title) > 160:
                continue
            if title.lower() in NON_JOB_LINK_TEXT:
                continue
            if full_url in seen_urls:
                continue

            location = self._guess_location(anchor)
            seen_urls.add(full_url)
            jobs.append(
                self._make_job(
                    external_id=full_url,
                    title=title,
                    url=full_url,
                    location=location,
                )
            )

        return jobs

    def _guess_location(self, anchor: Any) -> str:
        parent = anchor.parent
        if parent is None:
            return ""
        links_in_parent = parent.css("a[href]")
        if len(links_in_parent) != 1:
            return ""
        text = parent.text(strip=True)
        title = anchor.text(strip=True)
        if text.startswith(title):
            loc = text[len(title) :].strip(" -|,·")
            return loc
        return ""
