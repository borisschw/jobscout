"""SmartRecruiters ATS adapter."""

from __future__ import annotations

import httpx

from jobscout.models import Job
from jobscout.sources.base import Source, gather_limited, html_to_text


class SmartRecruitersSource(Source):
    async def fetch_jobs(self, client: httpx.AsyncClient) -> list[Job]:
        token = self.company.token or ""
        all_postings: list[dict] = []
        offset = 0
        limit = 100
        total = None

        while True:
            url = (
                f"https://api.smartrecruiters.com/v1/companies/{token}/postings"
                f"?limit={limit}&offset={offset}"
            )
            data = await self.get_json(client, url)
            content = data.get("content", [])
            all_postings.extend(content)
            total = data.get("totalFound", len(all_postings))
            offset += limit
            if offset >= total or not content:
                break

        async def fetch_detail(posting: dict) -> Job:
            pid = posting.get("id", "")
            detail_url = (
                f"https://api.smartrecruiters.com/v1/companies/{token}/postings/{pid}"
            )
            desc = ""
            try:
                detail = await self.get_json(client, detail_url)
                desc = detail.get("jobAd", {}).get("sections", {}).get("jobDescription", {}).get(
                    "text", ""
                ) or detail.get("jobAd", {}).get("sections", {}).get("companyDescription", {}).get(
                    "text", ""
                ) or ""
            except Exception:
                desc = posting.get("jobAd", {}).get("sections", {}).get("jobDescription", {}).get(
                    "text", ""
                ) or ""

            if desc and "<" in desc:
                desc = html_to_text(desc)

            loc = posting.get("location") or {}
            location = loc.get("city", "")
            if loc.get("country"):
                location = f"{location}, {loc['country']}".strip(", ")

            return self._make_job(
                external_id=str(pid),
                title=posting.get("name", ""),
                url=posting.get("ref", "") or posting.get("postingUrl", ""),
                location=location,
                department=(posting.get("department") or {}).get("label", ""),
                employment_type=(posting.get("typeOfEmployment") or {}).get("label", ""),
                description=desc,
                posted_at=posting.get("releasedDate", "") or "",
            )

        jobs = await gather_limited(
            all_postings,
            self.fetch.concurrency,
            fetch_detail,
        )
        return self._apply_job_limit(jobs)
