"""Greenhouse ATS adapter."""

from __future__ import annotations

import httpx

from jobscout.models import Job
from jobscout.sources.base import Source, html_to_text


class GreenhouseSource(Source):
    async def fetch_jobs(self, client: httpx.AsyncClient) -> list[Job]:
        token = self.company.token or ""
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        data = await self.get_json(client, url)
        jobs: list[Job] = []
        for item in data.get("jobs", []):
            loc = item.get("location") or {}
            location = loc.get("name", "") if isinstance(loc, dict) else str(loc)
            desc = item.get("content", "") or ""
            if desc and "<" in desc:
                desc = html_to_text(desc)
            jobs.append(
                self._make_job(
                    external_id=str(item.get("id", "")),
                    title=item.get("title", ""),
                    url=item.get("absolute_url", ""),
                    location=location,
                    department=(item.get("departments") or [{}])[0].get("name", "")
                    if item.get("departments")
                    else "",
                    description=desc,
                    posted_at=item.get("updated_at", "") or item.get("first_published", ""),
                )
            )
        return self._apply_job_limit(jobs)
