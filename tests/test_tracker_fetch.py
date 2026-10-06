import json
import unittest

from jobhunter.tracker.fetch import title_and_company

PUBLIC = lambda host: ["93.184.216.34"]          # noqa: E731


class Resp:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code, self.body, self.headers, self.closed = status, body, headers or {}, False

    def iter_content(self, chunk_size=16384):
        for i in range(0, len(self.body), chunk_size):
            yield self.body[i:i + chunk_size]

    def close(self):
        self.closed = True


class Http:
    def __init__(self, resp=None, error=None):
        self.resp, self.error, self.calls = resp, error, []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if self.error:
            raise self.error
        return self.resp


def page(ld=None, title="Careers"):
    script = f'<script type="application/ld+json">{json.dumps(ld)}</script>' if ld is not None else ""
    return f"<html><head><title>{title}</title>{script}</head><body></body></html>".encode()


class TestTitleAndCompany(unittest.TestCase):
    def test_json_ld_job_posting(self):
        ld = {"@context": "https://schema.org", "@type": "JobPosting", "title": " Backend\nEngineer ",
              "hiringOrganization": {"@type": "Organization", "name": "Acme"}}
        http = Http(Resp(body=page(ld)))
        self.assertEqual(title_and_company(http, "https://jobs.example.com/1", PUBLIC), ("Backend Engineer", "Acme"))
        self.assertFalse(http.calls[0][2]["allow_redirects"])

    def test_a_graph_or_list_and_a_list_type(self):
        ld = {"@graph": [{"@type": "WebPage"}, {"@type": ["JobPosting"], "title": "SWE", "hiringOrganization": "Globex"}]}
        self.assertEqual(title_and_company(Http(Resp(body=page(ld))), "https://e.example/1", PUBLIC), ("SWE", "Globex"))

    def test_the_page_title_when_there_is_no_job_posting(self):
        self.assertEqual(title_and_company(Http(Resp(body=page(title="Platform Engineer | Initech"))),
                                           "https://e.example/1", PUBLIC), ("Platform Engineer | Initech", ""))

    def test_failures_give_empty_fields(self):
        cases = [Http(Resp(status=403)), Http(Resp(status=301, headers={"Location": "http://127.0.0.1/"})),
                 Http(error=OSError("timed out")), Http(Resp(body=b"x" * 2_000_001))]
        for http in cases:
            with self.subTest(http=http):
                self.assertEqual(title_and_company(http, "https://e.example/1", PUBLIC), ("", ""))

    def test_a_private_address_is_never_requested(self):
        http = Http(Resp(body=page()))
        for url in ("http://127.0.0.1:8765/api/state", "http://localhost/", "file:///etc/passwd"):
            self.assertEqual(title_and_company(http, url, PUBLIC), ("", ""))
        self.assertEqual(title_and_company(http, "https://intranet.example/", lambda host: ["10.0.0.5"]), ("", ""))
        self.assertEqual(http.calls, [])
