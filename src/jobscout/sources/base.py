"""Shared utilities for source adapters."""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, TypeVar
from urllib.parse import urlparse

import httpx
from selectolax.parser import HTMLParser
from tenacity import retry, retry_if_exception_type, wait_exponential

from jobscout.config import CompanyConfig, FetchConfig
from jobscout.models import Job
from jobscout.search import job_matches_search_terms, normalize_search_terms

T = TypeVar("T")
R = TypeVar("R")

MAX_DESCRIPTION_LENGTH = 12_000

BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
    "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}


class SourceError(Exception):
    """Unrecoverable source fetch failure."""


class RetryableSourceError(SourceError):
    """Transient source failure that should be retried."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.status_code = status_code


class SourceRateLimit(SourceError):
    """The host asked us to stop. Callers should not retry this request."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


# LinkedIn guest search and Workable share a tight per-IP budget. A full run
# fetches several of these boards at once, and each LinkedIn company then
# hydrates postings in parallel, which is what produces HTTP 429.
_RATE_LIMITED_HOST_SUFFIXES = ("linkedin.com", "workable.com")
_HOST_GATES: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Semaphore]] = {}


def _host_gate(url: str) -> asyncio.Semaphore | None:
    host = urlparse(url).netloc.lower()
    if not host.endswith(_RATE_LIMITED_HOST_SUFFIXES):
        return None
    loop = asyncio.get_running_loop()
    current = _HOST_GATES.get(host)
    if current is None or current[0] is not loop:
        # One in-flight call per host. Parallel companies were tripping 429
        # before the first page of a later company (Apple, Vayyar) returned.
        current = (loop, asyncio.Semaphore(1))
        _HOST_GATES[host] = current
    return current[1]


def retryable_wait_seconds(exc: BaseException, attempt: int) -> float | None:
    """Seconds to wait after a transient HTTP error, or None for the default backoff."""
    if not isinstance(exc, RetryableSourceError):
        return None
    if exc.retry_after is not None:
        return min(max(exc.retry_after, 0.0), 60.0)
    if exc.status_code == 429:
        return min(30.0, 5.0 * (2 ** max(attempt - 1, 0)))
    return None


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    raw = (resp.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class Source(ABC):
    def __init__(self, company: CompanyConfig, fetch: FetchConfig) -> None:
        self.company = company
        self.fetch = fetch
        self.client: httpx.AsyncClient | None = None
        self.progress: Any = None
        self.store: Any = None
        # True when this fetch saw every current listing page. A resumed or
        # rate-limited fetch leaves it False so stored jobs past the stop
        # point are not treated as gone.
        self.listing_complete = True
        self.resume_start: int | None = None

    @abstractmethod
    async def fetch_jobs(self, client: httpx.AsyncClient) -> list[Job]:
        raise NotImplementedError

    @property
    def name(self) -> str:
        return self.company.name

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        stop_statuses: frozenset[int] | None = None,
        retry_then_stop: frozenset[int] | None = None,
        retry_then_stop_attempts: int = 1,
        **kwargs: Any,
    ) -> httpx.Response:
        retries = self.fetch.retries
        default_wait = wait_exponential(multiplier=1, min=1, max=15)

        def _should_stop(retry_state: Any) -> bool:
            exc = retry_state.outcome.exception() if retry_state.outcome else None
            limit = retries
            if (
                retry_then_stop
                and isinstance(exc, RetryableSourceError)
                and exc.status_code in retry_then_stop
            ):
                limit = retry_then_stop_attempts
            elif isinstance(exc, RetryableSourceError) and exc.status_code == 429:
                limit = max(retries, 5)
            return retry_state.attempt_number >= limit

        def _wait(retry_state: Any) -> float:
            exc = retry_state.outcome.exception() if retry_state.outcome else None
            override = retryable_wait_seconds(exc, retry_state.attempt_number) if exc else None
            if override is not None:
                return override
            return default_wait(retry_state)

        @retry(
            stop=_should_stop,
            wait=_wait,
            retry=retry_if_exception_type((httpx.TransportError, RetryableSourceError)),
            reraise=True,
        )
        async def _do() -> httpx.Response:
            try:
                resp = await client.request(
                    method,
                    url,
                    timeout=self.fetch.timeout_seconds,
                    **kwargs,
                )
            except httpx.TransportError as e:
                raise SourceError(f"Transport error fetching {url}: {e}") from e

            if stop_statuses and resp.status_code in stop_statuses:
                raise SourceRateLimit(
                    f"HTTP {resp.status_code} from {url}",
                    status_code=resp.status_code,
                )
            if resp.status_code == 429 or resp.status_code >= 500:
                raise RetryableSourceError(
                    f"HTTP {resp.status_code} from {url}",
                    retry_after=_retry_after_seconds(resp),
                    status_code=resp.status_code,
                )
            if 400 <= resp.status_code < 500:
                raise SourceError(f"HTTP {resp.status_code} from {url}: {resp.text[:200]}")
            return resp

        async def _call() -> httpx.Response:
            try:
                return await _do()
            except RetryableSourceError as exc:
                if (
                    retry_then_stop
                    and exc.status_code is not None
                    and exc.status_code in retry_then_stop
                ):
                    raise SourceRateLimit(str(exc), status_code=exc.status_code) from exc
                raise

        # Hold the host for the whole retry, including the backoff, so the next
        # company does not start until this IP budget has cooled down.
        gate = _host_gate(url)
        if gate is None:
            return await _call()
        async with gate:
            return await _call()

    async def get_json(
        self,
        client: httpx.AsyncClient,
        url: str,
        **kwargs: Any,
    ) -> Any:
        resp = await self._request(client, "GET", url, **kwargs)
        try:
            return resp.json()
        except Exception as e:
            raise SourceError(f"Non-JSON response from {url}") from e

    async def post_json(
        self,
        client: httpx.AsyncClient,
        url: str,
        body: dict[str, Any],
        **kwargs: Any,
    ) -> Any:
        resp = await self._request(
            client, "POST", url, json=body, headers={"Content-Type": "application/json"}, **kwargs
        )
        try:
            return resp.json()
        except Exception as e:
            raise SourceError(f"Non-JSON response from {url}") from e

    def _make_job(
        self,
        *,
        external_id: str,
        title: str,
        url: str,
        location: str = "",
        department: str = "",
        employment_type: str = "",
        description: str = "",
        posted_at: str = "",
    ) -> Job:
        desc = description[:MAX_DESCRIPTION_LENGTH] if description else ""
        return Job(
            company=self.company.name,
            source=self.company.source,
            external_id=external_id,
            title=title,
            url=url,
            location=location,
            department=department,
            employment_type=employment_type,
            description=desc,
            posted_at=posted_at,
        )

    def _filter_by_search_text(self, jobs: list[Job]) -> list[Job]:
        terms = normalize_search_terms(self.company.search_text)
        if not any(terms):
            return jobs
        matched = [job for job in jobs if job_matches_search_terms(job, terms)]
        dropped = len(jobs) - len(matched)
        if dropped and self.progress:
            self.progress.log(
                f"  {self.company.name}: {len(matched)}/{len(jobs)} matched search_text"
            )
        return matched

    def _apply_job_limit(self, jobs: list[Job]) -> list[Job]:
        jobs = self._filter_by_search_text(jobs)
        limit = self.fetch.max_jobs_per_company
        if limit and limit > 0:
            return jobs[:limit]
        return jobs

    async def enrich_job(self, client: httpx.AsyncClient, job: Job) -> Job:
        """Fetch full job details when listing data is incomplete."""
        return job


async def gather_limited(
    items: Iterable[T],
    limit: int,
    func: Callable[[T], Awaitable[R]],
) -> list[R]:
    semaphore = asyncio.Semaphore(limit)
    item_list = list(items)

    async def _wrap(item: T) -> R:
        async with semaphore:
            return await func(item)

    return list(await asyncio.gather(*[_wrap(item) for item in item_list]))


def html_to_text(html: str) -> str:
    """Convert HTML to plain text, breaking lines only on block tags."""
    marked = html
    for tag in BLOCK_TAGS:
        marked = re.sub(rf"</{tag}>", f"</{tag}>\n", marked, flags=re.IGNORECASE)
        marked = re.sub(rf"<{tag}(\s|>|/)", f"\n<{tag}\\1", marked, flags=re.IGNORECASE)

    tree = HTMLParser(marked)
    text = tree.body.text(separator=" ") if tree.body else tree.text(separator=" ")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def make_client(fetch: FetchConfig) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": fetch.user_agent},
    )


def build_source(company: CompanyConfig, fetch: FetchConfig) -> Source:
    from jobscout.sources.amazon import AmazonSource
    from jobscout.sources.ashby import AshbySource
    from jobscout.sources.comeet import ComeetSource
    from jobscout.sources.elbit import ElbitSource
    from jobscout.sources.greenhouse import GreenhouseSource
    from jobscout.sources.html_generic import HtmlGenericSource
    from jobscout.sources.lever import LeverSource
    from jobscout.sources.linkedin import LinkedInSource
    from jobscout.sources.smartrecruiters import SmartRecruitersSource
    from jobscout.sources.successfactors import SuccessFactorsSource
    from jobscout.sources.teamme import TeammeSource
    from jobscout.sources.workable import WorkableSource
    from jobscout.sources.workday import WorkdaySource

    REGISTRY: dict[str, type[Source]] = {
        "greenhouse": GreenhouseSource,
        "lever": LeverSource,
        "ashby": AshbySource,
        "smartrecruiters": SmartRecruitersSource,
        "comeet": ComeetSource,
        "workday": WorkdaySource,
        "amazon": AmazonSource,
        "elbit": ElbitSource,
        "linkedin": LinkedInSource,
        "workable": WorkableSource,
        "successfactors": SuccessFactorsSource,
        "teamme": TeammeSource,
        "html": HtmlGenericSource,
        "browser": HtmlGenericSource,
    }

    source_type = company.source
    if source_type == "auto":
        raise SourceError("auto source must be resolved before build_source")
    cls = REGISTRY.get(source_type)
    if cls is None:
        raise SourceError(f"Unknown source type: {source_type}")
    return cls(company, fetch)
