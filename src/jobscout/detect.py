"""ATS detection from career page URLs."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from urllib.parse import urljoin, urlparse

import httpx

from jobscout.config import CompanyConfig, FetchConfig

TOKEN_BLOCKLIST = {
    "embed", "assets", "static", "www", "api", "js", "css", "images", "search",
}

CANDIDATE_PATHS = ["", "/careers", "/jobs", "/careers/open-positions", "/open-positions"]


def candidate_urls(url: str) -> Iterator[str]:
    seen: set[str] = set()
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    for base in (url.rstrip("/"), origin):
        for path in CANDIDATE_PATHS:
            candidate = urljoin(base + "/", path.lstrip("/")) if path else base
            candidate = candidate.rstrip("/") or candidate
            if candidate not in seen:
                seen.add(candidate)
                yield candidate


def _is_valid_token(token: str) -> bool:
    return bool(token) and token.lower() not in TOKEN_BLOCKLIST


async def fetch_html(client: httpx.AsyncClient, url: str, fetch: FetchConfig) -> str:
    resp = await client.get(url, timeout=fetch.timeout_seconds)
    resp.raise_for_status()
    return resp.text


async def render_with_browser(url: str) -> str:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=60000)
        html = await page.content()
        await browser.close()
        return html


def detect_from_html(html: str, page_url: str) -> dict | None:
    """Detect ATS from HTML. Order-sensitive."""
    # 1. Comeet
    comeet = _detect_comeet(html, page_url)
    if comeet:
        return comeet

    # 2. Lever
    lever = _detect_lever(html)
    if lever:
        return lever

    # 3. Greenhouse
    gh = _detect_greenhouse(html)
    if gh:
        return gh

    # 4. Ashby
    ashby = _detect_ashby(html)
    if ashby:
        return ashby

    # 5. SmartRecruiters
    sr = _detect_smartrecruiters(html)
    if sr:
        return sr

    # 6. Workday
    wd = _detect_workday(html)
    if wd:
        return wd

    # 7. Workable
    workable = _detect_workable(html, page_url)
    if workable:
        return workable

    # 8. SuccessFactors RMK
    sf = _detect_successfactors(html, page_url)
    if sf:
        return sf

    # 9. Teamme
    teamme = _detect_teamme(html, page_url)
    if teamme:
        return teamme

    # 10. Elbit Systems career site
    elbit = _detect_elbit(html, page_url)
    if elbit:
        return elbit

    # 11. LinkedIn company jobs
    linkedin = _detect_linkedin(html, page_url)
    if linkedin:
        return linkedin

    return None


def _detect_comeet(html: str, page_url: str = "") -> dict | None:
    company_uid: str | None = None
    token: str | None = None

    url_uid = re.search(r"comeet\.com/jobs/[^/\"'\s]+/([^/?\"'\s]+)", page_url)
    if url_uid:
        company_uid = url_uid.group(1)

    for pat in (
        r'"company-uid"\s*:\s*"([^"]+)"',
        r"'company-uid'\s*:\s*'([^']+)'",
        r'company-uid=["\']([^"\']+)["\']',
    ):
        m = re.search(pat, html)
        if m:
            company_uid = m.group(1)
            break

    for pat in (
        r'"token"\s*:\s*"([^"]+)"',
        r"'token'\s*:\s*'([^']+)'",
        r'(?:comeet[^"\']*token|token)=["\']([^"\']+)["\']',
    ):
        m = re.search(pat, html, re.I)
        if m:
            token = m.group(1)
            break

    api_match = re.search(r"comeet\.co/careers-api/2\.0/company/([^/\"'\s]+)", html)
    if api_match and _is_valid_token(api_match.group(1)):
        company_uid = api_match.group(1)

    if company_uid and _is_valid_token(company_uid) and token:
        return {"source": "comeet", "company_uid": company_uid, "token": token}
    return None


def _detect_lever(html: str) -> dict | None:
    patterns = [
        r"api\.eu\.lever\.co/v0/postings/([^/?\"'\s]+)",
        r"api\.lever\.co/v0/postings/([^/?\"'\s]+)",
        r"jobs\.lever\.co/([^/?\"'\s]+)",
    ]
    for i, pat in enumerate(patterns):
        m = re.search(pat, html)
        if m and _is_valid_token(m.group(1)):
            return {"source": "lever", "token": m.group(1), "region": "eu" if i == 0 else "us"}
    return None


def _detect_greenhouse(html: str) -> dict | None:
    patterns = [
        r"boards-api\.greenhouse\.io/v1/boards/([^/?\"'\s]+)",
        r"boards\.greenhouse\.io/([^/?\"'\s]+)",
        r'gh_jid|greenhouse[^"\']*["\']?\s*:\s*["\']([^"\']+)["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.I)
        if m and _is_valid_token(m.group(1)):
            return {"source": "greenhouse", "token": m.group(1)}
    return None


def _detect_ashby(html: str) -> dict | None:
    patterns = [
        r"api\.ashbyhq\.com/posting-api/job-board/([^/?\"'\s]+)",
        r"jobs\.ashbyhq\.com/([^/?\"'\s]+)",
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m and _is_valid_token(m.group(1)):
            return {"source": "ashby", "token": m.group(1)}
    return None


def _detect_smartrecruiters(html: str) -> dict | None:
    patterns = [
        r"api\.smartrecruiters\.com/v1/companies/([^/?\"'\s]+)",
        r"careers\.smartrecruiters\.com/([^/?\"'\s]+)",
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m and _is_valid_token(m.group(1)):
            return {"source": "smartrecruiters", "token": m.group(1)}
    return None


def _detect_workday(html: str) -> dict | None:
    patterns = [
        r"(https?://[^/\"'\s]+)/wday/cxs/([^/\"'\s]+)/([^/\"'\s]+)/jobs",
        r'"(?:host|wdHost)"\s*:\s*"([^"]+)"[^}]*"(?:site|wdSite)"\s*:\s*"([^"]+)"',
    ]
    m = re.search(patterns[0], html)
    if m:
        host = m.group(1).replace("https://", "").replace("http://", "")
        tenant = m.group(2)
        site = m.group(3)
        return {"source": "workday", "host": host, "tenant": tenant, "site": site}

    m = re.search(patterns[1], html)
    if m:
        host = m.group(1)
        site = m.group(2)
        tenant = host.split(".")[0]
        return {"source": "workday", "host": host, "tenant": tenant, "site": site}

    iframe_match = re.search(r'<iframe[^>]+src="([^"]*myworkdayjobs[^"]*)"', html)
    if iframe_match:
        src = iframe_match.group(1)
        parsed = urlparse(src)
        host = parsed.netloc
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2:
            tenant = host.split(".")[0]
            site = parts[0]
            return {"source": "workday", "host": host, "tenant": tenant, "site": site}

    return None


def _detect_workable(html: str, page_url: str = "") -> dict | None:
    patterns = [
        r"apply\.workable\.com/api/v\d+/accounts/([^/?\"'\s]+)",
        r"www\.workable\.com/api/accounts/([^/?\"'\s]+)",
        r"apply\.workable\.com/([^/?\"'\s]+)",
    ]
    skip = TOKEN_BLOCKLIST | {"j", "jobs"}
    for text in (page_url, html):
        for pat in patterns:
            m = re.search(pat, text)
            if m and _is_valid_token(m.group(1)) and m.group(1).lower() not in skip:
                return {"source": "workable", "token": m.group(1)}
    return None


def _detect_successfactors(html: str, page_url: str = "") -> dict | None:
    blob = f"{page_url}\n{html}"
    if not re.search(
        r"rmkcdn\.successfactors\.com|tile-search-results|jobs2web|"
        r"performancemanager\d*\.successfactors\.com",
        blob,
        re.I,
    ):
        return None
    parsed = urlparse(page_url)
    if parsed.scheme and parsed.netloc:
        careers_url = f"{parsed.scheme}://{parsed.netloc}"
    else:
        careers_url = page_url
    if not careers_url:
        return None
    return {"source": "successfactors", "careers_url": careers_url}


def _detect_teamme(html: str, page_url: str = "") -> dict | None:
    for text in (page_url, html):
        m = re.search(r"https?://([^/\"'\s]+)\.teamme\.link", text)
        if m and _is_valid_token(m.group(1)):
            host = m.group(1)
            return {
                "source": "teamme",
                "careers_url": f"https://{host}.teamme.link",
            }
    parsed = urlparse(page_url)
    if parsed.netloc.endswith(".teamme.link") or parsed.netloc == "teamme.link":
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else page_url
        return {"source": "teamme", "careers_url": origin}
    return None


def _detect_elbit(html: str, page_url: str = "") -> dict | None:
    blob = f"{page_url}\n{html}"
    if not re.search(r"elbitsystemscareer\.com", blob, re.I):
        return None
    parsed = urlparse(page_url)
    if parsed.netloc and "elbitsystemscareer.com" in parsed.netloc.lower():
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else page_url
    else:
        origin = "https://elbitsystemscareer.com"
    return {"source": "elbit", "careers_url": origin.rstrip("/")}


def _detect_linkedin(html: str, page_url: str = "") -> dict | None:
    blob = f"{page_url}\n{html}"
    if not re.search(r"linkedin\.com/(?:company|jobs)", blob, re.I):
        return None
    token: str | None = None
    for pat in (
        r"[?&]f_C=(\d+)",
        r'"urn:li:organization:(\d+)"',
        r'"urn:li:fs_normalized_company:(\d+)"',
        r'"companyId":(\d+)',
    ):
        match = re.search(pat, blob)
        if match:
            token = match.group(1)
            break
    if not token:
        return None
    return {"source": "linkedin", "token": token}


async def detect_ats(
    url: str,
    client: httpx.AsyncClient,
    fetch: FetchConfig,
) -> CompanyConfig:
    """Detect ATS backing a careers URL."""
    last_error: Exception | None = None
    html: str | None = None
    loaded_url: str | None = None

    for candidate in candidate_urls(url):
        try:
            html = await fetch_html(client, candidate, fetch)
            loaded_url = candidate
            break
        except Exception as e:
            last_error = e
            continue

    if html is None:
        raise RuntimeError(f"Could not load any candidate URL for {url}: {last_error}")

    result = detect_from_html(html, loaded_url or url)

    if result is None and fetch.allow_browser:
        try:
            html = await render_with_browser(loaded_url or url)
            result = detect_from_html(html, loaded_url or url)
        except Exception:
            pass

    if result:
        extra = {k: v for k, v in result.items() if k != "source"}
        extra.setdefault("careers_url", url)
        return CompanyConfig(
            name="",
            source=result["source"],
            **extra,
        )

    if fetch.allow_browser:
        return CompanyConfig(name="", source="browser", careers_url=url)

    return CompanyConfig(name="", source="html", careers_url=url)


def _yaml_name(name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._+-]*", name):
        return name
    return json.dumps(name)


def format_yaml_block(name: str, config: CompanyConfig) -> str:
    lines = [f"  - name: {_yaml_name(name)}", f"    source: {config.source}"]
    field_map = {
        "token": config.token,
        "host": config.host,
        "tenant": config.tenant,
        "site": config.site,
        "country": config.country,
        "careers_url": config.careers_url,
        "region": config.region if config.source == "lever" else None,
        "company_uid": config.company_uid,
    }
    for key, val in field_map.items():
        if val:
            if key == "company_uid":
                lines.append(f"    {key}: '{val}'")
            else:
                lines.append(f"    {key}: {val}")
    return "\n".join(lines)
