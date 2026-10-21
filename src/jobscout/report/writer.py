"""Report builder and writer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from jobscout.config import AppConfig, WatchManuallyEntry
from jobscout.models import ScoredJob, VerdictLabel
from jobscout.schedule import next_check_label, parse_timestamp

_SUMMARY_FIELDS = (
    ("Fetched", "fetched"),
    ("New", "new"),
    ("Changed", "changed"),
    ("Removed from listings", "closed"),
    ("Removed", "removed"),
    ("Scored", "scored"),
    ("LLM calls", "llm_calls"),
    ("LLM errors", "llm_errors"),
)


@dataclass
class ReportGroup:
    title: str
    jobs: list[ScoredJob]


def _run_field(run: object, key: str, default: int | str = 0) -> int | str:
    if isinstance(run, dict):
        value = run.get(key, default)
    else:
        try:
            value = run[key]  # type: ignore[index]
        except (KeyError, IndexError):
            return default
    return default if value is None else value


def run_summary_view(run: object | None) -> dict | None:
    """Counts from the latest saved run, for the report sidebar."""
    if run is None:
        return None
    return {
        "rows": [(label, int(_run_field(run, field))) for label, field in _SUMMARY_FIELDS],
        "started_at": str(_run_field(run, "started_at", "")),
        "finished_at": str(_run_field(run, "finished_at", "") or ""),
    }


def mark_new_since_run(matches: list[ScoredJob], run: object | None) -> None:
    """Mark postings first stored during this run.

    The viewer rebuilds the page from the database, which does not keep the
    in-memory new flag. ``first_seen`` falls between the run's start and finish
    for a posting that showed up in that run.
    """
    view = run_summary_view(run)
    if view is None or not view["started_at"]:
        return
    started = view["started_at"]
    finished = view["finished_at"]
    for match in matches:
        seen = match.first_seen or ""
        match.is_new = seen >= started and (not finished or seen <= finished)


def build_groups(
    matches: list[ScoredJob],
    threshold: int,
) -> list[ReportGroup]:
    filtered = [
        m
        for m in matches
        if m.verdict.score >= threshold
        and m.verdict.verdict in (VerdictLabel.STRONG, VerdictLabel.POSSIBLE)
    ]

    groups: list[ReportGroup] = []
    placed: set[str] = set()

    def take(predicate) -> list[ScoredJob]:
        chosen: list[ScoredJob] = []
        for match in filtered:
            key = match.job.job_key
            if key in placed or not predicate(match):
                continue
            placed.add(key)
            chosen.append(match)
        return chosen

    new_jobs = take(lambda match: match.is_new)
    if new_jobs:
        groups.append(ReportGroup(title="New since the last run", jobs=new_jobs))

    strong = take(lambda match: match.verdict.verdict == VerdictLabel.STRONG)
    if strong:
        groups.append(ReportGroup(title="Strong matches", jobs=strong))

    worth = take(lambda match: match.verdict.verdict == VerdictLabel.POSSIBLE)
    if worth:
        groups.append(ReportGroup(title=f"Worth a look (score >= {threshold})", jobs=worth))

    return groups


def local_stamp(moment: datetime | None = None) -> str:
    """Wall-clock date and time on this machine, as ``dd/mm/yyyy HH:MM``."""
    local = (moment or datetime.now(timezone.utc)).astimezone()
    return local.strftime("%d/%m/%Y %H:%M")


def last_scan_stamp(summary: dict | None) -> str | None:
    """Local time the latest scan finished.

    Stored run times are UTC. The page shows when that scan ended, not when
    the viewer last rebuilt the page.
    """
    if not summary:
        return None
    raw = str(summary.get("finished_at") or summary.get("started_at") or "")
    moment = parse_timestamp(raw)
    if moment is None:
        return None
    return local_stamp(moment)


def shown_counts(groups: list[ReportGroup]) -> tuple[int, int]:
    """Applied and not-applied postings actually listed on the page."""
    jobs = [job for group in groups for job in group.jobs]
    applied = sum(1 for job in jobs if job.applied)
    return applied, len(jobs) - applied


def _schedule_view(
    config: AppConfig,
    summary: dict | None,
    interactive: bool,
    schedule_enabled: bool | None,
) -> dict | None:
    """Banner data for a viewer page that reloads after the daily check."""
    enabled = config.settings.schedule.enabled
    if schedule_enabled is not None:
        enabled = schedule_enabled
    if not interactive or not enabled:
        return None
    at = config.settings.schedule.at
    finished = ""
    if summary is not None:
        finished = str(summary.get("finished_at") or "")
    return {
        "enabled": True,
        "at": at,
        "label": next_check_label(datetime.now().astimezone(), at),
        "lastFinishedAt": finished or None,
    }


def write_reports(
    config: AppConfig,
    matches: list[ScoredJob],
    fetch_failures: list[str],
    *,
    interactive: bool = False,
    pending_scoring: int = 0,
    scoring_errors: list[str] | None = None,
    run: object | None = None,
    schedule_enabled: bool | None = None,
) -> tuple[Path, Path]:
    output_dir = config.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).astimezone()
    today = now.strftime("%Y-%m-%d")
    threshold = config.preferences.score_threshold
    summary = run_summary_view(run)
    if summary is not None:
        mark_new_since_run(matches, run)
    groups = build_groups(matches, threshold)
    shown_applied, shown_open = shown_counts(groups)
    watch = config.companies.watch_manually
    scoring_errors = scoring_errors or []

    template_dir = Path(__file__).parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("report.html.j2")

    html = template.render(
        groups=groups,
        watch_manually=watch,
        fetch_failures=fetch_failures,
        threshold=threshold,
        interactive=interactive,
        pending_scoring=pending_scoring,
        scoring_errors=scoring_errors,
        last_scan=last_scan_stamp(summary),
        run_summary=summary,
        schedule=_schedule_view(config, summary, interactive, schedule_enabled),
        shown_applied=shown_applied,
        shown_open=shown_open,
    )
    md = _render_markdown(
        groups,
        watch,
        fetch_failures,
        threshold,
        pending_scoring,
        scoring_errors,
        summary,
        shown_applied,
        shown_open,
    )

    html_dated = output_dir / f"report-{today}.html"
    md_dated = output_dir / f"report-{today}.md"
    html_latest = output_dir / "latest.html"
    md_latest = output_dir / "latest.md"

    html_dated.write_text(html, encoding="utf-8")
    md_dated.write_text(md, encoding="utf-8")
    html_latest.write_text(html, encoding="utf-8")
    md_latest.write_text(md, encoding="utf-8")

    return html_latest, md_latest


def _render_markdown(
    groups: list[ReportGroup],
    watch: list[WatchManuallyEntry],
    fetch_failures: list[str],
    threshold: int,
    pending_scoring: int = 0,
    scoring_errors: list[str] | None = None,
    run_summary: dict | None = None,
    shown_applied: int = 0,
    shown_open: int = 0,
) -> str:
    lines = [
        "# jobscout report",
        "",
        f"**Shown:** {shown_applied} applied, {shown_open} not applied",
        "",
    ]
    if run_summary:
        lines.append("## Run summary")
        lines.append("")
        for label, count in run_summary["rows"]:
            lines.append(f"- **{label}:** {count}")
        lines.append("")
    if fetch_failures:
        lines.append("## Fetch failures")
        for f in fetch_failures:
            lines.append(f"- {f}")
        lines.append("")
    if scoring_errors:
        lines.append("## Scoring failures")
        for e in scoring_errors:
            lines.append(f"- {e}")
        lines.append("")
    if pending_scoring and not groups:
        lines.append(
            f"Scoring did not finish: {pending_scoring} posting(s) still need "
            "an LLM score. The empty list is not a threshold miss."
        )
        lines.append("")

    for group in groups:
        lines.append(f"## {group.title}")
        lines.append("")
        for sj in group.jobs:
            v = sj.verdict
            lines.append(f"### [{v.score}] {sj.job.display_title} — {sj.job.company}")
            lines.append(f"- **Location:** {sj.job.location}")
            lines.append(f"- **URL:** {sj.job.url}")
            lines.append(f"- **Verdict:** {v.verdict.value}")
            if sj.applied:
                lines.append("- **Applied:** yes")
            lines.append(f"- **Why:** {v.why}")
            if v.matched_skills:
                lines.append(f"- **Skills:** {', '.join(v.matched_skills)}")
            lines.append("")

    if watch:
        lines.append("## Watch manually")
        for w in watch:
            lines.append(f"- [{w.name}]({w.url}) — {w.why}")
        lines.append("")

    return "\n".join(lines)
