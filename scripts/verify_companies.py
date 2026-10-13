"""Verify company ATS endpoints and extract Comeet tokens from hosted pages."""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]


async def check_workday(c: dict) -> tuple[int, int | None]:
    url = f"https://{c['host']}/wday/cxs/{c['tenant']}/{c['site']}/jobs"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            url,
            json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
        )
        if r.status_code == 200:
            return r.status_code, r.json().get("total")
        return r.status_code, None


async def check_lever(c: dict) -> tuple[int, int, str, str]:
    token = c.get("token") or c.get("tenant", "")
    region = "eu" if "eu" in (c.get("host") or "") else c.get("region", "us")
    host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
    url = f"https://{host}/v0/postings/{token}?mode=json"
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url)
        count = len(r.json()) if r.status_code == 200 and isinstance(r.json(), list) else 0
        return r.status_code, count, token, region


async def fetch_comeet_token(slug: str, uid: str) -> tuple[int, str | None, int]:
    url = f"https://www.comeet.com/jobs/{slug}/{uid}"
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        r = await client.get(url)
        html = r.text
    token_match = re.search(r'"token"\s*:\s*"([^"]+)"', html)
    token = token_match.group(1) if token_match else None
    return r.status_code, token, len(html)


async def main() -> int:
    companies = yaml.safe_load((ROOT / "config" / "companies.yaml").read_text())["companies"]
    failed = 0
    for c in companies:
        if c.get("enabled") is False:
            continue
        name, src = c["name"], c["source"]
        if src == "workday":
            code, total = await check_workday(c)
            ok = code == 200 and total is not None
            print(f"{'OK' if ok else 'FAIL'} workday {name}: {code} total={total}")
            failed += not ok
        elif src == "lever":
            code, count, token, region = await check_lever(c)
            ok = code == 200 and count >= 0
            print(f"{'OK' if ok else 'FAIL'} lever {name}: {code} jobs={count} token={token} region={region}")
            failed += code != 200
        elif src == "auto":
            from jobscout.detect import detect_from_html

            url = c.get("careers_url", "")
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                r = await client.get(url)
                detected = detect_from_html(r.text, url)
            ok = detected is not None and detected.get("token")
            print(
                f"{'OK' if ok else 'FAIL'} auto {name}: {detected.get('source') if detected else 'none'}"
            )
            failed += not ok
        elif src == "comeet":
            uid = c.get("company_uid", "")
            token = c.get("token", "")
            api_url = f"https://www.comeet.co/careers-api/2.0/company/{uid}/positions?token={token}"
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(api_url)
                count = len(r.json()) if r.status_code == 200 else 0
            ok = r.status_code == 200
            print(f"{'OK' if ok else 'FAIL'} comeet {name}: {r.status_code} jobs={count}")
            failed += not ok
    return failed


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
