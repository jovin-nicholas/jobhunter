"""Cover letters for the tracker's letter button: the same prompt and writer as alert letters (cover_letters.writer,
with its letter options), built whether or not letters in alerts are on."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Mapping

from jobhunter import cover_letter
from jobhunter.errors import SettingsError
from jobhunter.models import Job
from jobhunter.registry import build_registry
from jobhunter.resumes import load_resumes
from jobhunter.settings import load_settings


def make_letter_writer(settings_path: str | Path, env: Mapping[str, str] | None = None) -> Callable[[dict], str] | None:
    """A function from an application's letter inputs ({title, company, description, resume_id}) to a letter, or None
    when no writer is named. Raises SettingsError when the settings are invalid, the writer cannot write text or the
    resumes cannot be read. The writer itself is on the function as `.writer`."""
    settings = load_settings(settings_path, env)
    name = settings.cover_letters.writer
    spec = next((s for s in settings.scorers if s.name == name), None)
    if spec is None:                # no cover_letters section, so no writer named (a named one must be listed)
        return None
    from jobhunter.pipeline import _construct, build_letter_writer
    registry = build_registry(settings.plugins_dir)
    problems: list[str] = []
    scorer = _construct(f"scorers.{name}", registry.scorers[name], spec.options, problems)
    writer = build_letter_writer(settings, registry, scorer, problems) if scorer is not None else None
    resumes = load_resumes(settings.resumes, settings.data_dir / "resume_cache", env)
    if problems:
        raise SettingsError(problems)

    def write(inputs: dict) -> str:
        job = Job("tracker", inputs["title"], inputs["company"], "", "", description=inputs["description"])
        resume = resumes.resumes.get(inputs.get("resume_id") or "") or resumes.resumes[resumes.default_id]
        return writer.generate(cover_letter.build_prompt(job, resume)).strip()

    write.writer = writer          # type: ignore[attr-defined]
    return write
