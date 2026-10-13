"""Tests for config.py."""

import pytest

from jobscout.config import CompanyConfig, load_config


def test_load_config_from_project_fixture(app_config):
    assert app_config.profile.startswith("# Test Engineer")
    assert app_config.preferences.score_threshold == 60
    assert len(app_config.companies.companies) == 1
    assert app_config.visible_company_names() == {"acme"}
    assert app_config.visible_company_names(["Mobileye"]) == set()
    assert app_config.visible_company_names(["Acme"]) == {"acme"}


def test_company_config_validates_required_fields():
    with pytest.raises(ValueError, match="requires field 'token'"):
        CompanyConfig(name="X", source="greenhouse")


def test_company_uid_rejects_unquoted_float():
    with pytest.raises(ValueError, match="must be quoted"):
        CompanyConfig.model_validate(
            {"name": "X", "source": "comeet", "token": "t", "company_uid": 18.007}
        )


def test_company_uid_accepts_quoted_string():
    c = CompanyConfig(name="X", source="comeet", token="t", company_uid="18.007")
    assert c.company_uid == "18.007"


def test_company_validation_names_offending_entry():
    from jobscout.config import _validate_companies

    with pytest.raises(ValueError, match="Invalid company entry 'BadCo'"):
        _validate_companies(
            {"companies": [{"name": "BadCo", "source": "greenhouse"}]}
        )


def test_company_config_extra_forbid():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CompanyConfig(name="X", source="greenhouse", token="t", unknown_field="nope")


def test_global_search_text_is_inherited_by_companies():
    from jobscout.config import _validate_companies

    file = _validate_companies(
        {
            "search_text": ["embedded", "firmware"],
            "companies": [
                {"name": "Acme", "source": "greenhouse", "token": "acme"},
                {
                    "name": "Beta",
                    "source": "greenhouse",
                    "token": "beta",
                    "search_text": ["linux"],
                },
            ],
        }
    )
    assert file.search_text == ["embedded", "firmware"]
    assert file.companies[0].search_text == ["embedded", "firmware"]
    assert file.companies[1].search_text == ["linux"]


def test_llm_yaml_overrides_settings(project):
    (project / "config" / "llm.yaml").write_text(
        "provider: ollama\nmodel: llama3.1\n",
        encoding="utf-8",
    )
    config = load_config(project)
    assert config.settings.llm.provider == "ollama"
    assert config.settings.llm.model == "llama3.1"


def test_missing_llm_yaml_fails_when_settings_omit_provider(project):
    (project / "config" / "settings.yaml").write_text(
        "fetch:\n  retries: 1\n"
        "paths:\n  database: data/test.db\n  output_dir: out\n"
        "  profile: config/profile.md\n"
        "  preferences: config/preferences.yaml\n"
        "  companies: config/companies.yaml\n",
        encoding="utf-8",
    )
    with pytest.raises(FileNotFoundError, match="llm.yaml"):
        load_config(project)


def test_missing_profile_points_at_template(project):
    (project / "config" / "profile.md").unlink()
    with pytest.raises(FileNotFoundError, match="profile.md"):
        load_config(project)


def test_missing_companies_points_at_template(project):
    (project / "config" / "companies.yaml").unlink()
    with pytest.raises(FileNotFoundError, match="companies.yaml"):
        load_config(project)


def test_companies_example_covers_each_source():
    from pathlib import Path

    from jobscout.config import _load_yaml, _validate_companies

    example = Path(__file__).resolve().parents[1] / "config" / "companies.yaml.example"
    parsed = _validate_companies(_load_yaml(example))
    sources = {company.source for company in parsed.companies}
    assert sources == {
        "greenhouse",
        "lever",
        "ashby",
        "smartrecruiters",
        "comeet",
        "workday",
        "amazon",
        "elbit",
        "linkedin",
        "workable",
        "successfactors",
        "teamme",
        "html",
        "browser",
        "auto",
    }
    assert parsed.watch_manually
    assert all(not company.enabled for company in parsed.companies)


def test_empty_company_search_text_does_not_inherit_global():
    from jobscout.config import _validate_companies

    file = _validate_companies(
        {
            "search_text": ["embedded"],
            "companies": [
                {
                    "name": "Acme",
                    "source": "greenhouse",
                    "token": "acme",
                    "search_text": [],
                }
            ],
        }
    )
    assert file.companies[0].search_text == []
