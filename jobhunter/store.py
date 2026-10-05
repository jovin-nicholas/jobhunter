"""SQLite persistence and deduplication. Same `jobs` columns as job-notifier, plus resume_id and filter_reason."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from jobhunter.feedback import VERDICTS
from jobhunter.models import Job, ScoreResult

# Jobs in these statuses are finished and never processed again; anything else is retried on the next run.
TERMINAL = {"notified", "logged", "skipped", "filtered", "error_terminal", "stale", "gone", "duplicate"}
STATUS_FOR_DECISION = {"notify": "notified", "log": "logged", "skip": "skipped"}

COLUMNS = {
    "id": "TEXT PRIMARY KEY", "title": "TEXT", "company": "TEXT", "location": "TEXT", "url": "TEXT",
    "posted_at": "TEXT", "jd_text": "TEXT", "source": "TEXT", "ats_system": "TEXT", "match_score": "INTEGER",
    "resume_version": "TEXT", "match_reasoning": "TEXT", "matched_skills": "TEXT", "keyword_gaps": "TEXT",
    "seniority_fit": "TEXT", "role_type": "TEXT", "ats_tip": "TEXT", "status": "TEXT DEFAULT 'pending'",
    "run_at": "TEXT", "source_model": "TEXT", "jd_is_snippet": "INTEGER", "resume_id": "TEXT",
    "filter_reason": "TEXT", "first_run_at": "TEXT", "fit_probability": "REAL",
}
_SAVED = [c for c in COLUMNS if c not in ("resume_version", "seniority_fit", "ats_tip")]
_CHUNK = 900   # SQLite allows at most 999 bound parameters per statement


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(f"CREATE TABLE IF NOT EXISTS jobs ({', '.join(f'{c} {t}' for c, t in COLUMNS.items())})")
            existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
            for column, sql_type in COLUMNS.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {sql_type.replace(' PRIMARY KEY', '')}")
            conn.execute("CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                         "job_id TEXT NOT NULL, verdict TEXT NOT NULL, received_at TEXT NOT NULL, "
                         "message_id TEXT NOT NULL UNIQUE)")

    @contextmanager
    def _connect(self):
        # sqlite3's own `with` only commits; this also closes the connection.
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            with conn:
                yield conn
        finally:
            conn.close()

    def _statuses(self, ids: list[str]) -> dict[str, str]:
        status: dict[str, str] = {}
        with self._connect() as conn:
            for i in range(0, len(ids), _CHUNK):
                chunk = ids[i:i + _CHUNK]
                rows = conn.execute(f"SELECT id, status FROM jobs WHERE id IN ({','.join('?' * len(chunk))})", chunk)
                status.update({row[0]: row[1] or "" for row in rows})
        return status

    def filter_unseen(self, jobs: list[Job]) -> list[Job]:
        """Jobs never saved, or saved with a status that is retried (rate limits, errors)."""
        status = self._statuses([j.id for j in jobs])
        return [j for j in jobs if status.get(j.id) not in TERMINAL]

    def existing_ids(self, ids: list[str]) -> set[str]:
        return set(self._statuses(ids))

    def insert_missing(self, rows: list[dict]) -> int:
        """Insert rows whose id is not stored yet; stored rows are left as they are. Returns how many were added."""
        if not rows:
            return 0
        columns = [c for c in COLUMNS if c in rows[0]]
        sql = f"INSERT OR IGNORE INTO jobs ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"
        with self._connect() as conn:
            before = conn.total_changes
            conn.executemany(sql, [tuple(r.get(c) for c in columns) for r in rows])
            return conn.total_changes - before

    def save(self, job: Job, status: str, result: ScoreResult | None = None, *,
             resume_id: str | None = None, filter_reason: str | None = None) -> None:
        values = {
            "id": job.id, "title": job.title, "company": job.company, "location": job.location, "url": job.url,
            "posted_at": job.posted_at, "jd_text": job.description, "source": job.source, "ats_system": job.ats,
            "match_score": result.score if result else None,
            "match_reasoning": result.reasoning if result else None,
            "matched_skills": json.dumps(result.matched_skills) if result else None,
            "keyword_gaps": json.dumps(result.keyword_gaps) if result else None,
            "role_type": result.role_type if result else None,
            "status": status, "run_at": datetime.now(timezone.utc).isoformat(),
            "source_model": result.model if result else None,
            "jd_is_snippet": None if job.description_is_snippet is None else int(job.description_is_snippet),
            "resume_id": resume_id, "filter_reason": filter_reason,
            "first_run_at": datetime.now(timezone.utc).isoformat(),
            "fit_probability": result.probability if result else None,
        }
        # first_run_at keeps the time a job was first saved, so retries of a refused job are bounded in time.
        updates = ", ".join(f"{c} = excluded.{c}" for c in _SAVED if c not in ("id", "first_run_at"))
        with self._connect() as conn:
            conn.execute(f"INSERT INTO jobs ({', '.join(_SAVED)}) VALUES ({', '.join(':' + c for c in _SAVED)}) "
                         f"ON CONFLICT(id) DO UPDATE SET {updates}", values)

    def _remember(self, job_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO jobs (id, status, run_at) VALUES (?, ?, ?)",
                         (job_id, status, datetime.now(timezone.utc).isoformat()))

    def mark_stale(self, job_id: str) -> None:
        """Remember a posting older than max_age_hours; an existing row is left as it is."""
        self._remember(job_id, "stale")

    def mark_gone(self, job_id: str) -> None:
        """Remember a posting that no longer exists (404/410 or an empty page); an existing row is left as it is."""
        self._remember(job_id, "gone")

    def adopt_final(self, rows: list[dict]) -> int:
        """For stored jobs that are not finished, take the given row's status (and details) when that status is
        final. Returns how many rows changed."""
        rows = [r for r in rows if (r.get("status") or "") in TERMINAL]
        if not rows:
            return 0
        columns = [c for c in COLUMNS if c in rows[0] and c != "id"]
        final = ", ".join(f"'{s}'" for s in sorted(TERMINAL))
        sql = (f"UPDATE jobs SET {', '.join(f'{c} = ?' for c in columns)} "
               f"WHERE id = ? AND COALESCE(status, '') NOT IN ({final})")
        with self._connect() as conn:
            before = conn.total_changes
            conn.executemany(sql, [tuple(r.get(c) for c in columns) + (r["id"],) for r in rows])
            return conn.total_changes - before

    def add_feedback(self, rows: list[dict]) -> list[dict]:
        """Save feedback verdicts; a message already saved (same message_id) is ignored. Returns the new rows."""
        new = []
        with self._connect() as conn:
            for r in rows:
                before = conn.total_changes
                conn.execute("INSERT OR IGNORE INTO feedback (job_id, verdict, received_at, message_id) "
                             "VALUES (?, ?, ?, ?)", (r["job_id"], r["verdict"], r["received_at"], r["message_id"]))
                if conn.total_changes > before:
                    new.append(r)
        return new

    def latest_feedback(self, since: str | None = None) -> list[dict]:
        """Each job's latest verdict with its title and company, oldest first; `since` is a date or ISO time."""
        sql = ("SELECT f.job_id, j.title, j.company, f.verdict, f.received_at FROM feedback f "
               "LEFT JOIN jobs j ON j.id = f.job_id WHERE f.id = (SELECT g.id FROM feedback g WHERE g.job_id = "
               "f.job_id ORDER BY g.received_at DESC, g.id DESC LIMIT 1)")
        params: list = []
        if since:
            sql += " AND f.received_at >= ?"
            params.append(since)
        with self._connect() as conn:
            rows = conn.execute(sql + " ORDER BY f.received_at, f.id", params).fetchall()
        return [{"job_id": i, "job_title": t or "", "company": c or "", "verdict": v,
                 "decision": VERDICTS.get(v, ""), "received_at": at} for i, t, c, v, at in rows]

    def last_notified(self) -> tuple[Job, ScoreResult, str] | None:
        """The most recently notified job with its saved score, for send-test-alert."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, title, company, location, url, jd_text, posted_at, source, ats_system, match_score, "
                "source_model, match_reasoning, matched_skills, keyword_gaps, role_type, fit_probability, resume_id "
                "FROM jobs WHERE status = 'notified' ORDER BY run_at DESC LIMIT 1").fetchone()
        if not row:
            return None
        (i, t, c, loc, url, jd, posted, src, ats, score, model, why, skills, gaps, role, prob, resume_id) = row
        job = Job(i, t or "", c or "", loc or "", url or "", description=jd or "", posted_at=posted or "",
                  source=src or "", ats=ats or "")
        result = ScoreResult(score=score, model=model or "", decision="notify", reasoning=why or "",
                             matched_skills=json.loads(skills or "[]"), keyword_gaps=json.loads(gaps or "[]"),
                             role_type=role, probability=prob)
        return job, result, resume_id or ""

    def recently_notified(self, within_days: int = 30) -> list[tuple[str, str]]:
        """(title, company) of every job notified in the last `within_days`."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat()
        with self._connect() as conn:
            return [(r[0] or "", r[1] or "") for r in conn.execute(
                "SELECT title, company FROM jobs WHERE status = 'notified' AND run_at >= ?", (cutoff,))]

    def retry_candidates(self, within_hours: int = 24) -> list[Job]:
        """Jobs first saved in the last `within_hours` whose description was unavailable or whose alert no channel
        delivered: tried again even when their board no longer lists them (LinkedIn lists a job for about an hour)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=within_hours)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, title, company, location, url, jd_text, posted_at, source, ats_system, jd_is_snippet "
                "FROM jobs WHERE status IN ('error_unavailable', 'error_notify') AND first_run_at >= ? "
                "ORDER BY first_run_at",
                (cutoff,)).fetchall()
        return [Job(id=r[0], title=r[1] or "", company=r[2] or "", location=r[3] or "", url=r[4] or "",
                    description=r[5] or "", posted_at=r[6] or "", source=r[7] or "", ats=r[8] or "",
                    description_is_snippet=None if r[9] is None else bool(r[9])) for r in rows]

    def is_terminal(self, job_id: str) -> bool:
        """True when the job is already finished, so a board can skip fetching its details again."""
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return bool(row) and (row[0] or "") in TERMINAL

    def status_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            return dict(conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status ORDER BY status"))
