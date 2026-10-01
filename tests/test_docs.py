import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class TestReadme(unittest.TestCase):
    def test_fixed_limitations_are_not_listed_as_known(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        known = text[text.index("## Known limitations"):text.index("## Tests")]
        for fixed in ("404", "applyManually", "malformed listing", "rate limited", "Latin-1"):
            self.assertNotIn(fixed, known)

    def test_every_documented_file_exists(self):
        for path in ("docs/settings.md", "examples/full.yaml", "run_cron.sh", "scheduler.py"):
            self.assertTrue((ROOT / path).exists(), path)


class TestExample(unittest.TestCase):
    def test_commented_options_are_examples_not_claimed_defaults(self):
        text = (ROOT / "jobhunter.example.yaml").read_text(encoding="utf-8")
        self.assertNotIn("the defaults are shown", text)
        self.assertIn("default: all 12 built-in boards", text)
        self.assertIn("default: every 1h, no quiet time", text)


if __name__ == "__main__":
    unittest.main()
