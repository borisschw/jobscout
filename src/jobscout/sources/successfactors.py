"""SAP SuccessFactors Recruiting Marketing (RMK) adapter."""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import httpx
from selectolax.parser import HTMLParser

from jobscout.models import Job
from jobscout.sources.base import Source, gather_limited, html_to_text

MAX_PAGES = 40
MIN_DETAIL_LENGTH = 200
JOB_ID_RE = re.compile(r"/(\d+)/?$")


def board_origin(careers_url: str) -> str:
    parsed = urlparse(careers_url)
    if not parsed.scheme or not parsed.netloc:
        return careers_url.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}"


def job_id_from_url(url: str) -> str:
    match = JOB_ID_RE.search(urlparse(url).path)
    return match.group(1) if match else url


class SuccessFactorsSource(Source):
    def _origin(self) -> str:
        return board_origin(self.company.careers_url or self.company.host or "")

    async def fetch_jobs(self, client: httpx.AsyncClient) -> list[Job]:
        origin = self._origin()
        jobs = await self._fetch_from_tiles(client, origin)
        if not jobs:
            jobs = await self._fetch_from_sitemap(client, origin)

        async def with_detail(job: Job) -> Job:
            return await self.enrich_job(client, job)

        jobs = await gather_limited(jobs, self.fetch.concurrency, with_detail)
        return self._apply_job_limit(jobs)

    async def _fetch_from_tiles(self, client: httpx.AsyncClient, origin: str) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()
        startrow = 0
        for _ in range(MAX_PAGES):
            url = f"{origin}/tile-search-results/?startrow={startrow}"
            resp = await self._request(client, "GET", url)
            page_jobs = self._parse_tiles(resp.text, origin)
            unique_page: list[Job] = []
            for job in page_jobs:
                if job.external_id in seen:
                    continue
                seen.add(job.external_id)
                unique_page.append(job)
            if not unique_page:
                break
            jobs.extend(unique_page)
            startrow += len(page_jobs) or len(unique_page)
            if self.progress:
                self.progress.log(
                    f"    tiles startrow={startrow}: +{len(unique_page)} "
                    f"({len(jobs)} unique)"
                )
        return jobs

    def _parse_tiles(self, html: str, origin: str) -> list[Job]:
        tree = HTMLParser(html)
        jobs: list[Job] = []
        seen: set[str] = set()
        for tile in tree.css("li.job-tile"):
            href = tile.attributes.get("data-url", "") or ""
            if not href:
                link = tile.css_first("a.jobTitle-link")
                href = (link.attributes.get("href", "") if link else "") or ""
            if not href:
                continue
            url = urljoin(origin + "/", href)
            job_id = job_id_from_url(url)
            if not job_id or job_id in seen:
                continue
            seen.add(job_id)
            title_el = tile.css_first("a.jobTitle-link")
            title = title_el.text(strip=True) if title_el else ""
            city = self._section_value(tile, "city")
            country = self._section_value(tile, "country")
            location = ", ".join(p for p in (city, country) if p)
            jobs.append(
                self._make_job(
                    external_id=job_id,
                    title=title,
                    url=url,
                    location=location,
                )
            )
        return jobs

    def _section_value(self, tile: Any, field: str) -> str:
        node = tile.css_first(f".section-field.{field} [id$='-value']")
        if node is None:
            node = tile.css_first(f".section-field.{field}")
        if node is None:
            return ""
        return node.text(strip=True)

    async def _fetch_from_sitemap(self, client: httpx.AsyncClient, origin: str) -> list[Job]:
        resp = await self._request(client, "GET", f"{origin}/sitemap.xml")
        try:
            root = ElementTree.fromstring(resp.text)
        except ElementTree.ParseError:
            return []
        jobs: list[Job] = []
        seen: set[str] = set()
        for loc in root.findall(".//{*}loc"):
            url = (loc.text or "").strip()
            if "/job/" not in url:
                continue
            job_id = job_id_from_url(url)
            if not job_id or job_id in seen:
                continue
            seen.add(job_id)
            jobs.append(
                self._make_job(
                    external_id=job_id,
                    title=self._title_from_slug(url),
                    url=url,
                )
            )
        return jobs

    def _title_from_slug(self, url: str) -> str:
        path = urlparse(url).path.strip("/")
        parts = path.split("/")
        if len(parts) >= 2 and parts[0] == "job":
            slug = parts[1]
            slug = re.sub(r"[-_]+", " ", slug)
            slug = re.sub(r"\bIL\b|\bUS\b", "", slug)
            return " ".join(slug.split())
        return ""

    def _needs_detail(self, job: Job) -> bool:
        return len(job.description) < MIN_DETAIL_LENGTH

    async def enrich_job(self, client: httpx.AsyncClient, job: Job) -> Job:
        if not self._needs_detail(job):
            return job
        try:
            resp = await self._request(client, "GET", job.url)
        except Exception:
            return job
        title, location, description = self._parse_detail(resp.text, job)
        return replace(
            job,
            title=title or job.title,
            location=location or job.location,
            description=description[:12_000] if description else job.description,
        )

    def _parse_detail(self, html: str, job: Job) -> tuple[str, str, str]:
        tree = HTMLParser(html)
        title = job.title
        if not title:
            og = tree.css_first('meta[property="og:title"]')
            if og and og.attributes.get("content"):
                title = og.attributes["content"] or title
        desc = ""
        node = tree.css_first("span.jobdescription") or tree.css_first(
            '[itemprop="description"]'
        )
        if node is not None:
            desc = node.html or node.text() or ""
        if desc and "<" in desc:
            desc = html_to_text(desc)
        location = job.location
        loc_node = tree.css_first('[itemprop="jobLocation"]') or tree.css_first(
            ".joblocation"
        )
        if loc_node is not None:
            location = loc_node.text(strip=True) or location
        return title, location, desc
