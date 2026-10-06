"""Exceptions shared across the app."""


class SettingsError(Exception):
    """Every problem found in settings, plugins or resumes. The run stops before any network call."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("\n".join(self.problems))


class ScorerError(Exception):
    """A scorer could not score this job; the chain moves on to the next scorer."""


class ScorerUnavailable(ScorerError):
    """The scorer could not run at all (model not installed, server not running). The job is retried next run."""


class ScorerBusy(ScorerUnavailable):
    """The scorer is running but did not answer in time or failed on its side (a read timeout, HTTP 5xx). The job is
    retried next run like any unavailable one, but the scorer is not switched off for the rest of the run."""


class RateLimited(ScorerError):
    """The scorer's provider is rate-limiting. If every scorer fails, the job is retried on the next run."""


class BoardError(Exception):
    """A board's request failed."""


class BoardSkipped(BoardError):
    """A board cannot run on this machine (e.g. LinkedIn without Node.js). Reported as skipped, not failed."""
