import unittest
from urllib.parse import parse_qs, unquote, urlsplit

from jobhunter.feedback import BUTTONS, VERDICTS, mailto, parse_subject, plus_address
from jobhunter.models import Job


def job(job_id="greenhouse_123", title="Backend Engineer", company="Acme"):
    return Job(job_id, title, company, "Austin, TX", "https://example.com/1", source="greenhouse")


class TestVerdicts(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(VERDICTS, {"applied": "notify", "good": "notify", "maybe": "log", "bad": "skip"})
        self.assertEqual([v for v, _ in BUTTONS], ["applied", "good", "maybe", "bad"])


class TestPlusAddress(unittest.TestCase):
    def test_adds_and_replaces_a_tag(self):
        self.assertEqual(plus_address("sender@example.com", "jobhunter"), "sender+jobhunter@example.com")
        self.assertEqual(plus_address("sender+old@example.com", "jobhunter"), "sender+jobhunter@example.com")


class TestMailto(unittest.TestCase):
    def test_round_trip_keeps_odd_ids(self):
        for job_id in ("greenhouse_123", "workday_Mobile-Robots_JR-0088334-1", "linkedin_a/b.c", "x_has:colon",
                       "dice_a&b?c#d"):
            with self.subTest(job_id=job_id):
                link = mailto("sender+jobhunter-feedback@example.com", "good", job(job_id))
                parts = urlsplit(link)
                self.assertEqual(parts.scheme, "mailto")
                self.assertEqual(unquote(parts.path), "sender+jobhunter-feedback@example.com")
                query = parse_qs(parts.query)
                self.assertEqual(parse_subject(query["subject"][0]), ("good", job_id))
                self.assertIn("Backend Engineer at Acme", query["body"][0])

    def test_the_plus_in_the_address_is_percent_encoded(self):
        link = mailto("sender+jobhunter-feedback@example.com", "good", job())
        self.assertTrue(link.startswith("mailto:sender%2Bjobhunter-feedback@example.com?"), link)

    def test_unknown_verdict_is_refused(self):
        with self.assertRaises(ValueError):
            mailto("a@example.com", "great", job())


class TestParseSubject(unittest.TestCase):
    def test_accepts_prefixes_and_spaces(self):
        self.assertEqual(parse_subject("  jh:bad:dice_1  "), ("bad", "dice_1"))
        self.assertEqual(parse_subject("Re: jh:maybe:dice_1"), ("maybe", "dice_1"))
        self.assertEqual(parse_subject("Fwd: RE: jh:applied:dice_1"), ("applied", "dice_1"))

    def test_rejects_malformed(self):
        for subject in ("", "hello", "jh:great:dice_1", "jh:good:", "jh:good", "xx:good:dice_1", None):
            with self.subTest(subject=subject):
                self.assertIsNone(parse_subject(subject))


if __name__ == "__main__":
    unittest.main()
