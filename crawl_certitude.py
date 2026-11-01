"""Collect public Certitude statistics and retain resumable discovery in Neon."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import time
from urllib.parse import urljoin, urlparse

import requests

from certitude_source import extract_description_links, parse_checker_html
from certitude_store import CertitudeStore, merge_checker
from certitude_activity import index_activity, needs_detail_retry
from runtime_utils import (connect_postgres, create_geocaching_session,
                           looks_like_login_page, optional_cookie, require_env, setup_logging)


COUNTRIES = {"China": "china", "Taiwan": "taiwan", "Hong Kong": "hong-kong", "Macao": "macao"}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def load_candidates(connection):
    """Use existing project Mystery rows, including archived and unknown statuses."""
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT code, name, owner_username, country, city, cache_status,
                   difficulty, terrain, placed_date, details_url, premium_only,
                   logs_crawled_at, to_jsonb(c)->>'updated_at'
            FROM caches c WHERE geocache_type = 8 ORDER BY code
        """)
        fields = ("code", "name", "owner_username", "country", "city", "cache_status",
                  "difficulty", "terrain", "placed_date", "details_url", "premium_only",
                  "logs_crawled_at", "source_version")
        return [dict(zip(fields, row)) for row in cursor.fetchall()]


def description_fingerprint(cache):
    values = [cache.get(k) for k in ("code", "name", "owner_username", "cache_status", "source_version")]
    return hashlib.sha256(json.dumps(values, default=str, ensure_ascii=False).encode("utf-8")).hexdigest()


def select_due_candidates(candidates, descriptions, now, refresh_days, limit):
    due = []
    cutoff = now - timedelta(days=refresh_days)
    for cache in candidates:
        record = descriptions.get(cache["code"], {})
        scanned = record.get("descriptionScannedAt")
        try:
            scanned_time = datetime.fromisoformat(scanned.replace("Z", "+00:00")) if scanned else None
            if scanned_time and scanned_time.tzinfo is None:
                scanned_time = None
        except (ValueError, TypeError):
            scanned_time = None
        complete = record.get("descriptionScanStatus") == "ok" and record.get("descriptionScanCoverage") == "complete"
        if (not complete or not scanned_time or scanned_time <= cutoff or
                record.get("sourceFingerprint") != description_fingerprint(cache)):
            # Unscanned rows precede retries; repeated failures cannot starve discovery.
            due.append((bool(record), scanned or "", cache["code"], cache))
    ordered = [item[3] for item in sorted(due, key=lambda item: item[:3])]
    return ordered[:limit] if limit else ordered


class FetchError(RuntimeError):
    def __init__(self, code, *, metadata=None):
        super().__init__(code)
        self.metadata = dict(metadata or {})


class AuthenticationError(FetchError):
    pass


class BoundedFetcher:
    """Rate-limited reads with bounded redirects, response size, and retries."""
    def __init__(self, delay=1.5, retries=3, timeout=30, max_bytes=16 * 1024 * 1024):
        if delay < 0 or retries < 1 or timeout <= 0 or max_bytes < 1:
            raise ValueError("invalid_request_configuration")
        self.delay, self.retries, self.timeout, self.max_bytes = delay, retries, timeout, max_bytes
        self.last_request = None
        self.last_response_headers = {}

    def get(self, session, url, allowed_hosts):
        return self._request(session, url, allowed_hosts, method="GET")

    def post(self, session, url, allowed_hosts, data):
        parsed = urlparse(url)
        if (parsed.scheme != "https" or parsed.hostname not in ("certitudes.org", "www.certitudes.org")
                or parsed.path != "/" or parsed.username or parsed.password
                or parsed.hostname not in allowed_hosts):
            raise FetchError("untrusted_source_url")
        return self._request(session, url, allowed_hosts, method="POST", data=data)

    def _request(self, session, url, allowed_hosts, method="GET", data=None):
        self.last_response_headers = {}
        for attempt in range(self.retries):
            try:
                current = url
                current_method, current_data = method, data
                for _ in range(6):
                    parsed = urlparse(current)
                    if parsed.scheme not in ("https", "http") or parsed.hostname not in allowed_hosts or parsed.username or parsed.password:
                        raise FetchError("untrusted_source_url")
                    if current_method == "POST" and (
                            parsed.scheme != "https" or parsed.hostname not in ("certitudes.org", "www.certitudes.org")
                            or parsed.path != "/"):
                        raise FetchError("untrusted_source_url")
                    if self.last_request is not None:
                        time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
                    self.last_request = time.monotonic()
                    headers = {"Accept-Language": "en-US,en;q=0.8"}
                    if parsed.hostname in ("certitudes.org", "www.certitudes.org"):
                        headers["User-Agent"] = "Mozilla/5.0 (compatible; public-read research)"
                    with session.request(current_method, current, data=current_data, timeout=self.timeout,
                                         allow_redirects=False, stream=True,
                                         headers=headers) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            if not response.headers.get("Location"):
                                raise FetchError("redirect_missing_location")
                            current = urljoin(current, response.headers["Location"])
                            if current_method == "POST" and response.status_code in (301, 302, 303):
                                current_method, current_data = "GET", None
                            continue
                        if response.status_code != 200:
                            raise FetchError("http_" + str(response.status_code), metadata={
                                "status": response.status_code, "server": response.headers.get("Server"),
                                "contentType": response.headers.get("Content-Type"),
                                "challenge": response.headers.get("cf-mitigated") == "challenge",
                                "host": parsed.hostname, "path": parsed.path, "scheme": parsed.scheme})
                        if "html" not in response.headers.get("Content-Type", "").lower():
                            raise FetchError("non_html_source")
                        chunks, size = [], 0
                        for chunk in response.iter_content(65536):
                            size += len(chunk)
                            if size > self.max_bytes:
                                raise FetchError("response_too_large")
                            chunks.append(chunk)
                        # Both sites serve UTF-8; Requests otherwise defaults HTML to Latin-1.
                        text = b"".join(chunks).decode("utf-8-sig", errors="strict")
                        self.last_response_headers = {key: response.headers[key] for key in
                                                      ("Date", "Cache-Control", "Age", "Expires")
                                                      if key in response.headers}
                        return text, current
                raise FetchError("too_many_redirects")
            except (requests.RequestException, FetchError, UnicodeError) as exc:
                code = str(exc) if isinstance(exc, FetchError) else "request_" + type(exc).__name__.lower()
                if code in ("untrusted_source_url", "non_html_source", "response_too_large", "http_401", "http_403", "http_404") or attempt + 1 == self.retries:
                    raise FetchError(code, metadata=getattr(exc, "metadata", None)) from None
                time.sleep(min(30, 2 ** attempt))
        raise FetchError("request_failed")


def _description_record(previous, cache, now, *, links=None, source=None, error=None):
    record = dict(previous or {})
    record["lastAttemptAt"] = now
    record["sourceFingerprint"] = description_fingerprint(cache)
    if error:
        record.update(descriptionScanStatus="error", errorCode=error)
        record.setdefault("descriptionScanCoverage", "unknown")
        record.setdefault("descriptionLinks", [])
        return record
    # Historical entry links are retained even when a later description omits them.
    combined = list(record.get("descriptionLinks", []))
    for link in links or []:
        if not any(old.get("sourceUrl") == link.get("sourceUrl") and
                   (old.get("sourceOccurrence") or old.get("descriptionEvidence") or old.get("evidence")) ==
                   (link.get("sourceOccurrence") or link.get("descriptionEvidence") or link.get("evidence")) for old in combined):
            combined.append(link)
    record.update(descriptionLinks=combined, descriptionScanStatus="ok", descriptionScanCoverage="complete",
                  descriptionScannedAt=now, descriptionSource=source, errorCode=None)
    return record


def collect(connection, *, description_limit=200, refresh_days=30, activity_page_limit=100, fetcher=None, logger=None):
    store = CertitudeStore(connection)
    store.ensure_schema()
    candidates = load_candidates(connection)
    descriptions, checkers = store.load_descriptions(), store.load_checkers()
    candidate_codes = {row["code"] for row in candidates}
    fetcher = fetcher or BoundedFetcher()
    sessions = {}
    due = select_due_candidates(candidates, descriptions, datetime.now(timezone.utc), refresh_days, description_limit)
    attempted = scanned = 0
    for cache in due:
        code, now = cache["code"], utc_now()
        attempted += 1
        try:
            premium = bool(cache.get("premium_only"))
            if premium not in sessions:
                cookie = optional_cookie("GEOCOOKIE_PREMIUM") if premium else optional_cookie("GEOCOOKIE_NONPREMIUM", "GEOCACHING_COOKIE")
                if not cookie:
                    raise AuthenticationError("missing_geocaching_cookie")
                sessions[premium] = create_geocaching_session(cookie, profile="premium" if premium else "nonpremium")
            url = "https://www.geocaching.com/seek/cache_details.aspx?wp=" + code
            html, actual_url = fetcher.get(sessions[premium], url, {"www.geocaching.com", "geocaching.com"})
            if looks_like_login_page(actual_url, html):
                raise AuthenticationError("geocaching_login_required")
            links = extract_description_links(html, actual_url, expected_gc=code)
            record = _description_record(descriptions.get(code), cache, utc_now(), links=links, source=actual_url)
            scanned += 1
        except (FetchError, ValueError) as exc:
            error = str(exc) if isinstance(exc, FetchError) else "description_parse_failed"
            record = _description_record(descriptions.get(code), cache, now, error=error)
            if logger:
                logger.warning("Description scan failed: %s (%s)", code, error)
            store.save_description(code, record)
            descriptions[code] = record
            if isinstance(exc, AuthenticationError):
                break
            continue
        store.save_description(code, record)
        descriptions[code] = record

    # Retain discovered links independently of the daily activity index.
    for code, description in descriptions.items():
        if code not in candidate_codes:
            continue
        for link in description.get("descriptionLinks", []):
            cid = link.get("checkerId") or link.get("sourceWaypoint")
            if cid and cid not in checkers and link.get("sourceUrl"):
                pending = {
                    "checkerId": cid, "sourceUrl": link["sourceUrl"], "cacheCode": None,
                    "mappingStatus": "pending", "mappingEvidence": link.get("evidence"),
                    "role": "unknown", "stageLabel": None, "answerType": "unknown", "matchingMode": "unknown",
                    "successCount": None, "failureCount": None, "counterObservedAt": None,
                    "publicSolvers": [], "solverListCoverage": "unknown", "solverListObservedAt": None,
                    "solverRowsRetrieved": 0, "solverReportedTotal": None, "publicSolverCount": None,
                    "publishedAt": None, "latestActivityAt": None, "sourceTimezone": None,
                    "crawl": {"status": "not_collected", "lastAttemptAt": None, "lastSuccessAt": None,
                              "errorCode": None, "fieldCoverage": "unknown", "stale": None},
                }
                store.save_checker(pending)
                checkers[cid] = pending
            if cid and not link.get("checkerId"):
                link["checkerId"] = cid
        store.save_description(code, description)
    activity_result = index_activity(store, fetcher, candidates, checkers, page_limit=activity_page_limit,
                                     logger=logger, session_factory=requests.Session)
    sources = {}
    for cid, record in checkers.items():
        if record.get("sourceUrl") and needs_detail_retry(record):
            sources.setdefault(record["sourceUrl"], set()).add(cid)
    for code, record in descriptions.items():
        if code not in candidate_codes:
            continue
        for link in record.get("descriptionLinks", []):
            cid = link.get("checkerId")
            needs_read = (not cid or needs_detail_retry(checkers.get(cid)) or
                          link.get("mappingStatus") == "pending" or bool(link.get("errorCode")))
            if link.get("sourceUrl") and needs_read:
                sources.setdefault(link["sourceUrl"], set()).update([link["checkerId"]] if link.get("checkerId") else [])
    source_session = requests.Session()
    source_session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; public-read research)"})
    fetched_ids = set()
    valid_reads = 0
    for source_url, known_ids in sorted(sources.items()):
        if known_ids and known_ids.issubset(fetched_ids):
            continue
        now = utc_now()
        fresh = None
        try:
            html, actual_url = fetcher.get(source_session, source_url, {"www.certitudes.org", "certitudes.org"})
            fresh = parse_checker_html(html, actual_url, utc_now())
            cid = fresh["checkerId"]
            if fresh.get("cacheCode") not in candidate_codes:
                fresh["mappingStatus"] = "pending"
            if known_ids and cid not in known_ids:
                raise FetchError("checker_identity_changed")
            if cid in fetched_ids:
                continue
            previous = checkers.get(cid)
            merged = merge_checker(previous, fresh, last_attempt_at=now,
                                   freshness_hours=float(os.getenv("CERTITUDE_FRESHNESS_HOURS", "48")))
            merged.get("crawl", {}).pop("errorResponse", None)
            activity = merged.setdefault("activity", {})
            complete_read = (fresh.get("counterObservedAt") is not None and
                             fresh.get("solverListCoverage") == "complete" and
                             fresh.get("solverListObservedAt") is not None)
            activity["pending"] = not complete_read
            if complete_read:
                activity["detailStartedAt"] = now
                activity["detailObservedAt"] = fresh["solverListObservedAt"]
            snapshot = None
            if fresh.get("counterObservedAt"):
                snapshot = {key: fresh.get(key) for key in ("checkerId", "successCount", "failureCount", "publicSolverCount", "solverListCoverage", "solverListObservedAt", "latestActivityAt")}
                snapshot["observedAt"] = fresh["counterObservedAt"]
            store.save_checker(merged, snapshot=snapshot)
            checkers[cid] = merged
            fetched_ids.add(cid)
            valid_reads += 1
            for code, description in descriptions.items():
                changed = False
                for link in description.get("descriptionLinks", []):
                    if link.get("sourceUrl") == source_url:
                        description_evidence = link.get("descriptionEvidence") or link.get("evidence")
                        link.setdefault("descriptionEvidence", description_evidence)
                        link.update(checkerId=cid, mappingStatus=(merged.get("mappingStatus") if merged.get("cacheCode") == code else "conflict"),
                                    mappingEvidence=merged.get("mappingEvidence"),
                                    evidence="; ".join(str(item) for item in (description_evidence, merged.get("mappingEvidence")) if item),
                                    lastAttemptAt=now, errorCode=None)
                        changed = True
                if changed:
                    store.save_description(code, description)
        except (FetchError, ValueError) as exc:
            error = str(exc) if isinstance(exc, FetchError) else "checker_parse_failed"
            if logger:
                parsed_source = urlparse(source_url)
                logger.warning("Checker refresh failed: ids=%r source=%s://%s%s error=%s response=%r",
                               sorted(known_ids), parsed_source.scheme, parsed_source.hostname,
                               parsed_source.path, error, getattr(exc, "metadata", {}))
            # Keep failed, previously discovered but unverified links visible in storage.
            for cid in known_ids:
                if cid in checkers:
                    failed = merge_checker(checkers[cid], None, last_attempt_at=now, error_code=error,
                                           freshness_hours=float(os.getenv("CERTITUDE_FRESHNESS_HOURS", "48")))
                    failed.setdefault("activity", {})["pending"] = True
                    if getattr(exc, "metadata", None):
                        failed.setdefault("crawl", {})["errorResponse"] = dict(exc.metadata)
                    store.save_checker(failed)
                    checkers[cid] = failed
            for code, description in descriptions.items():
                changed = False
                for link in description.get("descriptionLinks", []):
                    if link.get("sourceUrl") == source_url:
                        link.update(lastAttemptAt=now, errorCode=error)
                        changed = True
                if changed:
                    store.save_description(code, description)
    for session in sessions.values():
        session.close()
    source_session.close()
    if logger:
        logger.info("Certitude collection: candidates=%s descriptions_due=%s attempted=%s complete=%s index_pages=%s queued=%s skipped=%s partial_regions=%s checker_reads=%s retained=%s",
                    len(candidates), len(due), attempted, scanned, activity_result["pages"],
                    activity_result["queued"], activity_result["skipped"],
                    len(activity_result["partialRegions"]), valid_reads, len(checkers))
    if activity_result["failedRegions"]:
        raise RuntimeError("certitude_dashboard_scan_failed; previous scan boundaries retained")
    if candidates and not scanned and not checkers and due:
        raise RuntimeError("no_valid_certitude_discovery; previous publication retained")
    if sources and not valid_reads and not any(c.get("mappingStatus") == "verified" for c in checkers.values()):
        raise RuntimeError("no_valid_checker_observation; previous publication retained")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--description-limit", type=int, default=int(os.getenv("CERTITUDE_DESCRIPTION_LIMIT", "200")))
    parser.add_argument("--activity-page-limit", type=int, default=int(os.getenv("CERTITUDE_ACTIVITY_PAGE_LIMIT", "100")))
    args = parser.parse_args()
    refresh_days = float(os.getenv("CERTITUDE_DESCRIPTION_REFRESH_DAYS", "30"))
    if args.description_limit < 0 or refresh_days <= 0 or args.activity_page_limit < 1:
        parser.error("description limit must be nonnegative; refresh days and activity page limit must be positive")
    logger = setup_logging("crawl_certitude.log")
    connection = connect_postgres(require_env("DATABASE_URL"), logger=logger)
    try:
        collect(connection, description_limit=args.description_limit, refresh_days=refresh_days,
                activity_page_limit=args.activity_page_limit, logger=logger,
                fetcher=BoundedFetcher(delay=float(os.getenv("CERTITUDE_REQUEST_DELAY_SECONDS", "1.5")),
                                       retries=int(os.getenv("CERTITUDE_MAX_RETRIES", "3")),
                                       timeout=float(os.getenv("CERTITUDE_REQUEST_TIMEOUT_SECONDS", "30"))))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
