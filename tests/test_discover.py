"""Tests for company discovery."""

from datetime import date
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from jobscout.cli import app
from jobscout.config import CompanyConfig, CompanyTypesFile, load_company_types
from jobscout.discover import (
    DiscoverResult,
    SearchHit,
    append_discovered,
    board_keys,
    choose_name,
    discover_companies,
    is_career_candidate,
    parse_duckduckgo_html,
    planned_queries,
    search_duckduckgo,
    with_location,
)

runner = CliRunner()

DDG_HTML = (
    "<html><body>"
    '<a class="result__a" href="https://duckduckgo.com/l/?uddg='
    'https%3A%2F%2Fwww.comeet.com%2Fjobs%2Fpliops%2F05.004&amp;rut=1">Pliops Careers</a>'
    '<a class="result__a" href="https://hiretik.com/jobs/abc">Embedded Engineer at Hiretik</a>'
    '<a class="result__a" href="https://example.com/about">Example</a>'
    '<a class="result__a" href="https://pliops.com/open-jobs/">Careers | Pliops</a>'
    "</body></html>"
)


def test_company_types_example_loads():
    example = Path(__file__).resolve().parents[1] / "config" / "company_types.yaml.example"
    parsed = load_company_types(example)
    assert {item.name for item in parsed.types} == {
        "embedded",
        "linux-kernel",
        "asic",
        "hardware",
        "ai-accelerator",
        "ai-infrastructure",
        "ai-hardware",
    }
    assert parsed.location == "your country"


def test_with_location_skips_a_query_that_already_names_the_place():
    assert with_location("Israel", "firmware engineer Israel") == "firmware engineer Israel"
    assert with_location("Israel", "firmware engineer") == "firmware engineer Israel"


def test_planned_queries_filters_types():
    types_file = CompanyTypesFile.model_validate(
        {
            "location": "Israel",
            "types": [
                {"name": "embedded", "queries": ["firmware careers"]},
                {"name": "asic", "queries": ["ASIC careers"]},
            ],
        }
    )
    planned = planned_queries(types_file, only_types=["ASIC"])
    assert planned == [("asic", "ASIC careers Israel")]


def test_planned_queries_rejects_unknown_type():
    types_file = CompanyTypesFile.model_validate(
        {"types": [{"name": "embedded", "queries": ["firmware"]}]}
    )
    with pytest.raises(ValueError, match="Unknown company type"):
        planned_queries(types_file, only_types=["nope"])


def test_parse_duckduckgo_unwraps_result_links():
    hits = parse_duckduckgo_html(DDG_HTML)
    assert hits[0].url == "https://www.comeet.com/jobs/pliops/05.004"
    assert hits[0].title == "Pliops Careers"
    assert any(hit.url.startswith("https://hiretik.com/") for hit in hits)


def test_career_candidate_keeps_boards_and_drops_aggregators():
    assert is_career_candidate("https://www.comeet.com/jobs/pliops/05.004")
    assert is_career_candidate("https://pliops.com/open-jobs/")
    assert not is_career_candidate("https://hiretik.com/jobs/abc")
    assert not is_career_candidate("https://en.wikipedia.org/wiki/Pliops")
    assert not is_career_candidate("https://example.com/about")


def test_choose_name_prefers_a_board_slug():
    assert choose_name("https://www.comeet.com/jobs/pliops/05.004", "Senior Engineer") == "Pliops"
    assert choose_name("https://pliops.com/open-jobs/", "Careers | Pliops") == "Pliops"
    assert choose_name("https://jobs.example/careers", "Senior Embedded Engineer") == "Jobs"


def test_board_keys_match_an_existing_comeet_url():
    existing = CompanyConfig(
        name="Pliops",
        source="auto",
        careers_url="https://www.comeet.com/jobs/pliops/05.004",
    )
    found = CompanyConfig(
        name="Pliops Israel",
        source="auto",
        careers_url="https://www.comeet.com/jobs/pliops/05.004/some-role/AA.1",
    )
    assert board_keys(existing) & board_keys(found)


@pytest.mark.asyncio
async def test_discover_adds_a_new_board_and_skips_known_ones():
    types_file = CompanyTypesFile.model_validate(
        {
            "location": "Israel",
            "ignore_names": ["recruiter"],
            "types": [
                {"name": "embedded", "queries": ["firmware"]},
                {"name": "asic", "queries": ["ASIC"]},
            ],
        }
    )
    existing = [
        CompanyConfig(
            name="Pliops",
            source="auto",
            careers_url="https://www.comeet.com/jobs/pliops/05.004",
        )
    ]

    async def search(query: str) -> list[SearchHit]:
        if "firmware" in query:
            return [
                SearchHit("https://www.comeet.com/jobs/pliops/05.004", "Pliops"),
                SearchHit("https://www.comeet.com/jobs/elsight/B9.006", "Elsight Careers"),
                    SearchHit("https://hiretik.com/jobs/1", "Hiretik"),
                    SearchHit("https://example.com/blog/embedded", "A blog"),
                    SearchHit("https://example.com/careers", "Example Careers"),
            ]
        return [
            SearchHit("https://boards.greenhouse.io/nova", "Nova Careers"),
            SearchHit("https://recruiter.example.com/careers", "Recruiter Agency Careers"),
            SearchHit("https://broken.example.com/careers", "Broken"),
        ]

    async def detect(url: str) -> CompanyConfig:
        if "elsight" in url:
            return CompanyConfig(
                name="",
                source="comeet",
                token="tok",
                company_uid="B9.006",
                careers_url=url,
            )
        if "greenhouse.io/nova" in url:
            return CompanyConfig(name="", source="greenhouse", token="nova", careers_url=url)
        if "recruiter" in url:
            return CompanyConfig(name="", source="greenhouse", token="recruiter", careers_url=url)
        if "broken" in url:
            raise RuntimeError("timeout")
        return CompanyConfig(name="", source="html", careers_url=url)

    found = await discover_companies(types_file, existing, search, detect, max_fetches=10)
    by_name = {company.name: company for company in found.added}
    assert set(by_name) == {"Elsight", "Nova"}
    assert by_name["Elsight"].config.source == "auto"
    assert by_name["Elsight"].config.careers_url == "https://www.comeet.com/jobs/elsight/B9.006"
    assert by_name["Elsight"].types == ["embedded"]
    assert by_name["Nova"].config.source == "greenhouse"
    assert by_name["Nova"].config.token == "nova"
    assert by_name["Nova"].config.careers_url is None
    assert by_name["Nova"].types == ["asic"]
    assert "Pliops" in found.already_tracked
    assert any("html" in item for item in found.skipped_unsupported)
    assert any("ignored name" in item for item in found.skipped_unsupported)
    assert any("timeout" in err for err in found.errors)


def test_append_discovered_inserts_before_watch_manually(tmp_path):
    path = tmp_path / "companies.yaml"
    path.write_text(
        "companies:\n"
        "  - name: Acme\n"
        "    source: greenhouse\n"
        "    token: acme\n"
        "watch_manually:\n"
        "  - name: Manual\n"
        "    url: https://example.com\n"
        "    why: check\n",
        encoding="utf-8",
    )
    from jobscout.discover import DiscoveredCompany

    append_discovered(
        path,
        [
            DiscoveredCompany(
                name="Elsight",
                types=["embedded", "hardware"],
                config=CompanyConfig(
                    name="Elsight",
                    source="auto",
                    careers_url="https://www.comeet.com/jobs/elsight/B9.006",
                ),
                url="https://www.comeet.com/jobs/elsight/B9.006",
            )
        ],
        when=date(2026, 10, 18),
    )
    text = path.read_text(encoding="utf-8")
    assert text.index("Elsight") < text.index("watch_manually:")
    assert "Discovered 2026-10-18" in text
    assert "embedded, hardware" in text
    from jobscout.config import _load_yaml, _validate_companies

    parsed = _validate_companies(_load_yaml(path))
    assert [company.name for company in parsed.companies] == ["Acme", "Elsight"]


@respx.mock
@pytest.mark.asyncio
async def test_search_duckduckgo_posts_the_query():
    route = respx.post("https://html.duckduckgo.com/html/").mock(
        return_value=httpx.Response(200, text=DDG_HTML)
    )
    async with httpx.AsyncClient() as client:
        hits = await search_duckduckgo(client, "firmware Israel", timeout=5)
    assert route.called
    assert hits[0].url.endswith("/pliops/05.004")


def test_discover_command_reports_a_missing_types_file(project):
    result = runner.invoke(app, ["discover", "--project", str(project), "--dry-run"])
    assert result.exit_code != 0
    assert "company_types.yaml" in (result.output + str(result.exception))


def test_discover_command_dry_run_does_not_write(project, monkeypatch):
    types_path = project / "config" / "company_types.yaml"
    types_path.write_text(
        "location: Israel\ntypes:\n  - name: embedded\n    queries: ['firmware']\n",
        encoding="utf-8",
    )
    companies_path = project / "config" / "companies.yaml"
    before = companies_path.read_text(encoding="utf-8")

    async def fake_discover(*args, **kwargs):
        return DiscoverResult(
            added=[],
        )

    monkeypatch.setattr("jobscout.cli.discover_companies", fake_discover)
    result = runner.invoke(app, ["discover", "--project", str(project), "--dry-run"])
    assert result.exit_code == 0
    assert "No new companies." in result.output
    assert companies_path.read_text(encoding="utf-8") == before
