import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from crawl_certitude import BoundedFetcher, FetchError


class Response:
    def __init__(self, status, location=None, body=b"ok"):
        self.status_code = status
        self.headers = {"Content-Type": "text/html"}
        if location:
            self.headers["Location"] = location
        self.body = body
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size): yield self.body


class Session:
    def __init__(self, responses): self.responses, self.calls = list(responses), []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class FetcherTests(unittest.TestCase):
    def test_certitude_reads_use_verified_request_profile(self):
        session = Session([Response(200)])
        BoundedFetcher(delay=0, retries=1).get(session, "https://www.certitudes.org/certitude?wp=GC1",
                                             {"www.certitudes.org"})
        self.assertIn("Mozilla/5.0", session.calls[0][2]["headers"].get("User-Agent", ""))

    def test_forbidden_response_retains_safe_diagnostics(self):
        response = Response(403)
        response.headers.update({"Server": "Google Frontend", "Set-Cookie": "secret"})
        with self.assertRaises(FetchError) as caught:
            BoundedFetcher(delay=0, retries=1).get(Session([response]),
                "https://www.certitudes.org/certitude?wp=GC1", {"www.certitudes.org"})
        self.assertEqual(str(caught.exception), "http_403")
        self.assertEqual(caught.exception.metadata["server"], "Google Frontend")
        self.assertNotIn("Set-Cookie", caught.exception.metadata)
        self.assertNotIn("secret", str(caught.exception.metadata))

    def test_redirect_cannot_post_filter_to_answer_endpoint(self):
        session = Session([Response(307, "/certify")])
        with self.assertRaises(FetchError):
            BoundedFetcher(delay=0, retries=1).post(session,
                "https://certitudes.org/", {"certitudes.org"}, {"filter_country": "47"})
        self.assertEqual(len(session.calls), 1)

    def test_dashboard_clock_headers_are_available_without_persisting_cookies(self):
        response = Response(200)
        response.headers.update({"Date": "Sun, 01 Nov 2026 00:00:00 GMT", "Cache-Control": "private",
                                 "Expires": "Sun, 01 Nov 2026 00:00:00 GMT", "Set-Cookie": "private"})
        fetcher = BoundedFetcher(delay=0, retries=1)
        fetcher.get(Session([response]), "https://certitudes.org/", {"certitudes.org"})
        self.assertEqual(fetcher.last_response_headers["Date"], response.headers["Date"])
        self.assertNotIn("Set-Cookie", fetcher.last_response_headers)

    def test_post_and_redirect_rules(self):
        session = Session([Response(303, "/next"), Response(200)])
        fetcher = BoundedFetcher(delay=0, retries=1)
        text, final_url = fetcher.post(session, "https://certitudes.org/", {"certitudes.org"}, {"country": "China"})
        self.assertEqual((text, final_url), ("ok", "https://certitudes.org/next"))
        self.assertEqual(session.calls[0][0], "POST")
        self.assertEqual(session.calls[0][2]["data"], {"country": "China"})
        self.assertEqual(session.calls[1][0], "GET")
        self.assertIsNone(session.calls[1][2]["data"])

    def test_external_redirect_is_rejected_before_request(self):
        session = Session([Response(307, "https://evil.example/")])
        with self.assertRaises(FetchError):
            BoundedFetcher(delay=0, retries=1).post(session, "https://certitudes.org/", {"certitudes.org"}, {"x": "y"})
        self.assertEqual(len(session.calls), 1)

    def test_307_preserves_post_body_and_returns_actual_url(self):
        session = Session([Response(307, "/?cursor=2"), Response(200, body=b"page")])
        result = BoundedFetcher(delay=0, retries=1).post(
            session, "https://certitudes.org/", {"certitudes.org"}, {"country": "China"})
        self.assertEqual(result, ("page", "https://certitudes.org/?cursor=2"))
        self.assertEqual([call[0] for call in session.calls], ["POST", "POST"])
        self.assertEqual(session.calls[1][2]["data"], {"country": "China"})

    def test_response_cap_and_non_dashboard_post_are_enforced(self):
        session = Session([Response(200, body=b"12345")])
        with self.assertRaises(FetchError):
            BoundedFetcher(delay=0, retries=1, max_bytes=4).post(
                session, "https://certitudes.org/", {"certitudes.org"}, {"sort": "act"})
        for url in ("https://certitudes.org/answer", "https://www.certitudes.org/certify"):
            with self.subTest(url=url), self.assertRaises(FetchError):
                BoundedFetcher(delay=0, retries=1).post(session, url,
                    {"certitudes.org", "www.certitudes.org"}, {"x": "y"})

    def test_get_preserves_existing_contract(self):
        session = Session([Response(200, body=b"html")])
        self.assertEqual(BoundedFetcher(delay=0, retries=1).get(
            session, "https://example.org/page", {"example.org"}), ("html", "https://example.org/page"))
        self.assertEqual(session.calls[0][0], "GET")


if __name__ == "__main__": unittest.main()
