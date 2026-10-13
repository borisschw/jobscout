"""Amazon jobs adapter."""

from __future__ import annotations

from typing import Any

import httpx

from jobscout.models import Job
from jobscout.sources.base import Source, html_to_text

_SEARCH_URL = "https://www.amazon.jobs/en/search.json"
_PAGE_SIZE = 100
_MAX_OFFSET = 2000


class AmazonSource(Source):
    async def fetch_jobs(self, client: httpx.AsyncClient) -> list[Job]:
        country = self.company.country or ""
        all_items: list[dict[str, Any]] = []
        offset = 0

        while offset <= _MAX_OFFSET:
            url = (
                f"{_SEARCH_URL}"
                f"?normalized_country_code[]={country}"
                f"&base_query=&result_limit={_PAGE_SIZE}&offset={offset}"
            )
            data = await self.get_json(client, url)
            items, total = _page_items(data)
            if not items:
                break
            all_items.extend(items)
            offset += len(items)
            if total and offset >= total:
                break

        jobs: list[Job] = []
        for item in all_items:
            jobs.append(
                self._make_job(
                    external_id=str(item.get("id_icims", "") or item.get("id", "")),
                    title=item.get("title", "") or "",
                    url=_job_url(item),
                    location=_job_location(item),
                    department=(
                        item.get("job_category")
                        or item.get("business_category")
                        or item.get("category")
                        or ""
                    ),
                    employment_type=item.get("job_schedule_type") or "",
                    description=_job_description(item),
                    posted_at=item.get("posted_date") or "",
                )
            )
        return self._apply_job_limit(jobs)


def _page_items(data: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    jobs = data.get("jobs")
    if isinstance(jobs, list):
        total = data.get("hits")
        if not isinstance(total, int):
            total = data.get("hitsFound", len(jobs))
        return jobs, int(total or 0)

    hits = data.get("hits")
    if isinstance(hits, list):
        total = data.get("hitsFound", len(hits))
        return hits, int(total or 0)
    return [], 0


def _job_url(item: dict[str, Any]) -> str:
    path = item.get("job_path") or ""
    if path:
        return f"https://www.amazon.jobs{path}"
    return item.get("url_next_step") or ""


def _job_location(item: dict[str, Any]) -> str:
    location = (item.get("location") or "").strip()
    if location:
        return location
    city = item.get("city") or ""
    country = item.get("country_code") or ""
    return f"{city}, {country}".strip(", ")


def _job_description(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("description", "basic_qualifications", "preferred_qualifications"):
        text = _plain(item.get(key) or "")
        if text:
            parts.append(text)
    if parts:
        return "\n\n".join(parts)
    return _plain(item.get("description_short") or "")


def _plain(value: str) -> str:
    if value and "<" in value:
        return html_to_text(value)
    return value
