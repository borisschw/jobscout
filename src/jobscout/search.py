"""Shared search-term helpers used by config inheritance and source adapters."""

from __future__ import annotations

import re

from jobscout.models import Job


def normalize_search_terms(search_text: str | list[str] | None) -> list[str]:
    if not search_text:
        return [""]
    if isinstance(search_text, list):
        terms = [t.strip() for t in search_text if t and t.strip()]
        return terms or [""]
    term = search_text.strip()
    return [term] if term else [""]


def text_matches_search_terms(haystack: str, terms: list[str]) -> bool:
    """True when haystack contains any configured term as a whole word."""
    concrete = [t for t in terms if t]
    if not concrete:
        return True
    lowered = haystack.lower()
    return any(re.search(rf"\b{re.escape(term.lower())}\b", lowered) for term in concrete)


def job_matches_search_terms(job: Job, terms: list[str]) -> bool:
    haystack = " ".join(
        [job.title, job.location, job.department, job.description]
    )
    return text_matches_search_terms(haystack, terms)
