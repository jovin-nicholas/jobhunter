import tempfile
import unittest
from pathlib import Path

from jobhunter.errors import SettingsError
from jobhunter.settings import DEFAULT_BOARDS, DEFAULT_GITHUB_READMES, load_settings
from tests.helpers import RESUME_TEXT, write_project

VALID = """
resumes:
  folder: resumes/
  default: backend.txt
  versions:
    fullstack.txt: {keywords: [react, node]}
search:
  queries: [software engineer]
  locations: [United States]
boards:
  dice: {}
  greenhouse: {companies: [acme], timeout_s: 30}
  linkedin: {enabled: false}
filters:
  location: {countries: [US]}
  seniority: {levels: [intern, entry], max_years_required: 3}
  keywords:
    exclude_title: [manager]
    exclude_companies: [spam staffing]
    exclude_roles:
      embedded: {terms: [c++, rtos], min_matches: 2}
scorers:
  - laya: {model: ~/models/laya}
  - ollama: {model: "gemma4:e4b"}
decisions: {notify_at: 7, log_at: 5}
notify:
  slack: {webhook_env: SLACK_WEBHOOK_URL}
"""

RESUMES = {"backend.txt": RESUME_TEXT, "fullstack.txt": RESUME_TEXT}


class TestLoadSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()

    def test_valid_file_parses_into_typed_settings(self):
        path = write_project(self.tmp, VALID, RESUMES)
        s = load_settings(path, env={"SLACK_WEBHOOK_URL": "https://hooks.example/x"})
        self.assertEqual(s.resumes.folder, (self.tmp / "resumes").resolve())
        self.assertEqual(s.resumes.versions, {"fullstack.txt": ["react", "node"]})
        self.assertEqual([(b.name, b.enabled, b.timeout_s, b.options) for b in s.boards],
                         [("dice", True, None, {}), ("greenhouse", True, 30, {"companies": ["acme"]}),
                          ("linkedin", False, None, {})])     # None: the board's own default, filled in at start
        self.assertEqual(s.filters.seniority.levels, ["intern", "entry"])
        self.assertEqual(s.filters.keywords.exclude_roles["embedded"].min_matches, 2)
        self.assertEqual(s.filters.keywords.exclude_companies, ["spam staffing"])
        self.assertEqual([(x.name, x.options) for x in s.scorers],
                         [("laya", {"model": "~/models/laya"}), ("ollama", {"model": "gemma4:e4b"})])
        self.assertEqual((s.decisions.notify_at, s.decisions.log_at), (7, 5))
        self.assertEqual(s.notify.slack, {"webhook_env": "SLACK_WEBHOOK_URL"})
        self.assertEqual((s.data_dir, s.plugins_dir), (self.tmp / "data", self.tmp / "plugins"))

    def test_every_problem_is_reported_together(self):
        bad = (VALID.replace("filters:", "filter:\n  x: 1\nfilters:")
               .replace("levels: [intern, entry]", "levels: [intern, junior]")
               .replace("min_matches: 2", "min_matches: 0")
               .replace("log_at: 5", "log_at: 8")
               .replace("fullstack.txt: {keywords", "ghost.txt: {keywords"))
        path = write_project(self.tmp, bad, RESUMES)
        with self.assertRaises(SettingsError) as caught:
            load_settings(path, env={})
        problems = caught.exception.problems
        expected = ["filter: unknown key", "unknown level 'junior'", "min_matches: must be at least 1",
                    "log_at (8) must not be above notify_at (7)", "ghost.txt", "SLACK_WEBHOOK_URL is not set"]
        for fragment in expected:
            with self.subTest(fragment=fragment):
                self.assertTrue(any(fragment in p for p in problems), problems)
        self.assertEqual(len(problems), len(expected), problems)
        self.assertTrue(all(p.startswith("jobhunter.yaml: ") for p in problems))

    def test_invalid_yaml_and_missing_file(self):
        with self.assertRaises(SettingsError) as missing:
            load_settings(self.tmp / "nope.yaml")
        self.assertIn("settings file not found", missing.exception.problems[0])
        path = write_project(self.tmp, "resumes: [unclosed\n", RESUMES)
        with self.assertRaises(SettingsError) as broken:
            load_settings(path)
        self.assertIn("not valid YAML", broken.exception.problems[0])


class TestPlan2Settings(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()

    def load(self, text, env=None):
        path = write_project(self.tmp, text, RESUMES)
        return load_settings(path, env={"SLACK_WEBHOOK_URL": "x", **(env or {})})

    def test_min_confidence(self):
        self.assertIsNone(self.load(VALID).decisions.min_confidence)
        s = self.load(VALID.replace("decisions: {notify_at: 7, log_at: 5}",
                                    "decisions: {notify_at: 8, log_at: 5, min_confidence: 0.7}"))
        self.assertEqual((s.decisions.notify_at, s.decisions.min_confidence), (8, 0.7))
        for bad in ("1.5", "high", "-0.1"):
            with self.assertRaises(SettingsError) as err:
                self.load(VALID.replace("decisions: {notify_at: 7, log_at: 5}", f"decisions: {{min_confidence: {bad}}}"))
            self.assertIn("decisions.min_confidence", "\n".join(err.exception.problems))

    def test_defaults(self):
        s = self.load(VALID)
        self.assertTrue(s.search.fetch_descriptions)
        self.assertEqual(s.discovery.github_readmes, DEFAULT_GITHUB_READMES)
        self.assertIsNone(s.discovery.google)
        self.assertEqual((s.cover_letters.enabled, s.cover_letters.writer), (False, None))

    def test_discovery_cover_letters_and_fetch_flag_parse(self):
        s = self.load(VALID.replace("  locations: [United States]\n",
                                    "  locations: [United States]\n  fetch_descriptions: false\n")
                      + "discovery:\n  github_readmes: [https://example.com/README.md]\n"
                        "  google: {api_key_env: GOOGLE_API_KEY, cx_env: GOOGLE_CX}\n"
                        "cover_letters: {enabled: true, writer: ollama}\n",
                      env={"GOOGLE_API_KEY": "k", "GOOGLE_CX": "c"})
        self.assertFalse(s.search.fetch_descriptions)
        self.assertEqual(s.discovery.github_readmes, ["https://example.com/README.md"])
        self.assertEqual(s.discovery.google, {"api_key_env": "GOOGLE_API_KEY", "cx_env": "GOOGLE_CX"})
        self.assertEqual((s.cover_letters.enabled, s.cover_letters.writer), (True, "ollama"))

    def test_empty_readme_list_turns_github_discovery_off(self):
        self.assertEqual(self.load(VALID + "discovery: {github_readmes: []}\n").discovery.github_readmes, [])

    def test_problems_are_reported_together(self):
        with self.assertRaises(SettingsError) as err:
            self.load(VALID.replace("  locations: [United States]\n",
                                    "  locations: [United States]\n  fetch_descriptions: 1\n")
                      + "discovery: {google: {api_key_env: GOOGLE_API_KEY}, extra: 1}\n"
                        "cover_letters: {enabled: maybe, writer: gemini}\n")
        text = "\n".join(err.exception.problems)
        for expected in ("search.fetch_descriptions: expected true or false",
                         "discovery.extra: unknown key",
                         "discovery.google: missing cx_env",
                         "discovery.google.api_key_env: environment variable GOOGLE_API_KEY is not set",
                         "cover_letters.enabled: expected true or false",
                         "cover_letters.writer: 'gemini' is not listed under scorers"):
            self.assertIn(expected, text)

    def test_enabled_cover_letters_need_a_writer(self):
        with self.assertRaises(SettingsError) as err:
            self.load(VALID + "cover_letters: {enabled: true}\n")
        self.assertIn("cover_letters.writer: missing", "\n".join(err.exception.problems))


class TestLeanSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()

    def load(self, text):
        return load_settings(write_project(self.tmp, text, RESUMES), env={"SLACK_WEBHOOK_URL": "x"})

    def without_boards(self):
        start = VALID.index("boards:")
        return VALID[:start] + VALID[VALID.index("filters:"):]

    def test_left_out_boards_are_the_twelve_built_ins(self):
        boards = self.load(self.without_boards()).boards
        self.assertEqual([b.name for b in boards], list(DEFAULT_BOARDS))
        self.assertTrue(all(b.enabled and b.options == {} and b.timeout_s is None for b in boards))

    def test_a_list_of_names(self):
        text = self.without_boards().replace("filters:", "boards: [dice, greenhouse]\nfilters:")
        self.assertEqual([(b.name, b.options) for b in self.load(text).boards], [("dice", {}), ("greenhouse", {})])

    def test_mapping_style_files_are_unchanged(self):
        boards = {b.name: b for b in self.load(VALID).boards}
        self.assertEqual((boards["greenhouse"].options, boards["greenhouse"].timeout_s), ({"companies": ["acme"]}, 30))
        self.assertFalse(boards["linkedin"].enabled)
        self.assertIsNone(boards["dice"].timeout_s)

    def test_missing_sections_ask_only_for_their_required_keys(self):
        text = "scorers: [ollama]\n"
        with self.assertRaises(SettingsError) as err:
            load_settings(write_project(self.tmp, text, RESUMES), env={})
        problems = "\n".join(err.exception.problems)
        self.assertIn("resumes: missing: needs `default`", problems)
        self.assertIn("search: missing: needs `queries`", problems)
        self.assertNotIn("`folder`", problems)
        self.assertNotIn("`locations`", problems)

    def test_bad_board_lists(self):
        for text, message in (("boards: []\n", "no boards listed"), ("boards: [dice, 3]\n", "board names")):
            with self.assertRaises(SettingsError) as err:
                self.load(self.without_boards().replace("filters:", text + "filters:"))
            self.assertIn(message, "\n".join(err.exception.problems))


if __name__ == "__main__":
    unittest.main()


class TestNotifyEmail(unittest.TestCase):
    ENV = {"SLACK_WEBHOOK_URL": "https://hooks.example/x", "GMAIL_ADDRESS": "sender@example.com",
           "GMAIL_APP_PASSWORD": "app-pass"}

    def load(self, email):
        text = VALID.replace("notify:\n  slack: {webhook_env: SLACK_WEBHOOK_URL}\n", f"notify:\n  email: {email}\n")
        return load_settings(write_project(Path(tempfile.mkdtemp()).resolve(), text, RESUMES), env=self.ENV)

    def problems(self, email):
        with self.assertRaises(SettingsError) as e:
            self.load(email)
        return e.exception.problems

    def test_to_env_is_optional_and_tags_are_kept(self):
        s = self.load("{from_env: GMAIL_ADDRESS, password_env: GMAIL_APP_PASSWORD, alert_tag: jobs, feedback_tag: jobs-fb}")
        self.assertEqual(s.notify.email["alert_tag"], "jobs")
        self.assertNotIn("to_env", s.notify.email)

    def test_bad_tags_are_reported(self):
        for tags, problem in (("alert_tag: 'a b'", "alert_tag"), ("alert_tag: x, feedback_tag: x", "must differ"),
                              ("feedback_tag: 'a+b'", "feedback_tag"), ("alert_tag: jobhunter-feedback", "must differ")):
            with self.subTest(tags=tags):
                found = self.problems(f"{{from_env: GMAIL_ADDRESS, password_env: GMAIL_APP_PASSWORD, {tags}}}")
                self.assertTrue(any(problem in p for p in found), found)

    def test_from_and_password_stay_required(self):
        found = self.problems("{from_env: GMAIL_ADDRESS}")
        self.assertTrue(any("missing password_env" in p for p in found), found)


class TestVcDiscoverySettings(unittest.TestCase):
    def load(self, discovery):
        text = VALID + f"discovery:\n{discovery}"
        return load_settings(write_project(Path(tempfile.mkdtemp()).resolve(), text, RESUMES),
                             env={"SLACK_WEBHOOK_URL": "https://hooks.example/x"})

    def problems(self, discovery):
        with self.assertRaises(SettingsError) as e:
            self.load(discovery)
        return e.exception.problems

    def test_getro_and_consider_parse_with_defaults(self):
        s = self.load("  getro: {collections: {189: Redpoint, 1124: Primary}, locations: [United States]}\n"
                      "  consider: {boards: {jobs.a16z.com: a16z}}\n")
        self.assertEqual(s.discovery.getro, {"collections": {189: "Redpoint", 1124: "Primary"},
                                             "job_functions": ["Software Engineering"], "locations": ["United States"],
                                             "seniority": [], "max_pages": 10})
        self.assertEqual(s.discovery.consider, {"boards": {"jobs.a16z.com": "a16z"}, "roles": ["software-engineer"]})

    def test_hyde_park_read_twice_is_a_note_not_an_error(self):
        from jobhunter.settings import settings_notes
        text = VALID.replace("  dice: {}\n", "  dice: {}\n  hydepark: {}\n")
        path = write_project(Path(tempfile.mkdtemp()).resolve(),
                             text + "discovery:\n  getro: {collections: {112: Hyde Park, 189: Redpoint}}\n", RESUMES)
        s = load_settings(path, env={"SLACK_WEBHOOK_URL": "https://hooks.example/x"})
        notes = settings_notes(s)
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("112", notes[0])
        self.assertIn("hydepark", notes[0])
        self.assertEqual(settings_notes(self.load("  getro: {collections: {189: Redpoint}}\n")), [])
        off = text.replace("  hydepark: {}\n", "  hydepark: {enabled: false}\n")
        path = write_project(Path(tempfile.mkdtemp()).resolve(),
                             off + "discovery:\n  getro: {collections: {112: Hyde Park}}\n", RESUMES)
        self.assertEqual(settings_notes(load_settings(path, env={"SLACK_WEBHOOK_URL": "https://hooks.example/x"})), [])

    def test_both_are_off_by_default(self):
        s = self.load("  github_readmes: []\n")
        self.assertIsNone(s.discovery.getro)
        self.assertIsNone(s.discovery.consider)

    def test_bad_values_are_reported(self):
        for discovery, problem in (
                ("  getro: {collections: {abc: X}}\n", "discovery.getro.collections"),
                ("  getro: {collections: {}}\n", "discovery.getro.collections"),
                ("  getro: {collections: {1: X}, max_pages: 0}\n", "discovery.getro.max_pages"),
                ("  getro: {collections: {1: X}, colour: red}\n", "colour"),
                ("  consider: {boards: {'https://jobs.a16z.com/jobs': a16z}}\n", "discovery.consider.boards"),
                ("  consider: {boards: {jobs.a16z.com: a16z}, roles: []}\n", "discovery.consider.roles"),
                ("  getro: {collections: {189: ''}}\n", "discovery.getro.collections"),
                ("  getro: {collections: {189: '  '}}\n", "discovery.getro.collections"),
                ("  consider: {boards: {jobs.a16z.com: 16}}\n", "discovery.consider.boards"),
                ("  consider: {boards: {jobs.a16z.com: [a16z]}}\n", "discovery.consider.boards"),
                ("  consider: {boards: {jobs.a16z.com: ''}}\n", "discovery.consider.boards")):
            with self.subTest(discovery=discovery):
                found = self.problems(discovery)
                self.assertTrue(any(problem in p for p in found), found)
