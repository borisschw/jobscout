"""Workday ATS adapter."""

from __future__ import annotations

from dataclasses import replace

import httpx

from jobscout.models import Job
from jobscout.search import normalize_search_terms, text_matches_search_terms
from jobscout.sources.base import Source, html_to_text

MAX_PAGES = 100
PAGE_SIZE = 20
MIN_DETAIL_LENGTH = 200


def build_workday_search_query(terms: list[str]) -> str:
    """Build a single Workday searchText value from configured terms."""
    concrete = [t for t in terms if t]
    if not concrete:
        return ""
    if len(concrete) == 1:
        return concrete[0]
    return " OR ".join(concrete)


def public_workday_job_url(host: str, site: str, external_id: str) -> str:
    """Build a browser-facing Workday job URL."""
    base = f"https://{host.rstrip('/')}"
    path = external_id if external_id.startswith("/") else f"/{external_id}"
    return f"{base}/{site}{path}"


def workday_url_needs_repair(url: str, host: str, site: str, external_id: str) -> bool:
    """True when url omits the career site segment Workday expects."""
    if not external_id.startswith("/job/"):
        return False
    expected = public_workday_job_url(host, site, external_id)
    if url == expected:
        return False
    broken_prefix = f"https://{host.rstrip('/')}/job/"
    return url.startswith(broken_prefix)


def repair_workday_job(job: Job, host: str, site: str) -> Job:
    if job.source != "workday" or not host or not site:
        return job
    if not workday_url_needs_repair(job.url, host, site, job.external_id):
        return job
    return replace(job, url=public_workday_job_url(host, site, job.external_id))


def posting_matches_search_terms(posting: dict, terms: list[str]) -> bool:
    """Keep listings that match any configured term (title/location)."""
    haystack = " ".join(
        [
            posting.get("title", "") or "",
            posting.get("locationsText", "") or posting.get("location", "") or "",
        ]
    )
    return text_matches_search_terms(haystack, terms)


class WorkdaySource(Source):
    def _base_url(self) -> str:
        return f"https://{self.company.host or ''}"

    def _detail_api_url(self, path: str) -> str:
        tenant = self.company.tenant or ""
        site = self.company.site or ""
        return f"{self._base_url()}/wday/cxs/{tenant}/{site}{path}"

    def _public_job_url(self, path: str) -> str:
        return public_workday_job_url(
            self.company.host or "",
            self.company.site or "",
            path,
        )

    def _needs_detail(self, job: Job) -> bool:
        if not job.external_id.startswith("/job/"):
            return False
        return len(job.description) < MIN_DETAIL_LENGTH

    def _job_from_posting(self, posting: dict) -> Job:
        path = posting.get("externalPath", "")
        location = posting.get("locationsText", "") or posting.get("location", "")
        return self._make_job(
            external_id=path,
            title=posting.get("title", ""),
            url=self._public_job_url(path),
            location=str(location),
            department=posting.get("department", "") or "",
            description="",
            posted_at=posting.get("postedOn", "") or "",
        )

    async def fetch_jobs(self, client: httpx.AsyncClient) -> list[Job]:
        tenant = self.company.tenant or ""
        site = self.company.site or ""
        api_url = f"{self._base_url()}/wday/cxs/{tenant}/{site}/jobs"

        search_terms = normalize_search_terms(self.company.search_text)
        search_query = build_workday_search_query(search_terms)
        label = search_query or "(all jobs)"
        if self.progress:
            self.progress.log(f"  Workday search {label!r}…")

        all_postings: dict[str, dict] = {}
        offset = 0
        pages = 0
        total: int | None = None

        while pages < MAX_PAGES:
            body = {
                "appliedFacets": {},
                "limit": PAGE_SIZE,
                "offset": offset,
                "searchText": search_query,
            }
            data = await self.post_json(client, api_url, body)
            postings = data.get("jobPostings", [])
            if total is None:
                total = data.get("total", 0)

            matched = 0
            for p in postings:
                if not posting_matches_search_terms(p, search_terms):
                    continue
                path = p.get("externalPath", "")
                if path:
                    all_postings[path] = p
                    matched += 1

            offset += PAGE_SIZE
            pages += 1
            if self.progress:
                total_label = str(total) if total else "?"
                self.progress.log(
                    f"    page {pages}: +{matched}/{len(postings)} matched "
                    f"({len(all_postings)} unique, total≈{total_label})"
                )
            if not postings:
                break
            if total and offset >= total:
                break
            if len(postings) < PAGE_SIZE and (
                not total or offset + len(postings) >= total
            ):
                break

        jobs = [self._job_from_posting(p) for p in all_postings.values()]
        return self._apply_job_limit(jobs)

    async def enrich_job(self, client: httpx.AsyncClient, job: Job) -> Job:
        if not self._needs_detail(job):
            return job

        path = job.external_id
        title = job.title
        location = job.location
        description = job.description
        url = job.url

        try:
            detail = await self.get_json(client, self._detail_api_url(path))
            info = detail.get("jobPostingInfo", {})
            if info.get("jobDescription"):
                description = html_to_text(info["jobDescription"])
            title = info.get("title", title) or title
            location = info.get("location", location) or location
            if info.get("externalUrl"):
                url = info["externalUrl"]
        except Exception:
            pass

        return replace(
            job,
            title=title,
            url=url,
            location=str(location),
            description=description[:12_000] if description else "",
        )
