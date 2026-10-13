"""Tests for detect.py."""

from jobscout.config import CompanyConfig
from jobscout.detect import (
    TOKEN_BLOCKLIST,
    candidate_urls,
    detect_from_html,
    format_yaml_block,
)


def test_candidate_urls_yields_origin_variants():
    urls = list(candidate_urls("https://example.com/about"))
    assert "https://example.com/about" in urls
    assert "https://example.com/careers" in urls
    assert "https://example.com/jobs" in urls


def test_token_blocklist_rejects_common_paths():
    assert "embed" in TOKEN_BLOCKLIST
    assert "static" in TOKEN_BLOCKLIST


def test_detect_greenhouse_from_html():
    html = '<script src="https://boards.greenhouse.io/acme/embed"></script>'
    result = detect_from_html(html, "https://example.com")
    assert result is not None
    assert result["source"] == "greenhouse"
    assert result["token"] == "acme"


def test_detect_lever_eu_from_html():
    html = 'https://api.eu.lever.co/v0/postings/mycompany'
    result = detect_from_html(html, "https://example.com")
    assert result is not None
    assert result["source"] == "lever"
    assert result["region"] == "eu"


def test_detect_workday_from_html():
    html = 'https://company.wd5.myworkdayjobs.com/wday/cxs/acme/careers/jobs'
    result = detect_from_html(html, "https://example.com")
    assert result is not None
    assert result["source"] == "workday"
    assert "host" in result


def test_format_yaml_block():
    cfg = CompanyConfig(name="Acme", source="greenhouse", token="acme")
    block = format_yaml_block("Acme", cfg)
    assert "name: Acme" in block
    assert "token: acme" in block


def test_detect_comeet_from_json_embed_and_url_uid():
    html = '{"token": "abc123", "company-name": "Acme"}'
    url = "https://www.comeet.com/jobs/acme/18.007"
    result = detect_from_html(html, url)
    assert result is not None
    assert result["source"] == "comeet"
    assert result["company_uid"] == "18.007"
    assert result["token"] == "abc123"


def test_detect_workable_from_hosted_url():
    result = detect_from_html("<html></html>", "https://apply.workable.com/vayyar/")
    assert result is not None
    assert result["source"] == "workable"
    assert result["token"] == "vayyar"


def test_detect_successfactors_from_rmk_html():
    html = '<link href="//rmkcdn.successfactors.com/abc/site.css" />'
    result = detect_from_html(html, "https://careers.stratasys.com/viewalljobs/")
    assert result is not None
    assert result["source"] == "successfactors"
    assert result["careers_url"] == "https://careers.stratasys.com"


def test_detect_teamme_from_host():
    result = detect_from_html("<html></html>", "https://qm.teamme.link/")
    assert result is not None
    assert result["source"] == "teamme"
    assert result["careers_url"] == "https://qm.teamme.link"


def test_detect_elbit_from_host():
    result = detect_from_html("<html></html>", "https://elbitsystemscareer.com/jobs/")
    assert result is not None
    assert result["source"] == "elbit"
    assert result["careers_url"] == "https://elbitsystemscareer.com"


def test_detect_linkedin_from_company_jobs_page():
    html = '<a href="https://www.linkedin.com/jobs/search/?f_C=4251">Jobs</a>'
    result = detect_from_html(html, "https://www.linkedin.com/company/sandisk/jobs/")
    assert result is not None
    assert result["source"] == "linkedin"
    assert result["token"] == "4251"
