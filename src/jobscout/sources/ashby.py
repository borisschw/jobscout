"""Ashby ATS adapter."""

from __future__ import annotations

import httpx

from jobscout.models import Job
from jobscout.sources.base import Source, html_to_text


class AshbySource(Source):
    async def fetch_jobs(self, client: httpx.AsyncClient) -> list[Job]:
        token = self.company.token or ""
        url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
        data = await self.get_json(client, url)
        jobs: list[Job] = []
        for item in data.get("jobs", []):
            desc = item.get("descriptionHtml", "") or item.get("descriptionPlain", "") or ""
            if desc and "<" in desc:
                desc = html_to_text(desc)
            location = item.get("location", "")
            if isinstance(location, dict):
                location = location.get("name", "")
            jobs.append(
                self._make_job(
                    external_id=str(item.get("id", "")),
                    title=item.get("title", ""),
                    url=item.get("jobUrl", "") or item.get("applyUrl", ""),
                    location=str(location),
                    department=item.get("department", "") or "",
                    employment_type=item.get("employmentType", "") or "",
                    description=desc,
                    posted_at=item.get("publishedAt", "") or "",
                )
            )
        return self._apply_job_limit(jobs)
