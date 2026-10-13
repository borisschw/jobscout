"""Tests for models.py."""

import hashlib

from jobscout.models import Job, Verdict, VerdictLabel


def test_job_key_is_stable_for_same_identity(make_job):
    job = make_job()
    assert job.job_key == make_job().job_key


def test_job_key_falls_back_to_url_when_no_external_id(make_job):
    job = make_job(external_id="", url="https://example.com/jobs/abc")
    expected = hashlib.sha256(b"acme|greenhouse|https://example.com/jobs/abc").hexdigest()
    assert job.job_key == expected


def test_content_hash_excludes_url_and_posted_at(make_job):
    base = make_job()
    changed = make_job(url="https://other.com", posted_at="2099-01-01")
    assert base.content_hash == changed.content_hash


def test_content_hash_changes_when_description_changes(make_job):
    a = make_job(description="Build APIs")
    b = make_job(description="Build microservices")
    assert a.content_hash != b.content_hash


def test_content_hash_normalizes_whitespace(make_job):
    a = make_job(description="Build  APIs\n\nwith Python")
    b = make_job(description="Build APIs with Python")
    assert a.content_hash == b.content_hash


def test_verdict_skipped_factory():
    v = Verdict.skipped("Title blocklist")
    assert v.verdict == VerdictLabel.SKIPPED
    assert v.score == 0
    assert v.why == "Title blocklist"


def test_verdict_errored_factory():
    v = Verdict.errored("timeout")
    assert v.verdict == VerdictLabel.ERROR
