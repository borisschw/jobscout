"""jobscout CLI."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from jobscout.config import load_config
from jobscout.detect import detect_ats, format_yaml_block
from jobscout.matching.prompts import prompt_fingerprint
from jobscout.report.writer import write_reports
from jobscout.runner import RunOptions, run_pipeline
from jobscout.serve import start_server
from jobscout.sources.base import make_client
from jobscout.store import Store

app = typer.Typer(name="jobscout", help="Daily job scraper with LLM scoring")
console = Console()


def _project_opt(
    project: Annotated[
        Path | None,
        typer.Option("--project", "-p", help="Project root directory"),
    ] = None,
) -> Path:
    return (project or Path.cwd()).resolve()


def _verbose_opt(
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> bool:
    return verbose


@app.command()
def run(
    project: Annotated[Path | None, typer.Option("--project", "-p")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    seed: Annotated[bool, typer.Option("--seed")] = False,
    rescore: Annotated[bool, typer.Option("--rescore")] = False,
    max_llm_calls: Annotated[
        int | None, typer.Option("--max-llm-calls")
    ] = None,
    company: Annotated[
        list[str] | None, typer.Option("--company")
    ] = None,
    no_report: Annotated[bool, typer.Option("--no-report")] = False,
    serve: Annotated[bool | None, typer.Option("--serve/--no-serve")] = None,
    port: Annotated[int, typer.Option("--port")] = 8765,
    host: Annotated[
        str,
        typer.Option("--host", help="Listen address. 0.0.0.0 accepts other PCs on the LAN."),
    ] = "0.0.0.0",
    open_browser: Annotated[bool, typer.Option("--open/--no-open")] = False,
    schedule: Annotated[
        bool | None,
        typer.Option(
            "--schedule/--no-schedule",
            help="Daily check while the viewer is open. Defaults to settings.yaml.",
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Fetch jobs, score new ones, and write a report."""
    root = (project or Path.cwd()).resolve()
    config = load_config(root)
    store = Store(config.db_path)

    options = RunOptions(
        dry_run=dry_run,
        seed=seed,
        rescore=rescore,
        max_llm_calls=max_llm_calls,
        only_companies=company or [],
        verbose=verbose,
    )

    try:
        summary = asyncio.run(run_pipeline(config, store, options))
    finally:
        pass

    _print_summary(summary)

    exit_code = 1 if summary.failures else 0
    if summary.llm_error_count and summary.scored == 0:
        exit_code = 1

    if not no_report and not dry_run:
        failures = [f"{r.company}: {r.error}" for r in summary.failures]
        counts = store.status_counts()
        write_reports(
            config,
            summary.matches,
            failures,
            interactive=False,
            pending_scoring=counts.get("pending_scoring", 0),
            scoring_errors=summary.llm_errors,
            run=store.latest_run(),
        )

    should_serve = serve if serve is not None else sys.stdout.isatty()
    if should_serve and not dry_run and not no_report:
        try:
            start_server(
                config,
                store,
                port,
                host=host,
                open_browser=open_browser,
                company_filter=company or None,
                schedule=schedule,
            )
        except SystemExit as e:
            exit_code = e.code if isinstance(e.code, int) else 0
    else:
        store.close()

    raise typer.Exit(exit_code)


@app.command()
def seed(
    project: Annotated[Path | None, typer.Option("--project", "-p")] = None,
) -> None:
    """Mark all current postings as seen without scoring."""
    root = (project or Path.cwd()).resolve()
    config = load_config(root)
    store = Store(config.db_path)
    options = RunOptions(seed=True)
    summary = asyncio.run(run_pipeline(config, store, options))
    store.close()
    console.print(f"Seeded {summary.fetched} postings.")


@app.command()
def detect_cmd(
    url: str,
    project: Annotated[Path | None, typer.Option("--project", "-p")] = None,
    add: Annotated[str | None, typer.Option("--add")] = None,
) -> None:
    """Detect ATS from a careers URL."""
    root = (project or Path.cwd()).resolve()
    config = load_config(root)
    client = make_client(config.settings.fetch)

    async def _detect():
        return await detect_ats(url, client, config.settings.fetch)

    result = asyncio.run(_detect())
    asyncio.run(client.aclose())

    name = add or "Company Name"
    block = format_yaml_block(name, result)
    console.print(block)

    if add:
        companies_path = root / config.settings.paths.companies
        text = companies_path.read_text(encoding="utf-8")
        if "companies:" not in text:
            text += "\ncompanies:\n"
        text = text.rstrip() + "\n" + block + "\n"
        companies_path.write_text(text, encoding="utf-8")
        console.print(f"Added to {companies_path}")


@app.command("jobs")
def jobs_cmd(
    project: Annotated[Path | None, typer.Option("--project", "-p")] = None,
    min_score: Annotated[int, typer.Option("--min-score")] = 0,
    company: Annotated[str | None, typer.Option("--company")] = None,
    days: Annotated[int | None, typer.Option("--days")] = None,
    include_closed: Annotated[bool, typer.Option("--include-closed")] = False,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
) -> None:
    """List scored jobs from the database."""
    from datetime import UTC, datetime, timedelta

    root = (project or Path.cwd()).resolve()
    config = load_config(root)
    store = Store(config.db_path)

    since = None
    if days:
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat().replace("+00:00", "Z")

    results = store.scored_jobs(
        min_score=min_score,
        include_closed=include_closed,
        company=company,
        since=since,
        limit=limit,
    )

    table = Table(title="Scored Jobs")
    table.add_column("Score")
    table.add_column("Title")
    table.add_column("Company")
    table.add_column("Verdict")

    for sj in results:
        table.add_row(
            str(sj.verdict.score),
            sj.job.display_title[:80],
            sj.job.company,
            sj.verdict.verdict.value,
        )

    console.print(table)
    store.close()


@app.command()
def ignore(
    keys: Annotated[list[str] | None, typer.Argument()] = None,
    project: Annotated[Path | None, typer.Option("--project", "-p")] = None,
    list_: Annotated[bool, typer.Option("--list")] = False,
    undo: Annotated[bool, typer.Option("--undo")] = False,
    clear: Annotated[bool, typer.Option("--clear")] = False,
) -> None:
    """Ignore job postings by key, prefix, or URL."""
    root = (project or Path.cwd()).resolve()
    config = load_config(root)
    store = Store(config.db_path)

    if list_:
        _print_mark_table(
            store.list_ignored(),
            title="Ignored jobs",
            when_column="Ignored",
            when_field="ignored_at",
            empty="No ignored jobs.",
            restore="uv run jobscout ignore --undo <key>",
        )
        store.close()
        return

    if clear:
        with store.transaction():
            n = store.clear_ignored()
        console.print(f"Cleared {n} ignored entries.")
        store.close()
        return

    if not keys:
        console.print("Provide keys or use --list / --clear")
        raise typer.Exit(1)

    found, unmatched = store.resolve_job_keys(keys)
    if unmatched:
        console.print(f"Unmatched: {unmatched}")
        raise typer.Exit(1)

    with store.transaction():
        if undo:
            changed = store.unignore(found)
            console.print(f"Unignored {changed} posting(s).")
        else:
            changed = store.ignore(found)
            console.print(f"Ignored {changed} posting(s).")

    store.close()


@app.command()
def applied(
    keys: Annotated[list[str] | None, typer.Argument()] = None,
    project: Annotated[Path | None, typer.Option("--project", "-p")] = None,
    list_: Annotated[bool, typer.Option("--list")] = False,
    undo: Annotated[bool, typer.Option("--undo")] = False,
    clear: Annotated[bool, typer.Option("--clear")] = False,
) -> None:
    """Mark postings you already applied to, or list and restore them."""
    root = (project or Path.cwd()).resolve()
    config = load_config(root)
    store = Store(config.db_path)

    if list_:
        _print_mark_table(
            store.list_applied(),
            title="Applied jobs",
            when_column="Applied",
            when_field="applied_at",
            empty="No applied jobs.",
            restore="uv run jobscout applied --undo <key>",
        )
        store.close()
        return

    if clear:
        with store.transaction():
            n = store.clear_applied()
        console.print(f"Cleared {n} applied entries.")
        store.close()
        return

    if not keys:
        console.print("Provide keys or use --list / --clear")
        raise typer.Exit(1)

    found, unmatched = store.resolve_job_keys(keys)
    if unmatched:
        console.print(f"Unmatched: {unmatched}")
        raise typer.Exit(1)

    with store.transaction():
        if undo:
            changed = store.unapply(found)
            console.print(f"Cleared applied mark on {changed} posting(s).")
        else:
            changed = store.mark_applied(found)
            console.print(f"Marked {changed} posting(s) as applied.")

    store.close()


@app.command()
def report(
    project: Annotated[Path | None, typer.Option("--project", "-p")] = None,
) -> None:
    """Regenerate report from database."""
    root = (project or Path.cwd()).resolve()
    config = load_config(root)
    store = Store(config.db_path)
    with store.transaction():
        repaired = store.repair_workday_urls(config.companies.companies)
    if repaired:
        console.print(f"Repaired {repaired} Workday job URL(s)")
    threshold = config.preferences.score_threshold
    allowed = config.visible_company_names()
    matches = [
        m
        for m in store.scored_jobs(min_score=threshold)
        if m.job.company.lower() in allowed
    ]
    counts = store.status_counts()
    html, md = write_reports(
        config,
        matches,
        [],
        interactive=False,
        pending_scoring=counts.get("pending_scoring", 0),
        run=store.latest_run(),
    )
    console.print(f"Wrote {html} and {md}")
    store.close()


@app.command()
def serve(
    project: Annotated[Path | None, typer.Option("--project", "-p")] = None,
    port: Annotated[int, typer.Option("--port")] = 8765,
    host: Annotated[
        str,
        typer.Option("--host", help="Listen address. 0.0.0.0 accepts other PCs on the LAN."),
    ] = "0.0.0.0",
    open_browser: Annotated[bool, typer.Option("--open/--no-open")] = False,
    schedule: Annotated[
        bool | None,
        typer.Option(
            "--schedule/--no-schedule",
            help="Daily check while the viewer is open. Defaults to settings.yaml.",
        ),
    ] = None,
) -> None:
    """Start the local report viewer.

    When the schedule in settings.yaml is enabled, this process also fetches
    and scores once a day. Leave the report tab open; it reloads when the
    check finishes.
    """
    from jobscout.serve import main as serve_main

    root = (project or Path.cwd()).resolve()
    serve_main(root, port, open_browser, schedule, host)


@app.command()
def status(
    project: Annotated[Path | None, typer.Option("--project", "-p")] = None,
) -> None:
    """Show database status."""
    root = (project or Path.cwd()).resolve()
    config = load_config(root)
    store = Store(config.db_path)
    counts = store.status_counts()
    latest = store.latest_run()
    pf = prompt_fingerprint(config.profile, config.preferences)

    table = Table(title="jobscout status")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("Total jobs", str(counts["total"]))
    table.add_row("Open", str(counts["open"]))
    table.add_row("Closed", str(counts["closed"]))
    table.add_row("Ignored", str(counts["ignored"]))
    table.add_row("Applied", str(counts["applied"]))
    table.add_row("Pending scoring", str(counts.get("pending_scoring", 0)))
    table.add_row("Prompt version", pf)
    if latest:
        table.add_row("Last run", latest["started_at"])
        table.add_row("Last fetched", str(latest["fetched"]))
        table.add_row("Last LLM calls", str(latest["llm_calls"]))
    console.print(table)
    store.close()


def _print_mark_table(
    rows: list,
    *,
    title: str,
    when_column: str,
    when_field: str,
    empty: str,
    restore: str,
) -> None:
    if not rows:
        console.print(empty)
        return
    table = Table(title=title, caption=f"Restore one with: {restore}")
    table.add_column(when_column)
    table.add_column("Company")
    table.add_column("Title")
    table.add_column("Key")
    for row in rows:
        when = (row[when_field] or "").replace("T", " ").removesuffix("Z")
        table.add_row(
            when,
            row["company"] or "?",
            row["title"] or "(missing job)",
            row["job_key"][:12],
        )
    console.print(table)


def _print_summary(summary) -> None:
    table = Table(title="Run Summary")
    table.add_column("Metric")
    table.add_column("Count")
    for label, val in [
        ("Fetched", summary.fetched),
        ("New", summary.new),
        ("Changed", summary.changed),
        ("Removed from listings", summary.closed),
        ("Removed", summary.removed),
        ("Scored", summary.scored),
        ("LLM calls", summary.llm_calls),
        ("LLM errors", summary.llm_error_count),
    ]:
        table.add_row(label, str(val))
    console.print(table)

    if summary.llm_errors:
        console.print("[red]Scoring failures:[/red]")
        for err in summary.llm_errors[:5]:
            console.print(f"  {err}")

    if summary.failures:
        console.print("[red]Fetch failures:[/red]")
        for f in summary.failures:
            console.print(f"  {f.company}: {f.error}")


if __name__ == "__main__":
    app()
