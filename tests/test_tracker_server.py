import csv
import http.client
import io
import json
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path

from jobhunter.models import Job
from jobhunter.settings import TrackerSettings
from jobhunter.store import Store
from jobhunter.tracker.records import Applications
from jobhunter.tracker.server import TrackerServer, csv_cell, parse_quick_add


class ServerTestCase(unittest.TestCase):
    settings = TrackerSettings()
    http = None

    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / "jobs.db")
        self.store.save(Job("dice_1", "Backend Engineer", "Acme", "Remote", "https://jobs.example.com/acme/1?gh_jid=1",
                            description="Java services", source="dice"), "notified")
        self.apps = Applications(self.store, today=lambda: date(2026, 10, 6))
        self.server = TrackerServer(self.apps, self.settings, port=0, http=self.http, today=lambda: date(2026, 10, 6),
                                    token="secret-token")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None, token="secret-token", host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.server.port}", "Content-Type": "application/json"}
        if token:
            headers["X-Tracker-Token"] = token
        conn.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, resp, data


class TestSecurity(ServerTestCase):
    def test_a_missing_or_wrong_token_is_refused(self):
        self.assertEqual(self.call("GET", "/api/state", token=None)[0], 403)
        self.assertEqual(self.call("GET", "/api/state", token="guess")[0], 403)
        self.assertEqual(self.call("GET", "/api/state")[0], 200)

    def test_a_foreign_host_is_refused_even_with_the_token(self):
        self.assertEqual(self.call("GET", "/api/state", host=f"evil.example:{self.server.port}")[0], 403)
        self.assertEqual(self.call("GET", "/", host="evil.example")[0], 403)
        self.assertEqual(self.call("GET", "/api/state", host=f"localhost:{self.server.port}")[0], 200)

    def test_the_page_is_served_with_a_strict_policy(self):
        status, resp, body = self.call("GET", "/", token=None)
        self.assertEqual(status, 200)
        self.assertIn(b"jobhunter", body)
        self.assertIn("default-src 'none'", resp.getheader("Content-Security-Policy"))
        self.assertEqual(resp.getheader("X-Content-Type-Options"), "nosniff")

    def test_it_listens_on_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_a_taken_port_falls_back_to_a_free_one(self):
        other = TrackerServer(self.apps, self.settings, port=self.server.port, token="t")
        self.addCleanup(other.server_close)
        self.assertNotEqual(other.port, self.server.port)
        self.assertIn(f"127.0.0.1:{other.port}/?t=t", other.url)

    def test_a_huge_body_is_refused(self):
        self.assertEqual(self.call("POST", "/api/applications", {"text": "x" * 70000})[0], 400)

    def test_a_query_token_is_accepted_only_for_the_csv_export(self):
        self.assertEqual(self.call("GET", "/api/state?t=secret-token", token=None)[0], 403)
        self.assertEqual(self.call("POST", "/api/applications?t=secret-token",
                                   {"text": "Globex — Platform Engineer"}, token=None)[0], 403)
        self.assertEqual(self.apps.all(), [])


class TestApi(ServerTestCase):
    def test_quick_add_matches_a_stored_job_with_and_without_its_query(self):
        for url in ("https://jobs.example.com/acme/1?gh_jid=1", "https://jobs.example.com/acme/1/?utm_source=x"):
            with self.subTest(url=url):
                status, _, body = self.call("POST", "/api/applications", {"text": url})
                self.assertEqual(status, 200)
                app = self.apps.get(json.loads(body)["id"])
                self.assertEqual((app["job_id"], app["company"]), ("dice_1", "Acme"))
        self.assertEqual(len(self.apps.all()), 1)       # the second paste opened the same open application
        self.assertEqual([r["verdict"] for r in self.store.latest_feedback()], ["applied"])

    def test_quick_add_of_company_and_title_and_of_an_unknown_link(self):
        _, _, body = self.call("POST", "/api/applications", {"text": "Globex — Platform Engineer"})
        app = self.apps.get(json.loads(body)["id"])
        self.assertEqual((app["company"], app["title"], app["applied_at"]), ("Globex", "Platform Engineer", "2026-10-06"))
        _, _, body = self.call("POST", "/api/applications", {"text": "https://careers.example.org/42"})
        app = self.apps.get(json.loads(body)["id"])
        self.assertEqual((app["url"], app["title"], app["job_id"]), ("https://careers.example.org/42", "", None))
        self.assertEqual(self.call("POST", "/api/applications", {"text": "  "})[0], 400)

    def test_stage_change_sets_the_follow_up_and_undo_removes_both(self):
        app_id = self.apps.add(title="SWE", company="Initech", applied_at="2026-10-01")
        status, _, body = self.call("POST", f"/api/applications/{app_id}/stage", {"stage": "screen"})
        ids = json.loads(body)["event_ids"]
        self.assertEqual((status, len(ids)), (200, 2))
        self.assertEqual((self.apps.get(app_id)["stage"], self.apps.get(app_id)["follow_up_at"]), ("screen", "2026-10-09"))
        self.call("POST", "/api/events/delete", {"ids": ids})
        self.assertEqual((self.apps.get(app_id)["stage"], self.apps.get(app_id)["follow_up_at"]), ("applied", None))
        self.assertEqual(self.store.latest_feedback(), [])                    # stage changes write no feedback
        self.assertEqual(self.call("POST", f"/api/applications/{app_id}/stage", {"stage": "hired"})[0], 400)

    def test_closing_clears_the_follow_up(self):
        app_id = self.apps.add(title="SWE", company="Initech", applied_at="2026-10-01")
        self.call("POST", f"/api/applications/{app_id}/follow-up", {"date": "2026-10-08"})
        self.call("POST", f"/api/applications/{app_id}/stage", {"stage": "rejected"})
        self.assertIsNone(self.apps.get(app_id)["follow_up_at"])

    def test_review_actions(self):
        status, _, body = self.call("POST", "/api/review", {"job_id": "dice_1", "action": "applied"})
        self.assertEqual(status, 200)
        self.assertEqual(self.apps.get(json.loads(body)["id"])["job_id"], "dice_1")
        state = json.loads(self.call("GET", "/api/state")[2])
        self.assertEqual(state["review"], [])
        self.assertEqual(self.call("POST", "/api/review", {"job_id": "dice_1", "action": "maybe"})[0], 400)

    def test_a_date_out_of_range_is_a_400(self):
        app_id = self.apps.add(title="SWE", company="Initech", applied_at="2026-10-01")
        for path, body in ((f"/api/applications/{app_id}", {"applied_at": "0002-10-06"}),
                           (f"/api/applications/{app_id}/follow-up", {"date": "0002-10-06"}),
                           (f"/api/applications/{app_id}/stage", {"stage": "screen", "at": "0002-10-06"})):
            with self.subTest(path=path):
                self.assertEqual(self.call("POST", path, body)[0], 400)
        app = self.apps.get(app_id)
        self.assertEqual((app["applied_at"], app["stage"], app["follow_up_at"], len(app["events"])),
                         ("2026-10-01", "applied", None, 1))

    def review(self, action):
        status, _, body = self.call("POST", "/api/review", {"job_id": "dice_1", "action": action})
        self.assertEqual(status, 200)
        return json.loads(body)

    def queue(self):
        return [r["job_id"] for r in json.loads(self.call("GET", "/api/state")[2])["review"]]

    def test_undo_of_a_bad_match_removes_the_label_and_the_job_is_back_to_review(self):
        r = self.review("bad")
        self.assertEqual((r["id"], r["created"]), (None, False))
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.call("POST", "/api/review/undo", {"message_id": r["message_id"]})[0], 200)
        self.assertEqual(self.store.latest_feedback(), [])
        self.assertEqual(self.queue(), ["dice_1"])

    def test_undo_of_applied_removes_the_application_it_created_and_the_label(self):
        r = self.review("applied")
        self.assertTrue(r["created"])
        self.assertEqual(self.call("POST", "/api/review/undo", {"message_id": r["message_id"], "id": r["id"]})[0], 200)
        self.assertEqual((self.apps.all(), self.store.latest_feedback(), self.queue()), ([], [], ["dice_1"]))

    def test_undo_of_applied_keeps_an_application_it_did_not_create(self):
        app_id = self.apps.add_from_job("dice_1", "2026-10-01")
        r = self.review("applied")
        self.assertEqual((r["id"], r["created"]), (app_id, False))
        self.call("POST", "/api/review/undo", {"message_id": r["message_id"]})
        self.assertEqual([a["id"] for a in self.apps.all()], [app_id])
        self.assertEqual(self.store.latest_feedback(), [])

    def test_undo_never_deletes_an_application_that_is_not_the_labels_own(self):
        other = self.apps.add(title="SWE", company="Initech", applied_at="2026-10-01")
        r = self.review("bad")
        self.assertEqual(self.call("POST", "/api/review/undo", {"message_id": r["message_id"], "id": other})[0], 400)
        self.assertEqual([a["id"] for a in self.apps.all()], [other])

    def test_undo_of_hide_shows_the_job_again(self):
        r = self.review("hide")
        self.assertEqual(self.queue(), [])
        self.assertEqual(self.call("POST", "/api/review/undo", {"job_id": "dice_1"})[0], 200)
        self.assertEqual(self.queue(), ["dice_1"])
        self.assertEqual(r["message_id"], None)

    def test_undo_refuses_a_label_that_is_not_the_trackers(self):
        self.store.add_feedback([{"job_id": "dice_1", "verdict": "good", "received_at": "2026-10-05T10:00:00+00:00",
                                  "message_id": "<m1@mail.example.com>"}])
        self.assertEqual(self.call("POST", "/api/review/undo", {"message_id": "<m1@mail.example.com>"})[0], 400)
        self.assertEqual([r["verdict"] for r in self.store.latest_feedback()], ["good"])

    def test_edit_and_description(self):
        app_id = self.apps.add_from_job("dice_1", "2026-10-01")
        self.assertEqual(self.call("POST", f"/api/applications/{app_id}", {"notes": "Call Friday"})[0], 200)
        self.assertEqual(self.apps.get(app_id)["notes"], "Call Friday")
        self.assertEqual(self.call("POST", f"/api/applications/{app_id}", {"stage": "offer"})[0], 400)
        self.assertEqual(json.loads(self.call("GET", f"/api/applications/{app_id}/description")[2]),
                         {"text": "Java services"})

    def test_export_is_csv_with_formula_cells_escaped(self):
        self.apps.add(title="=HYPERLINK(\"http://x\")", company="+Acme", applied_at="2026-10-01")
        status, resp, body = self.call("GET", "/api/export.csv?t=secret-token", token=None)
        self.assertEqual(status, 200)
        self.assertIn("attachment", resp.getheader("Content-Disposition"))
        rows = list(csv.DictReader(io.StringIO(body.decode())))
        self.assertEqual((rows[0]["company"], rows[0]["title"]), ("'+Acme", "'=HYPERLINK(\"http://x\")"))


class TestFetchSetting(ServerTestCase):
    class Fetcher:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kw):
            self.calls.append(url)
            raise OSError("no network in tests")

    def test_off_by_default_makes_no_request(self):
        self.server.http = fetcher = self.Fetcher()
        self.call("POST", "/api/applications", {"text": "https://careers.example.org/42"})
        self.assertEqual(fetcher.calls, [])

    def test_on_a_failing_fetch_still_adds_the_application(self):
        self.server.settings = TrackerSettings(fetch_titles=True)
        self.server.http = fetcher = self.Fetcher()
        status, _, body = self.call("POST", "/api/applications", {"text": "https://93.184.216.34/42"})  # no DNS
        self.assertEqual(status, 200)
        self.assertEqual(self.apps.get(json.loads(body)["id"])["title"], "")
        self.assertEqual(len(fetcher.calls), 1)


class TestHelpers(unittest.TestCase):
    def test_csv_cell(self):
        self.assertEqual([csv_cell(v) for v in ("=1+1", "+x", "-x", "@x", "\tx", "Acme", None, 7.5)],
                         ["'=1+1", "'+x", "'-x", "'@x", "'\tx", "Acme", "", "7.5"])

    def test_parse_quick_add(self):
        self.assertEqual(parse_quick_add(" https://e.example/1 "), ("https://e.example/1", "", ""))
        self.assertEqual(parse_quick_add("Acme — Backend Engineer"), ("", "Acme", "Backend Engineer"))
        self.assertEqual(parse_quick_add("Rolls-Royce - Engineer"), ("", "Rolls-Royce", "Engineer"))
        self.assertEqual(parse_quick_add("Acme"), ("", "Acme", ""))


if __name__ == "__main__":
    unittest.main()
