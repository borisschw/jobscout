"""Rule-based prefilter before LLM scoring."""

from __future__ import annotations

import re

from jobscout.config import PrefilterConfig
from jobscout.models import Job, Verdict


def check(job: Job, config: PrefilterConfig) -> Verdict | None:
    """Return a skip Verdict if the job should be dropped, else None to pass."""
    if not config.enabled:
        return None

    title_lower = job.title.lower()
    for blocked in config.title_blocklist:
        if _word_match(blocked, title_lower):
            return Verdict.skipped(f"Title blocklist match: {blocked}")

    location = job.location
    if not location or (
        not config.allowed_location_patterns
        and not config.denied_location_patterns
        and not config.ambiguous_location_patterns
    ):
        return None

    loc_lower = location.lower()

    for pattern in config.allowed_location_patterns:
        if _matches(pattern, loc_lower):
            return None

    for pattern in config.denied_location_patterns:
        if _matches(pattern, loc_lower):
            return Verdict.skipped(f"Location denied: {pattern}")

    for pattern in config.ambiguous_location_patterns:
        if _matches(pattern, loc_lower):
            return None

    return Verdict.skipped("Location out of scope")


def _word_match(pattern: str, text: str) -> bool:
    return bool(re.search(rf"\b{re.escape(pattern)}\b", text, re.IGNORECASE))


def _matches(pattern: str, text: str) -> bool:
    try:
        return bool(re.search(pattern, text, re.IGNORECASE))
    except re.error:
        return pattern.lower() in text
