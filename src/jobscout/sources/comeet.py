"""Comeet ATS adapter."""

from __future__ import annotations

import httpx

from jobscout.models import Job
from jobscout.sources.base import Source, html_to_text


def _parse_comeet_details(details: object) -> str:
    if not details:
        return ""
    if isinstance(details, dict):
        return str(details.get("description", "") or "")
    if isinstance(details, list):
        parts: list[str] = []
        for section in details:
            if not isinstance(section, dict):
                continue
            name = str(section.get("name", "") or "").strip()
            value = str(section.get("value", "") or "").strip()
            if not value:
                continue
            parts.append(f"{name}\n{value}" if name else value)
        return "\n\n".join(parts)
    return ""


class ComeetSource(Source):
    async def fetch_jobs(self, client: httpx.AsyncClient) -> list[Job]:
        uid = self.company.company_uid or ""
        token = self.company.token or ""
        url = (
            f"https://www.comeet.co/careers-api/2.0/company/{uid}/positions"
            f"?token={token}&details=true"
        )
        data = await self.get_json(client, url)
        jobs: list[Job] = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("is_internal"):
                continue
            desc = _parse_comeet_details(item.get("details"))
            position_id = str(item.get("uid") or item.get("position_uid") or "")
            if desc and "<" in desc:
                desc = html_to_text(desc)
            location_obj = item.get("location") or {}
            if isinstance(location_obj, dict):
                location_parts = [
                    location_obj.get("city", "") or location_obj.get("name", ""),
                    location_obj.get("country", ""),
                ]
            else:
                location_parts = [str(location_obj)]
            location = ", ".join(p for p in location_parts if p)
            jobs.append(
                self._make_job(
                    external_id=position_id,
                    title=item.get("name", ""),
                    url=item.get("url_active_page", "") or item.get("url_comeet_hosted_page", ""),
                    location=location,
                    department=item.get("department", "") or "",
                    employment_type=item.get("employment_type", "") or "",
                    description=desc,
                    posted_at=item.get("time_updated", "") or "",
                )
            )
        return self._apply_job_limit(jobs)
