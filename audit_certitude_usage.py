"""Audit project Mystery descriptions for direct Certitude links."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import uuid
from urllib.parse import urlencode

import requests

from certitude_usage_source import USAGE_RULE_VERSION, inspect_description
from crawl_certitude import BoundedFetcher, FetchError, load_candidates
from certitude_discovery import index_publications
from certitude_store import CertitudeStore
from generate_certitude import load_exclusions
from runtime_utils import (connect_postgres, create_geocaching_session, looks_like_login_page,
                           optional_cookie, require_env, setup_logging)


SCOPE = "usage-audit:all"
GEOCACHING_HOSTS = {"www.geocaching.com", "geocaching.com"}
DETAIL_BASE = "https://www.geocaching.com/seek/cache_details.aspx"
_GC = re.compile(r"GC[A-Z0-9]+", re.I)
_UPDATED_REVIEW_REASONS = {"unverified_description_image", "image_only_description", "unverified_interactive_content"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _known_created(checkers):
    result = set()
    for record in checkers.values():
        if not isinstance(record, dict):
            continue
        parent = record.get("cacheCode")
        if record.get("mappingStatus") == "verified" and isinstance(parent, str):
            result.add(parent.strip().upper())
        elif record.get("sourceUrl"):
            parent = (record.get("activity") or {}).get("expectedCacheCode")
            if isinstance(parent, str):
                result.add(parent.strip().upper())
    return result


def _atomic_json(root, name, value):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = (root / name).resolve()
    if path.parent != root:
        raise ValueError("output_path_outside_root")
    temp = root / ("." + name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, allow_nan=False, indent=2)
            handle.write("\n")
        os.replace(temp, path)
    finally:
        if temp.exists() and temp.parent == root:
            temp.unlink()


def _cookie(premium):
    return optional_cookie("GEOCOOKIE_PREMIUM") if premium else optional_cookie("GEOCOOKIE_NONPREMIUM", "GEOCACHING_COOKIE")


def _inspect_usage_page(html, url, code):
    if looks_like_login_page(url, html):
        return {"status": "unknown", "reason": "login_or_premium", "links": []}
    result = inspect_description(html, url, code)
    if (result.get("reason") == "login_or_premium"
            and re.search(r'"isAuthenticated"\s*:\s*true\b', html)):
        # A logged-in listing can discuss login requirements in its puzzle text.
        # Keep the description unconfirmed without treating it as a failed session.
        result = {"status": "unknown", "reason": "access_text_requires_review", "links": []}
    return result


def _needs_rule_recheck(record):
    version = record.get("ruleVersion", 0)
    return (record.get("status") == "unknown" and record.get("reason") in _UPDATED_REVIEW_REASONS
            and (not isinstance(version, int) or version < USAGE_RULE_VERSION))


def collect(connection, *, apply_exclusions=False, limit=0, restart=False,
            output_dir=".runtime/certitude-usage-audit", fetcher=None, logger=None,
            session_factory=create_geocaching_session):
    """Discover known checkers, inspect cache descriptions, and checkpoint every result."""
    if limit < 0:
        raise ValueError("limit must be nonnegative")
    store = CertitudeStore(connection)
    store.ensure_schema()
    candidates = load_candidates(connection)
    candidate_codes = {row["code"].strip().upper() for row in candidates}
    checkers = store.load_checkers()
    audits = deepcopy(store.load_usage_audits())
    scans = store.load_activity_scans()
    fetcher = fetcher or BoundedFetcher(
        delay=float(os.getenv("CERTITUDE_REQUEST_DELAY_SECONDS", "3")),
        retries=int(os.getenv("CERTITUDE_MAX_RETRIES", "3")),
        timeout=float(os.getenv("CERTITUDE_REQUEST_TIMEOUT_SECONDS", "30")))

    discovery = index_publications(store, fetcher, candidates, checkers, page_limit=0, logger=logger)
    discovery_failed = bool(discovery.get("failedRegions"))

    previous_run = scans.get(SCOPE) if isinstance(scans.get(SCOPE), dict) else {}
    old_run_id = previous_run.get("runId")
    rule_upgrade = any(code in candidate_codes and isinstance(record, dict)
                       and record.get("runId") == old_run_id and _needs_rule_recheck(record)
                       for code, record in audits.items())
    if (restart or previous_run.get("status") == "complete" and not rule_upgrade or not old_run_id
            or previous_run.get("applyExclusions", False) != apply_exclusions):
        run_id = str(uuid.uuid4())
    else:
        run_id = old_run_id

    known = _known_created(checkers)
    rows = sorted((row for row in candidates if isinstance(row, dict)
                   and isinstance(row.get("code"), str) and _GC.fullmatch(row["code"].strip().upper())),
                  key=lambda row: row["code"].strip().upper())
    pending = []
    for row in rows:
        code = row["code"].strip().upper()
        record = audits.get(code) if isinstance(audits.get(code), dict) else {}
        if (record.get("runId") != run_id or record.get("retryPending") or _needs_rule_recheck(record)
                or apply_exclusions and code in known and record.get("lastValidStatus") == "not_linked"
                and record.get("autoExcluded") is not True):
            pending.append((code, row, record))
    selected = pending[:limit] if limit else pending

    if logger:
        logger.info("Certitude usage audit started: run_id=%s candidates=%s pending=%s limit=%s",
                    run_id, len(rows), len(pending), limit)
    attempted = linked = not_linked = unknown_count = new_links = 0
    auth_stopped = False
    stop_reason = None
    scan = {"runId": run_id, "lastAttemptAt": _now(), "status": "partial",
            "applyExclusions": apply_exclusions, "discoveryFailed": discovery_failed, "attempted": 0,
            "ruleVersion": USAGE_RULE_VERSION}
    store.save_activity_scan(SCOPE, deepcopy(scan))
    sessions = {}
    try:
        for code, candidate, old in selected:
            attempted += 1
            previous = deepcopy(old)
            stamp = _now()
            premium = bool(candidate.get("premium_only"))
            if logger:
                logger.info("Certitude usage cache started: code=%s premium=%s attempted=%s pending=%s",
                            code, premium, attempted, len(selected))
            status, reason, links, retry = "unknown", "missing_cookie", [], True
            if not auth_stopped:
                profile = "premium" if premium else "nonpremium"
                cookie = _cookie(premium)
                if cookie:
                    try:
                        if profile not in sessions:
                            session = session_factory(cookie, profile=profile)
                            session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; public-read research)"})
                            sessions[profile] = session
                        url = DETAIL_BASE + "?" + urlencode({"wp": code})
                        html, actual_url = fetcher.get(sessions[profile], url, GEOCACHING_HOSTS)
                        result = _inspect_usage_page(html, actual_url, code)
                        if result.get("reason") == "login_or_premium" and not premium and _cookie(True):
                            premium_cookie = _cookie(True)
                            if "premium" not in sessions:
                                sessions["premium"] = session_factory(premium_cookie, profile="premium")
                                sessions["premium"].headers.update({"User-Agent": "Mozilla/5.0 (compatible; public-read research)"})
                            html, actual_url = fetcher.get(sessions["premium"], url, GEOCACHING_HOSTS)
                            result = _inspect_usage_page(html, actual_url, code)
                        status = result.get("status", "unknown")
                        reason = result.get("reason", "unknown")
                        links = result.get("links", []) if status == "linked" else []
                        retry = status == "unknown" and reason == "login_or_premium"
                        if retry:
                            auth_stopped = True
                    except Exception as exc:
                        reason = str(exc) if isinstance(exc, FetchError) and str(exc).startswith("http_") else type(exc).__name__.lower()
                        retry = True
                        if isinstance(exc, FetchError) and str(exc) in {"http_401", "http_403"}:
                            auth_stopped = True
                else:
                    auth_stopped = True
            # A batch stopped by auth still records every attempted item only.
            updated = deepcopy(previous)
            updated.update(runId=run_id, lastAttemptAt=stamp, status=status, reason=reason,
                           retryPending=bool(retry), ruleVersion=USAGE_RULE_VERSION)
            if status in {"linked", "not_linked"}:
                updated.update(checkedAt=_now(), lastValidStatus=status, links=links)
                updated.pop("lastValidReason", None)
                if status == "linked":
                    updated["lastValidReason"] = reason
            candidate_excluded = status == "not_linked" and code in known
            if apply_exclusions and (status == "linked" or candidate_excluded):
                updated["autoExcluded"] = bool(candidate_excluded)
                updated["autoExclusionReason"] = (
                    "完整藏点描述中未发现 Certitude 引用，且已有创建验证页的证据；核验时间：" + updated["checkedAt"]
                    if candidate_excluded else None)
            store.save_usage_audit(code, updated)
            audits[code] = updated
            scan.update(lastAttemptAt=_now(), attempted=attempted)
            store.save_activity_scan(SCOPE, deepcopy(scan))
            linked += status == "linked"
            not_linked += status == "not_linked"
            unknown_count += status == "unknown"
            old_link_keys = {link.get("sourceUrl") for link in (previous.get("links") or []) if isinstance(link, dict)}
            new_links += len({link.get("sourceUrl") for link in links if isinstance(link, dict)} - old_link_keys)
            if logger and (attempted % 10 == 0 or attempted == len(selected)):
                logger.info("Certitude usage audit progress: attempted=%s linked=%s not_linked=%s unknown=%s",
                            attempted, linked, not_linked, unknown_count)
            if auth_stopped:
                stop_reason = {"cacheCode": code, "reason": reason}
                if logger:
                    logger.warning("Certitude usage audit stopped: code=%s reason=%s; checkpoint saved for resume",
                                   code, reason)
                break
    finally:
        for session in sessions.values():
            try:
                session.close()
            except Exception:
                pass

    retry_pending = any((audits.get(row["code"].strip().upper()) or {}).get("retryPending") for row in rows)
    remaining = any((audits.get(row["code"].strip().upper()) or {}).get("runId") != run_id or
                    (audits.get(row["code"].strip().upper()) or {}).get("retryPending") or
                    _needs_rule_recheck(audits.get(row["code"].strip().upper()) or {}) for row in rows)
    complete = not remaining and not discovery_failed
    scan.update(lastAttemptAt=_now(), status="complete" if complete else "partial", attempted=attempted,
                stopReason=stop_reason)
    store.save_activity_scan(SCOPE, scan)

    excluded_codes = sorted(code for code, row in ((row["code"].strip().upper(), row) for row in rows)
                            if (audits.get(code) or {}).get("status") == "not_linked"
                            and (audits.get(code) or {}).get("runId") == run_id and code in known)
    manual = load_exclusions()
    merged_exclusions = {code: record["autoExclusionReason"] for code, record in audits.items()
                         if record.get("autoExcluded") is True and record.get("autoExclusionReason")}
    merged_exclusions.update(manual)
    for code in excluded_codes:
        merged_exclusions.setdefault(code, "完整藏点描述中未发现 Certitude 引用，且已有创建验证页的证据。")
    report_rows = []
    for code, row in ((row["code"].strip().upper(), row) for row in rows):
        record = audits.get(code) or {}
        current = record.get("runId") == run_id
        item = {"code": code, "status": record.get("status", "unknown") if current else "not_checked",
                "reason": record.get("reason"), "hasKnownChecker": code in known,
                "candidateExcluded": code in excluded_codes, "checkedAt": record.get("checkedAt"),
                "lastAttemptAt": record.get("lastAttemptAt"), "linkCount": len(record.get("links", []))}
        report_rows.append(item)
    current_records = [audits[row["code"].strip().upper()] for row in rows
                       if (audits.get(row["code"].strip().upper()) or {}).get("runId") == run_id]
    failed_count = sum(bool(record.get("retryPending")) for record in current_records)
    rule_pending = sum(_needs_rule_recheck(record) and not record.get("retryPending") for record in current_records)
    unchecked_count = len(rows) - len(current_records)
    summary = {"total": len(rows), "attempted": attempted, "checked": len(current_records),
               "linked": sum(record.get("status") == "linked" for record in current_records),
               "not_linked": sum(record.get("status") == "not_linked" for record in current_records),
               "unknown": sum(record.get("status") == "unknown" and not record.get("retryPending")
                              for record in current_records),
               "failed": failed_count, "unchecked": unchecked_count,
               "ruleRecheckPending": rule_pending,
               "pending": unchecked_count + failed_count + rule_pending,
               "reasonCounts": dict(Counter(record.get("reason", "unknown") for record in current_records)),
               "newlinks": new_links, "excludedCandidates": len(excluded_codes)}
    report = {"runId": run_id, "partial": not complete or discovery_failed,
              "retryPending": retry_pending,
              "discoveryFailed": discovery_failed, "stopReason": stop_reason,
              "summary": summary, "items": report_rows}
    _atomic_json(output_dir, "report.json", report)
    _atomic_json(output_dir, "certitude_exclusions.json", merged_exclusions)
    if logger:
        logger.info("Certitude usage audit finished: run_id=%s status=%s total=%s attempted=%s checked=%s linked=%s not_linked=%s review_unknown=%s failed=%s unchecked=%s rule_recheck_pending=%s pending=%s excluded=%s discovery_failed=%s",
                    run_id, scan["status"], len(rows), attempted, summary["checked"], summary["linked"], summary["not_linked"],
                    summary["unknown"], summary["failed"], summary["unchecked"], rule_pending, summary["pending"],
                    len(excluded_codes), discovery_failed)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply-exclusions", action="store_true",
                        help="Save confirmed automatic exclusions to Neon for later generation")
    parser.add_argument("--limit", type=int, default=0,
                        help="Maximum descriptions to inspect (0 means all remaining Mystery caches)")
    parser.add_argument("--restart", action="store_true",
                        help="Start a fresh full audit instead of resuming the saved run")
    parser.add_argument("--output-dir", default=".runtime/certitude-usage-audit")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be nonnegative")
    logger = setup_logging("audit_certitude_usage.log")
    connection = connect_postgres(require_env("DATABASE_URL"), logger=logger)
    try:
        report = collect(connection, apply_exclusions=args.apply_exclusions, limit=args.limit,
                         restart=args.restart, output_dir=args.output_dir, logger=logger)
        if report["discoveryFailed"] or report["retryPending"]:
            stopped = report.get("stopReason") or {}
            logger.error("Certitude usage audit incomplete: discovery_failed=%s failed=%s pending=%s stopped_cache=%s stop_reason=%s; saved progress will resume without --restart",
                         report["discoveryFailed"], report["summary"]["failed"], report["summary"]["pending"],
                         stopped.get("cacheCode"), stopped.get("reason"))
            raise SystemExit(1)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
