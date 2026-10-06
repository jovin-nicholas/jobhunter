import tempfile
import types
import unittest
from dataclasses import dataclass
from pathlib import Path

from jobhunter.errors import SettingsError
from jobhunter.registry import Registry, _register_module, board, build_registry, check_names
from jobhunter.settings import load_settings
from tests.helpers import write_project

FEED_PLUGIN = """
from dataclasses import dataclass
from jobhunter import board

@board("my_feed")
class MyFeed:
    @dataclass
    class Options:
        feed_url: str
        limit: int = 10

    def __init__(self, options):
        self.options = self.Options(**options)

    def search(self, ctx):
        return []
"""

SETTINGS = """
resumes: {folder: resumes, default: backend.txt}
search: {queries: [software engineer], locations: [Remote]}
boards:
  my_feed: {feed_url: "https://example.com/jobs.json", colour: blue}
  mystery: {}
  empty_feed: {}
scorers:
  - nobody: {}
"""


@board("elsewhere")
class DefinedInThisTestModule:
    pass


class TestBuildRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def plugins(self, files):
        folder = self.tmp / "plugins"
        folder.mkdir()
        for name, code in files.items():
            (folder / name).write_text(code, encoding="utf-8")
        return folder

    def test_plugin_board_is_discovered(self):
        registry = build_registry(self.plugins({"my_feed.py": FEED_PLUGIN}))
        self.assertIn("my_feed", registry.boards)
        self.assertTrue(registry.origins[("board", "my_feed")].endswith("my_feed.py"))

    def test_duplicate_names_across_files_are_reported(self):
        folder = self.plugins({"a.py": FEED_PLUGIN, "b.py": FEED_PLUGIN})
        with self.assertRaises(SettingsError) as caught:
            build_registry(folder)
        self.assertIn("registered twice", caught.exception.problems[0])
        self.assertIn("a.py", caught.exception.problems[0])
        self.assertIn("b.py", caught.exception.problems[0])

    def test_broken_plugin_is_reported_with_its_traceback(self):
        with self.assertRaises(SettingsError) as caught:
            build_registry(self.plugins({"broken.py": "def oops(:\n"}))
        self.assertIn("broken.py failed to import", caught.exception.problems[0])
        self.assertIn("SyntaxError", caught.exception.problems[0])

    def test_class_imported_from_another_module_is_not_registered(self):
        module = types.ModuleType("some_plugin")
        module.Imported = DefinedInThisTestModule
        registry = Registry()
        _register_module(registry, module)
        self.assertEqual(registry.boards, {})


class TestCheckNames(unittest.TestCase):
    def test_unknown_names_and_bad_options(self):
        tmp = Path(tempfile.mkdtemp())
        path = write_project(tmp, SETTINGS, plugins={"my_feed.py": FEED_PLUGIN, "empty_feed.py":
                             FEED_PLUGIN.replace('"my_feed"', '"empty_feed"')})
        settings = load_settings(path, env={})
        problems = check_names(settings, build_registry(settings.plugins_dir))
        for fragment in ("boards.my_feed.colour: unknown option", "boards.mystery: unknown board",
                         "boards.empty_feed.feed_url: required option missing", "scorers.nobody: unknown scorer"):
            with self.subTest(fragment=fragment):
                self.assertTrue(any(fragment in p for p in problems), problems)
        self.assertEqual(len(problems), 4, problems)



class TestOptionTypes(unittest.TestCase):
    def test_wrongly_typed_options_are_reported(self):
        from jobhunter.registry import _check_options
        from jobhunter.scorers.ollama import OllamaScorer
        from jobhunter.scorers.laya import LayaScorer
        problems = _check_options("scorers.ollama", OllamaScorer, {"model": "m", "timeout_s": "240", "url": 5})
        self.assertTrue(any("scorers.ollama.timeout_s: expected a whole number" in p for p in problems), problems)
        self.assertTrue(any("scorers.ollama.url: expected text" in p for p in problems), problems)
        self.assertEqual(_check_options("scorers.ollama", OllamaScorer, {"model": "m", "timeout_s": 240}), [])
        self.assertEqual(_check_options("scorers.laya", LayaScorer, {"model": "x", "device": None}), [])
        self.assertTrue(_check_options("scorers.laya", LayaScorer, {"model": "x", "device": True}))
        self.assertTrue(any("expected true or false" in p for p in
                            _check_options("boards.x", type("B", (), {"Options": _bool_options()}), {"flag": "yes"})))
        self.assertTrue(any("expected a list" in p for p in
                            _check_options("boards.x", type("B", (), {"Options": _list_options()}), {"items": "a"})))


def _bool_options():
    @dataclass
    class Options:
        flag: bool = True
    return Options


def _list_options():
    from dataclasses import field

    @dataclass
    class Options:
        items: list[str] = field(default_factory=list)
    return Options



class TestPublicApi(unittest.TestCase):
    def test_plugins_can_import_board_skipped_from_the_package(self):
        import jobhunter
        from jobhunter.errors import BoardSkipped
        self.assertIs(jobhunter.BoardSkipped, BoardSkipped)
        self.assertIn("BoardSkipped", jobhunter.__all__)

if __name__ == "__main__":
    unittest.main()
