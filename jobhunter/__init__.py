"""jobhunter: local-first job matching. Plugins import the public API from here."""
from jobhunter.errors import BoardError, RateLimited, ScorerError, ScorerUnavailable
from jobhunter.models import Job, Resume, ScoreResult, SearchContext
from jobhunter.registry import board, scorer

__all__ = ["BoardError", "Job", "RateLimited", "Resume", "ScoreResult", "ScorerError", "ScorerUnavailable", "SearchContext", "board", "scorer"]
