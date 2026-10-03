"""Command line: python -m jobhunter [--settings PATH] run | check-config | list-boards | list-scorers | import-db PATH |
compare-db PATH"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from jobhunter.compare import compare, report
from jobhunter.errors import SettingsError
from jobhunter.importer import import_db
from jobhunter.notify import Notifier
from jobhunter.pipeline import bootstrap, run
from jobhunter.registry import build_registry
from jobhunter.schedule import is_due, is_quiet, local_time, next_due, read_last_run, write_last_run
from jobhunter.settings import load_settings
from jobhunter.store import Store


def model_notes(app: Any) -> list[str]:
    """Models that cannot run yet (not downloaded, Ollama stopped, too big for this computer), each with what to do."""
    from jobhunter.ollama_check import GB, installed, is_local, memory_gb, memory_notes, ollama_problem, size_of
    from jobhunter.scorers.laya import LayaScorer
    from jobhunter.scorers.ollama import OllamaScorer
    from jobhunter.systemone import SystemOneClient

    tags: dict[str, Any] = {}           # one /api/tags request per Ollama URL

    def ollama_model(obj: Any) -> tuple[str, str] | None:
        if isinstance(obj, OllamaScorer):
            return obj.options.url, obj.options.model
        if isinstance(obj, SystemOneClient):
            return obj.settings.url, obj.settings.model
        return None

    def problem_of(obj: Any) -> str | None:
        try:
            where = ollama_model(obj)
            if where:
                url, model = where
                if url not in tags:
                    tags[url] = installed(url)
                return ollama_problem(url, model, found=tags[url])
            return obj.check() if callable(getattr(obj, "check", None)) else None
        except Exception as e:          # a plugin's check, or an unreadable disk, must not stop check-config
            return f"could not be checked ({type(e).__name__}: {e})"

    notes, seen = [], set()
    names = [name for name, _ in app.chain.scorers]
    problems = [problem_of(s) for _, s in app.chain.scorers]
    for i, (name, problem) in enumerate(zip(names, problems)):
        if problem:
            seen.add(problem)
            backup = next((names[j] for j in range(i + 1, len(names)) if not problems[j]), None)
            fallback = f" Until it is fixed, {backup} judges the jobs instead." if backup else ""
            notes.append(f"{name} cannot judge jobs yet (scorers.{name}): {problem}.{fallback}")
    if names and all(problems):
        notes.append("no model can judge jobs right now, so jobhunter can find jobs but cannot tell which ones fit "
                     "your resume: no alerts are sent. Found jobs are kept and judged on a later run once a model "
                     "works (for up to 24 hours). Fix one of the notes above, then run check-config again")
    for _, scorer in app.chain.scorers:
        note = scorer.download_note() if isinstance(scorer, LayaScorer) and not problem_of(scorer) else None
        if note:
            notes.append(note)

    writer = app.letter_writer
    own_writer = writer is not None and all(writer is not s for _, s in app.chain.scorers)
    writer_problem = problem_of(writer) if own_writer else None
    if writer_problem:
        said = "the same problem as above" if writer_problem in seen else writer_problem
        seen.add(writer_problem)
        notes.append(f"cover letters cannot be written yet (cover_letters): {said}. Until then alerts are sent "
                     "without a letter")
    clients = list({id(f.model): f.model for f in app.filters if getattr(f, "model", None) is not None}.values())
    client_problems = {id(c): problem_of(c) for c in clients}
    for client in clients:
        problem = client_problems[id(client)]
        if problem:
            said = "the same problem as above" if problem in seen else problem
            seen.add(problem)
            notes.append(f"the System One checks cannot run yet (filters.systemone): {said}. Until then those checks "
                         "are skipped and the jobs they would have removed are kept")

    # Memory: only the models that run together on this computer. The scorer that judges jobs is the first one that
    # can run (a backup loads only when it fails); the cover-letter writer and the System One model run alongside it.
    judge = next((s for s, p in zip((s for _, s in app.chain.scorers), problems) if not p), None)
    together = [judge] + ([writer] if own_writer and not writer_problem else [])
    together += [c for c in clients if not client_problems[id(c)]]
    sizes = {}
    for obj in together:
        where = ollama_model(obj)
        if where and is_local(where[0]) and isinstance(tags.get(where[0]), dict):
            size = size_of(tags[where[0]], where[1])
            if size:
                sizes[where[1]] = size / GB
    return notes + memory_notes(sizes, isinstance(judge, LayaScorer), memory_gb())


def _print_problems(problems: list[str]) -> int:
    print("jobhunter cannot start; fix these settings problems:", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    return 2


try:
    import fcntl
except ImportError:            # Windows: no lock (cron and scheduler.py are for macOS and Linux)
    fcntl = None


@contextmanager
def run_lock(path: Path):
    """True while this process holds data/run.lock; False when another run holds it (a slow run and the next cron
    tick must not both notify the same jobs)."""
    if fcntl is None:
        yield True
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jobhunter", description="Local-first job matching.")
    parser.add_argument("--settings", default="jobhunter.yaml", help="settings file (default: ./jobhunter.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)
    run_cmd = sub.add_parser("run", help="search the boards, filter, score, save and notify")
    run_cmd.add_argument("--only", help="comma-separated board names to run, e.g. dice,greenhouse")
    run_cmd.add_argument("--dry-run", action="store_true", help="score everything but save and send nothing")
    run_cmd.add_argument("--scheduled", action="store_true",
                         help="skip unless a run is due by schedule.every and it is not quiet time (for cron)")
    sub.add_parser("check-config", help="validate settings, plugins and resumes, then exit")
    sub.add_parser("list-boards", help="built-in and plugin boards")
    sub.add_parser("list-scorers", help="built-in and plugin scorers")
    imp = sub.add_parser("import-db", help="copy jobs from a job-notifier database so they are not notified again")
    imp.add_argument("path", help="path to job-notifier's data/jobs.db (opened read-only)")
    cmp_cmd = sub.add_parser("compare-db", help="compare decisions with a job-notifier database, job by job")
    cmp_cmd.add_argument("path", help="path to job-notifier's data/jobs.db (opened read-only)")
    cmp_cmd.add_argument("--since", help="only jobs jobhunter handled on or after this date (YYYY-MM-DD)")
    args = parser.parse_args(argv)

    settings_path = Path(args.settings).expanduser().resolve()
    load_dotenv(settings_path.parent / ".env")

    if args.command in ("list-boards", "list-scorers"):
        try:
            registry = build_registry(settings_path.parent / "plugins")
        except SettingsError as e:
            return _print_problems(e.problems)
        kind = "board" if args.command == "list-boards" else "scorer"
        table = registry.boards if kind == "board" else registry.scorers
        for name in sorted(table):
            print(f"{name:16} {registry.origins[(kind, name)]}")
        return 0

    if args.command in ("import-db", "compare-db"):
        try:
            settings = load_settings(settings_path)
        except SettingsError as e:
            return _print_problems(e.problems)
        store = Store(settings.data_dir / "jobs.db")
        try:
            if args.command == "import-db":
                s = import_db(Path(args.path), store)
                counts = ", ".join(f"{k}: {v}" for k, v in sorted(s.copied_by_status.items())) or "none"
                print(f"copied {s.copied} job(s) ({counts}); {s.already_present} were already in {store.path}"
                      + (f"; {s.finished_from_old} unfinished there took job-notifier's final status"
                         if s.finished_from_old else ""))
            else:
                print(report(compare(Path(args.path), store.path, args.since)))
        except (FileNotFoundError, ValueError, sqlite3.DatabaseError) as e:
            print(f"jobhunter: {e}", file=sys.stderr)
            return 2
        return 0

    try:
        app = bootstrap(settings_path)
    except SettingsError as e:
        return _print_problems(e.problems)

    if args.command == "check-config":
        print(f"Settings OK: {len(app.boards)} board(s) ({', '.join(app.boards) or 'none enabled'}), "
              f"scorers in order: {', '.join(name for name, _ in app.chain.scorers)}, "
              f"resumes: {', '.join(app.resumes.resumes)}")
        for note in model_notes(app):
            print(f"note: {note}")
        if not app.resumes.names:
            print("note: no name found on the default resume's first line, so none is removed before models see "
                  "the resumes; set CANDIDATE_NAMES in .env (or resumes.names) to be sure")
        return 0

    only = {n.strip() for n in args.only.split(",") if n.strip()} if args.only else None
    if only and only - set(app.boards):
        return _print_problems([f"--only: not an enabled board: {', '.join(sorted(only - set(app.boards)))}"])
    schedule, last_run_path, started = app.settings.schedule, app.settings.data_dir / "last_run", utc_now()
    if args.scheduled:
        quiet = is_quiet(schedule, started)
        if quiet:
            print(f"skipped: {quiet}")
            return 0
        last_run = read_last_run(last_run_path)
        if not is_due(schedule, last_run, started):
            print(f"skipped: next run due at {local_time(schedule, next_due(schedule, last_run)):%Y-%m-%d %H:%M}")
            return 0
    store = Store(app.settings.data_dir / "jobs.db")
    if args.dry_run:
        run(app, store, Notifier(app.settings.notify), only=only, dry_run=True)
        return 0
    with run_lock(app.settings.data_dir / "run.lock") as locked:
        if not locked:
            print("skipped: another run is in progress")
            return 0
        run(app, store, Notifier(app.settings.notify), only=only, dry_run=False)
        write_last_run(last_run_path, started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
