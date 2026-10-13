"""Per-source company config validation tests."""

import pytest

from jobscout.config import CompanyConfig


@pytest.mark.parametrize(
    ("source", "fields"),
    [
        ("greenhouse", {"token": "t"}),
        ("lever", {"token": "t"}),
        ("ashby", {"token": "t"}),
        ("smartrecruiters", {"token": "t"}),
        ("comeet", {"token": "t", "company_uid": "1.0"}),
        ("workday", {"host": "h.wd.com", "tenant": "h", "site": "careers"}),
        ("amazon", {"country": "ISR"}),
        ("elbit", {"careers_url": "https://elbitsystemscareer.com"}),
        ("linkedin", {"token": "4251"}),
        ("workable", {"token": "t"}),
        ("successfactors", {"careers_url": "https://careers.example.com"}),
        ("teamme", {"careers_url": "https://qm.teamme.link"}),
        ("html", {"careers_url": "https://example.com"}),
        ("browser", {"careers_url": "https://example.com"}),
        ("auto", {"careers_url": "https://example.com"}),
    ],
)
def test_valid_source_configs(source, fields):
    cfg = CompanyConfig(name="Co", source=source, **fields)
    assert cfg.source == source


@pytest.mark.parametrize(
    ("source", "fields"),
    [
        ("greenhouse", {}),
        ("workday", {"host": "h", "tenant": "t"}),
        ("comeet", {"token": "t"}),
        ("amazon", {}),
        ("elbit", {}),
        ("linkedin", {}),
    ],
)
def test_invalid_source_configs_missing_fields(source, fields):
    with pytest.raises(ValueError):
        CompanyConfig(name="Co", source=source, **fields)
