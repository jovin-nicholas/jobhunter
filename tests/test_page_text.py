import unittest

import requests

from jobhunter.page_text import PageUnavailable, best_description, classify, fetch_description, needs_page_fetch

PUBLIC = lambda host: ["93.184.216.34"]
PRIVATE = lambda host: ["10.0.0.5"]

POSTING = b"""<html><head><style>.x{}</style></head><body><nav>Jobs Search Sign in</nav>
<div class="top-card">Backend Engineer at Acme</div>
<div class="show-more-less-html__markup description__text"><p>About the role. You will build Java services.</p>
<ul><li>Requirements: 2 years of Java, Spring Boot, PostgreSQL.</li></ul></div>
<footer>Privacy Terms</footer></body></html>"""


class FakePage:
    def __init__(self, body: bytes, status_code=200, content_type="text/html", location=None):
        # requests reports ISO-8859-1 for text/html without a charset, which garbles UTF-8 pages
        self.body, self.status_code, self.encoding, self.closed = body, status_code, "ISO-8859-1", False
        self.headers = {"Content-Type": content_type, **({"Location": location} if location else {})}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size):
        for i in range(0, len(self.body), chunk_size):
            yield self.body[i:i + chunk_size]

    def close(self):
        self.closed = True


class FakeHttp:
    def __init__(self, page):
        self.page, self.calls = page, []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.page


class TestFetchDescription(unittest.TestCase):
    def setUp(self):
        self.logs = []

    def test_picks_the_description_block(self):
        http = FakeHttp(FakePage(POSTING))
        text = fetch_description(http, "https://www.linkedin.com/jobs/view/1", self.logs.append, resolve=PUBLIC)
        self.assertIn("Requirements: 2 years of Java", text)
        self.assertNotIn("Privacy", text)
        self.assertTrue(http.page.closed)

    def test_redirects_are_never_left_to_the_http_client(self):
        http = FakeHttp(FakePage(POSTING))
        fetch_description(http, "https://www.linkedin.com/jobs/view/1", self.logs.append, resolve=PUBLIC)
        self.assertEqual(http.calls[0][2]["allow_redirects"], False)      # followed by hand, each hop checked
        self.assertIn("Mozilla", http.calls[0][2]["headers"]["User-Agent"])

    def test_private_and_local_addresses_are_never_requested(self):
        for url, resolve in (("http://127.0.0.1/x", PUBLIC), ("http://localhost:8080/", PUBLIC),
                             ("https://intranet.example.com/job", PRIVATE), ("file:///etc/passwd", PUBLIC),
                             ("http://[::1]/", PUBLIC), ("", PUBLIC)):
            http = FakeHttp(FakePage(POSTING))
            self.assertEqual(fetch_description(http, url, self.logs.append, resolve=resolve), "", url)
            self.assertEqual(http.calls, [], url)

    def test_oversized_page_is_dropped(self):
        http = FakeHttp(FakePage(b"a" * 500_000))
        self.assertEqual(fetch_description(http, "https://careers.example.org/1", self.logs.append, resolve=PUBLIC), "")
        self.assertTrue(any("larger" in line for line in self.logs), self.logs)

    def test_http_error_is_logged_and_returns_empty(self):
        http = FakeHttp(FakePage(b"", status_code=404))
        self.assertEqual(fetch_description(http, "https://www.linkedin.com/jobs/view/1", self.logs.append,
                                           resolve=PUBLIC), "")
        self.assertTrue(any("404" in line for line in self.logs), self.logs)

    def test_linkedin_search_widget_text_is_rejected(self):
        page = b"<html><body><div class='description'>This button displays the currently selected search type." \
               b"</div></body></html>"
        self.assertEqual(best_description(page.decode()), "")

    def test_whole_page_text_when_no_block_stands_out(self):
        self.assertEqual(best_description("<html><body><p>Build APIs in Go.</p><script>x()</script></body></html>"),
                         "Build APIs in Go.")

    def test_needs_page_fetch(self):
        self.assertTrue(needs_page_fetch("", None))
        self.assertTrue(needs_page_fetch(None, None))
        self.assertTrue(needs_page_fetch("x" * 199, False))
        self.assertTrue(needs_page_fetch("x" * 500, True))
        self.assertFalse(needs_page_fetch("x" * 500, False))


class RaisingHttp:
    def __init__(self, error):
        self.error = error

    def request(self, method, url, **kw):
        raise self.error


class TestRefusedPages(unittest.TestCase):
    def test_rate_limits_server_errors_and_timeouts_raise(self):
        for http in (FakeHttp(FakePage(b"", status_code=429)), FakeHttp(FakePage(b"", status_code=503)),
                     RaisingHttp(requests.Timeout("slow")), RaisingHttp(requests.ConnectionError("reset"))):
            with self.assertRaises(PageUnavailable):
                fetch_description(http, "https://www.linkedin.com/jobs/view/1", lambda line: None, resolve=PUBLIC)

    def test_a_missing_page_is_not_a_refusal(self):
        http = FakeHttp(FakePage(b"", status_code=404))
        self.assertEqual(fetch_description(http, "https://www.linkedin.com/jobs/view/1", lambda line: None,
                                           resolve=PUBLIC), "")


class TestEncodings(unittest.TestCase):
    TEXT = "Caf\u00e9 d\u00e9j\u00e0 vu \u2014 you will build APIs."

    def fetch(self, body, content_type):
        http = FakeHttp(FakePage(body, content_type=content_type))
        return fetch_description(http, "https://careers.example.org/1", lambda line: None, resolve=PUBLIC)

    def test_utf8_without_a_declared_charset_is_detected(self):
        self.assertEqual(self.fetch(f"<html><body><p>{self.TEXT}</p></body></html>".encode("utf-8"), "text/html"),
                         self.TEXT)

    def test_a_declared_charset_is_used(self):
        body = "<html><body><p>Caf\u00e9 cr\u00e8me</p></body></html>".encode("latin-1")
        self.assertEqual(self.fetch(body, "text/html; charset=ISO-8859-1"), "Caf\u00e9 cr\u00e8me")
        meta = ('<html><head><meta charset="windows-1252"></head><body><p>Caf\u00e9 \u2014</p></body></html>'
                .encode("cp1252"))
        self.assertEqual(self.fetch(meta, "text/html"), "Caf\u00e9 \u2014")


class Pages:
    """FakeHttp by URL: each URL answers its own page."""

    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def request(self, method, url, **kw):
        self.calls.append(url)
        return self.pages[url]


def redirect(to, code=302):
    return FakePage(b"", status_code=code, location=to)


class TestAddressChecks(unittest.TestCase):
    def test_carrier_grade_nat_addresses_are_refused(self):
        self.assertEqual(classify("http://100.64.0.1/job", PUBLIC), (False, False))
        self.assertEqual(classify("https://tailnet.example.com/job", lambda host: ["100.100.1.1"]), (False, False))
        self.assertEqual(classify("https://www.linkedin.com/jobs/view/1", PUBLIC), (True, True))

    def test_a_redirect_to_a_private_address_is_not_followed(self):
        http = Pages({"https://www.linkedin.com/jobs/view/1": redirect("http://127.0.0.1:8080/admin")})
        self.assertEqual(fetch_description(http, "https://www.linkedin.com/jobs/view/1", lambda line: None,
                                           resolve=PUBLIC), "")
        self.assertEqual(http.calls, ["https://www.linkedin.com/jobs/view/1"])

    def test_relative_redirects_are_followed_on_trusted_sites(self):
        http = Pages({"https://www.linkedin.com/jobs/view/1": redirect("/jobs/view/2", 301),
                      "https://www.linkedin.com/jobs/view/2": FakePage(POSTING)})
        text = fetch_description(http, "https://www.linkedin.com/jobs/view/1", lambda line: None, resolve=PUBLIC)
        self.assertIn("Requirements", text)

    def test_unknown_sites_do_not_redirect_and_loops_stop(self):
        http = Pages({"https://careers.example.org/1": redirect("https://careers.example.org/2")})
        self.assertEqual(fetch_description(http, "https://careers.example.org/1", lambda line: None, resolve=PUBLIC), "")
        self.assertEqual(len(http.calls), 1)
        loop = {f"https://www.linkedin.com/r/{i}": redirect(f"https://www.linkedin.com/r/{i + 1}") for i in range(10)}
        http, logs = Pages(loop), []
        self.assertEqual(fetch_description(http, "https://www.linkedin.com/r/0", logs.append, resolve=PUBLIC), "")
        self.assertEqual(len(http.calls), 6)                       # the first request and 5 redirects
        self.assertTrue(any("redirects" in line for line in logs), logs)


class TestForbidden(unittest.TestCase):
    def test_a_403_from_a_job_board_is_a_refusal_elsewhere_it_is_not(self):
        with self.assertRaises(PageUnavailable):
            fetch_description(FakeHttp(FakePage(b"", status_code=403)), "https://www.linkedin.com/jobs/view/1",
                              lambda line: None, resolve=PUBLIC)
        self.assertEqual(fetch_description(FakeHttp(FakePage(b"", status_code=403)), "https://careers.example.org/1",
                                           lambda line: None, resolve=PUBLIC), "")


if __name__ == "__main__":
    unittest.main()
