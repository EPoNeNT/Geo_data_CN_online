import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from certitude_source import parse_dashboard_html


def dashboard(row=None, next_markup='<div class="next logo"><a href="/?sort=act&amp;dir=desc&amp;idx=a%2Fb%3D">[emptyvisibletext]</a></div>', action='/?sort=act&amp;dir=desc'):
    row = row if row is not None else '''<tr><td class="c-wp"><a href="https://www.certitudes.org/certitude?wp=W1AbC">checker</a></td>
      <td class="c-nam"><a href="https://coord.info/GC12345">parent</a></td>
      <td class="c-dat">1700000000</td><td class="c-woo">3</td><td class="c-doh">2</td>
      <td class="c-act act" t="83978"></td>
      </tr>'''
    return f'''<!doctype html><html><body><form action="{action}" method="post">
      <select name="filter_country"><option value="47">China</option><option value="194" selected>Taiwan</option>
      <option value="91">Hong Kong</option><option value="258">Macao</option></select>
      <select name="filter_region"><option selected>All</option></select><select name="filter_quadrangle"><option selected>All</option></select>
      <input name="filter_owner" value=""><input name="filter_user" value=""></form>
      <table><thead><tr><th>Solution</th><th>Owner</th><th>Puzzle</th><th>Location</th><th>Date published</th><th>WooHoo!</th><th>D'Oh!</th><th><a href="/?sort=act&amp;dir=desc">Latest activity</a></th></tr></thead>
      <tbody>{row}</tbody></table>{next_markup}<a href="/login?return=/?idx=decoy">Login</a></body></html>'''


class DashboardParserTests(unittest.TestCase):
    def test_extracts_observed_filters_rows_and_preserves_cursor(self):
        result = parse_dashboard_html(dashboard(), "https://www.certitudes.org/", expected_country_id="194")
        self.assertEqual(result["filterAction"], "https://www.certitudes.org/?sort=act&dir=desc")
        self.assertEqual(result["countries"], {"china": "47", "taiwan": "194", "hong-kong": "91", "macao": "258"})
        self.assertEqual(result["selectedCountry"], "194")
        self.assertEqual(result["filters"]["filter_region"], "All")
        self.assertEqual(result["nextUrl"], "https://www.certitudes.org/?sort=act&dir=desc&idx=a%2Fb%3D")
        self.assertEqual(result["rows"], [{"checkerId": "W1AbC", "sourceUrl": "https://www.certitudes.org/certitude?wp=W1AbC",
            "cacheCode": "GC12345", "publishedAt": None, "ageSeconds": 83978, "successCount": 3, "failureCount": 2}])
        self.assertNotIn("owner", result["rows"][0])

    def test_age_is_elapsed_seconds_never_epoch(self):
        html = dashboard().replace('t="83978"', 't="1700000000"')
        row = parse_dashboard_html(html, "https://www.certitudes.org/")["rows"][0]
        self.assertEqual(row["ageSeconds"], 1700000000)

    def test_publication_time_comes_from_date_cell_epoch_and_exposes_dat_sort(self):
        html = dashboard(action='/?sort=dat&amp;dir=desc',
                         next_markup='<div class="next logo"><a href="/?sort=dat&amp;dir=desc&amp;idx=older">Next</a></div>')
        html = html.replace('<td class="c-dat">1700000000</td>', '<td class="c-dat date" t="1793549100" title="Asia/Shanghai">ignored</td>')
        result = parse_dashboard_html(html, "https://www.certitudes.org/")
        self.assertEqual(result["publicationSortUrl"], "https://www.certitudes.org/?sort=dat&dir=desc")
        self.assertEqual(result["rows"][0]["publishedAt"], "2026-11-01T16:05:00Z")
        self.assertEqual(result["nextUrl"], "https://www.certitudes.org/?sort=dat&dir=desc&idx=older")

    def test_publication_time_ignores_text_title_and_missing_or_invalid_epoch(self):
        html = dashboard().replace('<td class="c-dat">1700000000</td>',
            '<td class="c-dat date" title="2026-11-02">publication text</td>')
        self.assertIsNone(parse_dashboard_html(html, "https://www.certitudes.org/")["rows"][0]["publishedAt"])
        html = dashboard().replace('<td class="c-dat">1700000000</td>',
            '<td class="c-dat date" t="not-a-time">publication text</td>')
        self.assertIsNone(parse_dashboard_html(html, "https://www.certitudes.org/")["rows"][0]["publishedAt"])

    def test_activity_and_publication_controls_are_extracted_independently(self):
        html = dashboard(action='/?sort=act&amp;dir=desc', next_markup='')
        html = html.replace('<th>Date published</th>', '<th><a href="/?sort=dat&amp;dir=desc">Date published</a></th>')
        result = parse_dashboard_html(html, "https://www.certitudes.org/")
        self.assertEqual(result["activitySortUrl"], "https://www.certitudes.org/?sort=act&dir=desc")
        self.assertEqual(result["publicationSortUrl"], "https://www.certitudes.org/?sort=dat&dir=desc")

    def test_activity_age_uses_only_the_whole_second_t_attribute(self):
        html = dashboard().replace('t="83978"', 'title="1700000000"')
        row = parse_dashboard_html(html, "https://www.certitudes.org/")["rows"][0]
        self.assertIsNone(row["ageSeconds"])

    def test_missing_activity_age_and_invalid_counts_are_null(self):
        row = dashboard().replace('t="83978"', 't="-1"').replace('class="c-woo">3', 'class="c-woo">-1')
        parsed = parse_dashboard_html(row, "https://www.certitudes.org/")["rows"][0]
        self.assertIsNone(parsed["ageSeconds"])
        self.assertIsNone(parsed["successCount"])

    def test_publication_epoch_is_not_used_as_activity_age(self):
        row = parse_dashboard_html(dashboard().replace('t="83978"', ''), "https://www.certitudes.org/")["rows"][0]
        self.assertIsNone(row["ageSeconds"])

    def test_expected_country_rejects_restrictive_subfilters(self):
        html = dashboard().replace('<option selected>All</option>', '<option value="North" selected>North</option>', 1)
        with self.assertRaises(ValueError):
            parse_dashboard_html(html, "https://www.certitudes.org/", expected_country_id="194")

    def test_terminal_page_without_cursor_is_valid(self):
        result = parse_dashboard_html(dashboard(next_markup=""), "https://www.certitudes.org/")
        self.assertIsNone(result["nextUrl"])

    def test_terminal_page_with_empty_next_logo_is_valid(self):
        result = parse_dashboard_html(dashboard(next_markup='<div class="next logo"></div>'), "https://www.certitudes.org/")
        self.assertIsNone(result["nextUrl"])

    def test_initial_dat_controls_page_accepts_matching_dat_cursor(self):
        html = dashboard(action='/?sort=dat&amp;dir=desc',
                         next_markup='<div class="next logo"><a href="/?sort=dat&amp;dir=desc&amp;idx=older">Next</a></div>')
        result = parse_dashboard_html(html, "https://www.certitudes.org/")
        self.assertEqual(result["sort"], "dat")
        self.assertEqual(result["direction"], "desc")
        self.assertEqual(result["nextUrl"], "https://www.certitudes.org/?sort=dat&dir=desc&idx=older")
        self.assertEqual(result["activitySortUrl"], "https://www.certitudes.org/?sort=act&dir=desc")

    def test_country_mismatch_and_incomplete_or_malformed_pages_raise(self):
        with self.assertRaises(ValueError):
            parse_dashboard_html(dashboard(), "https://www.certitudes.org/", expected_country_id="47")
        for html in (dashboard().replace("</html>", ""), dashboard().replace('name="filter_country"', 'name="country"'),
                     dashboard('<tr><td class="c-wp">broken')):
            with self.subTest(html=html[:40]), self.assertRaises(ValueError):
                parse_dashboard_html(html, "https://www.certitudes.org/")

    def test_empty_rows_are_allowed_if_dashboard_structure_is_valid(self):
        result = parse_dashboard_html(dashboard(row=""), "https://www.certitudes.org/")
        self.assertEqual(result["rows"], [])


if __name__ == "__main__":
    unittest.main()
