import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from certitude_source import extract_description_links, parse_checker_html


def checker_html(waypoint="GCABC12", parent="GCABC12", solvers='[["Alpha", 1700000000]]',
                 counters=True, timeline=True, tz="Europe/Paris", form=True):
    count_markup = '<b class="success">4</b><b class="error">2</b>' if counters else '<b class="success">4</b>'
    time_markup = 'showTimeline("en-US", 1600000000, 1700000000);' if timeline else ""
    timezone_markup = f'setTimezone("{tz}");' if tz else ""
    waypoint_form = f'<input type="hidden" name="waypoint" value="{waypoint}">' if form else ""
    parent_link = f'<a href="https://coord.info/{parent}">Cache</a>' if parent else ""
    return f'''<html><h1><a href="?wp={waypoint}">{waypoint}</a>{parent_link}</h1>
      {count_markup}<form method="post" action="/certify">{waypoint_form}
      <input id="guess" type="text" placeholder="Answer"></form>
      <table><tbody id="solvers"></tbody></table>
      <script src="https://static.certitudes.org/html/certitude.js"></script>
      <script>{timezone_markup}{time_markup}var solvers = {solvers}; showSolvers("", "en-US", solvers);</script></html>'''


class ExtractDescriptionLinksTests(unittest.TestCase):
    def test_legacy_containers_keep_duplicates_and_exact_waypoint_case(self):
        html = '''<div id="ShortDescription"><a href="/certitude?wp=GCAbC">one</a>
        <a href="/certitude?wp=GCAbC">duplicate</a></div>
        <div id="LongDescription"><a href="https://www.certitudes.org/path?wp=W1c00">two</a>
        <a href="https://example.com/?wp=GC99999">external</a></div>'''
        rows = extract_description_links(html, "https://www.certitudes.org/geocache/GC12345")
        self.assertEqual([r["sourceUrl"] for r in rows], [
            "https://www.certitudes.org/certitude?wp=GCAbC",
            "https://www.certitudes.org/certitude?wp=GCAbC",
            "https://www.certitudes.org/path?wp=W1c00",
        ])
        self.assertEqual([r["sourceWaypoint"] for r in rows], ["GCAbC", "GCAbC", "W1c00"])
        self.assertEqual(rows[0]["checkerId"], None)
        self.assertEqual(rows[0]["mappingStatus"], "pending")
        self.assertEqual(rows[0]["evidence"], "ShortDescription")

    def test_image_wrapped_link_is_extracted(self):
        html = '<div class="ShortDescription"><a href="https://www.certitudes.org/?wp=GCIMAGE"><img src="x"></a></div><div class="LongDescription"></div>'
        self.assertEqual(len(extract_description_links(html, "https://www.geocaching.com/x")), 1)

    def test_identical_links_keep_distinct_occurrence_and_description_evidence(self):
        html = '''<div id="ctl00_ContentBody_ShortDescription">
        <a href="https://www.certitudes.org/certitude?wp=GC12345">first</a>
        <a href="https://www.certitudes.org/certitude?wp=GC12345">second</a></div>
        <div id="ctl00_ContentBody_LongDescription"></div>'''
        rows = extract_description_links(html, "https://www.geocaching.com/seek/cache_details.aspx?wp=GC12345")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["sourceUrl"], rows[1]["sourceUrl"])
        self.assertNotEqual(rows[0]["sourceOccurrence"], rows[1]["sourceOccurrence"])
        self.assertEqual(rows[0]["descriptionEvidence"], "ctl00_ContentBody_ShortDescription")
        self.assertEqual(rows[1]["descriptionEvidence"], "ctl00_ContentBody_ShortDescription")
        self.assertEqual(rows[0]["evidence"], rows[1]["evidence"])

    def test_real_geocaching_prefixed_legacy_ids_and_link_without_wp(self):
        html = '''<div id="ctl00_ContentBody_ShortDescription"><a href="https://certitudes.org/checker/image.php?id=7"><img src="/logo.png"></a></div>
        <div id="ctl00_ContentBody_LongDescription"></div>'''
        rows = extract_description_links(html, "https://www.geocaching.com/seek/cache_details.aspx?wp=GC12345", expected_gc="GC12345")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sourceUrl"], "https://certitudes.org/checker/image.php?id=7")
        self.assertIsNone(rows[0]["sourceWaypoint"])

    def test_description_expected_gc_conflict_is_rejected(self):
        html = '<h1><a href="https://coord.info/GC99999">Cache</a></h1><div id="ctl00_ContentBody_ShortDescription"></div><div id="ctl00_ContentBody_LongDescription"></div>'
        with self.assertRaises(ValueError):
            extract_description_links(html, "https://www.geocaching.com/cache_details.aspx?wp=GC12345", expected_gc="GC12345")

    def test_requires_both_legacy_description_containers(self):
        with self.assertRaises(ValueError):
            extract_description_links('<div id="ShortDescription"><a href="https://www.certitudes.org/?wp=GC12345">x</a></div>', "https://www.geocaching.com/x")

    def test_truncated_long_description_is_not_complete(self):
        html = '''<div id="ShortDescription"></div>
        <div id="LongDescription"><a href="https://www.certitudes.org/?wp=GC12345">entry'''
        with self.assertRaises(ValueError):
            extract_description_links(html, "https://www.geocaching.com/x")

    def test_login_or_premium_wall_is_not_empty_success(self):
        with self.assertRaises(ValueError):
            extract_description_links('<div id="ShortDescription">Premium members only</div><div id="LongDescription"></div><a href="/login">Sign in</a>', "https://www.geocaching.com/x")


class ParseCheckerHtmlTests(unittest.TestCase):
    def parse(self, html, url="https://www.certitudes.org/certitude?wp=GCABC12"):
        return parse_checker_html(html, url, datetime(2026, 11, 1, tzinfo=timezone.utc))

    def test_verified_parent_counts_timestamps_and_source_timezone(self):
        row = self.parse(checker_html())
        self.assertEqual(row["checkerId"], "GCABC12")
        self.assertEqual(row["cacheCode"], "GCABC12")
        self.assertEqual(row["role"], "final")
        self.assertEqual(row["mappingStatus"], "verified")
        self.assertEqual(row["successCount"], 4)
        self.assertEqual(row["failureCount"], 2)
        self.assertEqual(row["sourceTimezone"], "Europe/Paris")
        self.assertEqual(row["publishedAt"], "2020-09-13T12:26:40Z")
        self.assertEqual(row["latestActivityAt"], "2023-11-14T22:13:20Z")
        self.assertEqual(row["answerType"], "unknown")
        self.assertEqual(row["matchingMode"], "unknown")

    def test_mismatched_waypoint_and_parent_stays_unknown(self):
        row = self.parse(checker_html("W1C00FV", "GCC00FV"), "https://www.certitudes.org/certitude?wp=W1C00FV")
        self.assertEqual(row["checkerId"], "W1C00FV")
        self.assertEqual(row["cacheCode"], "GCC00FV")
        self.assertEqual(row["role"], "unknown")

    def test_waypoint_case_preserved(self):
        row = self.parse(checker_html("GCAbC12", "GCAbC12"), "https://www.certitudes.org/?wp=GCAbC12")
        self.assertEqual(row["checkerId"], "GCAbC12")

    def test_role_comparison_is_case_sensitive_and_geocaching_slug_is_parsed(self):
        html = checker_html("gcAbC12", None).replace("</h1>", '<a href="https://www.geocaching.com/geocache/GCABC12_example-cache">Parent</a></h1>')
        row = self.parse(html, "https://www.certitudes.org/certitude?wp=gcAbC12")
        self.assertEqual(row["cacheCode"], "GCABC12")
        self.assertEqual(row["role"], "unknown")

    def test_crawl_metadata_is_json_safe_and_success_rate_is_derived(self):
        row = self.parse(checker_html())
        json.dumps(row)
        self.assertEqual(row["successRate"], 2 / 3)
        self.assertEqual(row["crawl"]["status"], "ok")
        self.assertEqual(row["crawl"]["fieldCoverage"], "complete")
        self.assertEqual(row["crawl"]["errorCode"], None)

    def test_conflicting_waypoint_or_parent_links_raise(self):
        mismatch = self.parse(checker_html("GCABC12", "GCOTHER1"))
        self.assertEqual(mismatch["mappingStatus"], "verified")
        self.assertEqual(mismatch["role"], "unknown")
        html = checker_html().replace('href="https://coord.info/GCABC12"', 'href="https://coord.info/GCOTHER1"')
        html = html.replace('</h1>', '<a href="https://coord.info/GCTHIRD1">Another</a></h1>')
        with self.assertRaises(ValueError):
            self.parse(html)

    def test_missing_hidden_waypoint_uses_url_query(self):
        row = self.parse(checker_html(form=False))
        self.assertEqual(row["checkerId"], "GCABC12")

    def test_incomplete_counter_pair_is_null_and_not_observed(self):
        row = self.parse(checker_html(counters=False))
        self.assertIsNone(row["successCount"])
        self.assertIsNone(row["failureCount"])
        self.assertIsNone(row["counterObservedAt"])

    def test_large_complete_solver_list_and_escaped_names(self):
        rows = [[f'user,{i}[x]"', 1700000000 + i] for i in range(1001)]
        import json
        encoded = json.dumps(rows, ensure_ascii=True)
        row = self.parse(checker_html(solvers=encoded))
        self.assertEqual(row["solverListCoverage"], "complete")
        self.assertEqual(row["publicSolverCount"], 1001)
        self.assertEqual(row["publicSolvers"][0]["username"], 'user,0[x]"')
        self.assertEqual(row["publicSolvers"][-1]["sourceRank"], 1001)
        self.assertEqual(row["publicSolvers"][0]["sourceUserId"], None)
        self.assertEqual(row["solverRowsRetrieved"], 1001)
        self.assertEqual(row["solverListObservedAt"], "2026-11-01T00:00:00Z")
        json.dumps(row)

    def test_zero_complete_solver_list_is_distinct_from_missing_or_malformed(self):
        row = self.parse(checker_html(solvers="[]"))
        self.assertEqual(row["solverListCoverage"], "complete")
        self.assertEqual(row["publicSolverCount"], 0)
        no_script = self.parse(checker_html(solvers="[]").replace("var solvers = []; showSolvers(\"\", \"en-US\", solvers);", ""))
        self.assertEqual(no_script["solverListCoverage"], "unknown")
        self.assertEqual(no_script["publicSolvers"], [])
        self.assertEqual(no_script["solverRowsRetrieved"], 0)
        self.assertIsNone(no_script["publicSolverCount"])
        self.assertIsNone(no_script["solverListObservedAt"])
        malformed = checker_html(solvers='[["truncated", 1700000000]')
        bad = self.parse(malformed)
        self.assertEqual(bad["solverListCoverage"], "unknown")
        self.assertEqual(bad["publicSolvers"], [])
        self.assertEqual(bad["solverRowsRetrieved"], 0)
        no_tbody = self.parse(checker_html(solvers="[]").replace('<table><tbody id="solvers"></tbody></table>', ""))
        self.assertEqual(no_tbody["solverListCoverage"], "unknown")
        unverified_function = self.parse(checker_html(solvers="[]").replace('<script src="https://static.certitudes.org/html/certitude.js"></script>', ""))
        self.assertEqual(unverified_function["solverListCoverage"], "unknown")

    def test_fractional_timestamps_are_utc(self):
        html = checker_html(solvers='[["Fractional", 1700000000.75]]').replace("1600000000, 1700000000", "1600000000.25, 1700000000.75")
        row = self.parse(html)
        self.assertEqual(row["publicSolvers"][0]["certifiedAt"], "2023-11-14T22:13:20.750000Z")
        self.assertEqual(row["publishedAt"], "2020-09-13T12:26:40.250000Z")


if __name__ == "__main__":
    unittest.main()
