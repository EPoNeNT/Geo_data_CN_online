import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audit_certitude_usage as audit


def page(code, body="Description text"):
    url = f"https://www.geocaching.com/seek/cache_details.aspx?wp={code}"
    return (f'<html><body><h1><a href="https://coord.info/{code}">{code}</a></h1>'
            f'<div id="ShortDescription">{body}</div><div id="LongDescription">Hint</div></body></html>', url)


class Store:
    def __init__(self, candidates, checkers=None, audits=None):
        self.candidates, self.checkers, self.audits = candidates, checkers or {}, audits or {}
        self.saved = []
        self.scans = {}
        self.events = []

    def ensure_schema(self): pass
    def load_checkers(self): return self.checkers
    def load_usage_audits(self): return self.audits
    def load_activity_scans(self): return self.scans
    def save_activity_scan(self, scope, record):
        self.events.append(("scan", dict(record)))
        self.scans[scope] = record
    def save_usage_audit(self, code, record):
        self.events.append(("audit", code))
        self.saved.append((code, dict(record)))
        self.audits[code] = dict(record)


class Session:
    def __init__(self, *args, **kwargs):
        self.args, self.closed = args, False
        self.headers = {}
    def close(self): self.closed = True


class Fetcher:
    def __init__(self, values): self.values = values
    def get(self, session, url, hosts):
        code = url.split("wp=")[-1]
        value = self.values[code]
        if isinstance(value, BaseException): raise value
        return value


class InterruptingFetcher:
    def __init__(self, values):
        self.values = values
        self.calls = []
    def get(self, session, url, hosts):
        code = url.split("wp=")[-1]
        self.calls.append(code)
        value = self.values[code]
        if isinstance(value, BaseException): raise value
        return value


class AuditRunnerTests(unittest.TestCase):
    def test_authenticated_access_text_does_not_abort_all_remaining_caches(self):
        candidates = [{"code": code, "premium_only": False} for code in ("GC12345", "GC12346")]
        html, url = page("GC12345", 'You must be logged in to another puzzle service.')
        html = html.replace('</body>', '<script>var state={"isAuthenticated":true};</script></body>')
        report, store = self.run_audit(candidates, {"GC12345": (html, url), "GC12346": page("GC12346")})
        self.assertEqual(report["summary"]["attempted"], 2)
        self.assertEqual(store.audits["GC12345"]["status"], "unknown")
        self.assertFalse(store.audits["GC12345"]["retryPending"])
        self.assertFalse(report["retryPending"])
        self.assertFalse(report["partial"])

    def test_actual_login_page_still_stops_even_with_authenticated_text(self):
        candidates = [{"code": code, "premium_only": False} for code in ("GC12345", "GC12346")]
        html = '<html><body><script>{"isAuthenticated":true}</script></body></html>'
        report, store = self.run_audit(candidates, {
            "GC12345": (html, "https://www.geocaching.com/account/signin"),
            "GC12346": page("GC12346"),
        })
        self.assertEqual(report["summary"]["attempted"], 1)
        self.assertTrue(report["retryPending"])
        self.assertEqual(report["stopReason"], {"cacheCode": "GC12345", "reason": "login_or_premium"})

    def test_summary_separates_unchecked_caches_from_unknown_and_failed(self):
        candidates = [{"code": code, "premium_only": False} for code in ("GC12345", "GC12346")]
        report, _ = self.run_audit(candidates, {"GC12345": page("GC12345")}, limit=1,
                                  audits={"GC12346": {"status": "unknown", "runId": "older-run"}})
        summary = report["summary"]
        self.assertEqual((summary["checked"], summary["unchecked"], summary["unknown"], summary["failed"]),
                         (1, 1, 0, 0))
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(next(row for row in report["items"] if row["code"] == "GC12346")["status"], "not_checked")

    def test_old_auth_checkpoint_retries_stopped_cache_and_continues_remaining(self):
        candidates = [{"code": code, "premium_only": code == "GC12346"}
                      for code in ("GC12345", "GC12346", "GC12347")]
        run_id = "saved-run"
        audits = {"GC12345": {"runId": run_id, "status": "linked", "retryPending": False},
                  "GC12346": {"runId": run_id, "status": "unknown", "reason": "login_or_premium", "retryPending": True}}
        scans = {audit.SCOPE: {"runId": run_id, "status": "partial", "applyExclusions": True,
                               "discoveryFailed": True}}
        html, url = page("GC12346", "You must be logged in to another puzzle service.")
        html = html.replace('</body>', '<script>{"isAuthenticated":true}</script></body>')
        report, store = self.run_audit(candidates, {"GC12346": (html, url), "GC12347": page("GC12347")},
                                      audits=audits, scans=scans, apply_exclusions=True)
        self.assertEqual(report["runId"], run_id)
        self.assertEqual(report["summary"]["attempted"], 2)
        self.assertEqual(report["summary"]["checked"], 3)
        self.assertEqual(report["summary"]["unchecked"], 0)
        self.assertEqual(report["summary"]["failed"], 0)
        self.assertFalse(report["partial"])
        self.assertEqual(store.audits["GC12346"]["reason"], "access_text_requires_review")

    def test_applied_audit_exclusion_is_consumed_by_generation(self):
        import generate_certitude
        from unittest.mock import MagicMock
        candidates = [{"code": "GC12345", "country": "China", "cache_status": 0}]
        checkers = {"Opaque": {"checkerId": "Opaque", "cacheCode": "GC12345",
                               "mappingStatus": "verified", "role": "final",
                               "sourceUrl": "https://www.certitudes.org/certitude?wp=Opaque"}}
        manual_before = generate_certitude.EXCLUSIONS_PATH.read_bytes()
        _, store = self.run_audit(candidates, {"GC12345": page("GC12345")},
                                  checkers=checkers, apply_exclusions=True)
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.side_effect = [list(checkers.items()), [],
                                       [("GC12345", store.audits["GC12345"])]]
        with patch.object(generate_certitude, "load_candidates", return_value=candidates):
            inputs = generate_certitude.load_inputs(connection)
        manifest, scopes, facts = generate_certitude.build_dataset(*inputs)
        self.assertEqual(facts, {})
        self.assertTrue(all(scope["summary"]["cacheCount"] == 0 for scope in scopes))
        self.assertEqual(len(scopes), 15)
        self.assertEqual(manifest["schemaVersion"], 2)
        self.assertEqual(generate_certitude.EXCLUSIONS_PATH.read_bytes(), manual_before)
        self.assertIn("Opaque", store.checkers)

    def run_audit(self, candidates, values, *, checkers=None, audits=None, scans=None, **kwargs):
        store = Store(candidates, checkers, audits)
        store.scans = scans or {}
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as scratch:
            kwargs.setdefault("output_dir", scratch)
            with patch.object(audit, "CertitudeStore", return_value=store), \
                patch.object(audit, "load_candidates", return_value=candidates), \
                patch.object(audit, "index_publications", return_value={"failedRegions": []}), \
                patch.object(audit, "optional_cookie", return_value="gspkauth=x"), \
                patch.object(audit, "create_geocaching_session", side_effect=lambda *a, **k: Session()):
                report = audit.collect(object(), fetcher=Fetcher(values),
                                       session_factory=audit.create_geocaching_session, **kwargs)
        return report, store

    def test_normal_non_certitude_mystery_is_not_excluded_by_default(self):
        candidates = [{"code": "GC12345", "premium_only": False}]
        report, store = self.run_audit(candidates, {"GC12345": page("GC12345")})
        self.assertEqual(report["summary"]["excludedCandidates"], 0)
        self.assertNotIn("autoExcluded", store.audits["GC12345"])

    def test_known_unused_parent_is_candidate_then_applied_only_with_flag(self):
        candidates = [{"code": "GC12345", "premium_only": False}]
        checkers = {"opaque": {"mappingStatus": "verified", "cacheCode": "GC12345"}}
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            report, store = self.run_audit(candidates, {"GC12345": page("GC12345")}, checkers=checkers,
                                           apply_exclusions=True, output_dir=directory)
            self.assertEqual(report["summary"]["excludedCandidates"], 1)
            self.assertTrue(store.audits["GC12345"]["autoExcluded"])
            self.assertIn("GC12345", json.loads((Path(directory) / "certitude_exclusions.json").read_text(encoding="utf-8")))

    def test_linked_parent_clears_only_automatic_exclusion(self):
        candidates = [{"code": "GC12345", "premium_only": False}]
        audit_record = {"autoExcluded": True, "autoExclusionReason": "old", "lastValidStatus": "not_linked"}
        body = '<a href="https://certitudes.org/?wp=GCAAA">checker</a>'
        report, store = self.run_audit(candidates, {"GC12345": page("GC12345", body)},
                                       audits={"GC12345": audit_record}, apply_exclusions=True)
        self.assertFalse(store.audits["GC12345"]["autoExcluded"])

    def test_failure_retains_last_valid_evidence_and_auto_flag(self):
        candidates = [{"code": "GC12345", "premium_only": False}]
        previous = {"autoExcluded": True, "lastValidStatus": "not_linked", "checkedAt": "old",
                    "links": [{"url": "https://certitudes.org/?wp=GCAAA"}]}
        report, store = self.run_audit(candidates, {"GC12345": RuntimeError("offline")}, audits={"GC12345": previous})
        current = store.audits["GC12345"]
        self.assertEqual(current["checkedAt"], "old")
        self.assertEqual(current["links"], previous["links"])
        self.assertTrue(current["autoExcluded"])
        self.assertTrue(current["retryPending"])

    def test_unknown_preserves_previous_valid_timestamp_links_and_automatic_flag(self):
        candidates = [{"code": "GC12345", "premium_only": False}]
        previous = {"autoExcluded": True, "lastValidStatus": "not_linked", "checkedAt": "old",
                    "links": [{"url": "https://certitudes.org/?wp=GCAAA"}]}
        html, url = page("GC12345", "Certitude checker reference without a direct link")
        html = html.replace('<div id="LongDescription">Hint</div>', '<div id="LongDescription"></div>')
        report, store = self.run_audit(candidates, {"GC12345": (html, url)},
                                       audits={"GC12345": previous}, apply_exclusions=True)
        current = store.audits["GC12345"]
        self.assertEqual(current["status"], "unknown")
        self.assertEqual(current["checkedAt"], "old")
        self.assertEqual(current["links"], previous["links"])
        self.assertTrue(current["autoExcluded"])

    def test_archived_premium_candidate_uses_premium_profile(self):
        candidates = [{"code": "GC12345", "premium_only": True, "cache_status": "Archived"}]
        store = Store(candidates)
        used = []
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as scratch, \
                patch.object(audit, "CertitudeStore", return_value=store), \
                patch.object(audit, "load_candidates", return_value=candidates), \
                patch.object(audit, "index_publications", return_value={"failedRegions": []}), \
                patch.object(audit, "optional_cookie", return_value="gspkauth=x"), \
                patch.object(audit, "create_geocaching_session", side_effect=lambda *a, **k: used.append(k["profile"]) or Session()):
            report = audit.collect(object(), fetcher=Fetcher({"GC12345": page("GC12345")}),
                                   output_dir=scratch, session_factory=audit.create_geocaching_session)
        self.assertEqual(used, ["premium"], report)

    def test_outputs_contain_no_html(self):
        candidates = [{"code": "GC12345", "premium_only": False}]
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            self.run_audit(candidates, {"GC12345": page("GC12345")}, output_dir=directory)
            for path in Path(directory).glob("*.json"):
                self.assertNotIn("<html>", path.read_text(encoding="utf-8"))

    def test_missing_cookie_checkpoints_unknown_and_stops_batch(self):
        candidates = [{"code": code, "premium_only": False} for code in ("GC12345", "GC12346")]
        store = Store(candidates)
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as scratch, \
                patch.object(audit, "CertitudeStore", return_value=store), \
                patch.object(audit, "load_candidates", return_value=candidates), \
                patch.object(audit, "index_publications", return_value={"failedRegions": []}), \
                patch.object(audit, "optional_cookie", return_value=""):
            report = audit.collect(object(), fetcher=Fetcher({}), output_dir=scratch)
        self.assertEqual(report["summary"]["attempted"], 1)
        self.assertEqual(store.audits["GC12345"]["reason"], "missing_cookie")
        self.assertTrue(store.audits["GC12345"]["retryPending"])
        self.assertNotIn("GC12346", store.audits)

    def test_limit_resumes_same_run_and_completion_starts_new_run(self):
        candidates = [{"code": code, "premium_only": False} for code in ("GC12345", "GC12346")]
        first, store = self.run_audit(candidates, {code: page(code) for code in ("GC12345", "GC12346")}, limit=1)
        run_id = first["runId"]
        second, _ = self.run_audit(candidates, {code: page(code) for code in ("GC12345", "GC12346")},
                                   audits=store.audits, scans=store.scans)
        self.assertEqual(second["runId"], run_id)

    def test_keyboard_interrupt_keeps_first_result_and_resume_only_fetches_second(self):
        candidates = [{"code": code, "premium_only": False} for code in ("GC12345", "GC12346")]
        store = Store(candidates)
        fetcher = InterruptingFetcher({"GC12345": page("GC12345"), "GC12346": KeyboardInterrupt()})
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as scratch, \
                patch.object(audit, "CertitudeStore", return_value=store), \
                patch.object(audit, "load_candidates", return_value=candidates), \
                patch.object(audit, "index_publications", return_value={"failedRegions": []}), \
                patch.object(audit, "optional_cookie", return_value="gspkauth=x"), \
                patch.object(audit, "create_geocaching_session", side_effect=lambda *a, **k: Session()):
            with self.assertRaises(KeyboardInterrupt):
                audit.collect(object(), fetcher=fetcher, output_dir=scratch,
                              session_factory=audit.create_geocaching_session)
            self.assertIn("GC12345", store.audits)
            self.assertEqual(store.scans[audit.SCOPE]["attempted"], 1)
            self.assertEqual([event[0] for event in store.events[:3]], ["scan", "audit", "scan"])
            run_id = store.scans[audit.SCOPE]["runId"]
            fetcher.values["GC12346"] = page("GC12346")
            audit.collect(object(), fetcher=fetcher, output_dir=scratch,
                          session_factory=audit.create_geocaching_session)
        self.assertEqual(fetcher.calls, ["GC12345", "GC12346", "GC12346"])
        self.assertEqual(store.scans[audit.SCOPE]["runId"], run_id)
        self.assertEqual(store.audits["GC12345"]["runId"], run_id)

    def test_unknown_current_read_does_not_export_old_nonautomatic_exclusion(self):
        candidates = [{"code": "GC12345", "premium_only": False}]
        checkers = {"opaque": {"mappingStatus": "verified", "cacheCode": "GC12345"}}
        old = {"runId": "old-run", "status": "not_linked", "lastValidStatus": "not_linked",
               "checkedAt": "old-check", "autoExcluded": False, "links": []}
        html, url = page("GC12345", "Certitude checker reference without a direct link")
        html = html.replace('<div id="LongDescription">Hint</div>', '<div id="LongDescription"></div>')
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            report, store = self.run_audit(candidates, {"GC12345": (html, url)}, checkers=checkers,
                                           audits={"GC12345": old}, apply_exclusions=True,
                                           output_dir=directory)
            exclusions = json.loads((Path(directory) / "certitude_exclusions.json").read_text(encoding="utf-8"))
        self.assertEqual(store.audits["GC12345"]["status"], "unknown")
        self.assertEqual(store.audits["GC12345"]["checkedAt"], "old-check")
        self.assertFalse(report["items"][0]["candidateExcluded"])
        self.assertNotIn("GC12345", exclusions)

    def test_tokenized_certitude_link_stays_in_database_only(self):
        candidates = [{"code": "GC12345", "premium_only": False}]
        secret = "PRIVATEQUERYTOKEN123"
        body = f'<a href="https://certitudes.org/?wp=GCAAA&amp;token={secret}">checker</a>'
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
            report, store = self.run_audit(candidates, {"GC12345": page("GC12345", body)},
                                           output_dir=directory)
            outputs = "\n".join(path.read_text(encoding="utf-8")
                                 for path in Path(directory).glob("*.json"))
        self.assertIn(secret, json.dumps(store.audits["GC12345"], ensure_ascii=False))
        self.assertNotIn(secret, outputs)
        self.assertNotIn(secret, json.dumps(report, ensure_ascii=False))
        self.assertIn("linkCount", report["items"][0])
        self.assertNotIn("links", report["items"][0])
        self.assertNotIn("sourceURL", report["items"][0])

    def test_switching_apply_mode_starts_new_run_and_rechecks_all(self):
        candidates = [{"code": code, "premium_only": False} for code in ("GC12345", "GC12346")]
        store = Store(candidates)
        fetcher = InterruptingFetcher({code: page(code) for code in ("GC12345", "GC12346")})
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as scratch, \
                patch.object(audit, "CertitudeStore", return_value=store), \
                patch.object(audit, "load_candidates", return_value=candidates), \
                patch.object(audit, "index_publications", return_value={"failedRegions": []}), \
                patch.object(audit, "optional_cookie", return_value="gspkauth=x"), \
                patch.object(audit, "create_geocaching_session", side_effect=lambda *a, **k: Session()):
            first = audit.collect(object(), fetcher=fetcher, limit=1, output_dir=scratch,
                                  session_factory=audit.create_geocaching_session)
            first_run_id = first["runId"]
            fetcher.calls.clear()
            second = audit.collect(object(), fetcher=fetcher, apply_exclusions=True,
                                   output_dir=scratch, session_factory=audit.create_geocaching_session)
        self.assertNotEqual(second["runId"], first_run_id)
        self.assertEqual(fetcher.calls, ["GC12345", "GC12346"])

    def test_publication_discovery_in_memory_checker_is_used_for_exclusion(self):
        candidates = [{"code": "GC12345", "premium_only": False}]
        store = Store(candidates)
        def discover(_store, _fetcher, _candidates, checkers, **_kwargs):
            checkers["new-opaque-id"] = {"mappingStatus": "verified", "cacheCode": "GC12345"}
            return {"failedRegions": []}
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as scratch, \
                patch.object(audit, "CertitudeStore", return_value=store), \
                patch.object(audit, "load_candidates", return_value=candidates), \
                patch.object(audit, "index_publications", side_effect=discover), \
                patch.object(audit, "optional_cookie", return_value="gspkauth=x"), \
                patch.object(audit, "create_geocaching_session", side_effect=lambda *a, **k: Session()):
            report = audit.collect(object(), fetcher=Fetcher({"GC12345": page("GC12345")}),
                                   apply_exclusions=True, output_dir=scratch,
                                   session_factory=audit.create_geocaching_session)
        self.assertTrue(report["items"][0]["hasKnownChecker"])
        self.assertTrue(report["items"][0]["candidateExcluded"])

    def test_changed_html_rule_rechecks_only_affected_unknowns_from_completed_run(self):
        candidates = [{"code": code} for code in ("GC12345", "GC12346", "GC12347")]
        audits = {"GC12345": {"runId": "old-run", "status": "linked", "retryPending": False},
                  "GC12346": {"runId": "old-run", "status": "unknown", "reason": "unverified_description_image"},
                  "GC12347": {"runId": "old-run", "status": "unknown", "reason": "image_only_description"}}
        scans = {audit.SCOPE: {"runId": "old-run", "status": "complete", "applyExclusions": True}}
        checkers = {"Opaque": {"mappingStatus": "verified", "cacheCode": "GC12346"}}
        first, store = self.run_audit(candidates, {"GC12346": page("GC12346", '<img src="puzzle.png">')},
                                      checkers=checkers, audits=audits, scans=scans, limit=1,
                                      apply_exclusions=True)
        self.assertEqual(first["runId"], "old-run")
        self.assertEqual(first["summary"]["attempted"], 1)
        self.assertEqual(first["summary"]["ruleRecheckPending"], 1)
        self.assertTrue(first["partial"])
        self.assertTrue(store.audits["GC12346"]["autoExcluded"])
        self.assertEqual(store.audits["GC12346"]["ruleVersion"], 2)
        second, store = self.run_audit(candidates, {"GC12347": page("GC12347", '<img src="map.png">')},
                                       checkers=checkers, audits=store.audits, scans=store.scans,
                                       apply_exclusions=True)
        self.assertEqual(second["summary"]["attempted"], 1)
        self.assertEqual(second["summary"]["ruleRecheckPending"], 0)
        self.assertFalse(second["partial"])

    def test_current_rule_unknown_does_not_get_rechecked_repeatedly(self):
        candidates = [{"code": "GC12345"}, {"code": "GC12346"}]
        audits = {"GC12345": {"runId": "saved", "ruleVersion": 2,
                              "status": "unknown", "reason": "ambiguous_certitude_text"}}
        scans = {audit.SCOPE: {"runId": "saved", "status": "partial", "applyExclusions": False}}
        report, _ = self.run_audit(candidates, {"GC12346": page("GC12346")}, audits=audits, scans=scans)
        self.assertEqual(report["summary"]["attempted"], 1)
        self.assertFalse(report["partial"])

    def test_old_review_for_cache_outside_mystery_scope_does_not_hold_run_open(self):
        candidates = [{"code": "GC12345"}]
        audits = {"GC12345": {"runId": "old-run", "status": "linked"},
                  "GCOUT": {"runId": "old-run", "status": "unknown", "reason": "unverified_description_image"}}
        scans = {audit.SCOPE: {"runId": "old-run", "status": "complete", "applyExclusions": False}}
        report, _ = self.run_audit(candidates, {"GC12345": page("GC12345")}, audits=audits, scans=scans)
        self.assertNotEqual(report["runId"], "old-run")
        self.assertEqual(report["summary"]["attempted"], 1)


if __name__ == "__main__":
    unittest.main()
