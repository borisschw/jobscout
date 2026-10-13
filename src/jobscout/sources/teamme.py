"""Teamme career-page adapter (sitemap + JobPosting JSON-LD)."""

from __future__ import annotations

import json
import re
from collections import deque
from dataclasses import replace
from typing import Any
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import httpx
from selectolax.parser import HTMLParser

from jobscout.models import Job
from jobscout.sources.base import Source, gather_limited, html_to_text

JOB_PATH_RE = re.compile(r"/jobs/([A-Za-z0-9.]+)")
RSC_HTML_STRING_RE = re.compile(r'"((?:[^"\\]|\\.){200,})"')


def board_origin(careers_url: str) -> str:
    parsed = urlparse(careers_url)
    if not parsed.scheme or not parsed.netloc:
        return careers_url.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}"


class TeammeSource(Source):
    def _origin(self) -> str:
        url = self.company.careers_url or ""
        if self.company.token and not url:
            return f"https://{self.company.token}.teamme.link"
        return board_origin(url)

    async def fetch_jobs(self, client: httpx.AsyncClient) -> list[Job]:
        origin = self._origin()
        listing_urls = await self._list_job_urls(client, origin)
        async def to_job(url: str) -> Job | None:
            return await self._job_from_page(client, url)

        fetched = await gather_limited(listing_urls, self.fetch.concurrency, to_job)
        jobs = [job for job in fetched if job is not None]
        return self._apply_job_limit(jobs)

    async def _list_job_urls(self, client: httpx.AsyncClient, origin: str) -> list[str]:
        urls = await self._urls_from_sitemap(client, origin)
        if urls:
            return urls
        resp = await self._request(client, "GET", origin + "/")
        return self._urls_from_html(resp.text, origin)

    async def _urls_from_sitemap(self, client: httpx.AsyncClient, origin: str) -> list[str]:
        try:
            resp = await self._request(client, "GET", f"{origin}/sitemap.xml")
        except Exception:
            return []
        try:
            root = ElementTree.fromstring(resp.text)
        except ElementTree.ParseError:
            return []
        urls: list[str] = []
        seen: set[str] = set()
        for loc in root.findall(".//{*}loc"):
            url = (loc.text or "").strip()
            if JOB_PATH_RE.search(urlparse(url).path) and url not in seen:
                seen.add(url)
                urls.append(url)
        return urls

    def _urls_from_html(self, html: str, origin: str) -> list[str]:
        seen: set[str] = set()
        urls: list[str] = []
        for match in JOB_PATH_RE.finditer(html):
            url = urljoin(origin + "/", f"jobs/{match.group(1)}")
            if url not in seen:
                seen.add(url)
                urls.append(url)
        return urls

    async def _job_from_page(self, client: httpx.AsyncClient, url: str) -> Job | None:
        try:
            resp = await self._request(client, "GET", url)
        except Exception:
            return None
        job = self._job_from_json_ld(resp.text, url)
        if job is None:
            job = self._job_from_meta(resp.text, url)
        if job is None:
            return None
        rsc_desc = self._description_from_rsc(resp.text)
        if len(rsc_desc) > len(job.description):
            job = replace(job, description=rsc_desc[:12_000])
        return job

    def _job_from_json_ld(self, html: str, page_url: str) -> Job | None:
        tree = HTMLParser(html)
        for script in tree.css('script[type="application/ld+json"]'):
            text = script.text()
            if not text:
                continue
            try:
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
                types = node_type if isinstance(node_type, list) else [node_type]
                if any(t == "JobPosting" for t in types):
                    return self._job_from_posting(node, page_url)
                for key in ("@graph", "itemListElement", "item", "mainEntity"):
                    val = node.get(key)
                    if val is not None:
                        queue.append(val)
        return None

    def _job_from_posting(self, node: dict[str, Any], page_url: str) -> Job | None:
        title = node.get("title") or ""
        if not title:
            return None
        url = node.get("url") or page_url
        desc = node.get("description") or ""
        if desc and "<" in desc:
            desc = html_to_text(desc)
        location = self._location_from_ld(node)
        match = JOB_PATH_RE.search(urlparse(str(url)).path) or JOB_PATH_RE.search(
            urlparse(page_url).path
        )
        external_id = match.group(1) if match else page_url
        return self._make_job(
            external_id=external_id,
            title=str(title),
            url=str(url),
            location=location,
            employment_type=str(node.get("employmentType") or ""),
            description=desc,
            posted_at=str(node.get("datePosted") or ""),
        )

    def _location_from_ld(self, node: dict[str, Any]) -> str:
        loc = node.get("jobLocation") or node.get("applicantLocationRequirements")
        if isinstance(loc, list) and loc:
            loc = loc[0]
        if not isinstance(loc, dict):
            return str(loc) if loc else ""
        addr = loc.get("address", loc)
        if isinstance(addr, dict):
            parts = [
                addr.get("addressLocality") or "",
                addr.get("addressRegion") or "",
                addr.get("addressCountry") or "",
            ]
            return ", ".join(str(p) for p in parts if p)
        return str(addr) if addr else ""

    def _job_from_meta(self, html: str, page_url: str) -> Job | None:
        tree = HTMLParser(html)
        title = ""
        og = tree.css_first('meta[property="og:title"]')
        if og:
            title = (og.attributes.get("content") or "").split(" - ")[0].strip()
        if not title:
            tag = tree.css_first("title")
            title = (tag.text(strip=True) if tag else "").split(" - ")[0].strip()
        if not title:
            return None
        desc = ""
        meta = tree.css_first('meta[name="description"]')
        if meta:
            desc = meta.attributes.get("content") or ""
        match = JOB_PATH_RE.search(urlparse(page_url).path)
        external_id = match.group(1) if match else page_url
        return self._make_job(
            external_id=external_id,
            title=title,
            url=page_url,
            description=desc,
        )

    def _description_from_rsc(self, html: str) -> str:
        """Pull the full JD out of Next.js RSC payloads when JSON-LD is a teaser."""
        best = ""
        for match in RSC_HTML_STRING_RE.finditer(html):
            raw = match.group(1)
            if "u003c" not in raw and "<p" not in raw.lower() and "<li" not in raw.lower():
                continue
            decoded = (
                raw.replace("\\u003c", "<")
                .replace("\\u003e", ">")
                .replace("\\u0026", "&")
                .replace("\\n", "\n")
                .replace('\\"', '"')
            )
            text = html_to_text(decoded) if "<" in decoded else decoded
            if len(text) > len(best):
                best = text
        return best
