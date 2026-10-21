"""Tests for report generation."""

import re
from datetime import datetime, timezone

from jobscout.models import Job, ScoredJob, Verdict, VerdictLabel, position_id_from_external
from jobscout.report.writer import build_groups, last_scan_stamp, local_stamp, write_reports
from jobscout.schedule import parse_timestamp


def test_display_title_appends_position_id_once():
    comeet = Job(
        company="Mentee Robotics",
        source="comeet",
        external_id="A6.37F",
        title="Embedded Engineer (BSP)",
        url="https://www.comeet.com/jobs/mentee_robotics/6A.002/embedded-engineer-bsp/A6.37F",
    )
    assert comeet.display_title == "Embedded Engineer (BSP) (A6.37F)"
    assert comeet.display_title == Job(
        company="Mentee Robotics",
        source="comeet",
        external_id="A6.37F",
        title="Embedded Engineer (BSP) (A6.37F)",
        url=comeet.url,
    ).display_title

    workday = Job(
        company="NVIDIA",
        source="workday",
        external_id="/job/Israel-Yokneam/Linux-Driver-Developer_JR2024027",
        title="Linux Driver Developer",
        url="https://nvidia.wd5.myworkdayjobs.com/job/Linux-Driver-Developer_JR2024027",
    )
    assert position_id_from_external(workday.external_id) == "JR2024027"
    assert workday.display_title == "Linux Driver Developer (JR2024027)"

    linked = Job(
        company="Page",
        source="html",
        external_id="https://example.com/careers/role",
        title="Driver Engineer",
        url="https://example.com/careers/role",
    )
    assert linked.display_title == "Driver Engineer"


def test_build_groups_lists_each_job_once(app_config, make_job):
    threshold = 60
    new_strong = ScoredJob(
        job=make_job(),
        verdict=Verdict(score=90, verdict=VerdictLabel.STRONG),
        first_seen="2024-01-02",
        is_new=True,
    )
    old_strong = ScoredJob(
        job=make_job(external_id="2", title="Older Strong Role"),
        verdict=Verdict(score=80, verdict=VerdictLabel.STRONG),
        first_seen="2024-01-01",
        is_new=False,
    )
    possible = ScoredJob(
        job=make_job(external_id="3", title="Possible Role"),
        verdict=Verdict(score=65, verdict=VerdictLabel.POSSIBLE),
        first_seen="2024-01-01",
        is_new=False,
    )
    groups = build_groups([new_strong, old_strong, possible], threshold)
    assert [g.title for g in groups] == [
        "New since the last run",
        "Strong matches",
        "Worth a look (score >= 60)",
    ]
    keys = [job.job.job_key for group in groups for job in group.jobs]
    assert keys == [
        new_strong.job.job_key,
        old_strong.job.job_key,
        possible.job.job_key,
    ]


def test_write_reports_creates_files(app_config, make_job, tmp_path):
    app_config.project_root = tmp_path
    app_config.settings.paths.output_dir = "out"
    match = ScoredJob(
        job=make_job(),
        verdict=Verdict(score=85, verdict=VerdictLabel.STRONG, why="Good fit"),
        first_seen="2024-01-01",
        is_new=True,
    )
    html, md = write_reports(app_config, [match], [], interactive=False)
    assert html.exists()
    assert md.exists()
    assert (tmp_path / "out" / "latest.html").exists()
    content = html.read_text(encoding="utf-8")
    md_text = md.read_text(encoding="utf-8")
    assert "Senior Backend Engineer (123)" in content
    assert "Senior Backend Engineer (123)" in md_text
    assert 'id="applied-count">0<' in content
    assert 'id="open-count">1<' in content
    assert "**Shown:** 0 applied, 1 not applied" in md_text
    assert "Applied" in content
    assert 'class="btn-applied"' in content


def test_write_reports_colors_applied_jobs(app_config, make_job, tmp_path):
    app_config.project_root = tmp_path
    app_config.settings.paths.output_dir = "out"
    match = ScoredJob(
        job=make_job(title="Applied Role"),
        verdict=Verdict(score=85, verdict=VerdictLabel.STRONG, why="Already sent"),
        first_seen="2024-01-01",
        is_new=False,
        applied=True,
    )
    html, md = write_reports(app_config, [match], [])
    html_text = html.read_text(encoding="utf-8")
    md_text = md.read_text(encoding="utf-8")
    assert "Strong matches" in html_text
    assert 'id="applied-count">1<' in html_text
    assert 'id="open-count">0<' in html_text
    assert "**Shown:** 1 applied, 0 not applied" in md_text
    assert "Already applied" not in html_text
    assert "Applied Role" in html_text
    assert 'class="card applied"' in html_text
    assert 'class="btn-unapply"' in html_text
    assert ">Unapply</button>" in html_text
    assert "**Applied:** yes" in md_text


def test_write_reports_shows_summary_and_highlights_new_jobs(app_config, make_job, tmp_path):
    app_config.project_root = tmp_path
    app_config.settings.paths.output_dir = "out"
    new_job = ScoredJob(
        job=make_job(title="New Role"),
        verdict=Verdict(score=88, verdict=VerdictLabel.STRONG, why="Just posted"),
        first_seen="2026-10-13T10:05:00Z",
        is_new=False,
    )
    old_job = ScoredJob(
        job=make_job(external_id="2", title="Old Role"),
        verdict=Verdict(score=70, verdict=VerdictLabel.POSSIBLE, why="Seen before"),
        first_seen="2026-10-01T10:00:00Z",
        is_new=False,
    )
    run = {
        "started_at": "2026-10-13T10:00:00Z",
        "finished_at": "2026-10-13T10:20:00Z",
        "fetched": 12,
        "new": 1,
        "changed": 0,
        "closed": 2,
        "removed": 0,
        "scored": 1,
        "llm_calls": 1,
        "llm_errors": 0,
    }
    html, md = write_reports(app_config, [new_job, old_job], [], run=run)
    html_text = html.read_text(encoding="utf-8")
    md_text = md.read_text(encoding="utf-8")
    assert "Run summary" in html_text
    assert "<dt>Fetched</dt><dd>12</dd>" in html_text
    assert "<dt>New</dt><dd>1</dd>" in html_text
    assert 'class="card new"' in html_text
    assert ">New</span>" in html_text
    aside = html_text.split("<main>", 1)[0]
    assert "Run summary" in aside
    assert "New Role" not in aside
    old_cards = re.findall(
        r'<div class="(card[^"]*)" data-key=',
        html_text.split("Old Role", 1)[0],
    )
    assert old_cards[-1] == "card"
    assert "**Fetched:** 12" in md_text
    assert new_job.is_new is True
    assert old_job.is_new is False
    finished = parse_timestamp(run["finished_at"])
    assert finished is not None
    assert f"Last scan {local_stamp(finished)}" in html_text
    assert "Generated" not in html_text


def test_write_reports_explains_pending_scoring(app_config, tmp_path):
    app_config.project_root = tmp_path
    app_config.settings.paths.output_dir = "out"
    html, md = write_reports(
        app_config,
        [],
        [],
        interactive=False,
        pending_scoring=483,
        scoring_errors=["Cursor Agent CLI 'cursor-agent' was not found on PATH."],
    )
    html_text = html.read_text(encoding="utf-8")
    md_text = md.read_text(encoding="utf-8")
    assert "Scoring did not finish: 483" in html_text
    assert "No matches above threshold" not in html_text
    assert "Scoring did not finish: 483" in md_text
    assert "cursor-agent" in html_text
    assert "Last scan" not in html_text
    assert "Generated" not in html_text


def test_last_scan_stamp_uses_finish_time():
    finished = "2026-10-18T20:10:00Z"
    moment = parse_timestamp(finished)
    assert moment is not None
    summary = {"started_at": "2026-10-18T19:00:00Z", "finished_at": finished}
    assert last_scan_stamp(summary) == local_stamp(moment)
    assert last_scan_stamp(None) is None
    assert last_scan_stamp({"started_at": "", "finished_at": ""}) is None


def test_local_stamp_converts_utc_to_machine_time():
    utc = datetime(2026, 10, 18, 20, 10, tzinfo=timezone.utc)
    local = utc.astimezone()
    assert local_stamp(utc) == local.strftime("%d/%m/%Y %H:%M")
    assert "GMT" not in local_stamp(utc)
    assert "UTC" not in local_stamp(utc)
