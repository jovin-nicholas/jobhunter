"""One run: boards -> dedupe -> unseen -> enrich -> filters -> resume -> scorer chain -> save -> notify."""
from __future__ import annotations

import threading
import time
from collections import Counter
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Mapping

from jobhunter import cover_letter
from jobhunter.discovery import Discovery
from jobhunter.errors import BoardSkipped, SettingsError
from jobhunter.filters import Filter, build_filters
from jobhunter.http import Http
from jobhunter.models import Job, SearchContext, score_label
from jobhunter.text_clean import repair_text
from jobhunter.text_match import word_text
from jobhunter.page_text import PageUnavailable, fetch_description, needs_page_fetch
from jobhunter.registry import Registry, build_registry, check_names
from jobhunter.resumes import ResumeSet, load_resumes
from jobhunter.scorers.chain import ScorerChain
from jobhunter.settings import Settings, load_settings
from jobhunter.store import STATUS_FOR_DECISION

Log = Callable[[str], None]
_REPAIRED = {"title", "company", "location", "description"}
REPEAT_DAYS = 30        # a job notified this recently is not notified again


def same_job_key(title: str, company: str) -> tuple[str, str]:
    """The same role at the same company is one job, whatever board, id, location, case or punctuation it comes with."""
    return word_text(title).strip(), word_text(company).strip()


@dataclass
class App:
    settings: Settings
    registry: Registry
    resumes: ResumeSet
    filters: list[Filter]
    chain: ScorerChain
    boards: dict[str, Any]            # enabled boards, by name
    letter_writer: Any = None         # scorer that writes cover letters, when enabled


@dataclass
class BoardSummary:
    found: int = 0
    new: int = 0
    filtered: Counter = field(default_factory=Counter)
    notified: int = 0
    logged: int = 0
    skipped: int = 0
    retry: int = 0
    errors: int = 0
    duplicates: int = 0               # would have been notified, but the same job already was
    failed: str | None = None         # why the board's search produced nothing
    skipped_reason: str | None = None  # the board cannot run here (e.g. LinkedIn without Node.js)


def bootstrap(settings_path: str | Path, env: Mapping[str, str] | None = None) -> App:
    """Everything a run needs, validated before any network call. Raises one SettingsError with every problem."""
    settings = load_settings(settings_path, env)
    registry = build_registry(settings.plugins_dir)
    problems = check_names(settings, registry)
    for b in settings.boards:
        if b.timeout_s is None:
            b.timeout_s = getattr(registry.boards.get(b.name), "default_timeout_s", 600)
    filters: list[Filter] = []
    resumes = None
    try:
        filters = build_filters(settings.filters)
    except SettingsError as e:
        problems += e.problems
    try:
        resumes = load_resumes(settings.resumes, settings.data_dir / "resume_cache", env)
    except SettingsError as e:
        problems += e.problems
    if problems:
        raise SettingsError(problems)

    boards, scorers = {}, []
    for b in settings.boards:
        if b.enabled:
            boards[b.name] = _construct(f"boards.{b.name}", registry.boards[b.name], b.options, problems)
    for s in settings.scorers:
        scorers.append((s.name, _construct(f"scorers.{s.name}", registry.scorers[s.name], s.options, problems)))
    letter_writer = None
    if settings.cover_letters.enabled:
        name = settings.cover_letters.writer
        letter_writer = dict(scorers).get(name)
        # Letters get their own writer when the scorer has letter defaults (Ollama: thinking on, some temperature)
        # or cover_letters.options are given; otherwise the scorer itself writes, sharing its keys and rate limits.
        defaults = getattr(registry.scorers.get(name), "LETTER_DEFAULTS", {})
        if letter_writer is not None and (defaults or settings.cover_letters.options):
            scorer = letter_writer
            scorer_options = next(s.options for s in settings.scorers if s.name == name)
            defaults = dict(defaults)
            if "timeout_s" in defaults and "timeout_s" in scorer_options:
                # Thinking makes letters slower than scoring, so they never get less time than the scorer has.
                defaults["timeout_s"] = max(defaults["timeout_s"], scorer_options["timeout_s"])
            letter_writer = _construct("cover_letters.options", registry.scorers[name],
                                       {**scorer_options, **defaults, **settings.cover_letters.options}, problems)
            # A cloud writer on the same API keys shares the scorer's key rotation and pacing, so letters and scoring
            # together stay within the free tier's rate limit.
            same_keys = getattr(getattr(letter_writer, "options", None), "api_keys_env", None)
            if same_keys and same_keys == getattr(getattr(scorer, "options", None), "api_keys_env", None):
                letter_writer.keys, letter_writer.pacer = scorer.keys, scorer.pacer
        if letter_writer is not None and not callable(getattr(letter_writer, "generate", None)):
            problems.append(f"cover_letters.writer: {settings.cover_letters.writer} cannot write text "
                            "(use ollama, gemini, groq, or a scorer plugin with a generate(prompt) method)")
    if problems:
        raise SettingsError(problems)
    return App(settings, registry, resumes, filters, ScorerChain(scorers, settings.decisions), boards, letter_writer)


def _construct(key: str, cls: type, options: dict, problems: list[str]) -> Any:
    try:
        return cls(dict(options))
    except Exception as e:
        problems.append(f"{key}: could not start ({e})")
        return None


def run(app: App, store: Any, notifier: Any, *, only: set[str] | None = None, dry_run: bool = False,
        http: Http | None = None, log: Log = print,
        fetch_page: Callable[[str], str] | None = None,
        feedback: Callable[[], Any] | None = None) -> dict[str, BoardSummary]:
    # Feedback from alert buttons is read first; a dry run never touches the mailbox, and a failure never stops alerts.
    if feedback is not None and not dry_run:
        try:
            got = feedback()
            line = got.line() if got is not None and callable(getattr(got, "line", None)) else None
            if line:
                log(line)
        except Exception as e:
            log(f"feedback: could not read the inbox ({type(e).__name__})")
    s = app.settings.search
    http = http or Http()
    ctx = SearchContext(s.queries, s.locations, s.max_age_hours, http, log,
                        discovery=Discovery(app.settings.discovery, s.queries, http, log),
                        is_known=store.is_terminal,
                        mark_stale=(lambda job_id: None) if dry_run else store.mark_stale,
                        mark_gone=(lambda job_id: None) if dry_run else store.mark_gone)
    if not s.fetch_descriptions:
        fetch_page = None
    elif fetch_page is None:
        fetch_page = lambda url: fetch_description(http, url, log)   # noqa: E731
    boards = {name: b for name, b in app.boards.items() if only is None or name in only}
    summary = {name: BoardSummary() for name in boards}

    found = _search_all(app, boards, ctx, summary, log)
    unique: dict[str, Job] = {}
    for job in found:
        unique.setdefault(job.id, job)
    new_jobs = store.filter_unseen(list(unique.values()))
    # Jobs whose description was refused on an earlier run are tried again, even if their board stopped listing them.
    listed = {job.id for job in new_jobs}
    for job in store.retry_candidates():
        if job.source in boards and job.id not in listed:
            new_jobs.append(job)
            listed.add(job.id)
    for job in new_jobs:
        summary[job.source].new += 1
    notified = {same_job_key(t, c) for t, c in store.recently_notified(REPEAT_DAYS)}

    for job in new_jobs:
        counts = summary[job.source]
        try:
            _process(app, job, boards[job.source], ctx, store, notifier, counts, dry_run, log, fetch_page, notified)
        except Exception as e:
            counts.errors += 1
            log(f"ERROR [{job.source}] {job.title} at {job.company}: {e}")
            if not dry_run:
                store.save(job, "error")

    for name, c in summary.items():
        detail = f"FAILED: {c.failed}" if c.failed else f"skipped: {c.skipped_reason}" if c.skipped_reason else (
            f"found {c.found}, new {c.new}, notified {c.notified}, duplicates {c.duplicates}, logged {c.logged}, "
            f"skipped {c.skipped}, "
            f"filtered {sum(c.filtered.values())} {dict(c.filtered) or ''}, retry {c.retry}, errors {c.errors}")
        log(f"[{name}] {detail}".rstrip())
    notes = {f.report() for f in app.filters if callable(getattr(f, "report", None))} - {None}
    for note in sorted(notes):
        log(note)
    return summary


def _search_all(app: App, boards: dict[str, Any], ctx: SearchContext, summary: dict[str, BoardSummary],
                log: Log) -> list[Job]:
    timeouts = {b.name: b.timeout_s for b in app.settings.boards}
    jobs: list[Job] = []
    if not boards:
        return jobs
    # Daemon threads, not a thread pool: Python waits for pool threads at exit, so one hung board would keep the
    # process (and the next cron run's) alive. A timed-out board is abandoned and its results dropped.
    outcomes: dict[str, tuple[str, Any]] = {}

    def search(name: str, b: Any) -> None:
        try:
            outcomes[name] = ("ok", list(b.search(ctx)))
        except BoardSkipped as e:
            outcomes[name] = ("skipped", str(e))
        except Exception as e:
            outcomes[name] = ("error", f"{type(e).__name__}: {e}")

    threads = {name: threading.Thread(target=search, args=(name, b), name=f"jobhunter-board-{name}", daemon=True)
               for name, b in boards.items()}
    started = time.monotonic()
    for t in threads.values():
        t.start()
    for name, t in threads.items():
        t.join(max(0.0, started + timeouts.get(name, 600) - time.monotonic()))
        if name not in outcomes:
            summary[name].failed = f"timed out after {timeouts.get(name, 600)}s"
            log(f"[{name}] search {summary[name].failed}")
            continue
        kind, results = outcomes[name]
        if kind == "skipped":
            summary[name].skipped_reason = results
            continue
        if kind == "error":
            summary[name].failed = results
            log(f"[{name}] search failed: {results}")
            continue
        for job in results:
            try:
                _clean(job)
            except Exception as e:
                log(f"[{name}] skipped a listing it could not read ({type(e).__name__}: {e})")
                continue
            job.source = name            # the board name, whatever the board wrote
            jobs.append(job)
        summary[name].found = len(results)
    return jobs


def _clean(job: Job) -> None:
    """Boards sometimes return None for text fields, or text garbled by a wrong encoding; the rest of the pipeline
    expects clean strings."""
    for f in fields(job):
        if f.type == "str":
            value = getattr(job, f.name)
            setattr(job, f.name, repair_text(value) if f.name in _REPAIRED else (value or ""))


def _process(app: App, job: Job, board: Any, ctx: SearchContext, store: Any, notifier: Any, counts: BoardSummary,
             dry_run: bool, log: Log, fetch_page: Callable[[str], str] | None,
             notified: set[tuple[str, str]] | None = None) -> None:
    if hasattr(board, "enrich"):
        try:
            job = board.enrich(job, ctx)
        except Exception as e:
            log(f"[{job.source}] could not fetch the full description for {job.id}: {e}")
            job.description_is_snippet = True
        _clean(job)

    # A job whose description source refused us is retried next run rather than scored from its title: Laya learned
    # to answer "log" for jobs without a description.
    refused = bool(job.extra.get("description_refused"))
    if fetch_page and not refused and needs_page_fetch(job.description, job.description_is_snippet):
        try:
            text = fetch_page(job.url)
        except PageUnavailable as e:
            log(f"[{job.source}] description of {job.id} unavailable: {e}")
            refused, text = True, ""
        if text and len(text) > len((job.description or "").strip()):
            job.description, job.description_is_snippet = text, False

    for f in app.filters:
        result = f.check(job)
        if not result.keep:
            counts.filtered[f.name] += 1
            log(f"FILTERED [{job.source}] {job.title} at {job.company}: {f.name}: {result.reason}")
            if not dry_run:
                store.save(job, "filtered", filter_reason=f"{f.name}: {result.reason}")
            return

    # After the filters: a job they drop needs no description, and one that passes is filtered again next run
    # once its description has loaded.
    if refused and needs_page_fetch(job.description, job.description_is_snippet):
        counts.retry += 1
        log(f"RETRY_LATER [{job.source}] {job.title} at {job.company}: description unavailable, not scored")
        if not dry_run:
            store.save(job, "error_unavailable")
        return

    resume = app.resumes.pick(job)
    outcome = app.chain.score(job, resume)
    if outcome.result is None:
        if outcome.status in ("error_429_retry", "error_unavailable"):
            counts.retry += 1
        else:
            counts.errors += 1
        log(f"{outcome.status.upper()} [{job.source}] {job.title} at {job.company}: {'; '.join(outcome.errors)}")
        if not dry_run:
            store.save(job, outcome.status, resume_id=resume.id)
        return

    for failure in outcome.first_failures:
        log(f"note: {failure}; scored with {outcome.result.model} instead (noted once per run)")
    status = STATUS_FOR_DECISION[outcome.result.decision]
    if status == "notified" and notified is not None:
        key = same_job_key(job.title, job.company)
        if key in notified:
            counts.duplicates += 1
            log(f"DUPLICATE [{job.source}] {job.title} at {job.company}: already notified in the last "
                f"{REPEAT_DAYS} days or earlier in this run")
            if not dry_run:
                store.save(job, "duplicate", outcome.result, resume_id=resume.id)
            return
        notified.add(key)
    setattr(counts, status, getattr(counts, status) + 1)
    log(f"{status.upper()} [{job.source}] {job.title} at {job.company}: {score_label(outcome.result)} "
        f"({outcome.result.model}, resume {resume.id})")
    if dry_run:
        return
    if status != "notified":
        store.save(job, status, outcome.result, resume_id=resume.id)
        return
    letter = cover_letter.write(app.letter_writer, job, resume, log) if app.letter_writer else None
    if letter:
        delivered = notifier.send(job, outcome.result, resume.id, cover_letter=letter)
    else:
        delivered = notifier.send(job, outcome.result, resume.id)
    if delivered is False:
        # Nobody received the alert: tried again next run rather than marked as notified.
        counts.notified -= 1
        counts.retry += 1
        if notified is not None:
            notified.discard(same_job_key(job.title, job.company))
        log(f"NOTIFY_FAILED [{job.source}] {job.title} at {job.company}: no channel delivered the alert; "
            "trying again next run")
        store.save(job, "error_notify", outcome.result, resume_id=resume.id)
        return
    store.save(job, "notified", outcome.result, resume_id=resume.id)
