"""The tracker's local page and its JSON API. It listens on 127.0.0.1 only; every /api/ call needs the per-launch
token (another site open in the browser can reach localhost, but cannot read the token), and a Host other than
127.0.0.1 or localhost is refused, which stops DNS rebinding."""
from __future__ import annotations

import csv
import hmac
import io
import json
import re
import secrets
import webbrowser
from datetime import date
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from jobhunter.settings import TrackerSettings
from jobhunter.tracker import CLOSED_STAGES, STAGES
from jobhunter.tracker.fetch import title_and_company
from jobhunter.tracker.records import Applications, iso_date
from jobhunter.tracker.state import build_state, follow_up_for

DEFAULT_PORT = 8765
MAX_BODY = 64 * 1024
TOKEN_HEADER = "X-Tracker-Token"
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
       "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
EXPORT_COLUMNS = ("company", "title", "url", "stage", "applied_at", "stage_at", "follow_up_at", "board", "fit",
                  "resume_id", "people", "notes")
_SPLIT = re.compile(r"\s+[—–-]\s+")          # "Acme — Backend Engineer" (but "Rolls-Royce" stays whole)
_APP_PATH = re.compile(r"/api/applications/(\d+)(/stage|/follow-up|/description|/delete)?")


@lru_cache(maxsize=1)
def page_html() -> bytes:
    return files("jobhunter.tracker").joinpath("page.html").read_bytes()


def csv_cell(value: Any) -> str:
    """A cell a spreadsheet will not run as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def parse_quick_add(text: str) -> tuple[str, str, str]:
    """(url, company, title) from what was typed in quick add."""
    text = " ".join((text or "").split())
    if re.match(r"https?://", text, re.I):
        return text, "", ""
    parts = _SPLIT.split(text, maxsplit=1)
    return ("", parts[0], parts[1]) if len(parts) == 2 else ("", text, "")


def export_csv(apps: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    for a in apps:
        writer.writerow([csv_cell(a.get(c)) for c in EXPORT_COLUMNS])
    return out.getvalue()


class TrackerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_port = False          # a second tracker must not share the port: it falls back to a free one

    def __init__(self, apps: Applications, settings: TrackerSettings, *, port: int = DEFAULT_PORT, http: Any = None,
                 today: Callable[[], date] = date.today, token: str | None = None):
        self.apps, self.settings, self.http, self.today = apps, settings, http, today
        self.token = token or secrets.token_urlsafe(24)
        try:
            super().__init__(("127.0.0.1", port), _Handler)
        except OSError:
            super().__init__(("127.0.0.1", 0), _Handler)          # the port is taken: any free one
        self.port = self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"


class _Handler(BaseHTTPRequestHandler):
    server: TrackerServer

    def log_message(self, format: str, *args: Any) -> None:
        pass                                    # quiet: the terminal only shows the address

    def do_GET(self) -> None:
        self._route("GET")

    def do_POST(self) -> None:
        self._route("POST")

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        for name, value in {"Content-Type": content_type, "Content-Length": str(len(body)), "Cache-Control": "no-store",
                            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                            "Content-Security-Policy": CSP, **(extra or {})}.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self.rfile.read(min(length, 16 * MAX_BODY))       # drained, so the client reads the 400 instead of a reset
            raise ValueError("request too large")
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    def _route(self, method: str) -> None:
        parts = urlsplit(self.path)
        port = self.server.port
        if self.headers.get("Host", "") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return self._json(403, {"error": "wrong host"})
        if method == "GET" and parts.path == "/":
            return self._send(200, page_html(), "text/html; charset=utf-8")
        if not parts.path.startswith("/api/"):
            return self._json(404, {"error": "not found"})
        if method == "GET" and parts.path == "/api/export.csv":
            given = self.headers.get(TOKEN_HEADER) or (parse_qs(parts.query).get("t") or [""])[0]
        else:
            given = self.headers.get(TOKEN_HEADER) or ""
        if not hmac.compare_digest(given.encode(), self.server.token.encode()):
            return self._json(403, {"error": "missing or wrong token"})
        try:
            body = self._body() if method == "POST" else {}
            self._api(method, parts.path, body)
        except (ValueError, KeyError) as e:
            self._json(400, {"error": str(e)})

    def _api(self, method: str, path: str, body: dict) -> None:
        apps, settings, today = self.server.apps, self.server.settings, self.server.today()
        m = _APP_PATH.fullmatch(path)
        if method == "GET" and path == "/api/state":
            return self._json(200, build_state(apps.all(), apps.review_queue(settings.review_days), settings, today))
        if method == "GET" and path == "/api/export.csv":
            return self._send(200, export_csv(apps.all()).encode(), "text/csv; charset=utf-8",
                              {"Content-Disposition": 'attachment; filename="applications.csv"'})
        if method == "GET" and m and m.group(2) == "/description":
            return self._json(200, {"text": apps.description(int(m.group(1)))})
        if method == "POST" and path == "/api/applications":
            app_id, existing = self._quick_add(body, today)
            return self._json(200, {"id": app_id, "existing": existing})
        if method == "POST" and m and m.group(2) is None:
            apps.update(int(m.group(1)), **body)
            return self._json(200, {"ok": True})
        if method == "POST" and m and m.group(2) == "/delete":
            apps.delete(int(m.group(1)))
            return self._json(200, {"ok": True})
        if method == "POST" and m and m.group(2) == "/stage":
            return self._json(200, {"event_ids": self._set_stage(int(m.group(1)), body, today)})
        if method == "POST" and m and m.group(2) == "/follow-up":
            app_id = int(m.group(1))
            if body.get("done"):
                ids = [apps.add_event(app_id, "follow_up_done", None, today.isoformat())]
            else:
                ids = [apps.add_event(app_id, "follow_up_set", body.get("date"), today.isoformat())]
            return self._json(200, {"event_ids": ids})
        if method == "POST" and path == "/api/events/delete":
            apps.delete_events([int(i) for i in body.get("ids") or []])
            return self._json(200, {"ok": True})
        if method == "POST" and path == "/api/review":
            return self._json(200, self._review(body, today))
        if method == "POST" and path == "/api/review/undo":
            self._review_undo(body)
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not found"})

    def _quick_add(self, body: dict, today: date) -> tuple[int, bool]:
        """(application id, whether it was already tracked): a repeat opens the open application instead of adding."""
        apps, settings = self.server.apps, self.server.settings
        if len(str(body.get("text") or "")) > 2000:
            raise ValueError("that is too long for a job link or Company — Title")
        url, company, title = parse_quick_add(str(body.get("text") or ""))
        if not (url or company):
            raise ValueError("paste a job link, or type Company — Title")
        duplicate = apps.open_duplicate(url=url, company=company, title=title)
        if duplicate:
            return duplicate, True
        if url:
            job_id = apps.find_job(url)
            if job_id:
                existing = apps.open_for_job(job_id)
                if existing:
                    return existing, True             # already tracked under another link to the same job
                app_id = apps.add_from_job(job_id, today.isoformat())
                apps.record_verdict(job_id, "applied")
                return app_id, False
            if settings.fetch_titles and self.server.http is not None:
                title, company = title_and_company(self.server.http, url)
        return apps.add(title=title, company=company, url=url, applied_at=today.isoformat()), False

    def _set_stage(self, app_id: int, body: dict, today: date) -> list[int]:
        apps, stage = self.server.apps, body.get("stage")
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r} (one of {', '.join(STAGES)})")
        at = iso_date(body.get("at") or today.isoformat())
        ids = [apps.add_event(app_id, "stage", stage, at)]
        follow_up = follow_up_for(stage, date.fromisoformat(at), self.server.settings)
        if follow_up:
            ids.append(apps.add_event(app_id, "follow_up_set", follow_up, at))
        elif stage in CLOSED_STAGES or stage == "offer":
            ids.append(apps.add_event(app_id, "follow_up_done", None, at))
        return ids

    def _review(self, body: dict, today: date) -> dict:
        """What undo needs: the label's message_id, and the application with whether this action created it."""
        apps, job_id, action = self.server.apps, str(body.get("job_id") or ""), body.get("action")
        if action == "applied":
            app_id = apps.open_for_job(job_id)
            created = app_id is None
            if created:
                app_id = apps.add_from_job(job_id, today.isoformat())
            if app_id is None:
                raise ValueError(f"unknown job {job_id!r}")
            return {"message_id": apps.record_verdict(job_id, "applied"), "id": app_id, "created": created}
        if action == "bad":
            return {"message_id": apps.record_verdict(job_id, "bad"), "id": None, "created": False}
        if action == "hide":
            apps.hide_from_review(job_id)
            return {"message_id": None, "id": None, "created": False}
        raise ValueError(f"unknown action {action!r} (applied, bad or hide)")

    def _review_undo(self, body: dict) -> None:
        """Undo of a review action: {message_id, id?} for a or b, {job_id} for hide. The application goes only when
        it is the label's own job and still holds nothing but its "applied" event, so this never becomes a delete."""
        apps, message_id = self.server.apps, body.get("message_id")
        if not message_id:
            if not body.get("job_id"):
                raise ValueError("nothing to undo")
            return apps.unhide(str(body["job_id"]))
        job_id = apps.verdict_job(str(message_id))
        app_id = int(body["id"]) if body.get("id") is not None else None
        if app_id is not None:
            app = apps.get(app_id)
            if app is None or job_id is None or app["job_id"] != job_id or len(app["events"]) != 1:
                raise ValueError("that application was not just added by this label, so it stays")
        apps.delete_verdict(str(message_id))
        if app_id is not None:
            apps.delete_application(app_id)


def serve(apps: Applications, settings: TrackerSettings, *, port: int, http: Any, open_browser: bool,
          log: Callable[[str], None] = print) -> None:
    server = TrackerServer(apps, settings, port=port, http=http)
    log(f"jobhunter tracker: {server.url}")
    log("Only this computer can open it. Ctrl+C stops it.")
    if open_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
