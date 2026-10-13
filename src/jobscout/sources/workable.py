"""Workable ATS adapter."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from jobscout.models import Job
from jobscout.sources.base import Source, SourceError, gather_limited, html_to_text

WIDGET_URL = "https://apply.workable.com/api/v1/widget/accounts/{token}"


def _location(item: dict[str, Any]) -> str:
    loc = item.get("location") or {}
    if isinstance(loc, str):
        return loc
    if not isinstance(loc, dict):
        return ""
    if loc.get("location_str"):
        return str(loc["location_str"])
    parts = [loc.get("city") or "", loc.get("region") or "", loc.get("country") or ""]
    text = ", ".join(str(p) for p in parts if p)
    if loc.get("telecommuting") and "remote" not in text.lower():
        text = f"{text}, Remote".strip(", ")
    return text


def _job_url(token: str, item: dict[str, Any]) -> str:
    for key in ("url", "application_url", "shortlink"):
        url = item.get(key) or ""
        if url:
            return str(url)
    shortcode = item.get("shortcode") or item.get("id") or ""
    return f"https://apply.workable.com/{token}/j/{shortcode}/"


class WorkableSource(Source):
    def _token(self) -> str:
        if self.company.token:
            return self.company.token
        url = self.company.careers_url or ""
        path = urlparse(url).path.strip("/")
        return path.split("/")[0] if path else ""

    async def fetch_jobs(self, client: httpx.AsyncClient) -> list[Job]:
        token = self._token()
        if not token:
            raise SourceError("workable source requires token or careers_url")

        listings = await self._list_jobs(client, token)

        async def to_job(item: dict[str, Any]) -> Job:
            return await self._job_from_item(client, token, item)

        jobs = await gather_limited(listings, self.fetch.concurrency, to_job)
        return self._apply_job_limit(jobs)

    async def _list_jobs(
        self,
        client: httpx.AsyncClient,
        token: str,
    ) -> list[dict[str, Any]]:
        # The v3 POST /jobs endpoint answers 429 with Retry-After of several
        # minutes. The public widget used by the careers page is the same board.
        url = WIDGET_URL.format(token=token)
        data = await self.get_json(
            client,
            url,
            headers={
                "Accept": "application/json",
                "Referer": f"https://apply.workable.com/{token}/",
            },
        )
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            return []
        return [item for item in jobs if isinstance(item, dict)]

    async def _job_from_item(
        self,
        client: httpx.AsyncClient,
        token: str,
        item: dict[str, Any],
    ) -> Job:
        shortcode = str(item.get("shortcode") or item.get("id") or "")
        title = item.get("title") or item.get("name") or ""
        location = _location(item)
        department = item.get("department") or ""
        if isinstance(department, dict):
            department = department.get("name") or department.get("label") or ""
        employment_type = item.get("employment_type") or item.get("type") or ""
        posted_at = str(item.get("created_at") or item.get("published_on") or "")
        desc = item.get("description") or item.get("full_description") or ""

        detail = await self._fetch_detail(client, token, shortcode)
        if detail:
            title = detail.get("title") or title
            if not location:
                location = _location(detail)
            department = detail.get("department") or department
            employment_type = detail.get("employment_type") or employment_type
            desc = (
                detail.get("description")
                or detail.get("full_description")
                or desc
            )
            reqs = detail.get("requirements") or ""
            if reqs and reqs not in desc:
                desc = f"{desc}\n\n{reqs}" if desc else reqs

        if desc and "<" in desc:
            desc = html_to_text(desc)

        return self._make_job(
            external_id=shortcode,
            title=str(title),
            url=_job_url(token, item),
            location=str(location),
            department=str(department),
            employment_type=str(employment_type),
            description=desc,
            posted_at=posted_at,
        )

    async def _fetch_detail(
        self,
        client: httpx.AsyncClient,
        token: str,
        shortcode: str,
    ) -> dict[str, Any] | None:
        if not shortcode:
            return None
        url = f"https://apply.workable.com/api/v2/accounts/{token}/jobs/{shortcode}"
        try:
            data = await self.get_json(
                client,
                url,
                headers={
                    "Accept": "application/json",
                    "Referer": f"https://apply.workable.com/{token}/j/{shortcode}/",
                },
            )
        except Exception:
            return None
        return data if isinstance(data, dict) else None
