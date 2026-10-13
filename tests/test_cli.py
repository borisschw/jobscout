"""Tests for CLI commands."""

from typer.testing import CliRunner

from jobscout.cli import app

runner = CliRunner()


def test_status_command(app_config, project):
    result = runner.invoke(app, ["status", "--project", str(project)])
    assert result.exit_code == 0
    assert "Total jobs" in result.stdout


def test_report_command(app_config, project, make_job):
    from jobscout.store import Store

    store = Store(project / "data" / "test.db")
    job = make_job()
    store.upsert_job(job)
    from jobscout.models import Verdict, VerdictLabel

    store.save_verdict(
        job,
        Verdict(score=85, verdict=VerdictLabel.STRONG, model="m", prompt_version="1"),
    )
    store._conn.commit()
    result = runner.invoke(app, ["report", "--project", str(project)])
    assert result.exit_code == 0
    assert (project / "out" / "latest.html").exists()
    store.close()


def test_ignore_list_command(project, make_job):
    from jobscout.store import Store

    store = Store(project / "data" / "test.db")
    job = make_job()
    store.upsert_job(job)
    store.ignore([job.job_key])
    store._conn.commit()
    result = runner.invoke(
        app,
        ["ignore", "--list", "--project", str(project)],
    )
    assert result.exit_code == 0
    assert "Ignored jobs" in result.stdout
    assert job.company in result.stdout
    assert job.title in result.stdout
    assert job.job_key[:12] in result.stdout
    store.close()


def test_applied_list_command(project, make_job):
    from jobscout.store import Store

    store = Store(project / "data" / "test.db")
    job = make_job(company="Amazon", title="Embedded Software Engineer")
    store.upsert_job(job)
    store.mark_applied([job.job_key])
    store._conn.commit()
    result = runner.invoke(
        app,
        ["applied", "--list", "--project", str(project)],
    )
    assert result.exit_code == 0
    assert "Applied jobs" in result.stdout
    assert "Amazon" in result.stdout
    assert "Embedded Software Engineer" in result.stdout
    assert job.job_key[:12] in result.stdout
    store.close()


def test_jobs_command_empty(project):
    result = runner.invoke(app, ["jobs", "--project", str(project)])
    assert result.exit_code == 0
