"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobscout.config import (
    AppConfig,
    CompanyConfig,
    CompaniesFile,
    FetchConfig,
    LLMConfig,
    PathsConfig,
    PreferencesConfig,
    PrefilterConfig,
    SettingsConfig,
    load_config,
)
from jobscout.models import Job
from jobscout.store import Store


@pytest.fixture
def make_job():
    def _make(
        company: str = "Acme",
        source: str = "greenhouse",
        external_id: str = "123",
        title: str = "Senior Backend Engineer",
        url: str = "https://example.com/jobs/123",
        location: str = "Tel Aviv, Israel",
        department: str = "Engineering",
        employment_type: str = "Full-time",
        description: str = "Build scalable backend systems with Python.",
        posted_at: str = "2024-01-01",
    ) -> Job:
        return Job(
            company=company,
            source=source,
            external_id=external_id,
            title=title,
            url=url,
            location=location,
            department=department,
            employment_type=employment_type,
            description=description,
            posted_at=posted_at,
        )

    return _make


@pytest.fixture
def preferences() -> PreferencesConfig:
    return PreferencesConfig(
        looking_for="Senior backend roles",
        dealbreakers="Sales roles",
        location_scope="Israel or remote",
        score_threshold=60,
        prefilter=PrefilterConfig(
            enabled=True,
            allowed_location_patterns=["tel aviv", "israel", "remote"],
            denied_location_patterns=["san francisco"],
            ambiguous_location_patterns=["multiple"],
            title_blocklist=["intern", "sales"],
        ),
    )


@pytest.fixture
def company_config() -> CompanyConfig:
    return CompanyConfig(name="Acme", source="greenhouse", token="acme")


@pytest.fixture
def store(tmp_path) -> Store:
    db = tmp_path / "test.db"
    s = Store(db)
    yield s
    s.close()


@pytest.fixture
def project(tmp_path, preferences) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "profile.md").write_text("# Test Engineer\nPython expert.", encoding="utf-8")
    (config_dir / "preferences.yaml").write_text(
        "looking_for: Senior backend\n"
        "dealbreakers: Sales\n"
        "location_scope: Israel\n"
        "score_threshold: 60\n"
        "prefilter:\n"
        "  enabled: true\n"
        "  allowed_location_patterns: ['tel aviv', 'remote']\n"
        "  denied_location_patterns: ['san francisco']\n"
        "  ambiguous_location_patterns: ['multiple']\n"
        "  title_blocklist: ['intern']\n",
        encoding="utf-8",
    )
    (config_dir / "settings.yaml").write_text(
        "llm:\n  provider: cursor\n  model: test-model\n  max_calls: 0\n"
        "fetch:\n  concurrency: 4\n  retries: 1\n"
        "paths:\n  database: data/test.db\n  output_dir: out\n"
        "  profile: config/profile.md\n"
        "  preferences: config/preferences.yaml\n"
        "  companies: config/companies.yaml\n",
        encoding="utf-8",
    )
    (config_dir / "companies.yaml").write_text(
        "companies:\n"
        "  - name: Acme\n    source: greenhouse\n    token: acme\n"
        "watch_manually:\n"
        "  - name: Manual Co\n    url: https://manual.example.com\n    why: No API\n",
        encoding="utf-8",
    )
    (tmp_path / "data").mkdir(exist_ok=True)
    return tmp_path


@pytest.fixture
def app_config(project) -> AppConfig:
    return load_config(project)


