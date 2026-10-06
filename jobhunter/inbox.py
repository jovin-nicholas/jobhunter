"""Reads feedback verdicts (feedback.py) from the sending Gmail account over IMAP, saves them, then labels the
messages jobhunter/feedback and takes them out of the inbox. A failure is logged and never stops the run."""
from __future__ import annotations

import hashlib
import imaplib
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from typing import Any, Callable, Mapping

from jobhunter.feedback import LABEL, parse_subject
from jobhunter.notify import email_addresses
from jobhunter.settings import NotifySettings

_ALL_MAIL = "[Gmail]/All Mail"            # the English name; other languages are found by the \All attribute
_LIST_RE = re.compile(rb'\((?P<flags>[^)]*)\) "[^"]*" "?(?P<name>[^"]+)"?$')
# One FETCH for every message; PEEK so reading does not mark it read; INTERNALDATE (when Gmail received it) for a
# message without a Date header.
_HEADERS = "(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID AUTHENTICATION-RESULTS)])"
_UID_RE = re.compile(rb"\bUID (\d+)")
# Gmail's own Authentication-Results header ("mx.google.com; dkim=pass header.i=@example.com; spf=pass ..."); one
# naming another server could have been written by the sender.
GMAIL_AUTHSERV = "mx.google.com"
_PASS_RE = re.compile(r"^\s*(?:dkim|spf|dmarc)\s*=\s*pass\b(?P<rest>.*)$", re.I | re.S)
_IDENTITY_RE = re.compile(r"\b(?:header\.i|header\.d|header\.from|smtp\.mailfrom)\s*=\s*(?P<id>[^\s;]+)", re.I)


class _Refused(Exception):
    """Gmail answered NO (imaplib returns that instead of raising)."""

    def __init__(self, what: str, data: Any):
        detail = data[0] if data else b""
        detail = detail.decode(errors="replace") if isinstance(detail, bytes) else str(detail)
        super().__init__(f"feedback: could not {what} (NO: {detail[:200]})")


def _ok(reply: tuple[str, Any], what: str) -> Any:
    typ, data = reply
    if typ != "OK":
        raise _Refused(what, data)
    return data


@dataclass
class FeedbackSummary:
    saved: Counter = field(default_factory=Counter)
    ignored: Counter = field(default_factory=Counter)

    def line(self) -> str | None:
        """The run log's feedback line, or None when nothing was found."""
        if not self.saved and not self.ignored:
            return None
        detail = ", ".join(f"{n} {v}" for v, n in sorted(self.saved.items()))
        text = f"feedback: {sum(self.saved.values())} saved" + (f" ({detail})" if detail else "")
        return text + (f", {sum(self.ignored.values())} ignored" if self.ignored else "")


def _all_mail(conn: Any) -> str:
    _, boxes = conn.list()
    for line in boxes or []:
        if not isinstance(line, bytes):     # a name sent as a literal arrives as a (line, literal) tuple
            continue
        m = _LIST_RE.search(line)
        if m and b"\\All" in m.group("flags"):
            return m.group("name").decode()
    return _ALL_MAIL


def _text(value: str | None) -> str:
    """A header as text, decoding =?UTF-8?...?= words."""
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def _when(value: str | None, received: bytes = b"") -> str:
    """The Date header as ISO UTC; without one, Gmail's INTERNALDATE from the FETCH line; else now."""
    try:
        dt = parsedate_to_datetime(value)
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    when = imaplib.Internaldate2tuple(received) if received else None
    if when:
        return datetime.fromtimestamp(time.mktime(when), tz=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def authenticated(msg: Any, sender: str) -> bool | None:
    """Whether Gmail's Authentication-Results header shows dkim=pass, spf=pass or dmarc=pass for the From address's
    domain or a subdomain of it (mail.example.com for example.com); None when Gmail added no such header (a message
    sent to yourself from Gmail may have none)."""
    gmail = [str(h) for h in msg.get_all("Authentication-Results") or []
             if str(h).strip().lower().startswith(GMAIL_AUTHSERV)]
    if not gmail:
        return None
    domain = sender.rpartition("@")[2].lower()
    for part in gmail[0].split(";")[1:]:            # the topmost one is Gmail's, added when the message arrived
        m = _PASS_RE.match(part)
        if not m:
            continue
        for identity in _IDENTITY_RE.findall(m.group("rest")):
            found = identity.rpartition("@")[2].lower().strip(".")
            if domain and (found == domain or found.endswith("." + domain)):
                return True
    return False


def _addresses(value: str) -> set[str]:
    """Every address in a header-style value: "Name <a@b>, c@d" gives {"a@b", "c@d"}, lower case."""
    return {addr.lower() for _, addr in getaddresses([value or ""]) if addr}


def read_feedback(settings: NotifySettings, env: Mapping[str, str], store: Any, log: Callable[[str], None] = print,
                  imap_factory: Callable[..., Any] = imaplib.IMAP4_SSL) -> FeedbackSummary:
    summary = FeedbackSummary()
    cfg = settings.email
    if not cfg:
        return summary
    sender, _, feedback_to = email_addresses(cfg, env)
    # Taps come from the sending account, or from the address alerts go to when that is another account.
    allowed = _addresses(sender) | (_addresses(env.get(cfg["to_env"], "")) if cfg.get("to_env") else set())
    conn = None
    try:
        conn = imap_factory("imap.gmail.com", 993, timeout=30)
        try:
            conn.login(sender, env.get(cfg["password_env"], ""))
        except imaplib.IMAP4.error as e:     # its message is the server's reason, never the password
            log(f"feedback: Gmail refused the login ({str(e)[:200]}); check the app password and that IMAP is on")
            return summary
        _ok(conn.select(f'"{_all_mail(conn)}"'), "open All Mail")
        # All Mail, not the inbox: Gmail may not put a message sent to yourself in the inbox.
        data = _ok(conn.uid("SEARCH", "X-GM-RAW", f'"to:{feedback_to} -label:{LABEL.replace("/", "-")}"'),
                   "search the inbox")
        uids = (data[0] or b"").decode().split() if data else []
        if not uids:
            return summary
        rows, unchecked, refused = [], 0, 0
        _, parts = conn.uid("FETCH", ",".join(uids), _HEADERS)
        for prefix, raw in (p for p in parts or [] if isinstance(p, tuple) and len(p) == 2):
            if not _UID_RE.search(prefix or b""):
                continue
            msg = message_from_bytes(raw or b"")
            parsed = parse_subject(_text(msg["Subject"]))
            from_address = parseaddr(msg["From"] or "")[1].lower()
            checked = authenticated(msg, from_address) if from_address in allowed else None
            if from_address not in allowed:
                summary.ignored["foreign sender"] += 1
            elif checked is False:
                summary.ignored["not authenticated"] += 1     # the From address may be forged
                refused += 1
            elif not parsed:
                summary.ignored["bad subject"] += 1
            else:
                mid = (msg["Message-ID"] or "").strip() or "sha1:" + hashlib.sha1(raw).hexdigest()
                rows.append({"job_id": parsed[1], "verdict": parsed[0], "received_at": _when(msg["Date"], prefix),
                             "message_id": mid})
                unchecked += checked is None
        if refused:
            log(f"feedback: {refused} message(s) from your address failed Gmail's DKIM, SPF and DMARC checks; "
                "ignored")
        if unchecked:
            log(f"feedback: {unchecked} message(s) had no Authentication-Results header from Gmail; trusted by "
                "their From address alone")
        known = store.existing_ids([r["job_id"] for r in rows]) if rows else set()
        if any(r["job_id"] not in known for r in rows):
            summary.ignored["unknown job"] += sum(r["job_id"] not in known for r in rows)
        for r in store.add_feedback([r for r in rows if r["job_id"] in known]):
            summary.saved[r["verdict"]] += 1
        # Labelled only after saving: if this fails, the next run finds them again and the unique message id
        # keeps them from being saved twice. Ignored messages are labelled too, so they are not checked every run.
        conn.create(f'"{LABEL}"')                          # NO when the label exists; harmless
        uid_set = ",".join(uids)
        _ok(conn.uid("STORE", uid_set, "+X-GM-LABELS", f'("{LABEL}")'), "label the messages")
        _ok(conn.uid("STORE", uid_set, "-X-GM-LABELS", "(\\Inbox)"), "archive the messages")
    except _Refused as e:
        log(str(e))
    except Exception as e:
        log(f"feedback: could not read the inbox ({type(e).__name__}: {str(e)[:200]})")
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:
                pass
    return summary
