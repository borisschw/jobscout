"""Find career boards from company_types.yaml and append them to companies.yaml."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from html import unescape
from urllib.parse import parse_qs, urlparse

import httpx
from selectolax.parser import HTMLParser

from jobscout.config import CompanyConfig, CompanyTypesFile
from jobscout.detect import format_yaml_block

SearchFn = Callable[[str], Awaitable[list["SearchHit"]]]
DetectFn = Callable[[str], Awaitable[CompanyConfig]]

DUCKDUCKGO_HTML = "https://html.duckduckgo.com/html/"

# Hosts that list other companies' jobs. Discover wants the employer's own board.
AGGREGATOR_HOSTS = {
    "alljobs.co.il",
    "angel.co",
    "builtin.com",
    "crunchbase.com",
    "dice.com",
    "drushim.co.il",
    "duckduckgo.com",
    "ethosia.co.il",
    "facebook.com",
    "glassdoor.co.il",
    "glassdoor.com",
    "google.com",
    "gotfriends.co.il",
    "hiretik.com",
    "indeed.com",
    "instagram.com",
    "jobmaster.co.il",
    "jooble.org",
    "levels.fyi",
    "linkedin.com",
    "monster.com",
    "nisha.co.il",
    "pitchbook.com",
    "reddit.com",
    "simplyhired.com",
    "sqlink.com",
    "startupnationcentral.org",
    "teamblind.com",
    "twitter.com",
    "wellfound.com",
    "wikipedia.org",
    "x.com",
    "youtube.com",
    "ziprecruiter.com",
}

ATS_HOSTS = {
    "ashbyhq.com",
    "comeet.com",
    "greenhouse.io",
    "lever.co",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "teamme.link",
    "workable.com",
}

# Pages with no stable board, or a board this command cannot identify safely.
SKIP_SOURCES = {"html", "browser", "linkedin"}

_SLUG_PATTERNS = (
    r"comeet\.com/jobs/([^/?#]+)",
    r"(?:boards|job-boards)\.greenhouse\.io/([^/?#]+)",
    r"jobs\.lever\.co/([^/?#]+)",
    r"jobs\.ashbyhq\.com/([^/?#]+)",
    r"jobs\.smartrecruiters\.com/([^/?#]+)",
    r"apply\.workable\.com/([^/?#]+)",
    r"://([^.]+)\.wd\d+\.myworkdayjobs\.com",
)

_TITLE_NOISE = re.compile(
    r"\b(careers?|jobs?|openings?|positions?|hiring|apply|join us|home)\b",
    re.I,
)
_ROLE_WORDS = re.compile(
    r"\b(engineer|developer|manager|senior|junior|intern|designer)\b",
    re.I,
)


@dataclass
class SearchHit:
    url: str
    title: str = ""


@dataclass
class DiscoveredCompany:
    name: str
    types: list[str]
    config: CompanyConfig
    url: str


@dataclass
class DiscoverResult:
    added: list[DiscoveredCompany] = field(default_factory=list)
    already_tracked: list[str] = field(default_factory=list)
    skipped_unsupported: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def planned_queries(
    types_file: CompanyTypesFile,
    only_types: list[str] | None = None,
    extra_queries: list[str] | None = None,
) -> list[tuple[str, str]]:
    """Return (type name, query) pairs, with location appended when missing."""
    selected = list(types_file.types)
    if only_types:
        known = {company_type.name.casefold(): company_type for company_type in types_file.types}
        missing = [name for name in only_types if name.casefold() not in known]
        if missing:
            names = ", ".join(company_type.name for company_type in types_file.types) or "(none)"
            raise ValueError(f"Unknown company type: {', '.join(missing)}. Known types: {names}")
        seen: set[str] = set()
        selected = []
        for name in only_types:
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            selected.append(known[key])

    planned = [
        (company_type.name, with_location(types_file.location, query))
        for company_type in selected
        for query in company_type.queries
    ]
    for query in extra_queries or []:
        text = query.strip()
        if text:
            planned.append(("custom", with_location(types_file.location, text)))
    if not planned:
        raise ValueError("No search queries. Add queries to company_types.yaml or pass --query.")
    return planned


def with_location(location: str, query: str) -> str:
    place = location.strip()
    if place and place.casefold() not in query.casefold():
        return f"{query} {place}"
    return query


def parse_duckduckgo_html(html: str) -> list[SearchHit]:
    tree = HTMLParser(html)
    nodes = tree.css("a.result__a") or tree.css("a.result-link")
    hits: list[SearchHit] = []
    seen: set[str] = set()
    for node in nodes:
        href = node.attributes.get("href") or ""
        url = unwrap_search_href(href)
        if not url or url in seen:
            continue
        seen.add(url)
        hits.append(SearchHit(url=url, title=node.text(strip=True)))
    return hits


def unwrap_search_href(href: str) -> str:
    href = unescape(href).strip()
    if not href:
        return ""
    parsed = urlparse(href)
    host = parsed.netloc.lower()
    if "duckduckgo.com" in host and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return target.strip()
    if href.startswith("//"):
        return "https:" + href
    return href


def is_career_candidate(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if parsed.path.lower().endswith((".pdf", ".jpg", ".jpeg", ".png", ".zip")):
        return False
    host = _host(url)
    if _matches_host(host, AGGREGATOR_HOSTS):
        return False
    if _matches_host(host, ATS_HOSTS):
        return True
    path = parsed.path.lower()
    return bool(re.search(r"(career|jobs?|opening|position|join-?us)", path))


def choose_name(url: str, title: str) -> str:
    slug = board_slug(url)
    if slug:
        return slug_to_name(slug)
    titled = clean_title(title)
    if titled:
        return titled
    stem = _host(url).split(".")[0]
    return slug_to_name(stem) or "Company"


def board_slug(url: str) -> str | None:
    for pattern in _SLUG_PATTERNS:
        match = re.search(pattern, url, re.I)
        if not match:
            continue
        slug = match.group(1)
        if slug.casefold() in {"embed", "jobs", "job", "careers", "www"}:
            return None
        return slug
    return None


def slug_to_name(slug: str) -> str:
    parts: list[str] = []
    for word in re.split(r"[-_]+", slug.strip()):
        if not word:
            continue
        if word.isupper():
            parts.append(word)
        else:
            parts.append(word[:1].upper() + word[1:])
    return " ".join(parts)


def clean_title(title: str) -> str:
    text = re.sub(r"\s+", " ", title).strip()
    if not text:
        return ""
    at_company = re.search(r"\bat\s+(.+)$", text, re.I)
    if at_company:
        text = at_company.group(1).strip()
    parts = re.split(r"\s+[|–—]\s+|\s+-\s+", text)
    cleaned: list[str] = []
    for part in parts:
        part = _TITLE_NOISE.sub(" ", part)
        part = re.sub(r"\s+", " ", part).strip(" -|:,")
        if part:
            cleaned.append(part)
    if not cleaned:
        return ""
    name = cleaned[0]
    if len(name) < 2 or len(name) > 60 or _ROLE_WORDS.search(name):
        return ""
    return name


def board_keys(company: CompanyConfig) -> set[tuple[str, str]]:
    """Identity keys used to skip a board that is already in companies.yaml."""
    keys = {("name", company.name.casefold())}
    if company.company_uid:
        keys.add(("uid", company.company_uid.casefold()))
    url = company.careers_url or ""
    comeet = re.search(
        r"comeet\.com/jobs/[^/?#]+/([^/?#]+)",
        url,
        re.I,
    )
    if comeet:
        keys.add(("uid", comeet.group(1).casefold()))
    if url:
        keys.add(("url", url.rstrip("/").casefold()))
    if company.source == "workday" and company.host:
        keys.add(
            (
                "workday",
                "|".join(
                    (
                        company.host.casefold(),
                        (company.tenant or "").casefold(),
                        (company.site or "").casefold(),
                    )
                ),
            )
        )
    elif company.token:
        keys.add(("board", company.token.casefold()))
    slug = board_slug(url)
    if slug and company.source != "workday":
        keys.add(("board", slug.casefold()))
    return keys


def tidy_detected(config: CompanyConfig, page_url: str, name: str) -> CompanyConfig:
    """Match the companies.yaml style: Comeet stays `auto`, other boards stay concrete."""
    if config.source == "comeet":
        return CompanyConfig(
            name=name,
            source="auto",
            careers_url=comeet_board_url(page_url) or page_url,
        )
    needs_url = config.source in {"successfactors", "teamme", "elbit", "auto"}
    careers_url = (config.careers_url or page_url) if needs_url else None
    return config.model_copy(update={"name": name, "careers_url": careers_url})


def comeet_board_url(url: str) -> str | None:
    match = re.search(
        r"https?://(?:www\.)?comeet\.com/jobs/([^/?#]+)/([^/?#]+)",
        url,
        re.I,
    )
    if not match:
        return None
    return f"https://www.comeet.com/jobs/{match.group(1)}/{match.group(2)}"


def ignored_name(name: str, patterns: list[str]) -> bool:
    folded = name.casefold()
    return any(pattern.strip().casefold() in folded for pattern in patterns if pattern.strip())


def append_discovered(path, companies: list[DiscoveredCompany], when: date | None = None) -> None:
    """Insert discovered boards before watch_manually, inside the companies list."""
    from pathlib import Path

    target = Path(path)
    text = target.read_text(encoding="utf-8")
    if "companies:" not in text:
        text = text.rstrip() + "\ncompanies:\n"
    day = (when or date.today()).isoformat()
    blocks = [
        f"  # {', '.join(company.types)}\n{format_yaml_block(company.name, company.config)}"
        for company in companies
    ]
    section = f"\n  # Discovered {day} from company_types.yaml\n" + "\n\n".join(blocks) + "\n"
    marker = "\nwatch_manually:"
    if marker in text:
        text = text.replace(marker, section + marker, 1)
    else:
        text = text.rstrip() + "\n" + section
    target.write_text(text, encoding="utf-8")


async def search_duckduckgo(
    client: httpx.AsyncClient,
    query: str,
    timeout: float,
) -> list[SearchHit]:
    response = await client.post(
        DUCKDUCKGO_HTML,
        data={"q": query},
        timeout=timeout,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    response.raise_for_status()
    return parse_duckduckgo_html(response.text)


async def discover_companies(
    types_file: CompanyTypesFile,
    existing: list[CompanyConfig],
    search: SearchFn,
    detect: DetectFn,
    *,
    only_types: list[str] | None = None,
    extra_queries: list[str] | None = None,
    max_fetches: int = 20,
) -> DiscoverResult:
    """Search, detect a supported board, and skip companies already listed."""
    result = DiscoverResult()
    queries = planned_queries(types_file, only_types, extra_queries)
    candidates: dict[str, _Candidate] = {}
    for type_name, query in queries:
        try:
            hits = await search(query)
        except Exception as exc:
            result.errors.append(f"{query}: {exc}")
            continue
        for hit in hits:
            url = hit.url.split("#", 1)[0].rstrip("/")
            if not is_career_candidate(url):
                continue
            candidate = candidates.get(url)
            if candidate is None:
                candidates[url] = _Candidate(url=url, title=hit.title, types=[type_name])
            elif type_name not in candidate.types:
                candidate.types.append(type_name)

    ordered = _round_robin(candidates)
    if max_fetches > 0:
        ordered = ordered[:max_fetches]

    tracked = set()
    for company in existing:
        tracked |= board_keys(company)

    for candidate in ordered:
        preview = CompanyConfig(name="", source="html", careers_url=candidate.url)
        if (board_keys(preview) - {("name", "")}) & tracked:
            result.already_tracked.append(choose_name(candidate.url, candidate.title))
            continue
        try:
            detected = await detect(candidate.url)
        except Exception as exc:
            result.errors.append(f"{candidate.url}: {exc}")
            continue
        if detected.source in SKIP_SOURCES:
            result.skipped_unsupported.append(f"{candidate.url} ({detected.source})")
            continue
        name = choose_name(candidate.url, candidate.title)
        if ignored_name(name, types_file.ignore_names):
            result.skipped_unsupported.append(f"{name} (ignored name)")
            continue
        config = tidy_detected(detected, candidate.url, name)
        keys = board_keys(config)
        if keys & tracked:
            result.already_tracked.append(name)
            continue
        tracked |= keys
        result.added.append(
            DiscoveredCompany(
                name=name,
                types=list(candidate.types),
                config=config,
                url=candidate.url,
            )
        )
    return result


@dataclass
class _Candidate:
    url: str
    title: str
    types: list[str]


def _round_robin(candidates: dict[str, _Candidate]) -> list[_Candidate]:
    buckets: dict[str, list[_Candidate]] = {}
    for candidate in candidates.values():
        buckets.setdefault(candidate.types[0], []).append(candidate)
    pending = list(buckets.values())
    ordered: list[_Candidate] = []
    while any(pending):
        for bucket in pending:
            if bucket:
                ordered.append(bucket.pop(0))
    return ordered


def _host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _matches_host(host: str, suffixes: set[str]) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)
