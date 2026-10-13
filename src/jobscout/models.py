"""Core data models for jobscout."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


_URL_ID_RE = re.compile(r"^https?://", re.IGNORECASE)
_REQ_ID_RE = re.compile(r"_([A-Za-z]{1,6}-?\d{3,})$")


def position_id_from_external(external_id: str) -> str:
    """Return a job or position id, or '' when the stored id is a URL or path."""
    raw = (external_id or "").strip()
    if not raw or _URL_ID_RE.match(raw):
        return ""
    if "/" in raw:
        tail = raw.rstrip("/").rsplit("/", 1)[-1]
        match = _REQ_ID_RE.search(tail)
        return match.group(1) if match else ""
    if len(raw) > 64:
        return ""
    return raw


@dataclass(slots=True)
class Job:
    """A normalized job posting from any ATS source."""

    company: str
    source: str
    external_id: str
    title: str
    url: str
    location: str = ""
    department: str = ""
    employment_type: str = ""
    description: str = ""
    posted_at: str = ""

    @property
    def position_id(self) -> str:
        return position_id_from_external(self.external_id)

    @property
    def display_title(self) -> str:
        """Title with the job or position id in parentheses, when one exists."""
        pid = self.position_id
        if not pid:
            return self.title
        marker = f"({pid})"
        if marker in self.title:
            return self.title
        return f"{self.title} {marker}"

    @property
    def job_key(self) -> str:
        identity = f"{self.company.lower()}|{self.source}|{self.external_id or self.url}"
        return hashlib.sha256(identity.encode()).hexdigest()

    @property
    def content_hash(self) -> str:
        parts = [
            _normalize_whitespace(self.title),
            _normalize_whitespace(self.location),
            _normalize_whitespace(self.department),
            _normalize_whitespace(self.description),
        ]
        payload = "\x1f".join(parts)
        return hashlib.sha256(payload.encode()).hexdigest()


class VerdictLabel(str, Enum):
    STRONG = "strong"
    POSSIBLE = "possible"
    NO = "no"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass(slots=True)
class Verdict:
    score: int
    verdict: VerdictLabel
    why: str = ""
    seniority_fit: str = ""
    location_fit: str = ""
    matched_skills: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    model: str = ""
    prompt_version: str = ""

    @classmethod
    def skipped(cls, reason: str) -> Verdict:
        return cls(score=0, verdict=VerdictLabel.SKIPPED, why=reason)

    @classmethod
    def errored(cls, reason: str) -> Verdict:
        return cls(score=0, verdict=VerdictLabel.ERROR, why=reason)


@dataclass(slots=True)
class ScoredJob:
    job: Job
    verdict: Verdict
    first_seen: str
    is_new: bool = False
    applied: bool = False


@dataclass(slots=True)
class FetchResult:
    company: str
    source: str
    jobs: list[Job] = field(default_factory=list)
    error: str | None = None
    # False when the listing stopped early. Unseen stored jobs must stay.
    complete: bool = True
    # LinkedIn page offset to request on the next run, when the fetch stopped.
    resume_start: int | None = None


@dataclass(slots=True)
class RunSummary:
    fetched: int = 0
    new: int = 0
    changed: int = 0
    closed: int = 0
    scored: int = 0
    llm_calls: int = 0
    llm_error_count: int = 0
    removed: int = 0
    llm_errors: list[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    matches: list[ScoredJob] = field(default_factory=list)
    fetch_results: list[FetchResult] = field(default_factory=list)

    @property
    def failures(self) -> list[FetchResult]:
        return [r for r in self.fetch_results if r.error]
