import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from certitude_usage_source import inspect_description


def page(short="ordinary text", long="more text", url="https://www.geocaching.com/seek/cache_details.aspx?wp=GC12345"):
    return f'<html><body><h1>Cache <a href="https://coord.info/GC12345">GC12345</a></h1><div id="ShortDescription">{short}</div><div id="LongDescription">{long}</div></body></html>', url


class InspectDescriptionTests(unittest.TestCase):
    def test_direct_link_is_linked_and_keeps_extractor_evidence(self):
        html, url = page('<a href="https://certitudes.org/?wp=GCAAA">checker</a>')
        result = inspect_description(html, url, "GC12345")
        self.assertEqual(result["status"], "linked")
        self.assertEqual(result["links"][0]["sourceWaypoint"], "GCAAA")

    def test_image_wrapped_link_and_non_parent_stage_checker_are_linked(self):
        html, url = page('<a href="https://www.certitudes.org/checker?id=9"><img src="qr.png"></a>')
        result = inspect_description(html, url, "GC12345")
        self.assertEqual(result["status"], "linked")
        self.assertEqual(len(result["links"]), 1)

    def test_complete_nonempty_description_without_checker_is_negative(self):
        html, url = page("A description", "A hint")
        self.assertEqual(inspect_description(html, url, "GC12345")["status"], "not_linked")

    def test_standard_certitude_image_button_is_linked(self):
        snippet = ('<A TARGET="_blank" HREF="https://www.certitudes.org/?wp=GCSTAGE&amp;c=9">'
                   '<img alt="Check" src="https://example.org/logo.png"></A>')
        html, url = page(snippet)
        result = inspect_description(html, url, "GC12345")
        self.assertEqual(result["status"], "linked")
        self.assertEqual(result["links"][0]["sourceWaypoint"], "GCSTAGE")

    def test_unlinked_ordinary_visual_or_interactive_content_is_negative(self):
        for content in ('Scan the code <img src="qr.png">',
                        '<iframe src="https://example.org/checker"></iframe>puzzle',
                        '<button onclick="goToChecker()">Validate</button>',
                        '<a href="javascript:goToChecker()">Validate</a>'):
            html, url = page(content, "Puzzle text")
            with self.subTest(content=content):
                self.assertEqual(inspect_description(html, url, "GC12345")["status"], "not_linked")

    def test_incomplete_or_unverifiable_pages_are_unknown(self):
        html, url = page("text", "text")
        cases = [
            (html[:-7], url, "GC12345"),
            (html.replace("GC12345", "GC99999"), url, "GC12345"),
            (html, "http://www.geocaching.com/cache_details.aspx?wp=GC12345", "GC12345"),
            ('<html><div id="ShortDescription">text</div></html>', url, "GC12345"),
            (page("Premium members only", "")[0], url, "GC12345"),
        ]
        for candidate, candidate_url, expected in cases:
            with self.subTest(url=candidate_url):
                self.assertEqual(inspect_description(candidate, candidate_url, expected)["status"], "unknown")

    def test_empty_plain_domain_and_shortener_are_unknown(self):
        cases = [page("", ""), page("certitudes.org checker", ""),
                 page('<a href="https://bit.ly/abc">checker</a>', "")]
        for html, url in cases:
            with self.subTest(html=html):
                self.assertEqual(inspect_description(html, url, "GC12345")["status"], "unknown")

    def test_ordinary_image_only_description_is_negative(self):
        html, url = page('<img src="qr.png">', "")
        self.assertEqual(inspect_description(html, url, "GC12345")["status"], "not_linked")

    def test_description_attribute_certitude_markers_are_unknown(self):
        cases = [
            page('<img src="https://certitudes.org/checker?id=9">', "hint"),
            page('<span data-checker="https://www.certitudes.org/?wp=GCAAA">puzzle</span>', "hint"),
            page("<div onclick=\"open('https://certitudes.org/checker?id=9')\">puzzle</div>", "hint"),
            page('<a href="https://www.bit.ly/abc">puzzle link</a>', "hint"),
        ]
        for html, url in cases:
            with self.subTest(html=html):
                self.assertEqual(inspect_description(html, url, "GC12345")["status"], "unknown")

    def test_footer_checker_link_is_ignored(self):
        html, url = page("description", "hint")
        html = html.replace("</body>", '<footer><a href="https://certitudes.org/?wp=GCFOOT">Certitude</a></footer></body>')
        self.assertEqual(inspect_description(html, url, "GC12345")["status"], "not_linked")


if __name__ == "__main__":
    unittest.main()
