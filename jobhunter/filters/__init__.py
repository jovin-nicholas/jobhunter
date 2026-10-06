"""Filters run before any model: location, then seniority, then keywords, then employment, then (when set up) the System One
checker. The first skip stops the chain."""
from __future__ import annotations

from typing import Protocol

from jobhunter.models import FilterResult, Job
from jobhunter.settings import FilterSettings


class Filter(Protocol):
    name: str

    def check(self, job: Job) -> FilterResult: ...


def build_filters(settings: FilterSettings) -> list[Filter]:
    """Filters in run order; raises SettingsError for problems only detectable here (unknown country codes)."""
    from jobhunter.filters.employment import EmploymentFilter
    from jobhunter.filters.keywords import KeywordFilter
    from jobhunter.filters.location import LocationFilter
    from jobhunter.filters.seniority import SeniorityFilter

    from jobhunter.filters.systemone import SystemOneFilter
    from jobhunter.systemone import SystemOneClient

    model = SystemOneClient(settings.systemone) if settings.systemone is not None else None
    filters: list[Filter] = []
    if settings.location is not None:
        filters.append(LocationFilter(settings.location, model))
    if settings.seniority is not None:
        filters.append(SeniorityFilter(settings.seniority))
    if settings.keywords is not None:
        filters.append(KeywordFilter(settings.keywords))
    if settings.employment is not None:
        filters.append(EmploymentFilter(settings.employment))
    if model is not None:
        # The students question only matters when internships are not wanted.
        levels = settings.seniority.levels if settings.seniority is not None else None
        location = next((f for f in filters if isinstance(f, LocationFilter)), None)
        filters.append(SystemOneFilter(model, ask_students=levels is not None and "intern" not in levels,
                                       skip_if=list(settings.systemone.skip_if),
                                       country=location.countries_text if location else "the country where the job is based"))
    return filters
