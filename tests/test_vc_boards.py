import unittest

from jobhunter.models import SearchContext
from jobhunter.vc_sources import VcListing


class Disc:
    def __init__(self, listings):
        self._l = listings

    def listings(self):
        return list(self._l)


def listing(key, url, source="Redpoint"):
    return VcListing(key, source, "Platform Engineer", "Beta", "SF", url, "2026-10-06T00:00:00+00:00")


class TestVcBoards(unittest.TestCase):
    def search(self, listings, known=(), discovery=True, discovering=None, log=lambda line: None):
        from jobhunter.boards.vc_boards import VcBoardsBoard
        ctx = SearchContext(["swe"], ["US"], 72, None, log, discovery=Disc(listings) if discovery else None,
                            is_known=lambda job_id: job_id in known, discovering=discovering)
        return list(VcBoardsBoard({}).search(ctx))

    def test_only_non_ats_listings_become_jobs(self):
        jobs = self.search([listing("getro_1", "https://boards.greenhouse.io/acme/jobs/1"),
                            listing("getro_2", "https://beta.example/careers/platform"),
                            listing("consider_x", "https://gamma.example/jobs/9", source="a16z")])
        self.assertEqual([j.id for j in jobs], ["getro_2", "consider_x"])
        j = jobs[0]
        self.assertEqual((j.title, j.company, j.location, j.url, j.source, j.description),
                         ("Platform Engineer", "Beta", "SF", "https://beta.example/careers/platform", "vc_boards", ""))

    def test_a_vc_source_that_could_not_be_read_is_the_boards_problem(self):
        from jobhunter.boards.vc_boards import VcBoardsBoard
        disc = Disc([listing("getro_2", "https://beta.example/careers/platform")])
        disc.failures = ["consider [jobs.a16z.com]: no job list on the page; its page format may have changed"]
        board = VcBoardsBoard({})
        ctx = SearchContext(["swe"], ["US"], 72, None, lambda line: None, discovery=disc, is_known=lambda i: False)
        self.assertEqual([j.id for j in board.search(ctx)], ["getro_2"])
        self.assertIn("jobs.a16z.com", board.problem)
        disc.failures = []
        list(board.search(ctx))
        self.assertIsNone(board.problem)

    def test_an_unknown_location_is_left_empty_not_called_remote(self):
        blank = VcListing("getro_9", "Redpoint", "Platform Engineer", "Beta", "", "https://beta.example/careers/9",
                          "2026-10-06T00:00:00+00:00")
        self.assertEqual([j.location for j in self.search([blank])], [""])

    def test_ats_links_stay_here_when_their_board_is_not_discovering(self):
        logs = []
        jobs = self.search([listing("getro_1", "https://boards.greenhouse.io/acme/jobs/1"),
                            listing("getro_2", "https://acme.wd5.myworkdayjobs.com/en-US/External/job/X_R1"),
                            listing("getro_3", "https://jobs.lever.co/acme/1"),
                            listing("getro_4", "https://beta.example/careers/platform")],
                           discovering={"lever"}, log=logs.append)
        self.assertEqual([(j.id, j.ats) for j in jobs], [("getro_1", "greenhouse"), ("getro_2", "workday"),
                                                         ("getro_4", "")])
        self.assertIn("vc_boards: 1 link(s) handed to ATS boards (lever 1), 3 kept here", logs)

    def test_the_same_posting_from_two_vc_boards_is_one_job(self):
        jobs = self.search([listing("getro_1", "https://Beta.example/careers/platform/"),
                            listing("consider_x", "https://beta.example/careers/platform#apply", source="a16z"),
                            listing("getro_1", "https://beta.example/other"),
                            listing("getro_5", "https://beta.example/careers/platform?id=2")])
        self.assertEqual([j.id for j in jobs], ["getro_1", "getro_5"])

    def test_known_jobs_and_no_discovery(self):
        self.assertEqual(self.search([listing("getro_2", "https://beta.example/c")], known={"getro_2"}), [])
        self.assertEqual(self.search([], discovery=False), [])
