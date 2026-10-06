"""Name -> board and scorer classes, collected from the built-in modules and the user's plugins/ folder.

`@board(name)` and `@scorer(name)` only tag a class. `build_registry` imports modules and registers the tagged classes
each module defines, so building a registry twice (tests, `check-config` then `run`) gives the same result.
"""
from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import inspect
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from jobhunter.errors import SettingsError

# Built-in modules whose tagged classes are always available.
BUILTIN_MODULES: tuple[str, ...] = (
    "jobhunter.boards.dice",
    "jobhunter.boards.greenhouse",
    "jobhunter.boards.lever",
    "jobhunter.boards.ashby",
    "jobhunter.boards.scraped_ats",
    "jobhunter.boards.dover",
    "jobhunter.boards.gem",
    "jobhunter.boards.adp",
    "jobhunter.boards.hydepark",
    "jobhunter.boards.vc_boards",
    "jobhunter.boards.industry_jobs",
    "jobhunter.boards.top_companies",
    "jobhunter.boards.linkedin",
    "jobhunter.scorers.laya",
    "jobhunter.scorers.ollama",
    "jobhunter.scorers.cloud",
)

_TAG = "_jobhunter_registration"


def board(name: str):
    """Mark a class as a job board available under `name` in the settings file."""
    def tag(cls):
        setattr(cls, _TAG, ("board", name))
        return cls
    return tag


def scorer(name: str):
    """Mark a class as a scorer available under `name` in the settings file."""
    def tag(cls):
        setattr(cls, _TAG, ("scorer", name))
        return cls
    return tag


@dataclass
class Registry:
    boards: dict[str, type] = field(default_factory=dict)
    scorers: dict[str, type] = field(default_factory=dict)
    origins: dict[tuple[str, str], str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def add(self, kind: str, name: str, cls: type) -> None:
        table = self.boards if kind == "board" else self.scorers
        origin = inspect.getsourcefile(cls) or cls.__module__
        if name in table:
            self.problems.append(f"{kind} name {name!r} is registered twice: {self.origins[(kind, name)]} and {origin}")
            return
        table[name] = cls
        self.origins[(kind, name)] = origin


def _register_module(registry: Registry, module: ModuleType) -> None:
    for obj in vars(module).values():
        registration = getattr(obj, _TAG, None)
        # Only classes defined in this module: a plugin that imports another plugin's class must not register it again.
        if inspect.isclass(obj) and registration and obj.__module__ == module.__name__:
            registry.add(*registration, obj)


def _import_plugin(registry: Registry, path: Path) -> None:
    module_name = f"jobhunter_plugins.{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module          # dataclasses look up the defining module while building a class
    try:
        spec.loader.exec_module(module)
    except Exception:
        del sys.modules[module_name]
        registry.problems.append(f"plugin {path} failed to import:\n{traceback.format_exc()}")
        return
    _register_module(registry, module)


def build_registry(plugins_dir: Path | None = None) -> Registry:
    """All built-in boards and scorers plus every plugins/*.py; raises SettingsError listing every plugin problem."""
    registry = Registry()
    for name in BUILTIN_MODULES:
        _register_module(registry, importlib.import_module(name))
    if plugins_dir is not None and Path(plugins_dir).is_dir():
        for path in sorted(Path(plugins_dir).glob("*.py")):
            _import_plugin(registry, path)
    if registry.problems:
        raise SettingsError(registry.problems)
    return registry


def check_names(settings: Any, registry: Registry) -> list[str]:
    """Problems with board/scorer names in the settings, and with their options."""
    problems = []
    for kind, entries, table in (("board", settings.boards, registry.boards),
                                 ("scorer", settings.scorers, registry.scorers)):
        for entry in entries:
            key = f"{kind}s.{entry.name}"
            cls = table.get(entry.name)
            if cls is None:
                known = ", ".join(sorted(table)) or "none"
                problems.append(f"{key}: unknown {kind} (available: {known}; add a plugins/{entry.name}.py for a new one)")
                continue
            problems += _check_options(key, cls, entry.options)
    return problems


def _check_options(key: str, cls: type, options: dict[str, Any]) -> list[str]:
    spec = getattr(cls, "Options", None)
    if spec is None or not dataclasses.is_dataclass(spec):
        return []
    fields = {f.name: f for f in dataclasses.fields(spec)}
    problems = [f"{key}.{name}: unknown option (allowed: {', '.join(fields) or 'none'})"
                for name in options if name not in fields]
    for name, f in fields.items():
        required = f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
        if required and name not in options:
            problems.append(f"{key}.{name}: required option missing")
        elif name in options:
            wrong = _type_problem(f.type, options[name])
            if wrong:
                problems.append(f"{key}.{name}: {wrong}")
    return problems


# The option types boards and scorers declare, and what a YAML value of each looks like.
_TYPES = {"str": ((str,), "expected text"), "int": ((int,), "expected a whole number"),
          "float": ((int, float), "expected a number"), "bool": ((bool,), "expected true or false"),
          "list": ((list,), "expected a list"), "dict": ((dict,), "expected a mapping"),
          "set": ((set, list), "expected a list"), "None": ((type(None),), "")}


def _type_problem(annotation: Any, value: Any) -> str | None:
    """A message when `value` cannot be of the declared type ("int", "str | None", "list[str]"), else None.
    Types this does not know (plugin classes, Any) are not checked."""
    text = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")
    parts = [part.strip().split("[", 1)[0] for part in text.split("|")]
    if not parts or any(part not in _TYPES for part in parts):
        return None
    for part in parts:
        kinds, _ = _TYPES[part]
        # true/false are ints to Python; a whole-number option must not accept them.
        if isinstance(value, kinds) and not (isinstance(value, bool) and bool not in kinds):
            return None
    messages = [_TYPES[part][1] for part in parts if _TYPES[part][1]]
    return " or ".join(messages) + f", got {value!r}"
