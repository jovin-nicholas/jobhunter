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


if __name__ == "__main__":
    unittest.main()
