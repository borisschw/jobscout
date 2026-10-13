"""Elbit Systems career-site adapter (public jobs.json dump)."""

from __future__ import annotations

from html import unescape
from typing import Any
from urllib.parse import urlparse

import httpx

from jobscout.models import Job
from jobscout.sources.base import Source, SourceError, html_to_text

JOBS_PATH = "/cron/jobs.json"


def board_origin(careers_url: str) -> str:
    parsed = urlparse(careers_url)
    if not parsed.scheme or not parsed.netloc:
        return careers_url.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}"


class ElbitSource(Source):
    def _origin(self) -> str:
        return board_origin(self.company.careers_url or "")

    async def fetch_jobs(self, client: httpx.AsyncClient) -> list[Job]:
        origin = self._origin()
        if not origin:
            raise SourceError("Elbit source requires careers_url")
        data = await self.get_json(client, f"{origin}{JOBS_PATH}")
        if not isinstance(data, list):
            raise SourceError(f"Unexpected Elbit jobs payload from {origin}{JOBS_PATH}")

        jobs: list[Job] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            job_id = item.get("jobId")
            title = (item.get("jobTitle") or "").strip()
            if not job_id or not title:
                continue
            jobs.append(
                self._make_job(
                    external_id=str(job_id),
                    title=title,
                    url=f"{origin}/job?jid={job_id}",
                    location=_location(item),
                    department=str(item.get("employerName") or ""),
                    employment_type=str(item.get("employmentType") or ""),
                    description=_description(item),
                    posted_at=str(item.get("openDate") or item.get("updateDate") or ""),
                )
            )
        return self._apply_job_limit(jobs)


def _location(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("locationAddress", "area"):
        value = str(item.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    joined = ", ".join(parts)
    if "israel" not in joined.lower():
        parts.append("Israel")
    return ", ".join(parts)


def _description(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("description", "requirements", "skills"):
        text = _plain(item.get(key) or "")
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _plain(value: Any) -> str:
    text = unescape(str(value or ""))
    if "&lt;" in text or "&amp;" in text:
        text = unescape(text)
    if "<" in text:
        text = html_to_text(text)
    return text.strip()
