"""LinkedIn guest jobs adapter (public job cards, no login)."""

from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import urlencode

import httpx
from selectolax.parser import HTMLParser

from jobscout.models import Job
from jobscout.sources.base import Source, SourceError, SourceRateLimit, html_to_text

LIST_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
JOB_URL = "https://www.linkedin.com/jobs/view/{job_id}"
MAX_PAGES = 40
JOB_URN_RE = re.compile(r"urn:li:jobPosting:(\d+)")
# 999 is a block: stop this company on the first response.
LINKEDIN_BLOCK = frozenset({999})
# 429 is a short limit. Retry a few times, then stop and resume next run.
LINKEDIN_SLOW = frozenset({429})
LINKEDIN_429_RETRIES = 3
LINKEDIN_429_ATTEMPTS = LINKEDIN_429_RETRIES + 1

GEO_IDS = {
    "il": "101620260",
    "isr": "101620260",
    "israel": "101620260",
}


class LinkedInSource(Source):
    def _company_id(self) -> str:
        token = (self.company.token or "").strip()
        if not token:
            raise SourceError("linkedin source requires token (numeric company id)")
        return token

    def _list_params(self, start: int) -> dict[str, str]:
        params = {"f_C": self._company_id(), "start": str(start)}
        country = (self.company.country or "").strip()
        if country:
            geo = GEO_IDS.get(country.lower())
            if geo:
                params["geoId"] = geo
            elif country.isdigit():
                params["geoId"] = country
            else:
                params["location"] = country
        return params

    async def fetch_jobs(self, client: httpx.AsyncClient) -> list[Job]:
        start_at = self.store.linkedin_cursor(self.company.name) if self.store is not None else 0
        if start_at and self.progress:
            self.progress.log(f"    linkedin resuming at start={start_at}")

        jobs: list[Job] = []
        seen: set[str] = set()
        start = start_at
        finished = False
        stopped_at: int | None = None
        reused = 0

        for _ in range(MAX_PAGES):
            url = f"{LIST_URL}?{urlencode(self._list_params(start))}"
            try:
                resp = await self._request(client, "GET", url, **self._linkedin_limits())
            except SourceRateLimit as exc:
                stopped_at = start
                self._log_stop(exc.status_code, start)
                break
            page = parse_listing_cards(resp.text)
            unique_page = [item for item in page if item["id"] not in seen]
            if not unique_page:
                finished = True
                break
            limited = False
            for item in unique_page:
                seen.add(item["id"])
                try:
                    job, was_reused = await self._job_from_listing(client, item)
                except SourceRateLimit as exc:
                    stopped_at = start
                    self._log_stop(exc.status_code, start)
                    limited = True
                    break
                jobs.append(job)
                if was_reused:
                    reused += 1
            if limited:
                break
            start += len(page)
            if self.progress:
                self.progress.log(
                    f"    linkedin start={start}: +{len(unique_page)} ({len(seen)} unique)"
                )
        else:
            stopped_at = start
            if self.progress:
                self.progress.log(
                    f"    linkedin page cap at start={start}; next run continues there"
                )

        if reused and self.progress:
            self.progress.log(
                f"    linkedin reused {reused} stored job(s) without a detail fetch"
            )

        # A run that began in the middle has not seen the earlier pages, so it
        # must not be treated as a full scan even when it reaches the end.
        self.listing_complete = finished and start_at == 0
        self.resume_start = stopped_at
        if finished and start_at and self.progress:
            self.progress.log("    linkedin caught up; next run starts from the first page")
        return self._apply_job_limit(jobs)

    async def _job_from_listing(
        self, client: httpx.AsyncClient, item: dict[str, str]
    ) -> tuple[Job, bool]:
        stored = self._stored_job(item["id"])
        if stored is not None and stored.description.strip():
            return self._refresh_stored(stored, item), True
        return await self._hydrate(client, item), False

    def _stored_job(self, external_id: str) -> Job | None:
        if self.store is None:
            return None
        return self.store.find_job(self.company.name, self.company.source, external_id)

    def _refresh_stored(self, stored: Job, item: dict[str, str]) -> Job:
        return replace(
            stored,
            title=item.get("title") or stored.title,
            location=item.get("location") or stored.location,
            posted_at=item.get("posted_at") or stored.posted_at,
            url=JOB_URL.format(job_id=item["id"]),
        )

    def _linkedin_limits(self) -> dict[str, frozenset[int] | int]:
        return {
            "stop_statuses": LINKEDIN_BLOCK,
            "retry_then_stop": LINKEDIN_SLOW,
            "retry_then_stop_attempts": LINKEDIN_429_ATTEMPTS,
        }

    def _log_stop(self, status_code: int, start: int) -> None:
        if self.progress:
            self.progress.log(
                f"    linkedin HTTP {status_code} at start={start}; next run continues there"
            )

    async def _hydrate(self, client: httpx.AsyncClient, item: dict[str, str]) -> Job:
        job_id = item["id"]
        title = item.get("title") or ""
        location = item.get("location") or ""
        description = ""
        posted_at = item.get("posted_at") or ""
        try:
            resp = await self._request(
                client,
                "GET",
                DETAIL_URL.format(job_id=job_id),
                **self._linkedin_limits(),
            )
            detail = parse_job_detail(resp.text)
            title = detail.get("title") or title
            location = detail.get("location") or location
            description = detail.get("description") or ""
            posted_at = detail.get("posted_at") or posted_at
        except SourceError as exc:
            if isinstance(exc, SourceRateLimit):
                raise
        return self._make_job(
            external_id=job_id,
            title=title,
            url=JOB_URL.format(job_id=job_id),
            location=location,
            description=description,
            posted_at=posted_at,
        )


def parse_listing_cards(html: str) -> list[dict[str, str]]:
    tree = HTMLParser(html)
    cards: list[dict[str, str]] = []
    for card in tree.css("[data-entity-urn*='jobPosting']"):
        urn = card.attributes.get("data-entity-urn") or ""
        match = JOB_URN_RE.search(urn)
        if not match:
            continue
        title_node = card.css_first(".base-search-card__title")
        loc_node = card.css_first(".job-search-card__location")
        time_node = card.css_first("time")
        cards.append(
            {
                "id": match.group(1),
                "title": (title_node.text(strip=True) if title_node else ""),
                "location": (loc_node.text(strip=True) if loc_node else ""),
                "posted_at": (time_node.attributes.get("datetime") or "") if time_node else "",
            }
        )
    return cards


def parse_job_detail(html: str) -> dict[str, str]:
    tree = HTMLParser(html)
    title_node = tree.css_first("h2.top-card-layout__title") or tree.css_first(
        ".top-card-layout__title"
    )
    loc_node = tree.css_first(".topcard__flavor--bullet") or tree.css_first(
        "span.topcard__flavor"
    )
    desc_node = tree.css_first(".show-more-less-html__markup")
    description = ""
    if desc_node:
        description = html_to_text(desc_node.html or "")
    posted_node = tree.css_first("time")
    return {
        "title": title_node.text(strip=True) if title_node else "",
        "location": loc_node.text(strip=True) if loc_node else "",
        "description": description,
        "posted_at": (posted_node.attributes.get("datetime") or "") if posted_node else "",
    }


def linkedin_company_id_from_html(html: str, page_url: str = "") -> str | None:
    blob = f"{page_url}\n{html}"
    for pat in (
        r"[?&]f_C=(\d+)",
        r'"urn:li:organization:(\d+)"',
        r'"urn:li:fs_normalized_company:(\d+)"',
        r'"companyId":(\d+)',
    ):
        match = re.search(pat, blob)
        if match:
            return match.group(1)
    return None
