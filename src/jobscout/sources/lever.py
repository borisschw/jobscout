"""Lever ATS adapter."""

from __future__ import annotations

import httpx

from jobscout.models import Job
from jobscout.sources.base import Source, html_to_text


class LeverSource(Source):
    async def fetch_jobs(self, client: httpx.AsyncClient) -> list[Job]:
        token = self.company.token or ""
        host = "api.eu.lever.co" if self.company.region == "eu" else "api.lever.co"
        url = f"https://{host}/v0/postings/{token}?mode=json"
        data = await self.get_json(client, url)
        jobs: list[Job] = []
        for item in data if isinstance(data, list) else []:
            desc = item.get("descriptionPlain", "") or item.get("description", "") or ""
            if desc and "<" in desc:
                desc = html_to_text(desc)
            categories = item.get("categories") or {}
            jobs.append(
                self._make_job(
                    external_id=str(item.get("id", "")),
                    title=item.get("text", ""),
                    url=item.get("hostedUrl", "") or item.get("applyUrl", ""),
                    location=categories.get("location", ""),
                    department=categories.get("department", ""),
                    employment_type=categories.get("commitment", ""),
                    description=desc,
                    posted_at=str(item.get("createdAt", "")),
                )
            )
        return self._apply_job_limit(jobs)
