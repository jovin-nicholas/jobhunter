"""The Colab notebooks are thin: their code compiles, calls only functions laya_train has, and the restore cell
never overwrites a training result from the same session."""
import ast
import builtins
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "training"))
import laya_train as lt   # noqa: E402


def code_cells(name: str) -> list[str]:
    nb = json.loads((ROOT / "training" / name).read_text(encoding="utf-8"))
    return ["".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code"]


def _tree(source: str) -> ast.AST:
    return ast.parse("\n".join("pass" if l.startswith("!") else l for l in source.splitlines()))


def lt_names(source: str) -> set[str]:
    tree = _tree(source)
    return {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "lt"}


def defined_names(source: str) -> set[str]:
    """Names a cell binds: import targets, def/class names and parameters, and any assignment target (including
    for/with/except targets), anywhere in the cell -- cheap enough for a notebook cell, not full scope analysis."""
    names = set()
    for node in ast.walk(_tree(source)):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                names.update(a.arg for a in args.posonlyargs + args.args + args.kwonlyargs)
                if args.vararg:
                    names.add(args.vararg.arg)
                if args.kwarg:
                    names.add(args.kwarg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return names


def free_names(source: str) -> set[str]:
    """Every bare name the cell loads, anywhere (including inside function bodies)."""
    return {n.id for n in ast.walk(_tree(source)) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


class TestNotebooks(unittest.TestCase):
    def test_cells_compile_and_call_existing_functions(self):
        for name in ("train_alert.ipynb", "train_questions.ipynb"):
            for source in code_cells(name):
                with self.subTest(notebook=name, cell=source.splitlines()[0]):
                    missing = {n for n in lt_names(source) if not hasattr(lt, n)}
                    self.assertEqual(missing, set())

    def test_restore_cells_run_only_without_a_result(self):
        for name in ("train_alert.ipynb", "train_questions.ipynb"):
            restore = [s for s in code_cells(name) if "best_path = SAFE" in s]
            with self.subTest(notebook=name):
                self.assertEqual(len(restore), 1)
                self.assertIn("if 'result' in globals():", restore[0])

    def test_questions_notebook_trains_and_exports_the_questions_method(self):
        text = "\n".join(code_cells("train_questions.ipynb"))
        for call in ("load_question_base", "sample_questions", "train_questions", "build_questions_report",
                     "export_questions_checkpoint"):
            self.assertIn(f"lt.{call}(", text)

    def test_restore_and_later_cells_never_rely_on_a_name_only_the_training_cell_defines(self):
        """A Colab disconnect means the user reruns setup, then data, then the restore cell -- skipping the
        training cell entirely. So every free name the restore cell (and anything after it) loads must already be
        defined by an earlier non-training cell (or be a builtin), never only by the training cell itself."""
        builtin_names = set(vars(builtins))
        for name in ("train_alert.ipynb", "train_questions.ipynb"):
            cells = code_cells(name)
            training_idx = next(i for i, s in enumerate(cells) if "result = lt.train" in s)
            restore_idx = next(i for i, s in enumerate(cells) if "best_path = SAFE" in s)
            self.assertLess(training_idx, restore_idx, name)
            defined_so_far = set(builtin_names)
            for i in range(restore_idx):
                if i != training_idx:
                    defined_so_far |= defined_names(cells[i])
            for i in range(restore_idx, len(cells)):
                with self.subTest(notebook=name, cell=cells[i].splitlines()[0]):
                    available = defined_so_far | defined_names(cells[i])
                    missing = free_names(cells[i]) - available
                    self.assertEqual(missing, set())
                defined_so_far |= defined_names(cells[i])


if __name__ == "__main__":
    unittest.main()
