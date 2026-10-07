"""Applications and their events, in jobs.db next to the jobs they came from. Every write goes through Store's
connection (WAL, 30 s busy timeout), so a scheduled run writing at the same moment waits instead of failing."""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from jobhunter.feedback import VERDICTS
from jobhunter.store import Store
from jobhunter.tracker import EVENT_KINDS, OPEN_STAGES, STAGES

_TABLES = (
    "CREATE TABLE IF NOT EXISTS applications (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, "
    "title TEXT NOT NULL, company TEXT NOT NULL, url TEXT, board TEXT, resume_id TEXT, fit TEXT, fit_score REAL, "
    "applied_at TEXT NOT NULL, stage TEXT NOT NULL, stage_at TEXT NOT NULL, follow_up_at TEXT, people TEXT, "
    "notes TEXT, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS application_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "application_id INTEGER NOT NULL, kind TEXT NOT NULL, value TEXT, at TEXT NOT NULL, created_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS application_events_by_app ON application_events (application_id)",
    "CREATE TABLE IF NOT EXISTS review_hidden (job_id TEXT PRIMARY KEY, hidden_at TEXT NOT NULL)",
)
_COLUMNS = ("id", "job_id", "title", "company", "url", "board", "resume_id", "fit", "fit_score", "applied_at", "stage",
            "stage_at", "follow_up_at", "people", "notes", "created_at")
_EDITABLE = ("title", "company", "url", "applied_at", "people", "notes")
_WEB = re.compile(r"^https?://", re.I)
EARLIEST = date(2000, 1, 1)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_date(value: Any) -> str:
    """`value` as YYYY-MM-DD (a longer ISO time keeps its date); ValueError for anything else."""
    if not isinstance(value, str):
        raise ValueError(f"not a date like 2026-10-05: {value!r}")
    try:
        return date.fromisoformat(value.strip()[:10]).isoformat()
    except ValueError:
        raise ValueError(f"not a date like 2026-10-05: {value!r}") from None


def _years_from(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:                                  # 29 February
        return day.replace(year=day.year + years, day=28)


def _within(value: Any, earliest: date, latest: date) -> str:
    """`value` as YYYY-MM-DD when it falls from `earliest` to `latest`; ValueError otherwise, so a typo like year 0002
    never reaches the timeline."""
    day = iso_date(value)
    if not earliest.isoformat() <= day <= latest.isoformat():
        raise ValueError(f"{day} is out of range ({earliest.isoformat()} to {latest.isoformat()})")
    return day


def event_date(value: Any, today: date, stage: str | None = None) -> str:
    """An applied date or a stage date: from 2000 to tomorrow (a time zone ahead). A screen or interview may be
    scheduled ahead, up to a year (its follow-up, a few days later, stays inside the follow-up range), so it can show
    as upcoming."""
    ahead = _years_from(today, 1) if stage in ("screen", "interview") else today + timedelta(days=1)
    return _within(value, EARLIEST, ahead)


def follow_up_date(value: Any, today: date) -> str:
    return _within(value, _years_from(today, -1), _years_from(today, 2))


def fit_text(score: Any) -> str:
    """The fit as the tracker shows it, one way for every scorer: "6.85", or "" without a score."""
    return f"{float(score):.2f}" if isinstance(score, (int, float)) else ""


def safe_url(url: str) -> str:
    """The link when it is http(s), else "": a javascript: or data: link must never reach the page."""
    url = (url or "").strip()
    return url if _WEB.match(url) else ""


def url_key(url: str) -> str:
    """How pasted links are matched: lower-case scheme and host, no query, fragment or trailing slash."""
    parts = urlsplit((url or "").strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def _refresh(conn: Any, app_id: int) -> None:
    """Rewrite the cached stage and follow-up from the events, so they always match the timeline. The stage is the
    last one set (by id), not the latest dated: editing a date, or dating a stage ahead, never undoes a later move."""
    applied_at = conn.execute("SELECT applied_at FROM applications WHERE id = ?", (app_id,)).fetchone()[0]
    stage = conn.execute("SELECT value, at FROM application_events WHERE application_id = ? AND kind = 'stage' "
                         "ORDER BY id DESC LIMIT 1", (app_id,)).fetchone() or ("applied", applied_at)
    follow = conn.execute("SELECT kind, value FROM application_events WHERE application_id = ? AND kind IN "
                          "('follow_up_set', 'follow_up_done') ORDER BY id DESC LIMIT 1", (app_id,)).fetchone()
    follow_up = follow[1] if follow and follow[0] == "follow_up_set" else None
    conn.execute("UPDATE applications SET stage = ?, stage_at = ?, follow_up_at = ? WHERE id = ?",
                 (stage[0], stage[1], follow_up, app_id))


def _insert_event(conn: Any, app_id: int, kind: str, value: str | None, at: str) -> int:
    return conn.execute("INSERT INTO application_events (application_id, kind, value, at, created_at) "
                        "VALUES (?, ?, ?, ?, ?)", (app_id, kind, value, at, now_iso())).lastrowid


class Applications:
    def __init__(self, store: Store, today: Callable[[], date] = date.today):
        self.store, self.today = store, today          # `today` bounds the dates; tests pin it
        with store.connect() as conn:
            for sql in _TABLES:
                conn.execute(sql)

    def add(self, *, title: str, company: str, applied_at: str, url: str = "", job_id: str | None = None,
            board: str = "", resume_id: str = "", fit: str = "", fit_score: float | None = None,
            stage: str = "applied") -> int:
        """A new application with its "applied" event, plus a second stage event when it starts further along."""
        applied_at = event_date(applied_at, self.today())
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r} (one of {', '.join(STAGES)})")
        with self.store.connect() as conn:
            app_id = conn.execute(
                "INSERT INTO applications (job_id, title, company, url, board, resume_id, fit, fit_score, applied_at, "
                "stage, stage_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?)",
                (job_id, title.strip(), company.strip(), safe_url(url), board, resume_id, fit, fit_score, applied_at,
                 applied_at, now_iso())).lastrowid
            _insert_event(conn, app_id, "stage", "applied", applied_at)
            if stage != "applied":
                _insert_event(conn, app_id, "stage", stage, applied_at)
            _refresh(conn, app_id)
        return app_id

    def job_fields(self, job_id: str) -> dict | None:
        """What an application copies from a stored job; None when the job is not in jobs.db."""
        with self.store.connect() as conn:
            row = conn.execute("SELECT title, company, url, source, resume_id, score_label, match_score FROM jobs "
                               "WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        title, company, url, board, resume_id, label, score = row
        return {"job_id": job_id, "title": title or "", "company": company or "", "url": url or "",
                "board": board or "", "resume_id": resume_id or "",
                "fit": fit_text(score), "fit_score": score}

    def add_from_job(self, job_id: str, applied_at: str) -> int | None:
        fields = self.job_fields(job_id)
        return None if fields is None else self.add(**fields, applied_at=applied_at)

    def update(self, app_id: int, **fields: Any) -> None:
        unknown = set(fields) - set(_EDITABLE)
        if unknown:
            raise ValueError(f"not editable: {', '.join(sorted(unknown))}")
        values = {k: str(v if v is not None else "").strip() for k, v in fields.items()}
        if "company" in values and not values["company"]:
            raise ValueError("the company cannot be empty")
        if "applied_at" in values:
            values["applied_at"] = event_date(values["applied_at"], self.today())
        if "url" in values:
            values["url"] = safe_url(values["url"])
        if not values:
            return
        with self.store.connect() as conn:
            if conn.execute("SELECT 1 FROM applications WHERE id = ?", (app_id,)).fetchone() is None:
                raise KeyError(app_id)
            conn.execute(f"UPDATE applications SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?",
                         (*values.values(), app_id))
            if "applied_at" in values:      # the timeline's first event is the application itself
                conn.execute("UPDATE application_events SET at = ? WHERE id = (SELECT MIN(id) FROM application_events "
                             "WHERE application_id = ? AND kind = 'stage' AND value = 'applied')",
                             (values["applied_at"], app_id))
            _refresh(conn, app_id)

    def add_event(self, app_id: int, kind: str, value: str | None, at: str) -> int:
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event {kind!r} (one of {', '.join(EVENT_KINDS)})")
        if kind == "stage" and value not in STAGES:
            raise ValueError(f"unknown stage {value!r} (one of {', '.join(STAGES)})")
        if kind == "follow_up_set":
            value = follow_up_date(value, self.today())
        if kind == "follow_up_done":
            value = None
        at = event_date(at, self.today(), value) if kind == "stage" else iso_date(at)
        with self.store.connect() as conn:
            if conn.execute("SELECT 1 FROM applications WHERE id = ?", (app_id,)).fetchone() is None:
                raise KeyError(app_id)
            event_id = _insert_event(conn, app_id, kind, value, at)
            _refresh(conn, app_id)
        return event_id

    def delete_events(self, event_ids: list[int]) -> None:
        """Undo: the events go, and each application's cached stage and follow-up follow the remaining ones."""
        with self.store.connect() as conn:
            for event_id in event_ids:
                row = conn.execute("SELECT application_id FROM application_events WHERE id = ?", (event_id,)).fetchone()
                if row:
                    conn.execute("DELETE FROM application_events WHERE id = ?", (event_id,))
                    _refresh(conn, row[0])

    def _rows(self, where: str = "", params: tuple = ()) -> list[dict]:
        with self.store.connect() as conn:
            apps = [dict(zip(_COLUMNS, r)) for r in conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM applications {where} ORDER BY id", params)]
            ids = [a["id"] for a in apps]
            events: dict[int, list[dict]] = {i: [] for i in ids}
            if ids:
                for r in conn.execute(f"SELECT id, application_id, kind, value, at, created_at FROM application_events "
                                      f"WHERE application_id IN ({','.join('?' * len(ids))}) ORDER BY at, id", ids):
                    events[r[1]].append({"id": r[0], "kind": r[2], "value": r[3], "at": r[4], "created_at": r[5]})
        for a in apps:
            a["events"] = events[a["id"]]
            if a["fit_score"] is not None:
                a["fit"] = fit_text(a["fit_score"])       # rows saved with a scorer's own label show the same way
        return apps

    def get(self, app_id: int) -> dict | None:
        rows = self._rows("WHERE id = ?", (app_id,))
        return rows[0] if rows else None

    def all(self) -> list[dict]:
        return self._rows()

    def open_for_job(self, job_id: str) -> int | None:
        with self.store.connect() as conn:
            row = conn.execute(f"SELECT id FROM applications WHERE job_id = ? AND stage IN "
                               f"({','.join('?' * len(OPEN_STAGES))}) ORDER BY id DESC LIMIT 1",
                               (job_id, *OPEN_STAGES)).fetchone()
        return row[0] if row else None

    def find_job(self, url: str) -> str | None:
        """The stored job a pasted link points at: the same URL, else the same one without tracking noise."""
        url, key = (url or "").strip(), url_key(url)
        if not key:
            return None
        pattern = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self.store.connect() as conn:
            row = conn.execute("SELECT id FROM jobs WHERE url = ? LIMIT 1", (url,)).fetchone()
            if row:
                return row[0]
            for job_id, candidate in conn.execute("SELECT id, url FROM jobs WHERE url LIKE ? ESCAPE '\\'",
                                                  (pattern,)):
                if url_key(candidate) == key:
                    return job_id
        return None

    def exists(self, company: str, title: str, applied_at: str) -> bool:
        with self.store.connect() as conn:
            return conn.execute("SELECT 1 FROM applications WHERE lower(company) = ? AND lower(title) = ? AND "
                                "applied_at = ? LIMIT 1",
                                (company.strip().lower(), title.strip().lower(), applied_at)).fetchone() is not None

    def description(self, app_id: int) -> str:
        """The stored job description, for preparing an interview after the posting is taken down."""
        with self.store.connect() as conn:
            row = conn.execute("SELECT j.jd_text FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.id = ?",
                               (app_id,)).fetchone()
        return (row[0] or "") if row else ""

    def review_queue(self, days: int, now: datetime | None = None) -> list[dict]:
        """Notified jobs from the last `days` with no application, not hidden and not judged bad, newest first."""
        cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat()
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT j.id, j.title, j.company, j.url, j.source, j.score_label, j.match_score, j.run_at FROM jobs j "
                "WHERE j.status = 'notified' AND j.run_at >= ? "
                "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id) "
                "AND NOT EXISTS (SELECT 1 FROM review_hidden h WHERE h.job_id = j.id) "
                "AND COALESCE((SELECT f.verdict FROM feedback f WHERE f.job_id = j.id "
                "ORDER BY f.received_at DESC, f.id DESC LIMIT 1), '') NOT IN ('bad', 'applied') "
                "ORDER BY j.run_at DESC, j.id DESC LIMIT 200", (cutoff,)).fetchall()
        return [{"job_id": i, "title": t or "", "company": c or "", "url": safe_url(u or ""), "board": b or "",
                 "fit": fit_text(score), "fit_score": score, "alerted_at": at}
                for i, t, c, u, b, label, score, at in rows]

    def hide_from_review(self, job_id: str) -> None:
        with self.store.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO review_hidden (job_id, hidden_at) VALUES (?, ?)", (job_id, now_iso()))

    def unhide(self, job_id: str) -> None:
        with self.store.connect() as conn:
            conn.execute("DELETE FROM review_hidden WHERE job_id = ?", (job_id,))

    def record_verdict(self, job_id: str, verdict: str) -> str:
        """A training label from the tracker (a or b), saved like an email verdict so export-feedback reads it.
        Returns its message_id, which undo passes back."""
        if verdict not in ("applied", "bad") or verdict not in VERDICTS:
            raise ValueError(f"the tracker records only applied or bad, not {verdict!r}")
        message_id = f"tracker:{uuid.uuid4()}"
        self.store.add_feedback([{"job_id": job_id, "verdict": verdict, "received_at": now_iso(),
                                  "message_id": message_id}])
        return message_id

    def verdict_job(self, message_id: str) -> str | None:
        """The job a tracker label is about; ValueError for a label from an email, which the tracker never undoes."""
        if not str(message_id or "").startswith("tracker:"):
            raise ValueError("only a label made in the tracker can be undone")
        with self.store.connect() as conn:
            row = conn.execute("SELECT job_id FROM feedback WHERE message_id = ?", (message_id,)).fetchone()
        return row[0] if row else None

    def delete_verdict(self, message_id: str) -> None:
        """Undo of a review a or b: the tracker's own label goes; an email's label is refused."""
        self.verdict_job(message_id)
        with self.store.connect() as conn:
            conn.execute("DELETE FROM feedback WHERE message_id = ?", (message_id,))

    def delete(self, app_id: int) -> None:
        """Delete an application made by mistake, with its events and its job's `applied` labels (tracker and email),
        so the job is back in the review queue and the email catch-up does not bring it back. KeyError when unknown."""
        app = self.get(app_id)
        if app is None:
            raise KeyError(app_id)
        with self.store.connect() as conn:
            if app["job_id"]:
                conn.execute("DELETE FROM feedback WHERE job_id = ? AND verdict = 'applied'", (app["job_id"],))
            conn.execute("DELETE FROM application_events WHERE application_id = ?", (app_id,))
            conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))

    def delete_application(self, app_id: int) -> None:
        """Only for undoing a review a that created this application; there is no general delete."""
        with self.store.connect() as conn:
            conn.execute("DELETE FROM application_events WHERE application_id = ?", (app_id,))
            conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))

    def apply_verdicts(self) -> int:
        """Every stored job whose latest verdict is `applied` (the email button) and which has no application at all
        becomes one, dated by that verdict. Read from the feedback table rather than from one run's new rows, so a run
        that failed here is made up by the next. Returns how many were added."""
        with self.store.connect() as conn:
            rows = conn.execute(
                "SELECT f.job_id, f.received_at FROM feedback f WHERE f.verdict = 'applied' "
                "AND f.id = (SELECT g.id FROM feedback g WHERE g.job_id = f.job_id "
                "ORDER BY g.received_at DESC, g.id DESC LIMIT 1) "
                "AND EXISTS (SELECT 1 FROM jobs j WHERE j.id = f.job_id) "
                "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id = f.job_id) "
                "ORDER BY f.received_at, f.id").fetchall()
        added = 0
        for job_id, received_at in rows:
            try:
                added += self.add_from_job(job_id, received_at[:10]) is not None
            except ValueError:
                continue                        # an email dated out of range: the others still go in
        return added
