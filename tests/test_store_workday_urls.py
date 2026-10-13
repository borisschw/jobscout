"""Tests for Workday URL repair in the store."""

from jobscout.config import CompanyConfig
from jobscout.models import Job
from jobscout.store import Store


def test_store_repair_workday_urls(project):
    store = Store(project / "data" / "test.db")
    path = "/job/Israel-Tel-Aviv/Senior-Firmware-Engineer---NVLink-Switch_JR2016514"
    job = Job(
        company="NVIDIA",
        source="workday",
        external_id=path,
        title="Senior Firmware Engineer",
        url=f"https://nvidia.wd5.myworkdayjobs.com{path}",
    )
    store.upsert_job(job)

    companies = [
        CompanyConfig(
            name="NVIDIA",
            source="workday",
            host="nvidia.wd5.myworkdayjobs.com",
            tenant="nvidia",
            site="NVIDIAExternalCareerSite",
        )
    ]
    with store.transaction():
        fixed = store.repair_workday_urls(companies)
    assert fixed == 1

    row = store._conn.execute(
        "SELECT url FROM jobs WHERE job_key = ?", (job.job_key,)
    ).fetchone()
    assert row["url"] == (
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
        + path
    )
    store.close()
