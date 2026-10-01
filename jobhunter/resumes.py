"""Loads the resume folder, removes the candidate's name, and picks a resume for each job."""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

from jobhunter.errors import SettingsError
from jobhunter.models import Job, Resume
from jobhunter.settings import ResumeSettings
from jobhunter.text_match import has_term, word_text

SUPPORTED = (".txt", ".md", ".pdf", ".docx")
MIN_TEXT_CHARS = 200
_HEADER_SEPARATORS = (" — ", " – ", " - ", " | ")


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md"):
        return path.read_text(encoding="utf-8")
    if suffix == ".pdf":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    if suffix == ".docx":
        import docx
        return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
    raise ValueError(f"unsupported format {path.suffix}")


def _cached_text(path: Path, cache_dir: Path) -> str:
    """PDF and DOCX extraction is slow, so their text is cached until the file changes."""
    if path.suffix.lower() in (".txt", ".md"):
        return extract_text(path)
    stat = path.stat()
    key = hashlib.sha1(f"{path.resolve()}:{stat.st_mtime_ns}:{stat.st_size}".encode()).hexdigest()
    cached = cache_dir / f"{key}.txt"
    if cached.is_file():
        return cached.read_text(encoding="utf-8")
    text = extract_text(path)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached.write_text(text, encoding="utf-8")
    return text


# Words that make a short first line a title, not a name ("Software Engineer", "Curriculum Vitae").
_NOT_NAME_WORDS = {"resume", "résumé", "cv", "curriculum", "vitae", "software", "engineer", "developer", "profile",
                   "summary", "contact", "senior", "junior", "backend", "frontend", "full", "stack", "data",
                   "scientist", "analyst", "intern", "student", "graduate", "portfolio", "objective"}


def header_name(text: str) -> str:
    """The name on a resume's first line: "<Name> — <headline>" (also –, - or |), "NAME: <Name>", or a line that is
    only a name: two to four capitalised words of letters ("John Doe", "Mary-Jane O'Neil", "J. R. Doe")."""
    first = text.strip().split("\n", 1)[0].strip()
    if first.upper().startswith("NAME:"):
        return first[5:].strip()
    for sep in _HEADER_SEPARATORS:
        if sep in first:
            return first.split(sep, 1)[0].strip()
    return first if _looks_like_a_name(first) else ""


def _looks_like_a_name(line: str) -> bool:
    words = line.split()
    if not 2 <= len(words) <= 4:
        return False
    for word in words:
        letters = word.replace("-", "").replace("'", "").replace("’", "").rstrip(".")
        if not letters.isalpha() or not word[0].isupper() or letters.lower() in _NOT_NAME_WORDS:
            return False
    return True


def anonymize(text: str, names: Iterable[str]) -> str:
    """Replace each full name with "the candidate", in any case and with any separator between its parts.

    The separator rule also catches the name inside emails and profile URLs (john.doe@..., /in/john-doe). A first name
    alone is never replaced, because it may be an ordinary word ("Will").
    """
    out = text
    for name in names:
        if not name.strip() or name.strip().lower() == "the candidate":
            continue
        pattern = r"\b" + r"[\s._-]*".join(re.escape(p) for p in name.split()) + r"\b"
        out = re.sub(pattern, "the candidate", out, flags=re.IGNORECASE)
    return out


def candidate_names(settings: ResumeSettings, default_text: str, env: Mapping[str, str]) -> list[str]:
    """Names from settings, else CANDIDATE_NAMES in the environment, else the default resume's header line."""
    if settings.names:
        return list(settings.names)
    configured = [n.strip() for n in env.get("CANDIDATE_NAMES", "").split(",") if n.strip()]
    if configured:
        return configured
    name = header_name(default_text)
    return [name] if name and name.lower() != "the candidate" else []


@dataclass
class ResumeSet:
    resumes: dict[str, Resume]
    default_id: str
    names: list[str] = field(default_factory=list)      # removed from every resume before any model sees it

    def pick(self, job: Job) -> Resume:
        """The version with the most keyword matches in the job; no matches or a tie use the default."""
        words = word_text(f"{job.title} {job.description}")
        hits = {rid: sum(has_term(words, k) for k in r.keywords) for rid, r in self.resumes.items()}
        best = max(hits.values(), default=0)
        winners = [rid for rid, h in hits.items() if h == best]
        if best == 0 or len(winners) > 1:
            return self.resumes[self.default_id]
        return self.resumes[winners[0]]


def load_resumes(settings: ResumeSettings, cache_dir: Path, env: Mapping[str, str] | None = None) -> ResumeSet:
    """Read every resume named in settings; raises SettingsError listing every unreadable one."""
    env = os.environ if env is None else env
    problems, texts = [], {}
    for name in [settings.default, *settings.versions]:
        path = settings.folder / name
        if path.suffix.lower() not in SUPPORTED:
            problems.append(f"resumes: {name}: unsupported format (use {', '.join(SUPPORTED)})")
            continue
        try:
            text = _cached_text(path, cache_dir)
        except Exception as e:
            problems.append(f"resumes: {name}: could not read its text ({e})")
            continue
        if len(text.strip()) < MIN_TEXT_CHARS:
            problems.append(f"resumes: {name}: only {len(text.strip())} characters of text; a scanned PDF needs a "
                            "text layer (export it again as text, .docx or a text-based PDF)")
            continue
        texts[name] = text
    if problems:
        raise SettingsError(problems)
    names = candidate_names(settings, texts[settings.default], env)
    resumes = {name: Resume(name, anonymize(text, names).strip(), tuple(settings.versions.get(name, ())))
               for name, text in texts.items()}
    return ResumeSet(resumes, settings.default, names)
