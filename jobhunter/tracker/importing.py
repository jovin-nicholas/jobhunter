"""One-time import of applications kept elsewhere (a spreadsheet saved as CSV): `jobhunter tracker-import`. Columns
company, title and applied_at (YYYY-MM-DD) are required; url and stage are optional; header case, spacing, column
order and extra columns do not matter. Imports write no feedback: an old spreadsheet says nothing about the alerts."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from jobhunter.tracker import STAGES
from jobhunter.tracker.records import Applications, event_date, iso_date

REQUIRED = ("company", "title", "applied_at")


@dataclass
class ImportReport:
    added: int = 0
    duplicates: int = 0
    problems: list[str] = field(default_factory=list)


def _header(name: str | None) -> str:
    return "_".join((name or "").strip().lower().split())          # "Applied At", " applied_at " -> applied_at


def import_csv(path: str | Path, apps: Applications, dry_run: bool = False) -> ImportReport:
    report = ImportReport()
    with open(path, newline="", encoding="utf-8-sig") as f:       # utf-8-sig: Excel's byte-order mark
        reader = csv.DictReader(f)
        headers = {_header(h): h for h in reader.fieldnames or []}
        missing = [c for c in REQUIRED if c not in headers]
        if missing:
            report.problems.append(f"missing column(s) {', '.join(missing)} "
                                   "(columns: company, title, applied_at, and optionally url and stage)")
            return report
        seen: set[tuple[str, str, str]] = set()
        for row in reader:
            line = reader.line_num

            def value(column: str) -> str:
                return (row.get(headers[column]) or "").strip() if column in headers else ""

            if not any(value(c) for c in headers):
                continue                                           # a blank line
            company, title, url = value("company"), value("title"), value("url")
            stage = value("stage").lower() or "applied"
            if not company or not title:
                report.problems.append(f"line {line}: company and title are both required")
                continue
            try:
                applied_at = iso_date(value("applied_at"))
            except ValueError:
                report.problems.append(f"line {line}: applied_at {value('applied_at')!r} is not a date like 2026-10-05")
                continue
            try:
                event_date(applied_at, apps.today())
            except ValueError as e:
                report.problems.append(f"line {line}: applied_at {e}")
                continue
            if stage not in STAGES:
                report.problems.append(f"line {line}: unknown stage {stage!r} (one of {', '.join(STAGES)})")
                continue
            key = (company.lower(), title.lower(), applied_at)
            if key in seen or apps.exists(company, title, applied_at):
                report.duplicates += 1
                continue
            seen.add(key)
            report.added += 1
            if dry_run:
                continue
            job_id = apps.find_job(url) if url else None
            fields = (apps.job_fields(job_id) or {}) if job_id else {}
            fields.update(title=title, company=company, url=url or fields.get("url", ""))
            apps.add(**fields, applied_at=applied_at, stage=stage)
    return report
