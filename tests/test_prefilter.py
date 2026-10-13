"""Tests for prefilter.py."""

from jobscout.config import PrefilterConfig
from jobscout.models import VerdictLabel
from jobscout.prefilter import check


def test_prefilter_passes_when_disabled(make_job):
    job = make_job(location="San Francisco")
    cfg = PrefilterConfig(enabled=False, denied_location_patterns=["san francisco"])
    assert check(job, cfg) is None


def test_prefilter_drops_blocklisted_title(make_job):
    job = make_job(title="Sales Intern")
    cfg = PrefilterConfig(enabled=True, title_blocklist=["intern", "sales"])
    result = check(job, cfg)
    assert result is not None
    assert result.verdict == VerdictLabel.SKIPPED


def test_prefilter_passes_empty_location(make_job):
    job = make_job(location="")
    cfg = PrefilterConfig(
        enabled=True,
        denied_location_patterns=["san francisco"],
    )
    assert check(job, cfg) is None


def test_prefilter_allowed_beats_denied(make_job):
    job = make_job(location="Tel Aviv and San Francisco")
    cfg = PrefilterConfig(
        enabled=True,
        allowed_location_patterns=["tel aviv"],
        denied_location_patterns=["san francisco"],
    )
    assert check(job, cfg) is None


def test_prefilter_drops_denied_location(make_job):
    job = make_job(location="San Francisco, CA")
    cfg = PrefilterConfig(
        enabled=True,
        denied_location_patterns=["san francisco"],
    )
    result = check(job, cfg)
    assert result is not None
    assert "denied" in result.why.lower()


def test_prefilter_passes_ambiguous_location(make_job):
    job = make_job(location="Multiple locations")
    cfg = PrefilterConfig(
        enabled=True,
        ambiguous_location_patterns=["multiple"],
        denied_location_patterns=["san francisco"],
    )
    assert check(job, cfg) is None


def test_prefilter_does_not_block_internal_titles(make_job):
    cfg = PrefilterConfig(enabled=True, title_blocklist=["intern"])
    job = make_job(title="Senior Internal Tools Engineer")
    assert check(job, cfg) is None


def test_prefilter_passes_israeli_city_and_il_suffix(make_job):
    cfg = PrefilterConfig(
        enabled=True,
        allowed_location_patterns=["haifa", "petah tikva", "israel"],
        denied_location_patterns=["san francisco"],
        ambiguous_location_patterns=[r"\d+\s+locations?"],
    )
    assert check(make_job(location="Haifa, IL"), cfg) is None
    assert check(make_job(location="Petah Tikva, IL"), cfg) is None
    assert check(make_job(location="2 Locations"), cfg) is None


def test_prefilter_drops_unknown_location_when_patterns_configured(make_job):
    job = make_job(location="Berlin, Germany")
    cfg = PrefilterConfig(
        enabled=True,
        allowed_location_patterns=["tel aviv"],
        denied_location_patterns=["san francisco"],
    )
    result = check(job, cfg)
    assert result is not None
    assert "out of scope" in result.why.lower()
