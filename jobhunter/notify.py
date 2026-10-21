"""Slack and email alerts for notify decisions. A failed channel is logged (without its secrets) and never stops the
run; send() says whether any channel delivered, so an alert nobody received can be tried again."""
from __future__ import annotations

import html
import os
import re
import smtplib
from urllib.parse import quote
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Callable, Mapping

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


def web_url(url) -> str:
    """A web link with spaces and other characters a link cannot hold percent-encoded (an existing %xx is kept), so
    Slack and mail apps make all of it clickable, not just the part before the first space."""
    return quote(str(url or "").strip(), safe=":/?#[]@!$&'()*+,;=%~")


def _slack_url(url) -> str:
    """A URL for inside Slack's <url|label>: | and > percent-encoded so they cannot end it early, & escaped."""
    return slack_escape(web_url(url).replace("|", "%7C").replace(">", "%3E")).replace("&lt;", "%3C")


def _slack_link(url, label: str = "Open job") -> str:
    """A web link in Slack's own <url|label> form; any other URL is shown as escaped text."""
    if not _is_web(url):
        return slack_escape(url)
    return f"<{_slack_url(url)}|{label}>"


def _summary(job: Job, result: ScoreResult, resume_id: str, scales: Mapping[str, Any] | None = None) -> str:
    """The email's plain-text alert. The model and the cut-offs are left out (Slack keeps them)."""
    return (f"{job.title} at {job.company}\n"
            f"Location: {job.location}\n"
            f"Link: {web_url(job.url) if _is_web(job.url) else job.url}\n\n"
            f"Score: {score_label(result, scales)}\n"
            f"Resume: {resume_id}\n"
            f"Why: {_for_reader(result.reasoning)}\n"
            f"Matched skills: {', '.join(result.matched_skills) or 'none'}\n"
            f"Gaps: {', '.join(result.keyword_gaps) or 'none'}\n"
            f"Source: {job.source} | Posted: {job.posted_at or 'unknown'}")


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
          cover_letter: str | None, scales: Mapping[str, Any] | None = None) -> str:
    """Tables and inline styles only (what mail clients render); every job field is escaped, so a posting cannot
    inject links or buttons of its own."""
    e = lambda s: html.escape(str(s or ""), quote=True)   # noqa: E731
    meta = " · ".join(e(x) for x in (job.company, job.location, job.source,
                                     f"posted {job.posted_at}" if job.posted_at else "") if x)
    # Only a web link gets a button: a scraped javascript: or data: URL would be live in some mail apps.
    open_job = (f'<p><a href="{e(web_url(job.url))}" style="{_BTN};background:#1a73e8;color:#ffffff">Open job ↗</a></p>'
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
        f'<p style="font-size:14px;line-height:1.5">Score {e(score_label(result, scales))} · '
        f'resume: {e(resume_id)}<br>Why: {e(_for_reader(result.reasoning))}<br>'
        f'Matched: {e(", ".join(result.matched_skills) or "none")}<br>'
        f'Gaps: {e(", ".join(result.keyword_gaps) or "none")}</p>'
        f'<p style="font-size:14px;margin:16px 0 6px"><b>Worth it?</b></p><div>{buttons}</div>'
        '<div style="color:#555;font-size:12px">Tap one, then send the email it opens.</div>'
        f'{letter}</td></tr></table></body></html>')


def send_email(cfg: dict, env: Mapping[str, str], subject: str, plain: str, html_body: str,
               log: Callable[[str], None], to: str | None = None, what: str = "") -> bool:
    """One email, plain text plus HTML, from the alert account (Gmail SMTP); `to` defaults to where alerts go. A
    failure is logged by its type only, never with the password, and returns False."""
    sender, alert_to, _ = email_addresses(cfg, env)
    to = to or alert_to
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(sender, env.get(cfg["password_env"], ""))
            server.sendmail(sender, to, msg.as_string())
    except Exception as e:
        log(f"Email send failed [{what or _one_line(subject)}]: {_failure(e)}")
        return False
    return True


class Notifier:
    def __init__(self, settings: NotifySettings, env: Mapping[str, str] | None = None,
                 log: Callable[[str], None] = print, scales: Mapping[str, Any] | None = None):
        self.settings = settings
        self.env = os.environ if env is None else env
        self.log = log
        self.scales = scales

    def send(self, job: Job, result: ScoreResult, resume_id: str, cover_letter: str | None = None) -> bool:
        """True when at least one channel delivered the alert, or none is set up."""
        delivered = []
        if self.settings.slack:
            delivered.append(self._slack(job, result, resume_id, cover_letter))
        if self.settings.email:
            delivered.append(self._email(job, result, resume_id, cover_letter))
        return not delivered or any(delivered)

    def _slack_payload(self, job: Job, result: ScoreResult, resume_id: str, cover_letter: str | None) -> dict:
        """Slack blocks: the title linked to the posting, an Open job button, the facts, Laya's reason, the email's
        feedback links (when email is set up, since verdicts are read from that inbox), then the model and cut-offs.
        Every job field is escaped, so a posting cannot mention anyone or add links; only web links are linked."""
        e, score = slack_escape, slack_escape(score_label(result, self.scales))
        title = _slack_link(job.url, e(job.title)) if _is_web(job.url) else e(job.title)
        posted = str(job.posted_at or "")
        posted = posted[:10] if re.match(r"\d{4}-\d{2}-\d{2}", posted) else posted
        facts = [("Location", job.location), ("Board", " · ".join(x for x in (job.source, posted and f"posted {posted}") if x)),
                 ("Resume", resume_id)]
        blocks: list[dict] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": f":dart: *New match · {score}*\n*{title}* at {e(job.company)}"}},
            {"type": "section", "fields": [{"type": "mrkdwn", "text": f"*{k}*\n{e(v)}"} for k, v in facts if v]},
        ]
        if result.reasoning:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": e(result.reasoning)}})
        if _is_web(job.url):
            blocks.append({"type": "actions", "elements": [{"type": "button", "style": "primary", "url": web_url(job.url),
                                                            "text": {"type": "plain_text", "text": "Open job"}}]})
        if self.settings.email:
            _, _, feedback_to = email_addresses(self.settings.email, self.env)
            links = " · ".join(f"<{_slack_url(mailto(feedback_to, verdict, job))}|{label}>" for verdict, label in BUTTONS)
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"Was it worth it? {links}"}]})
        detail = [f"model {e(result.model)}"]
        if result.matched_skills:
            detail.append(f"matched {e(', '.join(result.matched_skills))}")
        if result.keyword_gaps:
            detail.append(f"gaps {e(', '.join(result.keyword_gaps))}")
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(detail)}]})
        if cover_letter:
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                                                       "text": f"*Cover letter draft*\n{e(cover_letter)[:2800]}"}})
        return {"text": f":dart: New match, {score}: {e(job.title)} at {e(job.company)}", "blocks": blocks}

    def _slack(self, job: Job, result: ScoreResult, resume_id: str, cover_letter: str | None) -> bool:
        url = self.env.get(self.settings.slack["webhook_env"], "")
        try:
            requests.post(url, json=self._slack_payload(job, result, resume_id, cover_letter),
                          timeout=10).raise_for_status()
        except Exception as e:
            self.log(f"Slack send failed [{_one_line(job.title)}]: {_failure(e)}")
            return False
        return True

    def _email(self, job: Job, result: ScoreResult, resume_id: str, cover_letter: str | None) -> bool:
        cfg = self.settings.email
        _, _, feedback_to = email_addresses(cfg, self.env)
        links = [(label, mailto(feedback_to, verdict, job)) for verdict, label in BUTTONS]
        plain = (_summary(job, result, resume_id, scales=self.scales)
                 + "\n\nWorth it? Tap one, then send the email it opens:\n"
                 + "\n".join(f"{label}: {href}" for label, href in links))
        if cover_letter:
            plain += f"\n\nCover letter draft:\n{cover_letter}"
        subject = (f"[{score_label(result, self.scales)}] {_one_line(job.title)} at {_one_line(job.company)} "
                   f"({job.source})")
        return send_email(cfg, self.env, subject, plain,
                          _html(job, result, resume_id, links, cover_letter, scales=self.scales), self.log,
                          what=_one_line(job.title))
