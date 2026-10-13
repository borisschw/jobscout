"""Main pipeline runner."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field

from jobscout.config import AppConfig, CompanyConfig, FetchConfig
from jobscout.detect import detect_ats
from jobscout.matching.llm import JobJudge
from jobscout.matching.prompts import prompt_fingerprint
from jobscout.models import FetchResult, Job, RunSummary, ScoredJob, VerdictLabel
from jobscout.prefilter import check as prefilter_check
from jobscout.progress import RunProgress
from jobscout.sources.base import SourceError, build_source, gather_limited, make_client
from jobscout.sources.workday import repair_workday_job
from jobscout.store import Store, utc_now


@dataclass
class RunOptions:
    dry_run: bool = False
    seed: bool = False
    rescore: bool = False
    max_llm_calls: int | None = None
    only_companies: list[str] = field(default_factory=list)
    verbose: bool = False


async def run_pipeline(
    config: AppConfig,
    store: Store,
    options: RunOptions,
    judge: JobJudge | None = None,
) -> RunSummary:
    started_at = utc_now()
    prompt_version = prompt_fingerprint(config.profile, config.preferences)

    companies = [c for c in config.companies.companies if c.enabled]
    if options.only_companies:
        filters = {n.lower() for n in options.only_companies}
        companies = [c for c in companies if c.name.lower() in filters]

    fetch_cfg = config.settings.fetch
    client = make_client(fetch_cfg)
    semaphore = asyncio.Semaphore(fetch_cfg.concurrency)
    progress = RunProgress(
        enabled=sys.stderr.isatty() or options.verbose,
        verbose=options.verbose,
    )

    removed = 0
    if not options.only_companies and not options.dry_run:
        keep = {c.name for c in config.companies.companies}
        drop = [
            name
            for name in store.companies_in_store()
            if name.lower() not in {n.lower() for n in keep}
        ]
        if drop:
            progress.log(
                "Removing stored jobs for companies no longer in the list: "
                + ", ".join(drop)
            )
            with store.transaction():
                removed = store.delete_unlisted_companies(keep)
            progress.log(f"  deleted {removed} stored posting(s)")

    with store.transaction():
        repaired = store.repair_workday_urls(config.companies.companies)
    if repaired:
        progress.log(f"Repaired {repaired} stored Workday job URL(s)")

    if not companies:
        progress.log("No enabled companies to fetch.")
    else:
        names = ", ".join(c.name for c in companies)
        mode = "dry run" if options.dry_run else "seed" if options.seed else "run"
        progress.log(f"Starting jobscout {mode} ({len(companies)} companies: {names})")

    resolved_by_name: dict[str, CompanyConfig] = {}

    async def fetch_company(company: CompanyConfig) -> FetchResult:
        async with semaphore:
            try:
                resolved = await _resolve_company(company, client, fetch_cfg)
                resolved_by_name[company.name] = resolved
                source = build_source(resolved, fetch_cfg)
                source.progress = progress
                if resolved.source == "linkedin":
                    source.store = store
                progress.log(f"Fetching {company.name} ({resolved.source})…")
                jobs = await source.fetch_jobs(client)
                if resolved.source == "workday" and resolved.host and resolved.site:
                    jobs = [
                        repair_workday_job(job, resolved.host, resolved.site)
                        for job in jobs
                    ]
                suffix = ""
                if source.resume_start is not None:
                    suffix = f"; next run resumes at start={source.resume_start}"
                progress.log(f"  {company.name}: {len(jobs)} jobs fetched{suffix}")
                return FetchResult(
                    company=company.name,
                    source=resolved.source,
                    jobs=jobs,
                    complete=source.listing_complete,
                    resume_start=source.resume_start,
                )
            except Exception as e:
                progress.log(f"  {company.name}: fetch failed — {e}")
                return FetchResult(
                    company=company.name,
                    source=company.source,
                    error=str(e),
                )

    fetch_results = await asyncio.gather(*[fetch_company(c) for c in companies])

    summary = RunSummary(started_at=started_at, fetch_results=list(fetch_results))
    summary.removed = removed
    fresh_keys: set[str] = set()
    candidates: list[Job] = []
    failed_companies: set[str] = {r.company for r in fetch_results if r.error}

    if any(not r.error and r.jobs for r in fetch_results):
        progress.log("Saving jobs to database…")

    with store.transaction():
        for result in fetch_results:
            if result.error:
                continue
            seen_keys: set[str] = set()
            for job in result.jobs:
                summary.fetched += 1
                status = store.upsert_job(job)
                seen_keys.add(job.job_key)
                if status == "new":
                    summary.new += 1
                    fresh_keys.add(job.job_key)
                elif status == "changed":
                    summary.changed += 1
                    fresh_keys.add(job.job_key)
                if options.rescore or store.needs_scoring(job, prompt_version):
                    candidates.append(job)

            if result.source == "linkedin":
                if result.resume_start is not None:
                    store.set_linkedin_cursor(result.company, result.resume_start)
                else:
                    store.clear_linkedin_cursor(result.company)
            # A stopped or resumed LinkedIn fetch has not seen the whole board,
            # so jobs on pages it did not reach must stay in the database.
            if result.complete:
                closed = store.mark_missing(
                    result.company, seen_keys, source=result.source
                )
                summary.closed += closed

    if summary.fetched:
        progress.log(
            f"Database updated: {summary.fetched} fetched, "
            f"{summary.new} new, {summary.changed} changed, "
            f"{summary.closed} removed"
        )

    if options.seed:
        with store.transaction():
            for result in fetch_results:
                if result.error:
                    continue
                for job in result.jobs:
                    store.mark_scored(job)
    elif options.dry_run:
        would_prefilter = sum(
            1 for job in candidates if prefilter_check(job, config.preferences.prefilter) is None
        )
        summary.scored = would_prefilter
        progress.log(
            f"Dry run: {len(candidates)} scoring candidates, "
            f"{would_prefilter} would pass prefilter (LLM skipped)"
        )
    else:
        await _score_candidates(
            config,
            store,
            candidates,
            summary,
            options,
            judge,
            prompt_version,
            client,
            fetch_cfg,
            progress,
            resolved_by_name,
        )

    summary.matches = _standing_matches(
        store, config, fresh_keys, options.only_companies
    )
    summary.finished_at = utc_now()

    if not options.dry_run:
        with store.transaction():
            store.save_run(
                started_at=started_at,
                finished_at=summary.finished_at,
                fetched=summary.fetched,
                new=summary.new,
                changed=summary.changed,
                closed=summary.closed,
                scored=summary.scored,
                llm_calls=summary.llm_calls,
                removed=summary.removed,
                llm_errors=summary.llm_error_count,
            )

    await client.aclose()
    # Windows ProactorEventLoop needs a tick to close subprocess transports.
    if candidates and not options.dry_run and not options.seed:
        await asyncio.sleep(0)
    return summary


async def _resolve_company(
    company: CompanyConfig,
    client,
    fetch_cfg,
) -> CompanyConfig:
    if company.source != "auto":
        return company
    detected = await detect_ats(company.careers_url or "", client, fetch_cfg)
    return company.model_copy(
        update={
            "source": detected.source,
            "token": detected.token or company.token,
            "host": detected.host or company.host,
            "tenant": detected.tenant or company.tenant,
            "site": detected.site or company.site,
            "company_uid": detected.company_uid or company.company_uid,
            "region": detected.region,
        }
    )


async def _enrich_jobs_for_scoring(
    config: AppConfig,
    fetch_cfg: FetchConfig,
    client,
    jobs: list[Job],
    progress: RunProgress,
    resolved_by_name: dict[str, CompanyConfig] | None = None,
) -> list[Job]:
    if not jobs:
        return jobs

    resolved = dict(resolved_by_name or {})
    company_cfg = {c.name: c for c in config.companies.companies}
    by_company: dict[str, list[Job]] = {}
    for job in jobs:
        by_company.setdefault(job.company, []).append(job)

    enriched: list[Job] = []
    for company_name, company_jobs in by_company.items():
        cfg = resolved.get(company_name) or company_cfg.get(company_name)
        if cfg is None:
            enriched.extend(company_jobs)
            continue
        if cfg.source == "auto":
            cfg = await _resolve_company(cfg, client, fetch_cfg)
            resolved[company_name] = cfg
        source = build_source(cfg, fetch_cfg)
        source.progress = progress
        progress.log(f"Fetching job descriptions for {company_name} ({len(company_jobs)} jobs)…")
        done = await gather_limited(
            company_jobs,
            fetch_cfg.concurrency,
            lambda job, src=source: src.enrich_job(client, job),
        )
        enriched.extend(done)
    return enriched


async def _score_candidates(
    config: AppConfig,
    store: Store,
    candidates: list[Job],
    summary: RunSummary,
    options: RunOptions,
    judge: JobJudge | None,
    prompt_version: str,
    client,
    fetch_cfg: FetchConfig,
    progress: RunProgress,
    resolved_by_name: dict[str, CompanyConfig] | None = None,
) -> None:
    prefilter_cfg = config.preferences.prefilter
    to_judge: list[Job] = []
    skipped = 0

    progress.log(f"Prefiltering {len(candidates)} scoring candidates…")
    with store.transaction():
        for job in candidates:
            skip = prefilter_check(job, prefilter_cfg)
            if skip:
                skipped += 1
                store.save_verdict(job, skip)
                store.mark_scored(job)
            else:
                to_judge.append(job)

    progress.log(f"  {skipped} skipped by prefilter, {len(to_judge)} need LLM scoring")

    to_judge = await _enrich_jobs_for_scoring(
        config, fetch_cfg, client, to_judge, progress, resolved_by_name
    )

    j = judge or JobJudge(config)
    try:
        if to_judge:
            progress.log(
                f"Scoring {len(to_judge)} jobs with {config.settings.llm.model}…"
            )
        scored_pairs, llm_calls = await j.score_jobs(
            to_judge,
            store,
            rescore=options.rescore,
            max_llm_calls=options.max_llm_calls,
            progress=progress,
        )
        summary.llm_calls = llm_calls

        with store.transaction():
            for job, verdict in scored_pairs:
                if verdict.verdict == VerdictLabel.ERROR:
                    summary.llm_error_count += 1
                    if verdict.why and verdict.why not in summary.llm_errors:
                        summary.llm_errors.append(verdict.why)
                    continue
                store.save_verdict(job, verdict)
                store.mark_scored(job)
                if verdict.verdict in (VerdictLabel.STRONG, VerdictLabel.POSSIBLE):
                    summary.scored += 1
    finally:
        if judge is None:
            await j.aclose()


def _standing_matches(
    store: Store,
    config: AppConfig,
    fresh_keys: set[str],
    only_companies: list[str] | None = None,
) -> list[ScoredJob]:
    threshold = config.preferences.score_threshold
    allowed = config.visible_company_names(only_companies)
    all_matches = store.scored_jobs(min_score=threshold)
    result: list[ScoredJob] = []
    for sj in all_matches:
        if sj.job.company.lower() not in allowed:
            continue
        if sj.verdict.verdict in (VerdictLabel.STRONG, VerdictLabel.POSSIBLE):
            sj.is_new = sj.job.job_key in fresh_keys
            result.append(sj)
    return result
