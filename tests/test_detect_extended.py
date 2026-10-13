"""Extended ATS detection tests."""

import pytest

from jobscout.detect import detect_from_html


@pytest.mark.parametrize(
    ("html", "expected_source", "expected_key"),
    [
        (
            'api.ashbyhq.com/posting-api/job-board/mycompany',
            "ashby",
            "token",
        ),
        (
            'api.smartrecruiters.com/v1/companies/acme-corp/postings',
            "smartrecruiters",
            "token",
        ),
        (
            'company-uid="42.001" token="abc123"',
            "comeet",
            "company_uid",
        ),
        (
            "boards-api.greenhouse.io/v1/boards/stripe/jobs",
            "greenhouse",
            "token",
        ),
        (
            "apply.workable.com/api/v3/accounts/vayyar/jobs",
            "workable",
            "token",
        ),
        (
            "https://careers.example.com/tile-search-results/?startrow=0",
            "successfactors",
            "careers_url",
        ),
        (
            "https://qm.teamme.link/jobs/CC.F46",
            "teamme",
            "careers_url",
        ),
        (
            "https://elbitsystemscareer.com/cron/jobs.json",
            "elbit",
            "careers_url",
        ),
        (
            "https://www.linkedin.com/company/sandisk/jobs/?f_C=4251",
            "linkedin",
            "token",
        ),
    ],
)
def test_detect_from_html_finds_known_ats(html, expected_source, expected_key):
    result = detect_from_html(html, "https://example.com")
    assert result is not None
    assert result["source"] == expected_source
    assert expected_key in result


@pytest.mark.parametrize(
    "token",
    ["embed", "static", "www", "api"],
)
def test_detect_rejects_blocklisted_tokens(token):
    html = f"boards.greenhouse.io/v1/boards/{token}/jobs"
    result = detect_from_html(html, "https://example.com")
    assert result is None or result.get("token") != token


def test_detect_returns_none_for_unknown_page():
    assert detect_from_html("<html><body>Hello</body></html>", "https://example.com") is None
