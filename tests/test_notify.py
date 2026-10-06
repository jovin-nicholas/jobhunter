import email
import unittest
from unittest.mock import MagicMock, patch

from jobhunter.models import Job, ScoreResult
from jobhunter.notify import Notifier
from jobhunter.settings import NotifySettings
from tests.helpers import FakeResponse

JOB = Job("dice_1", "Backend Engineer", "Acme", "Austin, TX", "https://example.com/1", source="dice")
RESULT = ScoreResult(score=8, model="laya:/m", decision="notify", reasoning="Strong Java fit.",
                     matched_skills=["java"], keyword_gaps=["go"])
ENV = {"SLACK_WEBHOOK_URL": "https://hooks.example/abc", "GMAIL_ADDRESS": "sender@example.com",
       "GMAIL_APP_PASSWORD": "app-pass", "NOTIFY_EMAIL": "john.doe@example.com"}
BOTH = NotifySettings(slack={"webhook_env": "SLACK_WEBHOOK_URL"},
                      email={"from_env": "GMAIL_ADDRESS", "password_env": "GMAIL_APP_PASSWORD", "to_env": "NOTIFY_EMAIL"})


class TestNotifier(unittest.TestCase):
    def test_slack_and_email_get_the_job_and_score(self):
        smtp = MagicMock()
        with patch("jobhunter.notify.requests.post", return_value=FakeResponse()) as post, \
             patch("jobhunter.notify.smtplib.SMTP", return_value=smtp):
            Notifier(BOTH, env=ENV).send(JOB, RESULT, "backend.txt")
        self.assertEqual(post.call_args.args[0], "https://hooks.example/abc")
        self.assertIn("Backend Engineer", post.call_args.kwargs["json"]["text"])
        self.assertIn("8/10", post.call_args.kwargs["json"]["text"])
        session = smtp.__enter__.return_value
        session.login.assert_called_once_with("sender@example.com", "app-pass")
        sent = session.sendmail.call_args.args
        self.assertEqual(sent[:2], ("sender@example.com", "john.doe@example.com"))
        self.assertIn("[8/10] Backend Engineer at Acme", sent[2])

    def test_slack_failure_does_not_stop_email(self):
        logs, smtp = [], MagicMock()
        with patch("jobhunter.notify.requests.post", side_effect=OSError("network down")), \
             patch("jobhunter.notify.smtplib.SMTP", return_value=smtp):
            Notifier(BOTH, env=ENV, log=logs.append).send(JOB, RESULT, "backend.txt")
        smtp.__enter__.return_value.sendmail.assert_called_once()
        self.assertTrue(any("Slack" in line and "OSError" in line for line in logs), logs)

    def test_nothing_configured_sends_nothing(self):
        with patch("jobhunter.notify.requests.post") as post, patch("jobhunter.notify.smtplib.SMTP") as smtp:
            Notifier(NotifySettings(), env=ENV).send(JOB, RESULT, "backend.txt")
        post.assert_not_called()
        smtp.assert_not_called()

    def test_probability_result_shows_fit_percentage(self):
        result = ScoreResult(score=None, model="laya", decision="notify", probability=0.72)
        smtp = MagicMock()
        with patch("jobhunter.notify.requests.post", return_value=FakeResponse()) as post, \
             patch("jobhunter.notify.smtplib.SMTP", return_value=smtp):
            Notifier(BOTH, env=ENV).send(JOB, result, "resume.txt")
        self.assertIn("fit 72%", post.call_args.kwargs["json"]["text"])
        raw = smtp.__enter__.return_value.sendmail.call_args.args[2]
        self.assertIn("[fit 72%]", email.message_from_string(raw)["Subject"])
        self.assertNotIn("None/10", post.call_args.kwargs["json"]["text"])


class TestDelivery(unittest.TestCase):
    def test_send_says_whether_any_channel_delivered(self):
        smtp = MagicMock()
        with patch("jobhunter.notify.requests.post", side_effect=OSError("down")), \
             patch("jobhunter.notify.smtplib.SMTP", return_value=smtp):
            self.assertTrue(Notifier(BOTH, env=ENV, log=lambda line: None).send(JOB, RESULT, "backend.txt"))
        smtp.__enter__.return_value.sendmail.side_effect = OSError("auth")
        with patch("jobhunter.notify.requests.post", side_effect=OSError("down")), \
             patch("jobhunter.notify.smtplib.SMTP", return_value=smtp):
            self.assertFalse(Notifier(BOTH, env=ENV, log=lambda line: None).send(JOB, RESULT, "backend.txt"))
        self.assertTrue(Notifier(NotifySettings(), env=ENV).send(JOB, RESULT, "backend.txt"))

    def test_a_failed_slack_send_never_logs_the_webhook(self):
        import requests
        logs = []
        error = requests.HTTPError("404 Client Error: Not Found for url: https://hooks.example/abc",
                                   response=FakeResponse(status_code=404))
        slack_only = NotifySettings(slack={"webhook_env": "SLACK_WEBHOOK_URL"})
        with patch("jobhunter.notify.requests.post", side_effect=error):
            Notifier(slack_only, env=ENV, log=logs.append).send(JOB, RESULT, "backend.txt")
        with patch("jobhunter.notify.requests.post",
                   side_effect=requests.ConnectionError("Max retries with url: /abc (https://hooks.example/abc)")):
            Notifier(slack_only, env=ENV, log=logs.append).send(JOB, RESULT, "backend.txt")
        self.assertEqual(len(logs), 2)
        self.assertFalse(any("hooks.example" in line or "/abc" in line for line in logs), logs)
        self.assertIn("404", logs[0])

    def test_a_title_with_a_line_break_still_emails(self):
        smtp = MagicMock()
        job = Job("dice_2", "Backend\nEngineer", "Acme\r\nCorp", "Austin, TX", "https://example.com/2", source="dice")
        with patch("jobhunter.notify.smtplib.SMTP", return_value=smtp):
            sent = Notifier(NotifySettings(email=BOTH.email), env=ENV).send(job, RESULT, "backend.txt")
        self.assertTrue(sent)
        self.assertIn("[8/10] Backend Engineer at Acme Corp", smtp.__enter__.return_value.sendmail.call_args.args[2])

class TestFeedbackButtons(unittest.TestCase):
    def sent(self, settings=BOTH, job=JOB, letter=None):
        smtp = MagicMock()
        with patch("jobhunter.notify.requests.post", return_value=FakeResponse()) as post, \
             patch("jobhunter.notify.smtplib.SMTP", return_value=smtp):
            Notifier(settings, env=ENV).send(job, RESULT, "backend.txt", cover_letter=letter)
        args = smtp.__enter__.return_value.sendmail.call_args.args
        return args, email.message_from_string(args[2]), post

    def parts(self, msg):
        return {p.get_content_type(): p.get_payload(decode=True).decode() for p in msg.walk() if not p.is_multipart()}

    def test_plain_and_html_both_carry_four_buttons(self):
        _, msg, post = self.sent(letter="Dear team")
        parts = self.parts(msg)
        self.assertEqual(set(parts), {"text/plain", "text/html"})
        for verdict in ("applied", "good", "maybe", "bad"):
            for body in parts.values():
                self.assertIn(f"jh%3A{verdict}%3Adice_1", body)
        self.assertIn("mailto:sender%2Bjobhunter-feedback@example.com", parts["text/html"])
        self.assertLess(parts["text/html"].index("jh%3Abad"), parts["text/html"].index("Dear team"))
        self.assertEqual(msg["Subject"], "[8/10] Backend Engineer at Acme (dice)")
        self.assertNotIn("mailto", post.call_args.kwargs["json"]["text"])          # Slack unchanged

    def test_job_text_is_escaped_in_html(self):
        job = Job("dice_9", 'Eng <a href="x">', "A&B", "Austin", 'https://e/1?a=1&b="2"', source="dice")
        html_part = self.parts(self.sent(job=job)[1])["text/html"]
        self.assertNotIn('<a href="x">', html_part)
        self.assertIn("Eng &lt;a href=&quot;x&quot;&gt;", html_part)
        self.assertIn("A&amp;B", html_part)
        self.assertIn('href="https://e/1?a=1&amp;b=&quot;2&quot;"', html_part)

    def test_without_to_env_alerts_go_to_the_alert_plus_address(self):
        settings = NotifySettings(email={"from_env": "GMAIL_ADDRESS", "password_env": "GMAIL_APP_PASSWORD"})
        args, msg, _ = self.sent(settings=settings)
        self.assertEqual(args[1], "sender+jobhunter@example.com")
        self.assertEqual(msg["To"], "sender+jobhunter@example.com")

class TestEmailLeavesOutInternals(unittest.TestCase):
    RESULT = ScoreResult(score=6, model="laya:~/models/laya_questions_v1", decision="notify",
                         reasoning="Laya: fit 6.4/10 (alert at 6.07, save at 4.86); stack role fullstack",
                         label="fit 6.4/10")

    def test_email_has_no_model_path_or_cutoffs_but_slack_keeps_them(self):
        smtp = MagicMock()
        with patch("jobhunter.notify.requests.post", return_value=FakeResponse()) as post, \
             patch("jobhunter.notify.smtplib.SMTP", return_value=smtp):
            Notifier(BOTH, env=ENV).send(JOB, self.RESULT, "backend.txt")
        msg = email.message_from_string(smtp.__enter__.return_value.sendmail.call_args.args[2])
        for part in msg.walk():
            if part.is_multipart():
                continue
            body = part.get_payload(decode=True).decode()
            with self.subTest(part=part.get_content_type()):
                self.assertNotIn("laya_questions_v1", body)
                self.assertNotIn("alert at", body)
                self.assertNotIn("save at", body)
                self.assertIn("fit 6.4/10", body)
                self.assertIn("stack role fullstack", body)
        slack = post.call_args.kwargs["json"]["text"]
        self.assertIn("laya_questions_v1", slack)
        self.assertIn("alert at 6.07", slack)

    def test_percent_cutoffs_are_removed_too(self):
        from jobhunter.notify import _for_reader
        self.assertEqual(_for_reader("Laya: 72% fit (alert at 60%, save at 40%)."), "Laya: 72% fit.")

class TestOpenJobLink(unittest.TestCase):
    def html(self, url):
        smtp = MagicMock()
        job = Job("dice_7", "Backend Engineer", "Acme", "Austin", url, source="dice")
        with patch("jobhunter.notify.smtplib.SMTP", return_value=smtp):
            Notifier(NotifySettings(email=BOTH.email), env=ENV).send(job, RESULT, "backend.txt")
        msg = email.message_from_string(smtp.__enter__.return_value.sendmail.call_args.args[2])
        return next(p.get_payload(decode=True).decode() for p in msg.walk() if p.get_content_type() == "text/html")

    def test_only_web_links_get_an_open_job_button(self):
        self.assertIn('href="https://example.com/1"', self.html("https://example.com/1"))
        for bad in ("javascript:alert(1)", "data:text/html,x", ""):
            with self.subTest(url=bad):
                html_part = self.html(bad)
                self.assertNotIn("Open job", html_part)
                self.assertNotIn("javascript:", html_part)



if __name__ == "__main__":
    unittest.main()
