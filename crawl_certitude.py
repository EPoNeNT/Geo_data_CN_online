"""Collect Certitude publication/activity lists and retain resumable data in Neon."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
import time
from urllib.parse import urljoin, urlparse

import requests

from certitude_source import parse_checker_html
from certitude_store import CertitudeStore, merge_checker
from certitude_activity import index_activity, needs_detail_retry
from certitude_discovery import index_publications
from runtime_utils import connect_postgres, require_env, setup_logging


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


class FetchError(RuntimeError):
    def __init__(self, code, *, metadata=None):
        super().__init__(code)
        self.metadata = dict(metadata or {})


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


def collect(connection, *, activity_page_limit=100, publication_page_limit=100, fetcher=None, logger=None):
    store = CertitudeStore(connection)
    store.ensure_schema()
    candidates = load_candidates(connection)
    checkers = store.load_checkers()
    candidate_codes = {row["code"] for row in candidates}
    fetcher = fetcher or BoundedFetcher()
    if logger:
        logger.info("Certitude publication discovery started: candidates=%s page_limit=%s",
                    len(candidates), publication_page_limit)
    publication_result = index_publications(store, fetcher, candidates, checkers, page_limit=publication_page_limit,
                                            logger=logger, session_factory=requests.Session)
    if logger:
        logger.info("Certitude index stage started: candidates=%s page_limit=%s", len(candidates), activity_page_limit)
    activity_result = index_activity(store, fetcher, candidates, checkers, page_limit=activity_page_limit,
                                     logger=logger, session_factory=requests.Session)
    sources = {}
    for cid, record in checkers.items():
        parent = record.get("cacheCode") or record.get("activity", {}).get("expectedCacheCode")
        if parent and parent not in candidate_codes:
            continue
        if record.get("sourceUrl") and needs_detail_retry(record):
            sources.setdefault(record["sourceUrl"], set()).add(cid)
    source_session = requests.Session()
    source_session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; public-read research)"})
    fetched_ids = set()
    valid_reads = 0
    if logger:
        logger.info("Certitude detail stage started: pending_unique_urls=%s retained_checkers=%s",
                    len(sources), len(checkers))
    detail_processed = detail_failed = detail_skipped = 0
    for source_url, known_ids in sorted(sources.items()):
        detail_processed += 1
        if known_ids and known_ids.issubset(fetched_ids):
            detail_skipped += 1
            if logger and (detail_processed % 25 == 0 or detail_processed == len(sources)):
                logger.info("Certitude detail progress: processed=%s successful=%s failed=%s skipped_aliases=%s",
                            detail_processed, valid_reads, detail_failed, detail_skipped)
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
                detail_skipped += 1
                if logger and (detail_processed % 25 == 0 or detail_processed == len(sources)):
                    logger.info("Certitude detail progress: processed=%s successful=%s failed=%s skipped_aliases=%s",
                                detail_processed, valid_reads, detail_failed, detail_skipped)
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
        except (FetchError, ValueError) as exc:
            detail_failed += 1
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
        if logger and (detail_processed % 25 == 0 or detail_processed == len(sources)):
            logger.info("Certitude detail progress: processed=%s successful=%s failed=%s skipped_aliases=%s",
                        detail_processed, valid_reads, detail_failed, detail_skipped)
    source_session.close()
    if logger:
        logger.info("Certitude collection: candidates=%s publication_pages=%s publication_queued=%s publication_partial_regions=%s index_pages=%s queued=%s skipped=%s partial_regions=%s checker_reads=%s retained=%s",
                    len(candidates), publication_result["pages"],
                    publication_result["queued"], len(publication_result["partialRegions"]), activity_result["pages"],
                    activity_result["queued"], activity_result["skipped"],
                    len(activity_result["partialRegions"]), valid_reads, len(checkers))
    if publication_result["failedRegions"] or activity_result["failedRegions"]:
        raise RuntimeError("certitude_dashboard_scan_failed; previous scan boundaries retained")
    if sources and not valid_reads and not any(c.get("mappingStatus") == "verified" for c in checkers.values()):
        raise RuntimeError("no_valid_checker_observation; previous publication retained")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activity-page-limit", type=int, default=int(os.getenv("CERTITUDE_ACTIVITY_PAGE_LIMIT", "100")))
    parser.add_argument("--publication-page-limit", type=int, default=int(os.getenv("CERTITUDE_PUBLICATION_PAGE_LIMIT", "100")))
    args = parser.parse_args()
    if args.activity_page_limit < 1 or args.publication_page_limit < 1:
        parser.error("page limits must be positive")
    logger = setup_logging("crawl_certitude.log")
    connection = connect_postgres(require_env("DATABASE_URL"), logger=logger)
    try:
        collect(connection, activity_page_limit=args.activity_page_limit,
                publication_page_limit=args.publication_page_limit, logger=logger,
                fetcher=BoundedFetcher(delay=float(os.getenv("CERTITUDE_REQUEST_DELAY_SECONDS", "1.5")),
                                       retries=int(os.getenv("CERTITUDE_MAX_RETRIES", "3")),
                                       timeout=float(os.getenv("CERTITUDE_REQUEST_TIMEOUT_SECONDS", "30"))))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
