"""Discover project Mystery checkers from regional publication lists."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from urllib.parse import parse_qs, urlsplit

import requests

from certitude_activity import ALLOWED_HOSTS, BASE, REGIONS, _iso, _now, _time, needs_detail_retry
from certitude_source import parse_dashboard_html


def _candidate_fingerprint(codes):
    return hashlib.sha256("\n".join(sorted(codes)).encode("utf-8")).hexdigest()


def _safe_url(url, *, continuation):
    try:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        return (parsed.scheme == "https" and parsed.hostname in ALLOWED_HOSTS
                and not parsed.username and not parsed.password and parsed.path == "/"
                and query.get("sort") == ["dat"] and query.get("dir") == ["desc"]
                and (len(query.get("idx", [])) == 1 and bool(query["idx"][0])
                     if continuation else "idx" not in query))
    except (TypeError, ValueError):
        return False


def _pending_checker(row):
    return {
        "checkerId": row["checkerId"], "sourceUrl": row["sourceUrl"], "cacheCode": None,
        "mappingStatus": "pending", "mappingEvidence": None, "role": "unknown",
        "successCount": None, "failureCount": None, "counterObservedAt": None,
        "publicSolvers": [], "solverListCoverage": "unknown", "solverListObservedAt": None,
        "solverRowsRetrieved": 0, "solverReportedTotal": None, "publishedAt": None,
        "latestActivityAt": None,
        "crawl": {"status": "not_collected", "lastAttemptAt": None, "lastSuccessAt": None,
                  "errorCode": None, "fieldCoverage": "unknown"},
    }


def index_publications(store, fetcher, candidates, checkers, *, page_limit=100, logger=None,
                       session_factory=requests.Session):
    """Save candidates before advancing a resumable scan; zero removes the page cap."""
    if page_limit < 0:
        raise ValueError("publication page limit must be nonnegative (0 means unlimited)")
    result = {"pages": 0, "queued": 0, "skipped": 0, "failedRegions": [], "partialRegions": []}
    project_codes = {str(row.get("code", "")).strip().upper() for row in candidates}
    candidate_fingerprint = _candidate_fingerprint(project_codes)
    scans = store.load_activity_scans()

    def open_session():
        session = session_factory()
        session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; public-read research)"})
        return session

    session = open_session()
    try:
        html, actual = fetcher.get(session, BASE, ALLOWED_HOSTS)
        controls = parse_dashboard_html(html, actual)
        action = controls.get("publicationSortUrl")
        if not _safe_url(action, continuation=False):
            raise ValueError("publication filter action is invalid")
    except (ValueError, RuntimeError, requests.RequestException) as exc:
        for region in REGIONS:
            result["failedRegions"].append(region)
            old = deepcopy(scans.get("published:" + region) or {})
            old.update(lastAttemptAt=_iso(_now()), status="error", errorCode=type(exc).__name__)
            store.save_activity_scan("published:" + region, old)
        if logger:
            logger.warning("Certitude publication controls failed (%s)", type(exc).__name__)
        return result
    finally:
        session.close()

    for region in REGIONS:
        scope = "published:" + region
        old = deepcopy(scans.get(scope) or {})
        same_candidates = old.get("candidateFingerprint") == candidate_fingerprint
        resuming = bool(old.get("inProgress")) and same_candidates
        cursor = old.get("resumeUrl") if resuming else None
        frozen_head = _time(old.get("scanNewestPublishedAt")) if resuming else None
        # A newly imported Mystery can have an old Certitude publication date.
        # Rebuild discovery when the local GC set changes, rather than skipping it.
        cutoff = _time(old.get("publicationCoveredThroughAt")) if same_candidates else None
        all_known = bool(old.get("allTimesKnown", True)) if resuming else True
        state = deepcopy(old)
        region_pages_start = result["pages"]
        session = open_session()
        if logger:
            logger.info("Certitude publication region started: region=%s resumed=%s page_limit=%s",
                        region, resuming, page_limit)

        def save(next_url, status, *, completed=False, error=None):
            through = cutoff
            if completed and frozen_head is not None:
                through = max(value for value in (cutoff, frozen_head) if value is not None)
            state.update(resumeUrl=next_url, inProgress=bool(next_url), status=status,
                         candidateFingerprint=candidate_fingerprint,
                         lastAttemptAt=_iso(_now()), errorCode=error, allTimesKnown=all_known,
                         scanNewestPublishedAt=_iso(frozen_head), publicationCoveredThroughAt=_iso(through))
            store.save_activity_scan(scope, deepcopy(state))

        def read(url, country, payload=None):
            if payload is None:
                body, landed = fetcher.get(session, url, ALLOWED_HOSTS)
            else:
                body, landed = fetcher.post(session, url, ALLOWED_HOSTS, payload)
            page = parse_dashboard_html(body, landed, expected_country_id=country)
            filters = page.get("filters", {})
            if page.get("sort") != "dat" or page.get("direction") != "desc":
                raise ValueError("publication order is not confirmed")
            if (str(page.get("selectedCountry")) != str(country)
                    or any(filters.get(key) != "All" for key in ("filter_region", "filter_quadrangle"))
                    or any(filters.get(key, "") for key in ("filter_owner", "filter_user"))):
                raise ValueError("publication filter is not confirmed")
            result["pages"] += 1
            return page

        def process(page, previous_time=None):
            nonlocal all_known
            times = [_time(row.get("publishedAt")) for row in page["rows"]]
            all_known = all_known and all(value is not None for value in times)
            valid_times = [value for value in times if value is not None]
            for value in valid_times:
                if previous_time is not None and value > previous_time:
                    raise ValueError("publication order changed during pagination")
                previous_time = value
            for row in page["rows"]:
                parent = row.get("cacheCode")
                if not parent or parent.upper() not in project_codes:
                    continue
                cid = row["checkerId"]
                previous = checkers.get(cid)
                retry = needs_detail_retry(previous)
                record = deepcopy(previous) if previous else _pending_checker(row)
                activity = record.setdefault("activity", {})
                activity.update(pending=bool(activity.get("pending") or retry), expectedCacheCode=parent)
                record.setdefault("discovery", {}).update(
                    source="publication_list", sourceUrl=row["sourceUrl"],
                    dashboardCacheCode=parent, publishedAt=row.get("publishedAt"), region=region)
                if record != previous:
                    store.save_checker(record)
                checkers[cid] = record
                result["queued" if retry else "skipped"] += 1
            reached = bool(times and cutoff and all_known and max(valid_times) < cutoff)
            return previous_time, reached

        try:
            country = controls.get("countries", {}).get(region)
            if country is None:
                raise ValueError("country option unavailable")
            payload = {"filter_owner": "", "filter_user": "", "filter_region": "All",
                       "filter_quadrangle": "All", "filter_country": country}
            head = read(action, country, payload)
            if not resuming:
                frozen_head = next((_time(row.get("publishedAt")) for row in head["rows"]
                                    if _time(row.get("publishedAt")) is not None), None)
            if resuming:
                if not _safe_url(cursor, continuation=True):
                    raise ValueError("invalid saved publication cursor")
                # Freeze the original boundary: a later pass covers publications
                # inserted between this head and the saved continuation.
                process(head)
                save(cursor, "partial")
                page = read(cursor, country)
            else:
                page = head
            visited = {cursor} if cursor else set()
            previous_time = None
            count = 0
            while True:
                previous_time, reached = process(page, previous_time)
                count += 1
                next_url = page.get("nextUrl")
                if logger:
                    logger.info("Certitude publication page processed: region=%s page=%s rows=%s queued=%s skipped=%s has_next=%s",
                                region, count, len(page["rows"]), result["queued"], result["skipped"], bool(next_url))
                if next_url is None or reached:
                    if not all_known:
                        result["partialRegions"].append(region)
                    save(None, "ok" if all_known else "partial", completed=all_known)
                    break
                if not _safe_url(next_url, continuation=True) or next_url in visited:
                    raise ValueError("invalid or repeated publication cursor")
                visited.add(next_url)
                cursor = next_url
                save(cursor, "partial")
                if page_limit and count >= page_limit:
                    result["partialRegions"].append(region)
                    break
                page = read(cursor, country)
        except (ValueError, RuntimeError, requests.RequestException) as exc:
            save(cursor, "error", error=type(exc).__name__)
            result["failedRegions"].append(region)
            if logger:
                logger.warning("Certitude publication scan failed: %s (%s)", region, type(exc).__name__)
        finally:
            if logger:
                logger.info("Certitude publication region finished: region=%s status=%s pages=%s",
                            region, state.get("status", "error"), result["pages"] - region_pages_start)
            session.close()
    return result
