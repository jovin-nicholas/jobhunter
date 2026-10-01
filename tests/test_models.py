import unittest

from jobhunter.models import KEEP, Job, decide, skip


class TestDecide(unittest.TestCase):
    def test_default_thresholds(self):
        self.assertEqual([decide(s, 7, 5) for s in (10, 7, 6, 5, 4, 1)],
                         ["notify", "notify", "log", "log", "skip", "skip"])

    def test_custom_thresholds(self):
        self.assertEqual([decide(s, 9, 3) for s in (9, 8, 3, 2)], ["notify", "log", "log", "skip"])


class TestJob(unittest.TestCase):
    def test_extra_is_not_shared_between_jobs(self):
        a = Job("a", "t", "c", "l", "u")
        b = Job("b", "t", "c", "l", "u")
        a.extra["guid"] = "x"
        self.assertEqual(b.extra, {})


class TestFilterResults(unittest.TestCase):
    def test_keep_and_skip(self):
        self.assertTrue(KEEP.keep)
        result = skip("too senior")
        self.assertEqual((result.keep, result.reason), (False, "too senior"))


if __name__ == "__main__":
    unittest.main()
