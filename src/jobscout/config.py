"""Configuration loading and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SourceType = Literal[
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
]

RegionType = Literal["us", "eu"]


class PrefilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    allowed_location_patterns: list[str] = Field(default_factory=list)
    denied_location_patterns: list[str] = Field(default_factory=list)
    ambiguous_location_patterns: list[str] = Field(default_factory=list)
    title_blocklist: list[str] = Field(default_factory=list)


class PreferencesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    looking_for: str = ""
    dealbreakers: str = ""
    location_scope: str = ""
    score_threshold: int = 60
    prefilter: PrefilterConfig = Field(default_factory=PrefilterConfig)


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["cursor", "openai", "anthropic", "ollama"] = "cursor"
    model: str = "gpt-4o-mini"
    base_url: str = "http://localhost:11434"
    command: str = "cursor-agent"
    concurrency: int = 4
    max_calls: int = 0
    timeout_seconds: int = 120


class FetchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    concurrency: int = 8
    timeout_seconds: int = 45
    retries: int = 3
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 jobscout/0.1"
    )
    allow_browser: bool = False
    max_jobs_per_company: int = 0


class PathsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database: str = "data/jobscout.db"
    output_dir: str = "out"
    profile: str = "config/profile.md"
    preferences: str = "config/preferences.yaml"
    companies: str = "config/companies.yaml"
    company_types: str = "config/company_types.yaml"


def parse_hhmm(value: str) -> tuple[int, int]:
    """Parse a local time of day. Minutes are two digits, as in 23:00."""
    parts = value.strip().split(":")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError("schedule.at must be HH:MM, for example 23:00")
    hour, minute = int(parts[0]), int(parts[1])
    if hour > 23 or minute > 59 or len(parts[1]) != 2:
        raise ValueError("schedule.at must be HH:MM, for example 23:00")
    return hour, minute


class ScheduleConfig(BaseModel):
    """Daily check while the report viewer is running."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    at: str = "23:00"

    @field_validator("at")
    @classmethod
    def normalize_at(cls, value: str) -> str:
        hour, minute = parse_hhmm(value)
        return f"{hour:02d}:{minute:02d}"


class SettingsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm: LLMConfig = Field(default_factory=LLMConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)


class CompanyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    source: SourceType
    enabled: bool = True
    token: str | None = None
    host: str | None = None
    tenant: str | None = None
    site: str | None = None
    country: str | None = None
    careers_url: str | None = None
    region: RegionType = "us"
    search_text: str | list[str] | None = None
    company_uid: str | None = None
    allow_browser: bool | None = None

    @field_validator("company_uid", mode="before")
    @classmethod
    def reject_numeric_company_uid(cls, v: Any) -> Any:
        if isinstance(v, (int, float)):
            raise ValueError(
                "company_uid must be quoted in YAML (e.g. company_uid: '18.007') — "
                "unquoted numbers lose trailing zeros"
            )
        return v

    @model_validator(mode="after")
    def validate_source_fields(self) -> CompanyConfig:
        source = self.source
        required: dict[str, list[str]] = {
            "greenhouse": ["token"],
            "lever": ["token"],
            "ashby": ["token"],
            "smartrecruiters": ["token"],
            "comeet": ["token", "company_uid"],
            "workday": ["host", "tenant", "site"],
            "amazon": ["country"],
            "elbit": ["careers_url"],
            "linkedin": ["token"],
            "workable": ["token"],
            "successfactors": ["careers_url"],
            "teamme": ["careers_url"],
            "html": ["careers_url"],
            "browser": ["careers_url"],
            "auto": ["careers_url"],
        }
        for field_name in required.get(source, []):
            if getattr(self, field_name) in (None, ""):
                raise ValueError(f"source '{source}' requires field '{field_name}'")
        return self


class CompanyTypeConfig(BaseModel):
    """One kind of company `jobscout discover` should search for."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    queries: list[str] = Field(default_factory=list)

    @field_validator("queries")
    @classmethod
    def queries_not_empty(cls, value: list[str]) -> list[str]:
        cleaned = [query.strip() for query in value if query and query.strip()]
        if not cleaned:
            raise ValueError("each company type needs at least one query")
        return cleaned


class CompanyTypesFile(BaseModel):
    """Search criteria for discovering companies. Gitignored; the example is the template."""

    model_config = ConfigDict(extra="forbid")

    location: str = ""
    types: list[CompanyTypeConfig]
    ignore_names: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_type_names(self) -> CompanyTypesFile:
        names = [company_type.name.casefold() for company_type in self.types]
        if len(names) != len(set(names)):
            raise ValueError("company type names must be unique")
        return self


class WatchManuallyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    url: str
    why: str = ""


class CompaniesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search_text: str | list[str] | None = None
    companies: list[CompanyConfig] = Field(default_factory=list)
    watch_manually: list[WatchManuallyEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def inherit_global_search_text(self) -> CompaniesFile:
        if self.search_text is None:
            return self
        inherited: list[CompanyConfig] = []
        changed = False
        for company in self.companies:
            if company.search_text is None:
                inherited.append(
                    company.model_copy(update={"search_text": self.search_text})
                )
                changed = True
            else:
                inherited.append(company)
        if changed:
            self.companies = inherited
        return self


class AppConfig:
    def __init__(
        self,
        project_root: Path,
        profile: str,
        preferences: PreferencesConfig,
        settings: SettingsConfig,
        companies: CompaniesFile,
    ) -> None:
        self.project_root = project_root
        self.profile = profile
        self.preferences = preferences
        self.settings = settings
        self.companies = companies

    @property
    def db_path(self) -> Path:
        return self.project_root / self.settings.paths.database

    @property
    def output_dir(self) -> Path:
        return self.project_root / self.settings.paths.output_dir

    def visible_company_names(self, only: list[str] | None = None) -> set[str]:
        """Lowercased company names that should appear in reports.

        A full run uses every company still listed in companies.yaml.
        ``--company`` narrows that to the named subset without dropping
        other stored companies.
        """
        listed = {c.name.lower() for c in self.companies.companies}
        if only:
            return listed & {name.lower() for name in only}
        return listed


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def _validate_companies(data: dict[str, Any]) -> CompaniesFile:
    companies_raw = data.get("companies", [])
    watch_raw = data.get("watch_manually", [])
    companies: list[CompanyConfig] = []
    for i, entry in enumerate(companies_raw):
        try:
            companies.append(CompanyConfig.model_validate(entry))
        except Exception as e:
            name = entry.get("name", f"index {i}") if isinstance(entry, dict) else f"index {i}"
            raise ValueError(f"Invalid company entry '{name}': {e}") from e
    watch: list[WatchManuallyEntry] = []
    for i, entry in enumerate(watch_raw):
        try:
            watch.append(WatchManuallyEntry.model_validate(entry))
        except Exception as e:
            name = entry.get("name", f"index {i}") if isinstance(entry, dict) else f"index {i}"
            raise ValueError(f"Invalid watch_manually entry '{name}': {e}") from e
    return CompaniesFile(
        search_text=data.get("search_text"),
        companies=companies,
        watch_manually=watch,
    )


def _missing_user_file(path: Path) -> FileNotFoundError:
    example = path.parent / f"{path.name}.example"
    hint = ""
    if example.is_file():
        hint = f" Copy {example.name} to {path.name} and fill it in."
    return FileNotFoundError(
        f"Missing {path}.{hint} This file stays on your machine and is gitignored."
    )


def _apply_llm_file(root: Path, settings_data: dict[str, Any]) -> None:
    """Load gitignored config/llm.yaml over any llm block in settings.yaml.

    A checkout with no llm.yaml and no llm block fails closed, so a public
    clone does not fall through to the Cursor provider default.
    """
    llm_path = root / "config" / "llm.yaml"
    if llm_path.is_file():
        llm_data = _load_yaml(llm_path)
        nested = llm_data.get("llm")
        if isinstance(nested, dict):
            llm_data = nested
        if not llm_data:
            raise ValueError(f"{llm_path} is empty. Set provider and model.")
        settings_data["llm"] = llm_data
        return
    if "llm" in settings_data:
        return
    raise _missing_user_file(llm_path)


def load_company_types(path: Path) -> CompanyTypesFile:
    """Load the gitignored company-type file used by `jobscout discover`."""
    if not path.is_file():
        raise _missing_user_file(path)
    return CompanyTypesFile.model_validate(_load_yaml(path))


def load_config(project_root: Path | None = None) -> AppConfig:
    root = (project_root or Path.cwd()).resolve()
    load_dotenv(root / ".env")

    settings_path = root / "config" / "settings.yaml"
    settings_data = _load_yaml(settings_path)
    _apply_llm_file(root, settings_data)
    settings = SettingsConfig.model_validate(settings_data)

    profile_path = root / settings.paths.profile
    if not profile_path.is_file():
        raise _missing_user_file(profile_path)
    profile = profile_path.read_text(encoding="utf-8")

    preferences_path = root / settings.paths.preferences
    if not preferences_path.is_file():
        raise _missing_user_file(preferences_path)
    preferences = PreferencesConfig.model_validate(_load_yaml(preferences_path))

    companies_path = root / settings.paths.companies
    if not companies_path.is_file():
        raise _missing_user_file(companies_path)
    companies = _validate_companies(_load_yaml(companies_path))

    return AppConfig(
        project_root=root,
        profile=profile,
        preferences=preferences,
        settings=settings,
        companies=companies,
    )
