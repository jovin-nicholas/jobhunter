"""Loads jobhunter.yaml into typed settings, reporting every problem at once."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jobhunter.errors import SettingsError
from jobhunter.feedback import DEFAULT_ALERT_TAG, DEFAULT_FEEDBACK_TAG, TAG_RE

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_NAMES = {**{d: d for d in DAYS}, **{full: full[:3] for full in (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")}}
MIN_EVERY_MINUTES, MAX_EVERY_MINUTES = 15, 24 * 60
_EVERY = re.compile(r"^\s*(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?)\s*$", re.I)
_QUIET = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)\s*-\s*([01]\d|2[0-3]):([0-5]\d)$")
LEVELS = ("intern", "entry", "mid", "senior", "staff")
_TOP_KEYS = {"resumes", "search", "boards", "filters", "scorers", "decisions", "notify", "discovery", "cover_letters", "schedule"}
# Boards that run when `boards:` is left out: every built-in board that needs no setup of its own, except hydepark (one
# VC firm's portfolio), which runs only when listed.
DEFAULT_BOARDS = ("dice", "linkedin", "industry_jobs", "top_companies", "greenhouse", "lever", "ashby", "workday",
                  "dover", "adp", "gem", "vc_boards")
DEFAULT_GITHUB_READMES = [
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md",
    "https://raw.githubusercontent.com/pittcsc/Summer2025-Internships/dev/README.md",
]
_NOTIFY_KEYS = {"slack": {"webhook_env"},
                "email": {"from_env", "password_env", "to_env", "alert_tag", "feedback_tag"}}
_NOTIFY_REQUIRED = {"slack": {"webhook_env"}, "email": {"from_env", "password_env"}}


@dataclass
class ResumeSettings:
    folder: Path
    default: str
    versions: dict[str, list[str]] = field(default_factory=dict)   # file name -> keywords
    names: list[str] = field(default_factory=list)


@dataclass
class SearchSettings:
    queries: list[str]
    locations: list[str]
    max_age_hours: int = 72
    fetch_descriptions: bool = True   # fetch the posting page when a job has no usable description


@dataclass
class BoardSettings:
    name: str
    options: dict[str, Any]
    enabled: bool = True
    timeout_s: int | None = None      # None: the board's own default (bootstrap fills it in)


@dataclass
class LocationFilterSettings:
    countries: list[Any]          # country codes ("US") or {code, names, region_codes} mappings


@dataclass
class SeniorityFilterSettings:
    levels: list[str]
    max_years_required: int | None = None


@dataclass
class RoleGroup:
    terms: list[str]
    min_matches: int = 1


@dataclass
class KeywordFilterSettings:
    exclude_title: list[str] = field(default_factory=list)
    exclude_phrases: list[str] = field(default_factory=list)
    exclude_companies: list[str] = field(default_factory=list)
    exclude_roles: dict[str, RoleGroup] = field(default_factory=dict)


EMPLOYMENT_EXCLUDE = ("contract", "hourly")
EMPLOYMENT_TOGGLE = ("keep", "exclude")


@dataclass
class EmploymentFilterSettings:
    exclude: list[str]
    contract_to_hire: str = "keep"
    hourly_internships: str = "keep"


SYSTEMONE_CHECKS = ("citizenship", "clearance")


@dataclass
class SystemOneSettings:
    """A local System One decision model (Ollama 0.35+) for the questions the filters cannot answer."""
    model: str
    url: str = "http://localhost:11434"
    timeout_s: int = 30
    skip_if: list[str] = field(default_factory=list)    # "citizenship", "clearance": requirements to skip jobs for


@dataclass
class FilterSettings:
    location: LocationFilterSettings | None = None
    seniority: SeniorityFilterSettings | None = None
    keywords: KeywordFilterSettings | None = None
    employment: EmploymentFilterSettings | None = None
    systemone: SystemOneSettings | None = None


@dataclass
class ScorerSettings:
    name: str
    options: dict[str, Any]


@dataclass
class Decisions:
    notify_at: int = 7
    log_at: int = 5
    min_confidence: float | None = None    # a notify the scorer is less sure of than this is logged instead


@dataclass
class NotifySettings:
    slack: dict[str, str] | None = None
    email: dict[str, str] | None = None


@dataclass
class ScheduleSettings:
    every_minutes: int = 60
    quiet_hours: tuple[int, int] | None = None    # (start, end) in minutes after midnight; may cross midnight
    quiet_hours_text: str | None = None
    quiet_days: tuple[str, ...] = ()
    timezone: str | None = None                   # the computer's local time when None


@dataclass
class DiscoverySettings:
    github_readmes: list[str] = field(default_factory=lambda: list(DEFAULT_GITHUB_READMES))
    google: dict[str, str] | None = None      # {api_key_env, cx_env}; off unless set
    getro: dict | None = None          # {collections: {id: name}, job_functions, locations, seniority, max_pages}
    consider: dict | None = None       # {boards: {host: name}, roles}


@dataclass
class CoverLetterSettings:
    enabled: bool = False
    writer: str | None = None                 # a scorer listed under `scorers` that can write text
    options: dict = field(default_factory=dict)   # the writer's options for letters, over its scorer options


@dataclass
class Settings:
    path: Path
    resumes: ResumeSettings
    search: SearchSettings
    boards: list[BoardSettings]
    filters: FilterSettings
    scorers: list[ScorerSettings]
    decisions: Decisions
    notify: NotifySettings
    discovery: DiscoverySettings = field(default_factory=DiscoverySettings)
    cover_letters: CoverLetterSettings = field(default_factory=CoverLetterSettings)
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"

    @property
    def plugins_dir(self) -> Path:
        return self.base_dir / "plugins"


class _Checker:
    """Collects problems as "key.path: message" so all of them can be reported together."""

    def __init__(self):
        self.problems: list[str] = []

    def add(self, key: str, message: str) -> None:
        self.problems.append(f"{key}: {message}")

    def mapping(self, key: str, value: Any, allowed: set[str]) -> dict:
        if value is None:
            return {}
        if not isinstance(value, dict):
            self.add(key or "(top level)", f"expected a mapping, got {type(value).__name__}")
            return {}
        for k in value:
            if k not in allowed:
                self.add(f"{key}.{k}" if key else str(k), f"unknown key (allowed: {', '.join(sorted(allowed))})")
        return value

    def strings(self, key: str, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            self.add(key, "expected a list of text values")
            return []
        return value

    def boolean(self, key: str, value: Any, default: bool) -> bool:
        if value is None:
            return default
        if not isinstance(value, bool):
            self.add(key, "expected true or false")
            return default
        return value

    def integer(self, key: str, value: Any, minimum: int | None = None, maximum: int | None = None) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            self.add(key, "expected a whole number")
            return None
        if minimum is not None and value < minimum:
            self.add(key, f"must be at least {minimum}")
            return None
        if maximum is not None and value > maximum:
            self.add(key, f"must be at most {maximum}")
            return None
        return value


def load_settings(path: str | Path, env: Mapping[str, str] | None = None) -> Settings:
    """Parse and validate the settings file; raises SettingsError listing every problem."""
    path = Path(path).expanduser().resolve()
    env = os.environ if env is None else env
    if not path.is_file():
        raise SettingsError([f"{path}: settings file not found (copy jobhunter.example.yaml to {path.name})"])
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise SettingsError([f"{path.name}: not valid YAML: {e}"]) from e

    c = _Checker()
    raw = c.mapping("", raw, _TOP_KEYS)
    resumes = _resumes(c, raw.get("resumes"), path.parent)
    search = _search(c, raw.get("search"))
    boards = _boards(c, raw.get("boards"))
    filters = _filters(c, raw.get("filters"))
    scorers = _scorers(c, raw.get("scorers"))
    decisions = _decisions(c, raw.get("decisions"))
    notify = _notify(c, raw.get("notify"))
    discovery = _discovery(c, raw.get("discovery"))
    cover_letters = _cover_letters(c, raw.get("cover_letters"), scorers)
    schedule = _schedule(c, raw.get("schedule"))
    _check_env(c, scorers, notify, discovery, env)
    if c.problems:
        raise SettingsError([f"{path.name}: {p}" for p in c.problems])
    return Settings(path, resumes, search, boards, filters, scorers, decisions, notify, discovery, cover_letters,
                    schedule)


def _resumes(c: _Checker, raw: Any, base: Path) -> ResumeSettings:
    if raw is None:
        c.add("resumes", "missing: needs `default`, the resume file used when no version matches")
        return ResumeSettings(base / "resumes", "")
    raw = c.mapping("resumes", raw, {"folder", "default", "versions", "names"})
    folder = Path(str(raw.get("folder", "resumes"))).expanduser()
    folder = (folder if folder.is_absolute() else base / folder).resolve()
    default = raw.get("default")
    if not isinstance(default, str) or not default:
        c.add("resumes.default", "missing: name the resume file used when no version matches")
        default = ""
    versions: dict[str, list[str]] = {}
    raw_versions = raw.get("versions") or {}
    if not isinstance(raw_versions, dict):
        c.add("resumes.versions", "expected a mapping of file name to {keywords: [...]}")
        raw_versions = {}
    for name, spec in raw_versions.items():
        spec = c.mapping(f"resumes.versions.{name}", spec, {"keywords"})
        versions[str(name)] = c.strings(f"resumes.versions.{name}.keywords", spec.get("keywords"))
    for name in [default, *versions]:
        if name and not (folder / name).is_file():
            c.add("resumes", f"resume file not found: {folder / name}")
    return ResumeSettings(folder, default, versions, c.strings("resumes.names", raw.get("names")))


def _search(c: _Checker, raw: Any) -> SearchSettings:
    if raw is None:
        c.add("search", "missing: needs `queries`")
        return SearchSettings([], [])
    raw = c.mapping("search", raw, {"queries", "locations", "max_age_hours", "fetch_descriptions"})
    queries = c.strings("search.queries", raw.get("queries"))
    if not queries:
        c.add("search.queries", "needs at least one query")
    age = c.integer("search.max_age_hours", raw.get("max_age_hours", 72), minimum=1)
    fetch = c.boolean("search.fetch_descriptions", raw.get("fetch_descriptions"), True)
    return SearchSettings(queries, c.strings("search.locations", raw.get("locations")), age or 72, fetch)


def _boards(c: _Checker, raw: Any) -> list[BoardSettings]:
    if raw is None:
        return [BoardSettings(name, {}) for name in DEFAULT_BOARDS]
    if isinstance(raw, list):
        if not all(isinstance(name, str) for name in raw):
            c.add("boards", "expected a list of board names, e.g. [dice, greenhouse], or a mapping of name to options")
            return []
        raw = {name: {} for name in raw}
    if not raw:
        c.add("boards", "no boards listed (leave `boards:` out to use all built-in boards)")
        return []
    if not isinstance(raw, dict):
        c.add("boards", "expected a list of board names or a mapping of board name to options")
        return []
    boards = []
    for name, opts in raw.items():
        opts = {} if opts is None else opts
        if not isinstance(opts, dict):
            c.add(f"boards.{name}", "expected a mapping of options")
            continue
        opts = dict(opts)
        enabled = opts.pop("enabled", True)
        if not isinstance(enabled, bool):
            c.add(f"boards.{name}.enabled", "expected true or false")
            enabled = True
        timeout = None
        if opts.get("timeout_s") is not None:
            timeout = c.integer(f"boards.{name}.timeout_s", opts.pop("timeout_s"), minimum=1)
        opts.pop("timeout_s", None)
        boards.append(BoardSettings(str(name), opts, enabled, timeout))
    return boards


def _filters(c: _Checker, raw: Any) -> FilterSettings:
    raw = c.mapping("filters", raw, {"location", "seniority", "keywords", "employment", "systemone"})
    out = FilterSettings()
    if "location" in raw:
        r = c.mapping("filters.location", raw["location"], {"countries"})
        countries = r.get("countries")
        if not isinstance(countries, list) or not countries:
            c.add("filters.location.countries", "expected a non-empty list of country codes")
            countries = []
        for i, item in enumerate(countries):
            key = f"filters.location.countries[{i}]"
            if isinstance(item, dict):
                c.mapping(key, item, {"code", "names", "region_codes"})
                if not isinstance(item.get("code"), str):
                    c.add(f"{key}.code", "missing: a two-letter country code")
                c.strings(f"{key}.names", item.get("names"))
                c.strings(f"{key}.region_codes", item.get("region_codes"))
            elif not isinstance(item, str):
                c.add(key, "expected a country code or a {code, names, region_codes} mapping")
        out.location = LocationFilterSettings(countries)
    if "seniority" in raw:
        r = c.mapping("filters.seniority", raw["seniority"], {"levels", "max_years_required"})
        levels = c.strings("filters.seniority.levels", r.get("levels"))
        for level in levels:
            if level not in LEVELS:
                c.add("filters.seniority.levels", f"unknown level {level!r} (allowed: {', '.join(LEVELS)})")
        if not levels:
            c.add("filters.seniority.levels", "needs at least one level")
        years = None
        if r.get("max_years_required") is not None:
            years = c.integer("filters.seniority.max_years_required", r["max_years_required"], minimum=0)
        out.seniority = SeniorityFilterSettings(levels, years)
    if "keywords" in raw:
        key = "filters.keywords"
        r = c.mapping(key, raw["keywords"],
                      {"exclude_title", "exclude_phrases", "exclude_companies", "exclude_roles"})
        roles = r.get("exclude_roles") or {}
        if not isinstance(roles, dict):
            c.add(f"{key}.exclude_roles", "expected a mapping of group name to {terms, min_matches}")
            roles = {}
        groups = {}
        for name, group in roles.items():
            gkey = f"{key}.exclude_roles.{name}"
            group = c.mapping(gkey, group, {"terms", "min_matches"})
            terms = c.strings(f"{gkey}.terms", group.get("terms"))
            if not terms:
                c.add(f"{gkey}.terms", "needs at least one term")
            matches = c.integer(f"{gkey}.min_matches", group.get("min_matches", 1), minimum=1)
            groups[str(name)] = RoleGroup(terms, matches or 1)
        out.keywords = KeywordFilterSettings(
            c.strings(f"{key}.exclude_title", r.get("exclude_title")),
            c.strings(f"{key}.exclude_phrases", r.get("exclude_phrases")),
            c.strings(f"{key}.exclude_companies", r.get("exclude_companies")),
            groups,
        )
    if "employment" in raw:
        key = "filters.employment"
        r = c.mapping(key, raw["employment"], {"exclude", "contract_to_hire", "hourly_internships"})
        exclude = c.strings(f"{key}.exclude", r.get("exclude"))
        for value in exclude:
            if value not in EMPLOYMENT_EXCLUDE:
                c.add(f"{key}.exclude", f"unknown value {value!r} (allowed: {', '.join(EMPLOYMENT_EXCLUDE)})")
        if not exclude:
            c.add(f"{key}.exclude", f"needs at least one of: {', '.join(EMPLOYMENT_EXCLUDE)}")
        contract_to_hire = r.get("contract_to_hire", "keep")
        if contract_to_hire not in EMPLOYMENT_TOGGLE:
            c.add(f"{key}.contract_to_hire", f"expected keep or exclude, got {contract_to_hire!r}")
            contract_to_hire = "keep"
        hourly_internships = r.get("hourly_internships", "keep")
        if hourly_internships not in EMPLOYMENT_TOGGLE:
            c.add(f"{key}.hourly_internships", f"expected keep or exclude, got {hourly_internships!r}")
            hourly_internships = "keep"
        out.employment = EmploymentFilterSettings(exclude, contract_to_hire, hourly_internships)
    if "systemone" in raw:
        key = "filters.systemone"
        r = c.mapping(key, raw["systemone"], {"model", "url", "timeout_s", "skip_if"})
        model = r.get("model")
        if not isinstance(model, str) or not model.strip():
            c.add(f"{key}.model", "missing: the Ollama model to ask, e.g. nimble:9b-q4_K_M")
            model = ""
        url = r.get("url", SystemOneSettings.url)
        if not isinstance(url, str):
            c.add(f"{key}.url", "expected a URL such as http://localhost:11434")
            url = SystemOneSettings.url
        timeout = SystemOneSettings.timeout_s
        if "timeout_s" in r:
            timeout = c.integer(f"{key}.timeout_s", r["timeout_s"], minimum=1) or timeout
        skip_if = c.strings(f"{key}.skip_if", r.get("skip_if"))
        unknown = [x for x in skip_if if x not in SYSTEMONE_CHECKS]
        if unknown:
            c.add(f"{key}.skip_if", f"unknown check {', '.join(unknown)} (allowed: {', '.join(SYSTEMONE_CHECKS)})")
        out.systemone = SystemOneSettings(model, url, timeout, [x for x in skip_if if x in SYSTEMONE_CHECKS])
    return out


def _scorers(c: _Checker, raw: Any) -> list[ScorerSettings]:
    if not raw:
        c.add("scorers", "no scorers listed")
        return []
    if not isinstance(raw, list):
        c.add("scorers", "expected a list, e.g. `- laya: {model: ...}` then `- ollama: {model: ...}`")
        return []
    scorers = []
    for i, item in enumerate(raw):
        if isinstance(item, str):
            scorers.append(ScorerSettings(item, {}))
            continue
        if not isinstance(item, dict) or len(item) != 1:
            c.add(f"scorers[{i}]", "expected one scorer name with its options, e.g. `- ollama: {model: gemma4:e4b}`")
            continue
        (name, opts), = item.items()
        opts = {} if opts is None else opts
        if not isinstance(opts, dict):
            c.add(f"scorers.{name}", "expected a mapping of options")
            continue
        scorers.append(ScorerSettings(str(name), dict(opts)))
    return scorers


def _decisions(c: _Checker, raw: Any) -> Decisions:
    raw = c.mapping("decisions", raw, {"notify_at", "log_at", "min_confidence"})
    notify_at = c.integer("decisions.notify_at", raw.get("notify_at", 7), minimum=1, maximum=10)
    log_at = c.integer("decisions.log_at", raw.get("log_at", 5), minimum=1, maximum=10)
    if notify_at is not None and log_at is not None and log_at > notify_at:
        c.add("decisions", f"log_at ({log_at}) must not be above notify_at ({notify_at})")
    min_confidence = raw.get("min_confidence")
    if min_confidence is not None and (isinstance(min_confidence, bool) or not isinstance(min_confidence, (int, float))
                                       or not 0 <= min_confidence <= 1):
        c.add("decisions.min_confidence", f"expected a number from 0 to 1, got {min_confidence!r}")
        min_confidence = None
    return Decisions(notify_at or 7, log_at or 5, None if min_confidence is None else float(min_confidence))


def _notify(c: _Checker, raw: Any) -> NotifySettings:
    raw = c.mapping("notify", raw, set(_NOTIFY_KEYS))
    channels = {}
    for channel, allowed in _NOTIFY_KEYS.items():
        if raw.get(channel) is None:
            continue
        r = c.mapping(f"notify.{channel}", raw[channel], allowed)
        missing = _NOTIFY_REQUIRED[channel] - set(r)
        if missing:
            c.add(f"notify.{channel}", f"missing {', '.join(sorted(missing))}")
        if channel == "email":
            for key in ("alert_tag", "feedback_tag"):
                if key in r and not TAG_RE.match(str(r[key])):
                    c.add(f"notify.email.{key}", "use only letters, digits, - and _")
            if str(r.get("alert_tag", DEFAULT_ALERT_TAG)) == str(r.get("feedback_tag", DEFAULT_FEEDBACK_TAG)):
                c.add("notify.email", "alert_tag and feedback_tag must differ")
        channels[channel] = {k: str(v) for k, v in r.items()}
    return NotifySettings(channels.get("slack"), channels.get("email"))


def _discovery(c: _Checker, raw: Any) -> DiscoverySettings:
    raw = c.mapping("discovery", raw, {"github_readmes", "google", "getro", "consider"})
    readmes = (list(DEFAULT_GITHUB_READMES) if "github_readmes" not in raw
               else c.strings("discovery.github_readmes", raw["github_readmes"]))
    google = None
    if raw.get("google") is not None:
        g = c.mapping("discovery.google", raw["google"], {"api_key_env", "cx_env"})
        missing = {"api_key_env", "cx_env"} - set(g)
        if missing:
            c.add("discovery.google", f"missing {', '.join(sorted(missing))}")
        google = {k: str(v) for k, v in g.items()}
    getro = None
    if raw.get("getro") is not None:
        g = c.mapping("discovery.getro", raw["getro"], {"collections", "job_functions", "locations", "seniority", "max_pages"})
        collections = {}
        given = g.get("collections")
        if not isinstance(given, dict) or not given:
            c.add("discovery.getro.collections", "expected collection ids with names, e.g. {189: Redpoint}")
        else:
            for key, name in given.items():
                if isinstance(key, bool) or not str(key).isdigit() or int(key) <= 0 or not isinstance(name, str) \
                        or not name.strip():
                    c.add("discovery.getro.collections", f"{key!r}: expected a positive collection id and a name")
                else:
                    collections[int(key)] = name
        functions = c.strings("discovery.getro.job_functions", g.get("job_functions", ["Software Engineering"]))
        if not functions:
            c.add("discovery.getro.job_functions", "expected at least one, e.g. [Software Engineering]")
        getro = {"collections": collections, "job_functions": functions,
                 "locations": c.strings("discovery.getro.locations", g.get("locations", [])),
                 "seniority": c.strings("discovery.getro.seniority", g.get("seniority", [])),
                 "max_pages": c.integer("discovery.getro.max_pages", g.get("max_pages", 10), 1, 50) or 10}
    consider = None
    if raw.get("consider") is not None:
        k = c.mapping("discovery.consider", raw["consider"], {"boards", "roles"})
        boards = k.get("boards")
        if not isinstance(boards, dict) or not boards:
            c.add("discovery.consider.boards", "expected board hosts with names, e.g. {jobs.a16z.com: a16z}")
            boards = {}
        for host, name in boards.items():
            if not re.fullmatch(r"[A-Za-z0-9.-]+\.[A-Za-z]{2,}", str(host)):
                c.add("discovery.consider.boards", f"{host!r}: expected a host name such as jobs.a16z.com")
            elif not isinstance(name, str) or not name.strip():
                c.add("discovery.consider.boards", f"{host}: expected the board's name as text, e.g. a16z")
        roles = c.strings("discovery.consider.roles", k.get("roles", ["software-engineer"]))
        if not roles:
            c.add("discovery.consider.roles", "expected at least one role, e.g. [software-engineer]")
        consider = {"boards": {str(h): str(n) for h, n in boards.items()}, "roles": roles}
    return DiscoverySettings(readmes, google, getro, consider)


def settings_notes(settings: Settings) -> list[str]:
    """Settings that work but probably do not do what was meant; check-config shows them as notes."""
    notes = []
    collections = (settings.discovery.getro or {}).get("collections") or {}
    for b in settings.boards:
        if b.name == "hydepark" and b.enabled:
            cid = b.options.get("collection_id", 112)
            if cid in collections:
                notes.append(f"boards.hydepark and discovery.getro both read Getro collection {cid} "
                             f"({collections[cid]}), so its jobs are fetched and judged twice; set boards.hydepark to "
                             f"enabled: false or remove {cid} from discovery.getro.collections")
    return notes


def _cover_letters(c: _Checker, raw: Any, scorers: list[ScorerSettings]) -> CoverLetterSettings:
    raw = c.mapping("cover_letters", raw, {"enabled", "writer", "options"})
    enabled = c.boolean("cover_letters.enabled", raw.get("enabled"), False)
    writer = raw.get("writer")
    if writer is not None and not isinstance(writer, str):
        c.add("cover_letters.writer", "expected the name of a scorer, e.g. ollama")
        writer = None
    if enabled and not writer:
        c.add("cover_letters.writer", "missing: name the scorer that writes the letters (ollama, gemini or groq)")
    elif writer and writer not in {s.name for s in scorers}:
        c.add("cover_letters.writer", f"{writer!r} is not listed under scorers")
    options = raw.get("options") or {}
    if not isinstance(options, dict):
        c.add("cover_letters.options", "expected a mapping of the writer's options, e.g. {think: true, temperature: 0.7}")
        options = {}
    return CoverLetterSettings(enabled, writer, dict(options))


def _schedule(c: _Checker, raw: Any) -> ScheduleSettings:
    raw = c.mapping("schedule", raw, {"every", "quiet_hours", "quiet_days", "timezone"})
    out = ScheduleSettings()
    if raw.get("every") is not None:
        m = _EVERY.match(str(raw["every"])) if isinstance(raw["every"], str) else None
        if not m:
            c.add("schedule.every", "expected minutes or hours, e.g. 30m or 2h")
        else:
            minutes = int(m.group(1)) * (60 if m.group(2).lower().startswith("h") else 1)
            if minutes < MIN_EVERY_MINUTES:
                c.add("schedule.every", f"must be at least {MIN_EVERY_MINUTES}m (job sites refuse faster searches)")
            elif minutes > MAX_EVERY_MINUTES:
                c.add("schedule.every", "must be at most 24h")
            else:
                out.every_minutes = minutes
    if raw.get("quiet_hours") is not None:
        m = _QUIET.match(str(raw["quiet_hours"]).strip())
        if not m:
            c.add("schedule.quiet_hours", 'expected "HH:MM-HH:MM" in 24-hour time, e.g. "22:00-07:00"')
        else:
            start, end = int(m.group(1)) * 60 + int(m.group(2)), int(m.group(3)) * 60 + int(m.group(4))
            if start == end:
                c.add("schedule.quiet_hours", "start and end must differ")
            else:
                out.quiet_hours, out.quiet_hours_text = (start, end), f"{m.group(1)}:{m.group(2)}-{m.group(3)}:{m.group(4)}"
    days = []
    for day in c.strings("schedule.quiet_days", raw.get("quiet_days")):
        short = _DAY_NAMES.get(day.strip().lower())
        if short is None:
            c.add("schedule.quiet_days", f"unknown day {day!r} (use mon, tue, wed, thu, fri, sat, sun)")
        elif short not in days:
            days.append(short)
    out.quiet_days = tuple(days)
    if raw.get("timezone") is not None:
        try:
            ZoneInfo(str(raw["timezone"]))
            out.timezone = str(raw["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            c.add("schedule.timezone", f"unknown time zone {raw['timezone']!r} (e.g. America/New_York)")
    return out


def _check_env(c: _Checker, scorers: list[ScorerSettings], notify: NotifySettings, discovery: DiscoverySettings,
               env: Mapping[str, str]) -> None:
    for s in scorers:
        for key, var in s.options.items():
            if key.endswith("_env") and not env.get(str(var)):
                c.add(f"scorers.{s.name}.{key}", f"environment variable {var} is not set (add it to .env)")
    for channel in ("slack", "email"):
        for key, var in (getattr(notify, channel) or {}).items():
            if key.endswith("_env") and not env.get(var):
                c.add(f"notify.{channel}.{key}", f"environment variable {var} is not set (add it to .env)")
    for key, var in (discovery.google or {}).items():
        if not env.get(var):
            c.add(f"discovery.google.{key}", f"environment variable {var} is not set (add it to .env)")
