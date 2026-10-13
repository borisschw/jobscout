"""Extended prefilter tests."""

import pytest

from jobscout.config import PrefilterConfig
from jobscout.models import VerdictLabel
from jobscout.prefilter import check


@pytest.mark.parametrize(
    "title",
    ["Software Intern", "INTERN backend", "VP Sales Manager"],
)
def test_title_blocklist_is_case_insensitive(make_job, title):
    cfg = PrefilterConfig(
        enabled=True,
        title_blocklist=["intern", "sales"],
    )
    job = make_job(title=title)
    result = check(job, cfg)
    assert result is not None
    assert result.verdict == VerdictLabel.SKIPPED


@pytest.mark.parametrize(
    "location",
    ["Tel Aviv, Israel", "Remote - EMEA", "Hybrid Tel Aviv"],
)
def test_allowed_location_patterns_pass(make_job, location):
    cfg = PrefilterConfig(
        enabled=True,
        allowed_location_patterns=["tel aviv", "remote"],
        denied_location_patterns=["san francisco"],
    )
    job = make_job(location=location)
    assert check(job, cfg) is None
