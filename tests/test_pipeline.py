import sqlite3
from datetime import datetime, timedelta, timezone
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from jobhunter.errors import SettingsError
from jobhunter.models import Job
from jobhunter.pipeline import bootstrap, run
from jobhunter.store import Store
from tests.helpers import status_counts, write_project

BOARDS = """
import time
from jobhunter import Job, board

TITLES = ["Great Engineer", "Maybe Engineer", "Meh Engineer", "Senior Engineer", "Boom Engineer", "Busy Engineer"]


@board("fake")
class FakeBoard:
    def __init__(self, options):
        pass

    def search(self, ctx):
        for i, title in enumerate(TITLES):
            yield Job(f"fake_{i}", title, "Acme", "Remote - US", f"https://example.com/{i}", description="Java work")
        yield Job("fake_nones", None, None, None, "https://example.com/n", description=None)
        yield Job("fake_0", "Great Engineer", "Acme", "Remote - US", "https://example.com/0")   # duplicate
        yield Job("fake_enrich", "Great Engineer II", "Acme", "Remote", "https://example.com/e", description="short")

    def enrich(self, job, ctx):
        if job.id == "fake_enrich":
            raise RuntimeError("detail page gone")
        return job


@board("crashy")
class CrashyBoard:
    def __init__(self, options):
        pass

    def search(self, ctx):
        raise RuntimeError("site changed its HTML")


@board("sleepy")
class SleepyBoard:
    def __init__(self, options):
        pass

    def search(self, ctx):
        time.sleep(2)
        return []
"""

SCORERS = """
from jobhunter import RateLimited, ScoreResult, scorer


@scorer("fixed")
class Fixed:
    def __init__(self, options):
        pass

    def score(self, job, resume):
        if job.title.startswith("Boom"):
            raise RuntimeError("bug in the scorer")
        if job.title.startswith("Busy"):
            raise RateLimited("429")
        value = 9 if job.title.startswith("Great") else 6 if job.title.startswith("Maybe") else 3
        return ScoreResult(score=value, model="fixed", reasoning=f"resume {resume.id}")
"""

SETTINGS = """
resumes: {folder: resumes, default: backend.txt}
search: {queries: [software engineer], locations: [Remote], fetch_descriptions: false}
boards:
  fake: {}
  crashy: {}
  sleepy: {timeout_s: 1}
filters:
  seniority: {levels: [intern, entry, mid]}
scorers:
  - fixed: {}
decisions: {notify_at: 7, log_at: 5}
"""


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, job, result, resume_id):
        self.sent.append((job.id, result.score, resume_id))


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        path = write_project(self.tmp, SETTINGS, plugins={"boards.py": BOARDS, "scorers.py": SCORERS})
        self.app = bootstrap(path, env={})
        self.store = Store(self.tmp / "data" / "jobs.db")
        self.logs = []

    def run_once(self, **kw):
        notifier = FakeNotifier()
        summary = run(self.app, self.store, notifier, log=self.logs.append, **kw)
        return summary, notifier

    def rows(self):
        with closing(sqlite3.connect(self.store.path)) as conn:
            return {r[0]: r[1:] for r in conn.execute("SELECT id, status, jd_is_snippet, filter_reason FROM jobs")}


class TestRun(PipelineTestCase):
    def test_statuses_notifications_and_board_isolation(self):
        summary, notifier = self.run_once()
        status = {k: v[0] for k, v in self.rows().items()}
        self.assertEqual(status, {"fake_0": "notified", "fake_1": "logged", "fake_2": "skipped",
                                  "fake_3": "filtered", "fake_4": "error_unavailable", "fake_5": "error_429_retry",
                                  "fake_nones": "skipped", "fake_enrich": "notified"})
        self.assertEqual(notifier.sent, [("fake_0", 9, "backend.txt"), ("fake_enrich", 9, "backend.txt")])
        self.assertIn("seniority:", self.rows()["fake_3"][2])
        self.assertEqual(self.rows()["fake_enrich"][1], 1)
        fake = summary["fake"]
        self.assertEqual((fake.found, fake.new, fake.notified, fake.logged, fake.skipped, fake.retry, fake.errors),
                         (9, 8, 2, 1, 2, 2, 0))
        self.assertEqual(dict(fake.filtered), {"seniority": 1})
        self.assertIn("site changed its HTML", summary["crashy"].failed)
        self.assertIn("timed out", summary["sleepy"].failed)

    def test_dry_run_writes_and_sends_nothing(self):
        summary, notifier = self.run_once(dry_run=True)
        self.assertEqual((self.rows(), notifier.sent), ({}, []))
        self.assertEqual(summary["fake"].notified, 2)

    def test_second_run_only_retries_unfinished_jobs(self):
        self.run_once()
        summary, notifier = self.run_once()
        # Only unfinished jobs come back: the rate-limited one and the one a scorer crashed on (not the job's fault).
        self.assertEqual(summary["fake"].new, 2)
        self.assertEqual(notifier.sent, [])

    def test_only_limits_the_boards(self):
        summary, _ = self.run_once(only={"fake"})
        self.assertEqual(set(summary), {"fake"})


class TestBootstrap(unittest.TestCase):
    def test_problems_from_names_and_resumes_are_reported_together(self):
        tmp = Path(tempfile.mkdtemp())
        path = write_project(tmp, SETTINGS.replace("  crashy: {}", "  nope: {}"),
                             resumes={"backend.txt": "Too short."}, plugins={"boards.py": BOARDS, "scorers.py": SCORERS})
        with self.assertRaises(SettingsError) as caught:
            bootstrap(path, env={})
        text = "\n".join(caught.exception.problems)
        self.assertIn("boards.nope: unknown board", text)
        self.assertIn("characters of text", text)



class TestHungBoard(unittest.TestCase):
    def test_a_hung_board_does_not_keep_the_process_alive(self):
        import threading
        import time
        tmp = Path(tempfile.mkdtemp())
        path = write_project(tmp, SETTINGS.replace("  sleepy: {timeout_s: 1}", "  sleepy: {timeout_s: 1}")
                             .replace("  fake: {}\n  crashy: {}\n", ""),
                             plugins={"boards.py": BOARDS, "scorers.py": SCORERS})
        app = bootstrap(path, env={})
        started = time.monotonic()
        run(app, Store(tmp / "data" / "jobs.db"), FakeNotifier(), log=lambda line: None)
        self.assertLess(time.monotonic() - started, 1.8)
        board_threads = [t for t in threading.enumerate() if t.name.startswith("jobhunter-board-") and t.is_alive()]
        self.assertTrue(board_threads)
        self.assertTrue(all(t.daemon for t in board_threads), board_threads)


class TestPageFetch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def run_with(self, fetch_flag, fetch_page):
        settings = SETTINGS.replace("fetch_descriptions: false", f"fetch_descriptions: {fetch_flag}")
        path = write_project(self.tmp, settings, plugins={"boards.py": BOARDS, "scorers.py": SCORERS})
        app = bootstrap(path, env={})
        run(app, Store(self.tmp / "data" / "jobs.db"), FakeNotifier(), log=lambda line: None, fetch_page=fetch_page)
        with closing(sqlite3.connect(self.tmp / "data" / "jobs.db")) as conn:
            return conn.execute("SELECT jd_text, jd_is_snippet FROM jobs WHERE id = 'fake_0'").fetchone()

    def test_short_descriptions_are_replaced_with_the_page_text(self):
        fetched = []

        def fetch(url):
            fetched.append(url)
            return "Full posting text. " * 20

        jd_text, is_snippet = self.run_with("true", fetch)
        self.assertIn("https://example.com/0", fetched)
        self.assertTrue(jd_text.startswith("Full posting text."))
        self.assertEqual(is_snippet, 0)

    def test_turned_off_means_no_fetch(self):
        fetched = []
        jd_text, _ = self.run_with("false", lambda url: fetched.append(url) or "page")
        self.assertEqual((fetched, jd_text), ([], "Java work"))


PEEK = """
from jobhunter import board

SEEN = []


@board("peek")
class Peek:
    def __init__(self, options):
        pass

    def search(self, ctx):
        SEEN.append((type(ctx.discovery).__name__, ctx.discovery.urls(), ctx.is_known("fake_0"), ctx.is_known("x")))
        return []
"""


STALE = """
from jobhunter import board


@board("stale")
class Stale:
    def __init__(self, options):
        pass

    def search(self, ctx):
        ctx.mark_stale("stale_1")
        ctx.mark_gone("gone_1")
        return []
"""


class TestStaleMemory(unittest.TestCase):
    def run_board(self, dry_run):
        tmp = Path(tempfile.mkdtemp())
        settings = ("resumes: {folder: resumes, default: backend.txt}\n"
                    "search: {queries: [swe], locations: [Remote], fetch_descriptions: false}\n"
                    "boards: {stale: {}}\nscorers: [fixed]\ndiscovery: {github_readmes: []}\n")
        path = write_project(tmp, settings, plugins={"stale.py": STALE, "scorers.py": SCORERS})
        store = Store(tmp / "data" / "jobs.db")
        run(bootstrap(path, env={}), store, FakeNotifier(), log=lambda line: None, dry_run=dry_run)
        return store

    def test_stale_postings_are_remembered_as_finished(self):
        store = self.run_board(dry_run=False)
        self.assertTrue(store.is_terminal("stale_1"))
        self.assertTrue(store.is_terminal("gone_1"))
        self.assertEqual(status_counts(store), {"gone": 1, "stale": 1})

    def test_dry_run_remembers_nothing(self):
        self.assertEqual(status_counts(self.run_board(dry_run=True)), {})


class TestSearchContext(unittest.TestCase):
    def test_boards_get_discovery_and_the_store_lookup(self):
        tmp = Path(tempfile.mkdtemp())
        settings = ("resumes: {folder: resumes, default: backend.txt}\n"
                    "search: {queries: [swe], locations: [Remote], fetch_descriptions: false}\n"
                    "boards: {peek: {}}\nscorers: [fixed]\ndiscovery: {github_readmes: []}\n")
        path = write_project(tmp, settings, plugins={"peek.py": PEEK, "scorers.py": SCORERS})
        app = bootstrap(path, env={})
        store = Store(tmp / "data" / "jobs.db")
        store.save(Job("fake_0", "Great Engineer", "Acme", "Remote - US", "https://example.com/0"), "notified")
        run(app, store, FakeNotifier(), log=lambda line: None)
        self.assertEqual(sys.modules["jobhunter_plugins.peek"].SEEN, [("Discovery", set(), True, False)])


REFUSING = """
from jobhunter import Job, board


@board("refusing")
class Refusing:
    def __init__(self, options):
        pass

    def search(self, ctx):
        yield Job("r_refused", "Great Engineer", "Acme", "Remote - US", "https://example.com/refused")
        yield Job("r_pagefail", "Great Engineer", "Acme", "Remote - US", "https://example.com/pagefail")
        yield Job("r_empty", "Great Engineer", "Acme", "Remote - US", "https://example.com/empty")
        yield Job("r_abroad", "Great Engineer", "Acme", "Madrid, Spain", "https://example.com/abroad")

    def enrich(self, job, ctx):
        if job.id in ("r_refused", "r_abroad"):
            job.extra["description_refused"] = True     # e.g. LinkedIn's guest endpoint answered 429
        return job
"""


class TestNoBlindScoring(unittest.TestCase):
    def test_jobs_whose_description_was_refused_are_retried_not_scored(self):
        from jobhunter.page_text import PageUnavailable
        tmp = Path(tempfile.mkdtemp())
        settings = ("resumes: {folder: resumes, default: backend.txt}\n"
                    "search: {queries: [swe], locations: [Remote]}\n"
                    "boards: {refusing: {}}\nscorers: [fixed]\ndiscovery: {github_readmes: []}\n"
                    "filters: {location: {countries: [US]}}\n")
        path = write_project(tmp, settings, plugins={"refusing.py": REFUSING, "scorers.py": SCORERS})
        fetched = []

        def fetch(url):
            fetched.append(url)
            if url.endswith("pagefail"):
                raise PageUnavailable("HTTP 429")
            return ""

        store = Store(tmp / "data" / "jobs.db")
        summary = run(bootstrap(path, env={}), store, FakeNotifier(), log=lambda line: None, fetch_page=fetch)
        with closing(sqlite3.connect(store.path)) as conn:
            statuses = dict(conn.execute("SELECT id, status FROM jobs"))
        self.assertEqual(statuses, {"r_refused": "error_unavailable", "r_pagefail": "error_unavailable",
                                    "r_empty": "notified", "r_abroad": "filtered"})
        self.assertNotIn("https://example.com/refused", fetched)   # the board was already refused; no second try
        self.assertEqual(summary["refusing"].retry, 2)


NEEDS_TEXT = """
from jobhunter import Job, board


@board("needs_text")
class NeedsText:
    needs_description = True          # like vc_boards: the listing has no description of its own

    def __init__(self, options):
        pass

    def search(self, ctx):
        for name in ("empty", "short", "full"):
            yield Job(f"n_{name}", "Great Engineer", "Acme", "Remote - US", f"https://example.com/{name}")
"""


class TestNoDescriptionNoScore(unittest.TestCase):
    def test_a_job_whose_page_gave_no_description_is_retried_not_scored_from_its_title(self):
        tmp = Path(tempfile.mkdtemp())
        settings = ("resumes: {folder: resumes, default: backend.txt}\n"
                    "search: {queries: [swe], locations: [Remote]}\n"
                    "boards: {needs_text: {}}\nscorers: [fixed]\ndiscovery: {github_readmes: []}\n")
        path = write_project(tmp, settings, plugins={"needs_text.py": NEEDS_TEXT, "scorers.py": SCORERS})
        pages = {"empty": "", "short": "Apply now. " * 10, "full": "Build our platform in Python. " * 20}
        store, logs = Store(tmp / "data" / "jobs.db"), []
        summary = run(bootstrap(path, env={}), store, FakeNotifier(), log=logs.append,
                      fetch_page=lambda url: pages[url.rsplit("/", 1)[1]])
        with closing(sqlite3.connect(store.path)) as conn:
            statuses = dict(conn.execute("SELECT id, status FROM jobs"))
        self.assertEqual(statuses, {"n_empty": "error_unavailable", "n_short": "error_unavailable",
                                    "n_full": "notified"})
        self.assertEqual((summary["needs_text"].retry, summary["needs_text"].no_description), (2, 2))
        line = next(l for l in logs if l.startswith("[needs_text] found"))
        self.assertIn("no description 2", line)
        self.assertTrue(any(l.startswith("RETRY_LATER [needs_text]") and "no description" in l for l in logs), logs)

    def test_vc_boards_jobs_need_a_description(self):
        from jobhunter.boards.vc_boards import VcBoardsBoard
        self.assertTrue(VcBoardsBoard.needs_description)


NEEDS_TEXT_INDEED = """
from jobhunter import Job, board


@board("needs_text_indeed")
class NeedsTextIndeed:
    needs_description = True          # like vc_boards: the listing has no description of its own

    def __init__(self, options):
        pass

    def search(self, ctx):
        yield Job("n_indeed", "Great Engineer", "Acme", "Remote - US", "https://www.indeed.com/viewjob?jk=abc")
        yield Job("n_other", "Great Engineer", "Acme", "Remote - US", "https://example.com/full")
"""


class TestUnreadableHostIsFiltered(unittest.TestCase):
    def test_a_job_on_a_host_that_refuses_automated_reading_is_filtered_with_no_fetch_attempted(self):
        tmp = Path(tempfile.mkdtemp())
        settings = ("resumes: {folder: resumes, default: backend.txt}\n"
                    "search: {queries: [swe], locations: [Remote]}\n"
                    "boards: {needs_text_indeed: {}}\nscorers: [fixed]\ndiscovery: {github_readmes: []}\n")
        path = write_project(tmp, settings, plugins={"needs_text_indeed.py": NEEDS_TEXT_INDEED,
                                                       "scorers.py": SCORERS})
        fetched = []

        def fetch(url):
            fetched.append(url)
            return "Build our platform in Python. " * 20

        store = Store(tmp / "data" / "jobs.db")
        summary = run(bootstrap(path, env={}), store, FakeNotifier(), log=lambda line: None, fetch_page=fetch)
        with closing(sqlite3.connect(store.path)) as conn:
            statuses = dict(conn.execute("SELECT id, status FROM jobs"))
            reason = conn.execute("SELECT filter_reason FROM jobs WHERE id = 'n_indeed'").fetchone()[0]
        self.assertEqual(statuses, {"n_indeed": "filtered", "n_other": "notified"})
        self.assertEqual(reason, "no description: indeed.com does not allow automated reading")
        self.assertNotIn("https://www.indeed.com/viewjob?jk=abc", fetched)   # a blocked host is never fetched
        self.assertEqual(summary["needs_text_indeed"].filtered["description"], 1)


NEEDS_TEXT_ONE = """
from jobhunter import Job, board


@board("needs_text_one")
class NeedsTextOne:
    needs_description = True          # like vc_boards: the listing has no description of its own

    def __init__(self, options):
        pass

    def search(self, ctx):
        yield Job("n_one", "Great Engineer", "Acme", "Remote - US", "https://example.com/one")
"""


class TestNoDescriptionGivesUpAfterHours(unittest.TestCase):
    def test_a_job_with_no_description_is_retried_then_filtered_after_6_hours(self):
        tmp = Path(tempfile.mkdtemp())
        settings = ("resumes: {folder: resumes, default: backend.txt}\n"
                    "search: {queries: [swe], locations: [Remote]}\n"
                    "boards: {needs_text_one: {}}\nscorers: [fixed]\ndiscovery: {github_readmes: []}\n")
        path = write_project(tmp, settings, plugins={"needs_text_one.py": NEEDS_TEXT_ONE, "scorers.py": SCORERS})
        store = Store(tmp / "data" / "jobs.db")
        app = bootstrap(path, env={})

        run(app, store, FakeNotifier(), log=lambda line: None, fetch_page=lambda url: "")
        with closing(sqlite3.connect(store.path)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM jobs WHERE id = 'n_one'").fetchone()[0],
                              "error_unavailable")

        # Still inside its first NO_DESCRIPTION_HOURS: the first-run (retry) behaviour is unchanged.
        run(app, store, FakeNotifier(), log=lambda line: None, fetch_page=lambda url: "")
        with closing(sqlite3.connect(store.path)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM jobs WHERE id = 'n_one'").fetchone()[0],
                              "error_unavailable")

        seven_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
        with closing(sqlite3.connect(store.path)) as conn, conn:
            conn.execute("UPDATE jobs SET first_run_at = ? WHERE id = 'n_one'", (seven_hours_ago,))

        summary = run(app, store, FakeNotifier(), log=lambda line: None, fetch_page=lambda url: "")
        with closing(sqlite3.connect(store.path)) as conn:
            status, reason = conn.execute(
                "SELECT status, filter_reason FROM jobs WHERE id = 'n_one'").fetchone()
        self.assertEqual(status, "filtered")
        self.assertEqual(reason, "no description: the posting page gave no text after several tries")
        self.assertEqual(summary["needs_text_one"].filtered["description"], 1)

        # Final: a board listing it again does not revive it, since `filtered` is terminal.
        run(app, store, FakeNotifier(), log=lambda line: None, fetch_page=lambda url: "")
        with closing(sqlite3.connect(store.path)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM jobs WHERE id = 'n_one'").fetchone()[0], "filtered")


SKIPPING = """
from jobhunter import board
from jobhunter.errors import BoardSkipped


@board("needs_node")
class NeedsNode:
    default_timeout_s = 77

    def __init__(self, options):
        pass

    def search(self, ctx):
        raise BoardSkipped("needs Node.js (npx); install it or leave needs_node out of boards")
"""


class TestLeanBoards(unittest.TestCase):
    def bootstrap(self, boards):
        tmp = Path(tempfile.mkdtemp())
        settings = ("resumes: {folder: resumes, default: backend.txt}\n"
                    "search: {queries: [swe], locations: [Remote], fetch_descriptions: false}\n"
                    f"boards: {boards}\nscorers: [fixed]\ndiscovery: {{github_readmes: []}}\n")
        return tmp, bootstrap(write_project(tmp, settings, plugins={"skip.py": SKIPPING, "scorers.py": SCORERS,
                                                                     "boards.py": BOARDS}), env={})

    def test_board_timeouts_default_per_board(self):
        _, app = self.bootstrap("{needs_node: {}, fake: {}, greenhouse: {}, linkedin: {}, dice: {timeout_s: 5}}")
        timeouts = {b.name: b.timeout_s for b in app.settings.boards}
        self.assertEqual(timeouts, {"needs_node": 77, "fake": 600, "greenhouse": 1800, "linkedin": 1200, "dice": 5})

    def test_a_skipped_board_is_not_a_failure(self):
        tmp, app = self.bootstrap("[needs_node]")
        logs = []
        summary = run(app, Store(tmp / "data" / "jobs.db"), FakeNotifier(), log=logs.append)
        self.assertIsNone(summary["needs_node"].failed)
        self.assertIn("needs Node.js", summary["needs_node"].skipped_reason)
        self.assertTrue(any(line.startswith("[needs_node] skipped: needs Node.js") for line in logs), logs)


PROBLEM = """
from jobhunter import Job, board


@board("half_read")
class HalfRead:
    def __init__(self, options):
        self.problem = None

    def search(self, ctx):
        self.problem = "consider [jobs.a16z.com]: no job list on the page"
        yield Job("half_1", "Great Engineer", "Acme", "Remote - US", "https://example.com/h1", description="Java")
"""


class TestBoardProblems(unittest.TestCase):
    def test_a_problem_a_board_reports_shows_in_the_summary_and_its_jobs_still_count(self):
        tmp = Path(tempfile.mkdtemp())
        settings = ("resumes: {folder: resumes, default: backend.txt}\n"
                    "search: {queries: [swe], locations: [Remote], fetch_descriptions: false}\n"
                    "boards: [half_read]\nscorers: [fixed]\ndiscovery: {github_readmes: []}\n"
                    "decisions: {notify_at: 7, log_at: 5}\n")
        app = bootstrap(write_project(tmp, settings, plugins={"problem.py": PROBLEM, "scorers.py": SCORERS}), env={})
        logs = []
        summary = run(app, Store(tmp / "data" / "jobs.db"), FakeNotifier(), log=logs.append)
        self.assertEqual(summary["half_read"].problem, "consider [jobs.a16z.com]: no job list on the page")
        self.assertEqual(summary["half_read"].notified, 1)
        line = next(l for l in logs if l.startswith("[half_read]"))
        self.assertIn("notified 1", line)
        self.assertIn("PROBLEM: consider [jobs.a16z.com]", line)


GARBLED = """
from jobhunter import Job, board

RIGHT = "Senior Platform Engineer \\u2014 Google Cloud"


@board("garbled")
class Garbled:
    def __init__(self, options):
        pass

    def search(self, ctx):
        yield Job("g_1", RIGHT.encode("utf-8").decode("latin-1"), "R&amp;D Labs", "Remote - US", "https://example.com/g",
                  description="Java work " * 30)
"""


class TestTextRepair(unittest.TestCase):
    def test_garbled_board_text_is_repaired_before_saving(self):
        tmp = Path(tempfile.mkdtemp())
        settings = ("resumes: {folder: resumes, default: backend.txt}\n"
                    "search: {queries: [swe], locations: [Remote], fetch_descriptions: false}\n"
                    "boards: {garbled: {}}\nscorers: [fixed]\ndiscovery: {github_readmes: []}\n")
        path = write_project(tmp, settings, plugins={"garbled.py": GARBLED, "scorers.py": SCORERS})
        store = Store(tmp / "data" / "jobs.db")
        run(bootstrap(path, env={}), store, FakeNotifier(), log=lambda line: None)
        with closing(sqlite3.connect(store.path)) as conn:
            self.assertEqual(conn.execute("SELECT title, company FROM jobs WHERE id = 'g_1'").fetchone(),
                             ("Senior Platform Engineer \u2014 Google Cloud", "R&D Labs"))


ODD = """
from jobhunter import Job, board


@board("odd")
class Odd:
    def __init__(self, options):
        pass

    def search(self, ctx):
        yield Job("o_1", 123, ["not", "text"], "Remote - US", "https://example.com/o1", description="Java " * 50)
        yield Job("o_2", "Great Engineer", "Acme", "Remote - US", "https://example.com/o2", description="Java " * 50)
"""


class TestOddFields(unittest.TestCase):
    def test_a_listing_with_odd_fields_does_not_stop_the_run(self):
        tmp = Path(tempfile.mkdtemp())
        settings = ("resumes: {folder: resumes, default: backend.txt}\n"
                    "search: {queries: [swe], locations: [Remote], fetch_descriptions: false}\n"
                    "boards: {odd: {}}\nscorers: [fixed]\ndiscovery: {github_readmes: []}\n")
        path = write_project(tmp, settings, plugins={"odd.py": ODD, "scorers.py": SCORERS})
        store = Store(tmp / "data" / "jobs.db")
        run(bootstrap(path, env={}), store, FakeNotifier(), log=lambda line: None)
        with closing(sqlite3.connect(store.path)) as conn:
            self.assertEqual(conn.execute("SELECT id, title FROM jobs ORDER BY id").fetchall(),
                             [("o_1", "123"), ("o_2", "Great Engineer")])


ONCE = """
from jobhunter import Job, board

CALLS = []


@board("once")
class Once:
    # LinkedIn lists a job for about an hour; this board lists it only on its first search.
    def __init__(self, options):
        pass

    def search(self, ctx):
        CALLS.append("search")
        if len(CALLS) == 1:
            yield Job("once_1", "Great Engineer", "Acme", "Remote - US", "https://example.com/once")

    def enrich(self, job, ctx):
        CALLS.append("enrich")
        if CALLS.count("enrich") == 1:
            job.extra["description_refused"] = True
        else:
            job.description, job.description_is_snippet = "Java services " * 30, False
        return job
"""


class TestRetryFromStore(unittest.TestCase):
    def test_a_refused_job_is_scored_on_a_later_run_even_when_no_longer_listed(self):
        tmp = Path(tempfile.mkdtemp())
        settings = ("resumes: {folder: resumes, default: backend.txt}\n"
                    "search: {queries: [swe], locations: [Remote], fetch_descriptions: false}\n"
                    "boards: {once: {}}\nscorers: [fixed]\ndiscovery: {github_readmes: []}\n")
        path = write_project(tmp, settings, plugins={"once.py": ONCE, "scorers.py": SCORERS})
        store = Store(tmp / "data" / "jobs.db")
        app = bootstrap(path, env={})
        first = run(app, store, FakeNotifier(), log=lambda line: None)
        notifier = FakeNotifier()
        second = run(app, store, notifier, log=lambda line: None)
        self.assertEqual((first["once"].retry, second["once"].notified), (1, 1))
        self.assertEqual([sent[0] for sent in notifier.sent], ["once_1"])


REPEATS = """
from jobhunter import Job, board


@board("repeats")
class Repeats:
    def __init__(self, options):
        pass

    def search(self, ctx):
        text = "Java services " * 30
        # The same role under two ids (Dice listed "NodeJS AI Engineer @ Randstad" twice in one run).
        yield Job("r_1", "Great NodeJS AI Engineer", "Randstad Digital", "Remote - US", "https://example.com/1", description=text)
        yield Job("r_2", "Great NodeJS AI  Engineer", "RANDSTAD DIGITAL", "Austin, TX", "https://example.com/2", description=text)
        yield Job("r_3", "Great Data Engineer 4", "Capital One", "Remote - US", "https://example.com/3", description=text)
        yield Job("r_4", "Great Backend Engineer", "Acme", "Remote - US", "https://example.com/4", description=text)
        yield Job("r_5", "Maybe Engineer", "Randstad Digital", "Remote - US", "https://example.com/5", description=text)
        yield Job("r_6", "Maybe Engineer", "Randstad Digital", "Remote - US", "https://example.com/6", description=text)
"""


class TestAlertOncePerJob(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        settings = ("resumes: {folder: resumes, default: backend.txt}\n"
                    "search: {queries: [swe], locations: [Remote], fetch_descriptions: false}\n"
                    "boards: {repeats: {}}\nscorers: [fixed]\ndiscovery: {github_readmes: []}\n")
        path = write_project(self.tmp, settings, plugins={"repeats.py": REPEATS, "scorers.py": SCORERS})
        self.app = bootstrap(path, env={})
        self.store = Store(self.tmp / "data" / "jobs.db")

    def earlier_notification(self, title, company, days_ago):
        self.store.save(Job(f"old_{days_ago}", title, company, "Remote - US", "https://example.com/old"), "notified")
        when = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        with closing(sqlite3.connect(self.store.path)) as conn:
            conn.execute("UPDATE jobs SET run_at = ? WHERE id = ?", (when, f"old_{days_ago}"))
            conn.commit()

    def statuses(self):
        with closing(sqlite3.connect(self.store.path)) as conn:
            return dict(conn.execute("SELECT id, status FROM jobs WHERE id LIKE 'r_%'"))

    def test_the_same_job_twice_in_one_run_is_notified_once(self):
        notifier = FakeNotifier()
        summary = run(self.app, self.store, notifier, log=lambda line: None)
        self.assertEqual([sent[0] for sent in notifier.sent], ["r_1", "r_3", "r_4"])
        statuses = self.statuses()
        self.assertEqual((statuses["r_2"], statuses["r_5"], statuses["r_6"]), ("duplicate", "logged", "logged"))
        self.assertEqual((summary["repeats"].notified, summary["repeats"].duplicates), (3, 1))

    def test_a_job_notified_in_the_last_30_days_is_not_notified_again(self):
        self.earlier_notification("Great Data Engineer 4", "Capital One", days_ago=10)
        self.earlier_notification("Great Backend Engineer", "Acme", days_ago=40)       # long enough ago
        notifier = FakeNotifier()
        run(self.app, self.store, notifier, log=lambda line: None)
        self.assertEqual([sent[0] for sent in notifier.sent], ["r_1", "r_4"])
        self.assertEqual(self.statuses()["r_3"], "duplicate")
        self.assertTrue(self.store.is_terminal("r_3"))

    def test_a_dry_run_reports_duplicates_and_saves_nothing(self):
        logs = []
        summary = run(self.app, self.store, FakeNotifier(), log=logs.append, dry_run=True)
        self.assertEqual(summary["repeats"].duplicates, 1)
        self.assertTrue(any(line.startswith("DUPLICATE [repeats] Great NodeJS AI  Engineer") for line in logs), logs)
        self.assertEqual(self.statuses(), {})

class FeedbackLine:
    def line(self):
        return "feedback: 1 saved (1 good)"


class TestFeedbackHook(PipelineTestCase):
    def test_feedback_is_read_before_searching_and_logged(self):
        seen = []

        def feedback():
            seen.append((list(self.logs), dict(self.rows())))   # nothing searched or saved yet
            return FeedbackLine()
        _, notifier = self.run_once(feedback=feedback)
        self.assertEqual(seen, [([], {})])
        self.assertEqual(self.logs[0], "feedback: 1 saved (1 good)")
        self.assertTrue(notifier.sent)

    def test_a_failing_feedback_reader_does_not_stop_the_run(self):
        def boom():
            raise RuntimeError("imap down")
        _, notifier = self.run_once(feedback=boom)
        self.assertEqual(self.logs[0], "feedback: could not read the inbox (RuntimeError)")
        self.assertTrue(notifier.sent)

    def test_dry_run_never_reads_feedback(self):
        called = []
        self.run_once(feedback=lambda: called.append(1), dry_run=True)
        self.assertEqual(called, [])


DISCOVERING = """
from jobhunter import board

SEEN = []


@board("look")
class Look:
    def __init__(self, options):
        pass

    def search(self, ctx):
        SEEN.append(ctx.discovering)
        return []
"""


class TestDiscoveringBoards(unittest.TestCase):
    def run_with(self, boards, only=None):
        tmp = Path(tempfile.mkdtemp())
        settings = ("resumes: {folder: resumes, default: backend.txt}\n"
                    "search: {queries: [swe], locations: [Remote], fetch_descriptions: false}\n"
                    f"boards: {boards}\nscorers: [fixed]\ndiscovery: {{github_readmes: []}}\n")
        path = write_project(tmp, settings, plugins={"look.py": DISCOVERING, "scorers.py": SCORERS})
        seen = sys.modules.get("jobhunter_plugins.look")
        if seen is not None:
            seen.SEEN.clear()
        run(bootstrap(path, env={}), Store(tmp / "data" / "jobs.db"), FakeNotifier(), log=lambda line: None,
            only=only, http=NoHttp())
        return sys.modules["jobhunter_plugins.look"].SEEN[0]

    def test_boards_learn_which_ats_boards_read_discovered_links_this_run(self):
        self.assertEqual(self.run_with("{look: {}, greenhouse: {companies: [acme]}, lever: {}, workday: {discover: false}}"),
                         {"lever"})
        self.assertEqual(self.run_with("{look: {}, lever: {}, ashby: {}}", only={"look", "ashby"}), {"ashby"})


class NoHttp:
    def request(self, method, url, **kw):
        raise RuntimeError("no network in tests")


class TestVcCachePath(PipelineTestCase):
    def test_dry_run_gives_discovery_no_cache_path_and_a_real_run_does(self):
        from unittest.mock import patch
        import jobhunter.pipeline as pipeline
        seen = []
        real = pipeline.Discovery

        def record(*a, **kw):
            seen.append(kw.get("cache_path"))
            return real(*a, **kw)
        with patch.object(pipeline, "Discovery", side_effect=record):
            self.run_once(dry_run=True)
            self.run_once()
        self.assertIsNone(seen[0])
        self.assertEqual(seen[1].name, "vc_listings.json")


if __name__ == "__main__":
    unittest.main()


class TestScorerErrorsAreRetriedForADay(PipelineTestCase):
    def test_a_job_every_scorer_failed_on_is_retried_then_final_after_24_hours(self):
        from jobhunter.errors import ScorerError
        from jobhunter.scorers.chain import ScorerChain

        class Garbled:
            def score(self, job, resume):
                raise ScorerError("no JSON object in the model's reply")
        self.app.chain = ScorerChain([("garbled", Garbled())], self.app.settings.decisions)
        self.run_once()
        status = {k: v[0] for k, v in self.rows().items()}
        self.assertEqual(status["fake_0"], "error_scorer")
        self.assertIn("fake_0", {j.id for j in self.store.retry_candidates()})
        self.run_once()
        self.assertEqual(self.rows()["fake_0"][0], "error_scorer")          # still inside its 24 hours
        day_ago = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        with closing(sqlite3.connect(self.store.path)) as conn, conn:
            conn.execute("UPDATE jobs SET first_run_at = ? WHERE id = 'fake_0'", (day_ago,))
        self.run_once()
        self.assertEqual(self.rows()["fake_0"][0], "error_terminal")
        self.run_once()
        self.assertEqual(self.rows()["fake_0"][0], "error_terminal")        # final: never scored again


class FailingNotifier(FakeNotifier):
    def send(self, job, result, resume_id):
        super().send(job, result, resume_id)
        return False


class TestUndeliveredAlerts(PipelineTestCase):
    def test_an_alert_no_channel_delivered_is_tried_again_next_run(self):
        summary = run(self.app, self.store, FailingNotifier(), log=self.logs.append)
        status = {k: v[0] for k, v in self.rows().items()}
        self.assertEqual((status["fake_0"], status["fake_enrich"]), ("error_notify", "error_notify"))
        self.assertEqual(sum(s.retry for s in summary.values()), 4)   # two alerts, the 429 job, the crashed one
        self.assertTrue(any(line.startswith("NOTIFY_FAILED") for line in self.logs))
        self.assertLessEqual({"fake_0", "fake_enrich"}, {j.id for j in self.store.retry_candidates()})
        _, notifier = self.run_once()
        status = {k: v[0] for k, v in self.rows().items()}
        self.assertEqual((status["fake_0"], status["fake_enrich"]), ("notified", "notified"))
        self.assertEqual(sorted(s[0] for s in notifier.sent), ["fake_0", "fake_enrich"])
