import tempfile
import unittest
from pathlib import Path

from jobhunter.inbox import read_feedback
from jobhunter.models import Job
from jobhunter.settings import NotifySettings
from jobhunter.store import Store

ENV = {"GMAIL_ADDRESS": "sender@example.com", "GMAIL_APP_PASSWORD": "app-pass", "NOTIFY_EMAIL": "john.doe@example.com"}
SETTINGS = NotifySettings(email={"from_env": "GMAIL_ADDRESS", "password_env": "GMAIL_APP_PASSWORD",
                                 "to_env": "NOTIFY_EMAIL"})
ENCODED = "=?UTF-8?B?amg6Z29vZDpkaWNlXzE=?="         # jh:good:dice_1, as a phone mail app may write it


def header(frm, subject, mid="<m1@x>", date="Mon, 05 Oct 2026 10:00:00 +0000", auth=None):
    lines = [f"Authentication-Results: {a}" for a in ([auth] if isinstance(auth, str) else auth or [])]
    lines += [f"From: {frm}", f"Subject: {subject}"]
    lines += [f"Message-ID: {mid}"] if mid else []
    lines += [f"Date: {date}"] if date else []
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


class FakeImap:
    """The imaplib replies inbox.py relies on: (status, data) tuples, FETCH data as [(prefix, bytes), b")"]."""

    def __init__(self, messages, folders=None, fail_on=None, no_on=None, internal=None):
        self.messages, self.calls, self.fail_on, self.stored, self.no_on = messages, [], fail_on, [], no_on
        self.internal = internal or {}
        self.folders = folders or [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"']

    def _step(self, name):
        self.calls.append(name)
        if self.fail_on == name:
            raise OSError(f"{name} failed")
        return self.no_on == name           # imaplib answers NO without raising

    def login(self, user, password):
        self._step("login")
        return "OK", [b""]

    def list(self):
        return "OK", self.folders

    def select(self, box, readonly=False):
        self.selected = box
        if self._step("select"):
            return "NO", [b"[NONEXISTENT] Unknown Mailbox"]
        return "OK", [str(len(self.messages)).encode()]

    def create(self, box):
        return "NO", [b"[ALREADYEXISTS]"]

    def uid(self, command, *args):
        if self._step(command.lower() + (" " + args[1] if command == "STORE" else "")):
            return "NO", [b"[CANNOT] Command failed"]
        if command == "SEARCH":
            self.search = args
            return "OK", [" ".join(self.messages).encode()]
        if command == "FETCH":
            # One FETCH for a whole UID set, as imaplib returns it: (prefix, literal) per message, then b")".
            parts = []
            for n, uid in enumerate(args[0].split(","), 1):
                when = self.internal.get(uid, "05-Oct-2026 12:00:00 +0000")
                prefix = f'{n} (UID {uid} INTERNALDATE "{when}" BODY[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID)] ' \
                         f'{{{len(self.messages[uid])}}}'
                parts += [(prefix.encode(), self.messages[uid]), b")"]
            return "OK", parts
        if command == "STORE":
            self.stored.append(args)
            return "OK", [b""]
        raise AssertionError(command)

    def logout(self):
        self.calls.append("logout")


class InboxTestCase(unittest.TestCase):
    def setUp(self):
        self.store = Store(Path(tempfile.mkdtemp()) / "jobs.db")
        self.store.save(Job("dice_1", "Backend Engineer", "Acme", "Austin", "https://e/1", source="dice"), "notified")
        self.logs = []

    def read(self, imap):
        return read_feedback(SETTINGS, ENV, self.store, log=self.logs.append, imap_factory=lambda *a, **k: imap)

    def problems(self):
        """The log without the note about messages Gmail added no Authentication-Results to (these fakes have none)."""
        return [line for line in self.logs if "Authentication-Results" not in line]


class TestReadFeedback(InboxTestCase):
    def test_saves_valid_and_labels_everything_matched(self):
        imap = FakeImap({"11": header('"Sender" <Sender@Example.com>', "jh:good:dice_1"),
                         "12": header("john.doe@example.com", "jh:bad:dice_1", mid="<m2@x>"),
                         "13": header("stranger@example.com", "jh:good:dice_1", mid="<m3@x>"),
                         "14": header("sender@example.com", "hello", mid="<m4@x>"),
                         "15": header("sender@example.com", "jh:good:nope_9", mid="<m5@x>")})
        summary = self.read(imap)
        self.assertEqual(dict(summary.saved), {"good": 1, "bad": 1})
        self.assertEqual(dict(summary.ignored), {"foreign sender": 1, "bad subject": 1, "unknown job": 1})
        self.assertEqual(imap.search, ("X-GM-RAW", '"to:sender+jobhunter-feedback@example.com -label:jobhunter-feedback"'))
        self.assertEqual(imap.selected, '"[Gmail]/All Mail"')
        self.assertEqual(set(imap.stored[0][0].split(",")), {"11", "12", "13", "14", "15"})
        self.assertEqual([s[1] for s in imap.stored], ["+X-GM-LABELS", "-X-GM-LABELS"])
        self.assertEqual(imap.stored[0][2], '("jobhunter/feedback")')
        self.assertEqual(summary.line(), "feedback: 2 saved (1 bad, 1 good), 3 ignored")
        self.assertEqual(self.problems(), [])

    def test_labels_only_after_saving(self):
        imap = FakeImap({"11": header("sender@example.com", "jh:good:dice_1")})
        original = self.store.add_feedback

        def add(rows):
            self.assertNotIn("store +X-GM-LABELS", imap.calls)
            return original(rows)
        self.store.add_feedback = add
        self.read(imap)
        self.assertIn("store +X-GM-LABELS", imap.calls)

    def test_nothing_to_read_says_nothing(self):
        imap = FakeImap({})
        self.assertIsNone(self.read(imap).line())
        self.assertEqual(imap.stored, [])

    def test_login_failure_is_logged_without_secrets(self):
        summary = self.read(FakeImap({}, fail_on="login"))
        self.assertIsNone(summary.line())
        self.assertEqual(len(self.logs), 1)
        self.assertIn("OSError", self.logs[0])
        self.assertNotIn("app-pass", self.logs[0])
        self.assertNotIn("sender@example.com", self.logs[0])

    def test_label_failure_keeps_rows_and_a_rerun_does_not_duplicate(self):
        msgs = {"11": header("sender@example.com", "jh:maybe:dice_1")}
        self.assertEqual(dict(self.read(FakeImap(msgs, fail_on="store +X-GM-LABELS")).saved), {"maybe": 1})
        again = self.read(FakeImap(msgs))
        self.assertEqual(len(self.store.latest_feedback()), 1)
        self.assertEqual(dict(again.saved), {})

    def test_localized_all_mail_folder(self):
        imap = FakeImap({}, folders=[b'(\\HasNoChildren) "/" "INBOX"',
                                     b'(\\All \\HasNoChildren) "/" "[Gmail]/Alle Nachrichten"'])
        self.read(imap)
        self.assertEqual(imap.selected, '"[Gmail]/Alle Nachrichten"')

    def test_encoded_subject_and_missing_headers(self):
        msgs = {"11": header("sender@example.com", ENCODED, mid=None, date=None)}
        self.assertEqual(dict(self.read(FakeImap(msgs)).saved), {"good": 1})
        self.assertEqual(dict(self.read(FakeImap(msgs)).saved), {})      # same message again: not saved twice

    def test_no_email_settings_reads_nothing(self):
        called = []
        summary = read_feedback(NotifySettings(), ENV, self.store, log=self.logs.append,
                                imap_factory=lambda *a, **k: called.append(1))
        self.assertIsNone(summary.line())
        self.assertEqual(called, [])

class TestImapReplies(InboxTestCase):
    def test_a_list_line_with_a_literal_is_skipped(self):
        imap = FakeImap({}, folders=[(b'(\\HasNoChildren) "/" {9}', b'Odd"Label'),
                                     b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"'])
        self.read(imap)
        self.assertEqual(imap.selected, '"[Gmail]/All Mail"')
        self.assertEqual(self.logs, [])

    def test_a_refused_search_is_reported_and_nothing_is_fetched(self):
        imap = FakeImap({"11": header("sender@example.com", "jh:good:dice_1")}, no_on="search")
        summary = self.read(imap)
        self.assertNotIn("fetch", imap.calls)
        self.assertIsNone(summary.line())
        self.assertEqual(self.logs, ["feedback: could not search the inbox (NO: [CANNOT] Command failed)"])

    def test_a_refused_select_is_reported(self):
        self.read(FakeImap({}, no_on="select"))
        self.assertEqual(self.logs, ["feedback: could not open All Mail (NO: [NONEXISTENT] Unknown Mailbox)"])

    def test_a_refused_label_keeps_saves_and_is_reported(self):
        imap = FakeImap({"11": header("sender@example.com", "jh:good:dice_1")}, no_on="store +X-GM-LABELS")
        summary = self.read(imap)
        self.assertEqual(dict(summary.saved), {"good": 1})
        self.assertEqual(self.problems(), ["feedback: could not label the messages (NO: [CANNOT] Command failed)"])

    def test_a_rejected_login_says_why_without_the_password(self):
        import imaplib

        class Rejecting(FakeImap):
            def login(self, user, password):
                raise imaplib.IMAP4.error("b'[AUTHENTICATIONFAILED] Invalid credentials (Failure)'")
        self.read(Rejecting({}))
        self.assertEqual(len(self.logs), 1)
        self.assertIn("AUTHENTICATIONFAILED", self.logs[0])
        self.assertIn("app password", self.logs[0])
        self.assertNotIn("app-pass", self.logs[0])

class TestFetchAndSenders(InboxTestCase):
    def test_all_messages_come_in_one_fetch(self):
        imap = FakeImap({str(u): header("sender@example.com", "jh:good:dice_1", mid=f"<m{u}@x>") for u in range(11, 16)})
        self.read(imap)
        self.assertEqual(imap.calls.count("fetch"), 1)

    def test_a_missing_date_uses_gmails_received_time(self):
        imap = FakeImap({"11": header("sender@example.com", "jh:good:dice_1", date=None)},
                        internal={"11": "03-Oct-2026 09:30:00 +0000"})
        self.read(imap)
        self.assertEqual(self.store.latest_feedback()[0]["received_at"], "2026-10-03T09:30:00+00:00")

    def test_to_env_written_with_a_name_or_as_a_list_still_matches(self):
        from jobhunter.inbox import read_feedback
        env = dict(ENV, NOTIFY_EMAIL='"John" <John.Doe@example.com>, phone@example.com')
        imap = FakeImap({"11": header("john.doe@example.com", "jh:good:dice_1"),
                         "12": header("phone@example.com", "jh:bad:dice_1", mid="<m2@x>")})
        summary = read_feedback(SETTINGS, env, self.store, log=self.logs.append, imap_factory=lambda *a, **k: imap)
        self.assertEqual(dict(summary.saved), {"good": 1, "bad": 1})


class TestAuthenticationResults(InboxTestCase):
    PASS_DKIM = "mx.google.com; dkim=pass header.i=@example.com header.s=s1; spf=neutral smtp.mailfrom=x@other.example"
    PASS_SPF = "mx.google.com; dkim=none; spf=pass (google.com: domain of sender@example.com designates 1.2.3.4) " \
               "smtp.mailfrom=sender@example.com"
    FAIL = "mx.google.com; dkim=fail header.i=@example.com; spf=softfail smtp.mailfrom=sender@example.com"
    OTHER_DOMAIN = "mx.google.com; dkim=pass header.i=@attacker.example; spf=pass smtp.mailfrom=a@attacker.example"

    def test_a_message_gmail_authenticated_is_saved(self):
        imap = FakeImap({"11": header("sender@example.com", "jh:good:dice_1", auth=self.PASS_DKIM),
                         "12": header("john.doe@example.com", "jh:bad:dice_1", mid="<m2@x>", auth=self.PASS_SPF)})
        self.assertEqual(dict(self.read(imap).saved), {"good": 1, "bad": 1})
        self.assertEqual(self.logs, [])

    def test_a_message_that_failed_authentication_is_ignored(self):
        imap = FakeImap({"11": header("sender@example.com", "jh:good:dice_1", auth=self.FAIL),
                         "12": header("sender@example.com", "jh:bad:dice_1", mid="<m2@x>", auth=self.OTHER_DOMAIN)})
        summary = self.read(imap)
        self.assertEqual(dict(summary.saved), {})
        self.assertEqual(dict(summary.ignored), {"not authenticated": 2})
        self.assertIn("jobhunter/feedback", [s[2] for s in imap.stored][0])     # still labelled, not read again

    def test_a_subdomain_or_a_dmarc_pass_counts(self):
        sub = "mx.google.com; dkim=pass header.i=@mail.example.com; spf=none"
        dmarc = "mx.google.com; dkim=none; spf=none; dmarc=pass (p=NONE) header.from=example.com"
        lookalike = "mx.google.com; dkim=pass header.i=@notexample.com; dmarc=pass header.from=badexample.com"
        imap = FakeImap({"11": header("sender@example.com", "jh:good:dice_1", auth=sub),
                         "12": header("sender@example.com", "jh:bad:dice_1", mid="<m2@x>", auth=dmarc),
                         "13": header("sender@example.com", "jh:bad:dice_1", mid="<m3@x>", auth=lookalike)})
        summary = self.read(imap)
        self.assertEqual(dict(summary.saved), {"good": 1, "bad": 1})
        self.assertEqual(dict(summary.ignored), {"not authenticated": 1})
        self.assertEqual([l for l in self.logs if "failed" in l],
                         ["feedback: 1 message(s) from your address failed Gmail's DKIM, SPF and DMARC checks; ignored"])

    def test_only_gmails_own_header_counts(self):
        forged = "mx.attacker.example; dkim=pass header.i=@example.com"
        imap = FakeImap({"11": header("sender@example.com", "jh:good:dice_1", auth=[self.FAIL, forged])})
        self.assertEqual(dict(self.read(imap).ignored), {"not authenticated": 1})

    def test_without_the_header_the_from_address_decides_and_it_is_logged_once(self):
        imap = FakeImap({"11": header("sender@example.com", "jh:good:dice_1"),
                         "12": header("sender@example.com", "jh:bad:dice_1", mid="<m2@x>")})
        self.assertEqual(dict(self.read(imap).saved), {"good": 1, "bad": 1})
        notes = [line for line in self.logs if "Authentication-Results" in line]
        self.assertEqual(len(notes), 1, self.logs)
        self.assertIn("2 message(s)", notes[0])



if __name__ == "__main__":
    unittest.main()
