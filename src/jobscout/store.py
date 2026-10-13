"""SQLite storage layer for jobscout."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from jobscout.config import CompanyConfig
from jobscout.models import Job, ScoredJob, Verdict, VerdictLabel
from jobscout.sources.workday import public_workday_job_url, workday_url_needs_repair

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_key         TEXT PRIMARY KEY,
    company         TEXT NOT NULL,
    source          TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    title           TEXT NOT NULL,
    url             TEXT NOT NULL,
    location        TEXT NOT NULL DEFAULT '',
    department      TEXT NOT NULL DEFAULT '',
    employment_type TEXT NOT NULL DEFAULT '',
    description     TEXT NOT NULL DEFAULT '',
    posted_at       TEXT NOT NULL DEFAULT '',
    content_hash    TEXT NOT NULL,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    missing_runs    INTEGER NOT NULL DEFAULT 0,
    scored_hash     TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs (company, status);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs (first_seen);
CREATE TABLE IF NOT EXISTS verdicts (
    job_key        TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    score          INTEGER NOT NULL,
    verdict        TEXT NOT NULL,
    why            TEXT NOT NULL DEFAULT '',
    seniority_fit  TEXT NOT NULL DEFAULT '',
    location_fit   TEXT NOT NULL DEFAULT '',
    matched_skills TEXT NOT NULL DEFAULT '[]',
    gaps           TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL,
    PRIMARY KEY (job_key, content_hash, model, prompt_version)
);
CREATE TABLE IF NOT EXISTS ignored (
    job_key    TEXT PRIMARY KEY,
    ignored_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applied (
    job_key    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    fetched     INTEGER NOT NULL DEFAULT 0,
    new         INTEGER NOT NULL DEFAULT 0,
    changed     INTEGER NOT NULL DEFAULT 0,
    closed      INTEGER NOT NULL DEFAULT 0,
    scored      INTEGER NOT NULL DEFAULT 0,
    llm_calls   INTEGER NOT NULL DEFAULT 0,
    removed     INTEGER NOT NULL DEFAULT 0,
    llm_errors  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS linkedin_cursors (
    company    TEXT PRIMARY KEY,
    next_start INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_external ON jobs (company, source, external_id);
"""

MISSING_RUNS_BEFORE_CLOSED = 2


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Store:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.executescript(SCHEMA)
            self._ensure_run_columns(conn)
            conn.execute("PRAGMA journal_mode=WAL")

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    @staticmethod
    def _ensure_run_columns(conn: sqlite3.Connection) -> None:
        """Add summary columns that older databases were created without."""
        present = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
        for name in ("removed", "llm_errors"):
            if name not in present:
                conn.execute(
                    f"ALTER TABLE runs ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
                )

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            del self._local.conn

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        return Job(
            company=row["company"],
            source=row["source"],
            external_id=row["external_id"],
            title=row["title"],
            url=row["url"],
            location=row["location"],
            department=row["department"],
            employment_type=row["employment_type"],
            description=row["description"],
            posted_at=row["posted_at"],
        )

    def find_job(self, company: str, source: str, external_id: str) -> Job | None:
        row = self._conn.execute(
            """
            SELECT * FROM jobs
            WHERE company = ? AND source = ? AND external_id = ?
            """,
            (company, source, external_id),
        ).fetchone()
        return self._row_to_job(row) if row else None

    def linkedin_cursor(self, company: str) -> int:
        row = self._conn.execute(
            "SELECT next_start FROM linkedin_cursors WHERE company = ?",
            (company,),
        ).fetchone()
        return int(row["next_start"]) if row else 0

    def set_linkedin_cursor(self, company: str, next_start: int) -> None:
        self._conn.execute(
            """
            INSERT INTO linkedin_cursors (company, next_start) VALUES (?, ?)
            ON CONFLICT(company) DO UPDATE SET next_start = excluded.next_start
            """,
            (company, next_start),
        )

    def clear_linkedin_cursor(self, company: str) -> None:
        self._conn.execute(
            "DELETE FROM linkedin_cursors WHERE company = ?",
            (company,),
        )

    def upsert_job(self, job: Job) -> str:
        now = utc_now()
        content_hash = job.content_hash
        existing = self._conn.execute(
            "SELECT content_hash FROM jobs WHERE job_key = ?",
            (job.job_key,),
        ).fetchone()

        if existing is None:
            self._conn.execute(
                """
                INSERT INTO jobs (
                    job_key, company, source, external_id, title, url,
                    location, department, employment_type, description, posted_at,
                    content_hash, first_seen, last_seen, status, missing_runs
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 0)
                """,
                (
                    job.job_key,
                    job.company,
                    job.source,
                    job.external_id,
                    job.title,
                    job.url,
                    job.location,
                    job.department,
                    job.employment_type,
                    job.description,
                    job.posted_at,
                    content_hash,
                    now,
                    now,
                ),
            )
            return "new"

        changed = existing["content_hash"] != content_hash
        self._conn.execute(
            """
            UPDATE jobs SET
                company = ?, source = ?, external_id = ?, title = ?, url = ?,
                location = ?, department = ?, employment_type = ?, description = ?,
                posted_at = ?, content_hash = ?, last_seen = ?,
                status = 'open', missing_runs = 0
            WHERE job_key = ?
            """,
            (
                job.company,
                job.source,
                job.external_id,
                job.title,
                job.url,
                job.location,
                job.department,
                job.employment_type,
                job.description,
                job.posted_at,
                content_hash,
                now,
                job.job_key,
            ),
        )
        return "changed" if changed else "unchanged"

    def repair_workday_urls(self, companies: list[CompanyConfig]) -> int:
        """Fix stored Workday URLs that omit the career site segment."""
        workday = {
            c.name: c
            for c in companies
            if c.source == "workday" and c.host and c.site
        }
        if not workday:
            return 0

        rows = self._conn.execute(
            "SELECT job_key, company, external_id, url FROM jobs WHERE source = 'workday'"
        ).fetchall()
        fixed = 0
        for row in rows:
            cfg = workday.get(row["company"])
            if cfg is None:
                continue
            host = cfg.host or ""
            site = cfg.site or ""
            if not workday_url_needs_repair(row["url"], host, site, row["external_id"]):
                continue
            new_url = public_workday_job_url(host, site, row["external_id"])
            self._conn.execute(
                "UPDATE jobs SET url = ? WHERE job_key = ?",
                (new_url, row["job_key"]),
            )
            fixed += 1
        return fixed

    def delete_job(self, job_key: str) -> None:
        """Remove a posting, its verdicts, and any ignore or applied mark."""
        self._conn.execute("DELETE FROM verdicts WHERE job_key = ?", (job_key,))
        self._conn.execute("DELETE FROM ignored WHERE job_key = ?", (job_key,))
        self._conn.execute("DELETE FROM applied WHERE job_key = ?", (job_key,))
        self._conn.execute("DELETE FROM jobs WHERE job_key = ?", (job_key,))

    def mark_missing(
        self, company: str, seen_keys: set[str], source: str | None = None
    ) -> int:
        """Drop postings that a successful fetch no longer lists.

        One miss only increments ``missing_runs``. The next miss deletes the
        job, its verdicts, and its ignore and applied rows. Jobs already marked closed
        are deleted on the next successful fetch that does not list them.
        """
        if source:
            rows = self._conn.execute(
                """
                SELECT job_key, status, missing_runs
                FROM jobs WHERE company = ? AND source = ?
                """,
                (company, source),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT job_key, status, missing_runs
                FROM jobs WHERE company = ?
                """,
                (company,),
            ).fetchall()
        removed = 0
        for row in rows:
            key = row["job_key"]
            if key in seen_keys:
                continue
            if (
                row["status"] == "closed"
                or row["missing_runs"] + 1 >= MISSING_RUNS_BEFORE_CLOSED
            ):
                self.delete_job(key)
                removed += 1
                continue
            self._conn.execute(
                "UPDATE jobs SET missing_runs = missing_runs + 1 WHERE job_key = ?",
                (key,),
            )
        return removed

    def is_ignored(self, job_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM ignored WHERE job_key = ?",
            (job_key,),
        ).fetchone()
        return row is not None

    def is_applied(self, job_key: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM applied WHERE job_key = ?",
            (job_key,),
        ).fetchone()
        return row is not None

    def needs_scoring(self, job: Job, prompt_version: str | None = None) -> bool:
        if self.is_ignored(job.job_key) or self.is_applied(job.job_key):
            return False

        row = self._conn.execute(
            "SELECT scored_hash, content_hash FROM jobs WHERE job_key = ?",
            (job.job_key,),
        ).fetchone()

        if row is None:
            return True

        content_hash = job.content_hash
        if row["scored_hash"] != content_hash:
            return True

        if prompt_version is None:
            return False

        verdicts = self._conn.execute(
            """
            SELECT prompt_version FROM verdicts
            WHERE job_key = ? AND content_hash = ?
            """,
            (job.job_key, content_hash),
        ).fetchall()

        if not verdicts:
            return False

        for v in verdicts:
            if v["prompt_version"] == prompt_version:
                return False
        return True

    def mark_scored(self, job: Job) -> None:
        self._conn.execute(
            "UPDATE jobs SET scored_hash = ? WHERE job_key = ?",
            (job.content_hash, job.job_key),
        )

    def save_verdict(self, job: Job, verdict: Verdict) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO verdicts (
                job_key, content_hash, model, prompt_version,
                score, verdict, why, seniority_fit, location_fit,
                matched_skills, gaps, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.job_key,
                job.content_hash,
                verdict.model,
                verdict.prompt_version,
                verdict.score,
                verdict.verdict.value,
                verdict.why,
                verdict.seniority_fit,
                verdict.location_fit,
                json.dumps(verdict.matched_skills),
                json.dumps(verdict.gaps),
                utc_now(),
            ),
        )

    def latest_verdict(
        self,
        job_key: str,
        content_hash: str | None = None,
        prompt_version: str | None = None,
    ) -> Verdict | None:
        query = """
            SELECT * FROM verdicts
            WHERE job_key = ?
        """
        params: list[str] = [job_key]
        if content_hash is not None:
            query += " AND content_hash = ?"
            params.append(content_hash)
        if prompt_version is not None:
            query += " AND prompt_version = ?"
            params.append(prompt_version)
        query += " ORDER BY created_at DESC LIMIT 1"
        row = self._conn.execute(query, params).fetchone()
        if row is None:
            return None
        return self._row_to_verdict(row)

    def _row_to_verdict(self, row: sqlite3.Row) -> Verdict:
        return Verdict(
            score=row["score"],
            verdict=VerdictLabel(row["verdict"]),
            why=row["why"],
            seniority_fit=row["seniority_fit"],
            location_fit=row["location_fit"],
            matched_skills=json.loads(row["matched_skills"]),
            gaps=json.loads(row["gaps"]),
            model=row["model"],
            prompt_version=row["prompt_version"],
        )

    def scored_jobs(
        self,
        *,
        min_score: int = 0,
        include_closed: bool = False,
        include_ignored: bool = False,
        only_applied: bool = False,
        since: str | None = None,
        limit: int | None = None,
        company: str | None = None,
    ) -> list[ScoredJob]:
        status_clause = "" if include_closed else "AND j.status = 'open'"
        ignored_clause = "" if include_ignored else "AND ig.job_key IS NULL"
        applied_clause = "AND ap.job_key IS NOT NULL" if only_applied else ""
        since_clause = "AND j.first_seen >= ?" if since else ""
        company_clause = "AND j.company = ?" if company else ""

        params: list[str | int] = []
        if since:
            params.append(since)
        if company:
            params.append(company)

        query = f"""
            WITH ranked AS (
                SELECT v.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY v.job_key ORDER BY v.created_at DESC
                    ) AS rn
                FROM verdicts v
            )
            SELECT j.*, r.score, r.verdict, r.why, r.seniority_fit, r.location_fit,
                   r.matched_skills, r.gaps, r.model, r.prompt_version,
                   (ap.job_key IS NOT NULL) AS is_applied
            FROM jobs j
            JOIN ranked r ON j.job_key = r.job_key AND r.rn = 1
            LEFT JOIN ignored ig ON j.job_key = ig.job_key
            LEFT JOIN applied ap ON j.job_key = ap.job_key
            WHERE r.score >= ?
            {status_clause}
            {ignored_clause}
            {applied_clause}
            {since_clause}
            {company_clause}
            ORDER BY r.score DESC, j.first_seen DESC
        """
        params = [min_score, *params]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        rows = self._conn.execute(query, params).fetchall()
        result: list[ScoredJob] = []
        for row in rows:
            job = self._row_to_job(row)
            verdict = Verdict(
                score=row["score"],
                verdict=VerdictLabel(row["verdict"]),
                why=row["why"],
                seniority_fit=row["seniority_fit"],
                location_fit=row["location_fit"],
                matched_skills=json.loads(row["matched_skills"]),
                gaps=json.loads(row["gaps"]),
                model=row["model"],
                prompt_version=row["prompt_version"],
            )
            result.append(
                ScoredJob(
                    job=job,
                    verdict=verdict,
                    first_seen=row["first_seen"],
                    is_new=False,
                    applied=bool(row["is_applied"]),
                )
            )
        return result

    def get_job(self, job_key: str) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE job_key = ?", (job_key,)).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def open_jobs_for_company(self, company: str) -> list[Job]:
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE company = ? AND status = 'open'",
            (company,),
        ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def ignore(self, keys: list[str]) -> int:
        now = utc_now()
        changed = 0
        for key in keys:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO ignored (job_key, ignored_at) VALUES (?, ?)",
                (key, now),
            )
            self._conn.execute("DELETE FROM applied WHERE job_key = ?", (key,))
            changed += cur.rowcount
        return changed

    def unignore(self, keys: list[str]) -> int:
        changed = 0
        for key in keys:
            cur = self._conn.execute("DELETE FROM ignored WHERE job_key = ?", (key,))
            changed += cur.rowcount
        return changed

    def list_ignored(self) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """
                SELECT i.job_key, i.ignored_at, j.company, j.title
                FROM ignored i
                LEFT JOIN jobs j ON j.job_key = i.job_key
                ORDER BY i.ignored_at DESC
                """
            ).fetchall()
        )

    def clear_ignored(self) -> int:
        cur = self._conn.execute("DELETE FROM ignored")
        return cur.rowcount

    def mark_applied(self, keys: list[str]) -> int:
        now = utc_now()
        changed = 0
        for key in keys:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO applied (job_key, applied_at) VALUES (?, ?)",
                (key, now),
            )
            self._conn.execute("DELETE FROM ignored WHERE job_key = ?", (key,))
            changed += cur.rowcount
        return changed

    def unapply(self, keys: list[str]) -> int:
        changed = 0
        for key in keys:
            cur = self._conn.execute("DELETE FROM applied WHERE job_key = ?", (key,))
            changed += cur.rowcount
        return changed

    def list_applied(self) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """
                SELECT a.job_key, a.applied_at, j.company, j.title
                FROM applied a
                LEFT JOIN jobs j ON j.job_key = a.job_key
                ORDER BY a.applied_at DESC
                """
            ).fetchall()
        )

    def clear_applied(self) -> int:
        cur = self._conn.execute("DELETE FROM applied")
        return cur.rowcount

    def resolve_job_keys(self, needles: list[str]) -> tuple[list[str], list[str]]:
        found: list[str] = []
        unmatched: list[str] = []
        all_keys = {
            r["job_key"]: r["url"]
            for r in self._conn.execute("SELECT job_key, url FROM jobs").fetchall()
        }

        for needle in needles:
            matched = False
            if needle in all_keys:
                found.append(needle)
                matched = True
            else:
                for key, url in all_keys.items():
                    if key.startswith(needle) or needle == url or needle in url:
                        found.append(key)
                        matched = True
                        break
            if not matched:
                unmatched.append(needle)

        return list(dict.fromkeys(found)), unmatched

    def save_run(
        self,
        *,
        started_at: str,
        finished_at: str,
        fetched: int,
        new: int,
        changed: int,
        closed: int,
        scored: int,
        llm_calls: int,
        removed: int = 0,
        llm_errors: int = 0,
    ) -> int:
        cur = self._conn.execute(
            """
            INSERT INTO runs (
                started_at, finished_at, fetched, new, changed, closed, scored, llm_calls,
                removed, llm_errors
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_at,
                finished_at,
                fetched,
                new,
                changed,
                closed,
                scored,
                llm_calls,
                removed,
                llm_errors,
            ),
        )
        return cur.lastrowid or 0

    def latest_run(self) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def status_counts(self) -> dict[str, int]:
        row = self._conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS open,
                SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS closed
            FROM jobs
            """
        ).fetchone()
        ignored = self._conn.execute("SELECT COUNT(*) AS c FROM ignored").fetchone()
        applied = self._conn.execute("SELECT COUNT(*) AS c FROM applied").fetchone()
        pending = self._conn.execute(
            """
            SELECT COUNT(*) AS c FROM jobs j
            LEFT JOIN ignored ig ON j.job_key = ig.job_key
            LEFT JOIN applied ap ON j.job_key = ap.job_key
            WHERE j.status = 'open'
              AND ig.job_key IS NULL
              AND ap.job_key IS NULL
              AND j.scored_hash IS NULL
            """
        ).fetchone()
        return {
            "total": row["total"] if row else 0,
            "open": row["open"] if row else 0,
            "closed": row["closed"] if row else 0,
            "ignored": ignored["c"] if ignored else 0,
            "applied": applied["c"] if applied else 0,
            "pending_scoring": pending["c"] if pending else 0,
        }

    def companies_in_store(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT company FROM jobs ORDER BY company"
        ).fetchall()
        return [row["company"] for row in rows]

    def delete_company_jobs(self, company: str) -> int:
        """Delete jobs, verdicts, and ignore or applied rows for one company name."""
        count_row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE company = ?",
            (company,),
        ).fetchone()
        n = int(count_row["c"]) if count_row else 0
        if n == 0:
            return 0
        self._conn.execute(
            """
            DELETE FROM verdicts
            WHERE job_key IN (SELECT job_key FROM jobs WHERE company = ?)
            """,
            (company,),
        )
        self._conn.execute(
            """
            DELETE FROM ignored
            WHERE job_key IN (SELECT job_key FROM jobs WHERE company = ?)
            """,
            (company,),
        )
        self._conn.execute(
            """
            DELETE FROM applied
            WHERE job_key IN (SELECT job_key FROM jobs WHERE company = ?)
            """,
            (company,),
        )
        self._conn.execute("DELETE FROM jobs WHERE company = ?", (company,))
        return n

    def delete_unlisted_companies(self, keep_names: set[str]) -> int:
        keep = {name.lower() for name in keep_names}
        dropped = 0
        for name in self.companies_in_store():
            if name.lower() not in keep:
                dropped += self.delete_company_jobs(name)
        return dropped
