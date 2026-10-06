"""Slack and email alerts for notify decisions. A failed channel is logged (without its secrets) and never stops the
run; send() says whether any channel delivered, so an alert nobody received can be tried again."""
from __future__ import annotations

import html
import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Callable, Mapping

import requests

from jobhunter.feedback import BUTTONS, DEFAULT_ALERT_TAG, DEFAULT_FEEDBACK_TAG, mailto, plus_address
from jobhunter.models import Job, ScoreResult, score_label
from jobhunter.settings import NotifySettings


def _one_line(text: str) -> str:
    """Line breaks would end an email header early ("header value appears to contain an embedded header")."""
    return " ".join((text or "").split())


def _failure(e: Exception) -> str:
    """What went wrong, never the webhook URL or server text that may quote it."""
    status = getattr(getattr(e, "response", None), "status_code", None)
    return f"{type(e).__name__}" + (f" (HTTP {status})" if status else "")


# A scorer's cut-offs in its reasoning, "(alert at 6.07, save at 4.86)": useful in logs, noise in an email.
_CUTOFFS = re.compile(r"\s*\(alert at [^)]*\)")


def _for_reader(reasoning: str) -> str:
    """Reasoning as the email shows it: without the scorer's cut-offs."""
    return _CUTOFFS.sub("", reasoning or "")


def _is_web(url) -> bool:
    return str(url or "").lower().startswith(("https://", "http://"))


def slack_escape(text) -> str:
    """Slack's three control characters, so a posting cannot @channel, mention people or add links of its own."""
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _slack_link(url) -> str:
    """A web link in Slack's own <url|label> form, with | and > percent-encoded so they cannot end it early; any
    other URL is shown as escaped text."""
    if not _is_web(url):
        return slack_escape(url)
    return f"<{slack_escape(str(url).replace('|', '%7C').replace('>', '%3E')).replace('&lt;', '%3C')}|Open job>"


def _summary(job: Job, result: ScoreResult, resume_id: str, for_email: bool = False) -> str:
    """The alert text. Slack keeps the model and the cut-offs, with every field escaped for Slack; the email leaves
    both out."""
    e = (lambda s: str(s)) if for_email else slack_escape
    model = "" if for_email else f" ({e(result.model)})"
    why = _for_reader(result.reasoning) if for_email else e(result.reasoning)
    link = job.url if for_email else _slack_link(job.url)
    return (f"{e(job.title)} at {e(job.company)}\n"
            f"Location: {e(job.location)}\n"
            f"Link: {link}\n\n"
            f"Score: {e(score_label(result))}{model}\n"
            f"Resume: {e(resume_id)}\n"
            f"Why: {why}\n"
            f"Matched skills: {e(', '.join(result.matched_skills) or 'none')}\n"
            f"Gaps: {e(', '.join(result.keyword_gaps) or 'none')}\n"
            f"Source: {e(job.source)} | Posted: {e(job.posted_at or 'unknown')}")


def email_addresses(cfg: dict, env: Mapping[str, str]) -> tuple[str, str, str]:
    """(sender, where alerts go, where feedback goes). Alerts go to to_env when set, else the sender's alert plus
    address; feedback always goes to the sender's own mailbox, the only one jobhunter can read."""
    sender = env.get(cfg["from_env"], "")
    alert_to = (env.get(cfg["to_env"], "") if cfg.get("to_env")
                else plus_address(sender, cfg.get("alert_tag", DEFAULT_ALERT_TAG)))
    return sender, alert_to, plus_address(sender, cfg.get("feedback_tag", DEFAULT_FEEDBACK_TAG))


_BTN = ("display:inline-block;padding:8px 12px;margin:0 6px 6px 0;border-radius:6px;background:#eef1f5;"
        "color:#1a1a1a;text-decoration:none;font-size:14px")


def _html(job: Job, result: ScoreResult, resume_id: str, links: list[tuple[str, str]],
          cover_letter: str | None) -> str:
    """Tables and inline styles only (what mail clients render); every job field is escaped, so a posting cannot
    inject links or buttons of its own."""
    e = lambda s: html.escape(str(s or ""), quote=True)   # noqa: E731
    meta = " · ".join(e(x) for x in (job.company, job.location, job.source,
                                     f"posted {job.posted_at}" if job.posted_at else "") if x)
    # Only a web link gets a button: a scraped javascript: or data: URL would be live in some mail apps.
    open_job = (f'<p><a href="{e(job.url)}" style="{_BTN};background:#1a73e8;color:#ffffff">Open job ↗</a></p>'
                if _is_web(job.url) else "")
    buttons = "".join(f'<a href="{e(href)}" style="{_BTN}">{e(label)}</a>' for label, href in links)
    letter = (f'<h3 style="font-size:15px;margin:20px 0 6px">Cover letter draft</h3>'
              f'<div style="white-space:pre-wrap">{e(cover_letter)}</div>') if cover_letter else ""
    return (
        '<!doctype html><html><body style="margin:0;padding:16px;font-family:-apple-system,Segoe UI,Arial,'
        'sans-serif;color:#1a1a1a;background:#ffffff"><table role="presentation" width="100%" '
        'style="max-width:600px">'
        f'<tr><td><h2 style="font-size:18px;margin:0 0 4px">{e(job.title)}</h2>'
        f'<div style="color:#555;font-size:13px">{meta}</div>'
        f'{open_job}'
        f'<p style="font-size:14px;line-height:1.5">Score {e(score_label(result))} · '
        f'resume: {e(resume_id)}<br>Why: {e(_for_reader(result.reasoning))}<br>'
        f'Matched: {e(", ".join(result.matched_skills) or "none")}<br>'
        f'Gaps: {e(", ".join(result.keyword_gaps) or "none")}</p>'
        f'<p style="font-size:14px;margin:16px 0 6px"><b>Worth it?</b></p><div>{buttons}</div>'
        '<div style="color:#555;font-size:12px">Tap one, then send the email it opens.</div>'
        f'{letter}</td></tr></table></body></html>')


class Notifier:
    def __init__(self, settings: NotifySettings, env: Mapping[str, str] | None = None,
                 log: Callable[[str], None] = print):
        self.settings = settings
        self.env = os.environ if env is None else env
        self.log = log

    def send(self, job: Job, result: ScoreResult, resume_id: str, cover_letter: str | None = None) -> bool:
        """True when at least one channel delivered the alert, or none is set up."""
        body = _summary(job, result, resume_id)
        if cover_letter:
            body += f"\n\nCover letter draft:\n{slack_escape(cover_letter)}"
        delivered = []
        if self.settings.slack:
            delivered.append(self._slack(job, result, body))
        if self.settings.email:
            delivered.append(self._email(job, result, resume_id, cover_letter))
        return not delivered or any(delivered)

    def _slack(self, job: Job, result: ScoreResult, body: str) -> bool:
        url = self.env.get(self.settings.slack["webhook_env"], "")
        try:
            requests.post(url, json={"text": f":dart: *New match, {slack_escape(score_label(result))}*\n{body}"},
                          timeout=10).raise_for_status()
        except Exception as e:
            self.log(f"Slack send failed [{_one_line(job.title)}]: {_failure(e)}")
            return False
        return True

    def _email(self, job: Job, result: ScoreResult, resume_id: str, cover_letter: str | None) -> bool:
        cfg = self.settings.email
        sender, to, feedback_to = email_addresses(cfg, self.env)
        password = self.env.get(cfg["password_env"], "")
        links = [(label, mailto(feedback_to, verdict, job)) for verdict, label in BUTTONS]
        plain = (_summary(job, result, resume_id, for_email=True) + "\n\nWorth it? Tap one, then send the email it opens:\n"
                 + "\n".join(f"{label}: {href}" for label, href in links))
        if cover_letter:
            plain += f"\n\nCover letter draft:\n{cover_letter}"
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(_html(job, result, resume_id, links, cover_letter), "html", "utf-8"))
        msg["Subject"] = f"[{score_label(result)}] {_one_line(job.title)} at {_one_line(job.company)} ({job.source})"
        msg["From"], msg["To"] = sender, to
        try:
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.login(sender, password)
                server.sendmail(sender, to, msg.as_string())
        except Exception as e:
            self.log(f"Email send failed [{_one_line(job.title)}]: {_failure(e)}")
            return False
        return True
