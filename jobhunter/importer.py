"""Copies the jobs of a job-notifier database into jobhunter's, so jobs already handled there are not scored or
notified again. The source database is opened read-only and never changed."""
from __future__ import annotations

import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from jobhunter.store import COLUMNS, Store


def _open_read_only(path: Path) -> sqlite3.Connection:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"{path}: no such database file")
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def read_jobs(path: Path) -> list[dict]:
    """Every row of the `jobs` table, with the columns jobhunter also has."""
    with closing(_open_read_only(path)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "id" not in columns:
            raise ValueError(f"{path}: no jobs table (expected job-notifier's data/jobs.db)")
        shared = [c for c in COLUMNS if c in columns]
        return [dict(row) for row in conn.execute(f"SELECT {', '.join(shared)} FROM jobs")]


@dataclass
class ImportSummary:
    copied: int
    already_present: int
    copied_by_status: dict[str, int] = field(default_factory=dict)
    finished_from_old: int = 0     # jobs jobhunter had not finished that job-notifier had: they take its status


def import_db(source: Path, store: Store) -> ImportSummary:
    rows = [r for r in read_jobs(source) if r.get("id")]
    present = store.existing_ids([r["id"] for r in rows])
    new = [r for r in rows if r["id"] not in present]
    copied = store.insert_missing(new)
    # A job jobhunter left for retry (rate limit, error) but job-notifier already notified must not be notified again.
    finished = store.adopt_final([r for r in rows if r["id"] in present])
    return ImportSummary(copied, len(rows) - len(new), dict(Counter(r.get("status") or "" for r in new)), finished)
