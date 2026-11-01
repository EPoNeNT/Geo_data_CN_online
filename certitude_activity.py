"""Durable, conservative indexing of activity from the public dashboard."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import requests
from urllib.parse import urlsplit, parse_qs

from certitude_source import parse_dashboard_html

BASE = "https://www.certitudes.org/"
ALLOWED_HOSTS = {"certitudes.org", "www.certitudes.org"}
REGIONS = ("china", "taiwan", "hong-kong", "macao")


def _time(value):
    if not isinstance(value, str): return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return d.astimezone(timezone.utc) if d.tzinfo else None
    except ValueError: return None


def _iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if value else None


def _http_date(value):
    try:
        d = parsedate_to_datetime(value)
        return d.astimezone(timezone.utc) if d and d.tzinfo else None
    except (TypeError, ValueError, OverflowError): return None


def activity_window(age_seconds, requested_at, observed_at, headers):
    req, obs = _time(requested_at), _time(observed_at)
    date = _http_date((headers or {}).get("Date") or (headers or {}).get("date"))
    duration = max(timedelta(0), obs - req) if req and obs else timedelta(0)
    lower = min([x for x in (req, date - duration - timedelta(seconds=1) if date else None) if x]) if req or date else None
    upper = max([x for x in (obs, date + timedelta(seconds=1) if date else None) if x]) if obs or date else None
    ref = _iso(lower)
    try:
        age = int(age_seconds)
        if isinstance(age_seconds, bool) or age < 0: raise ValueError()
    except (TypeError, ValueError): age = None
    start = lower - timedelta(seconds=age + 1) if lower and age is not None else None
    end = upper - timedelta(seconds=age) if upper and age is not None else None
    h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    expires = _http_date(h.get("expires"))
    cc = h.get("cache-control", "").lower()
    try: cached_age = int(h.get("age", "0"))
    except ValueError: cached_age = 1
    verified = bool(date and cached_age == 0 and (expires is not None and expires <= date or "no-cache" in cc or "no-store" in cc))
    return {"startAt": _iso(start), "endAt": _iso(end), "clockVerified": verified,
            "referenceStartAt": ref}


def _valid_pair(record):
    return (_time(record.get("counterObservedAt")) is not None and
            all(isinstance(record.get(k), int) and not isinstance(record.get(k), bool) and record[k] >= 0
                for k in ("successCount", "failureCount")))


def needs_detail_retry(record):
    """Only an actual complete detail read satisfies the pending refresh."""
    return (not record or record.get("crawl", {}).get("status") != "ok" or
            record.get("activity", {}).get("pending", False) or not _valid_pair(record) or
            record.get("solverListCoverage") != "complete" or
            _time(record.get("solverListObservedAt")) is None)


def should_refresh_checker(previous, row, window):
    if needs_detail_retry(previous): return True
    crawl = previous.get("crawl") or {}
    if not all(isinstance(row.get(k), int) and not isinstance(row.get(k), bool) and row[k] >= 0
               for k in ("successCount", "failureCount")): return True
    if (previous.get("successCount"), previous.get("failureCount")) != (row.get("successCount"), row.get("failureCount")): return True
    if window.get("clockVerified") is False: return True
    end, start = _time(window.get("endAt")), _time(window.get("startAt"))
    if not end or not start: return True
    detail = _time((previous.get("activity") or {}).get("detailStartedAt"))
    if detail is None and crawl.get("status") == "ok": detail = _time(crawl.get("lastAttemptAt"))
    return detail is None or end >= detail


def _safe_page_url(url):
    p = urlsplit(url)
    q = parse_qs(p.query, keep_blank_values=True)
    return (p.scheme == "https" and p.hostname in ("www.certitudes.org", "certitudes.org")
            and not p.username and not p.password and p.path == "/" and q.get("sort") == ["act"] and q.get("dir") == ["desc"]
            and bool(q.get("idx", [""])[0]))


def _now(): return datetime.now(timezone.utc)


def index_activity(store, fetcher, candidates, checkers, *, page_limit=100, logger=None, session_factory=requests.Session):
    """Persist pending detail reads before acknowledging each regional index page."""
    if page_limit < 1:
        raise ValueError("activity page limit must be positive")
    result = {"pages": 0, "queued": 0, "skipped": 0, "failedRegions": [], "partialRegions": []}
    if not candidates: return result
    cache_to_country = {}
    unknown = False
    for c in candidates:
        code = (c.get("code") or "").upper()
        country = (c.get("country") or "").casefold()
        region = next((r for r, names in {"china": ("china",), "taiwan": ("taiwan",), "hong-kong": ("hong kong", "hong-kong"), "macao": ("macao", "macau")}.items() if country in names), None)
        if region: cache_to_country[code] = region
        else: unknown = True
    active = list(REGIONS) if unknown else sorted(set(cache_to_country.values()))
    project_codes = {str(c["code"]).strip().upper() for c in candidates}
    scans = store.load_activity_scans()
    session = None
    user_agent = {"User-Agent": "Mozilla/5.0 (compatible; public-read research)"}

    def open_session():
        value = session_factory()
        value.headers.update(user_agent)
        return value

    def read_page(current_session, url, country, payload=None):
        requested = _now()
        if payload is None:
            html, actual = fetcher.get(current_session, url, ALLOWED_HOSTS)
        else:
            html, actual = fetcher.post(current_session, url, ALLOWED_HOSTS, payload)
        observed = _now()
        headers = dict(getattr(fetcher, "last_response_headers", {}) or {})
        page = parse_dashboard_html(html, actual, expected_country_id=country)
        if page.get("sort") != "act" or page.get("direction") != "desc":
            raise ValueError("dashboard activity order is not confirmed")
        filters = page.get("filters", {})
        if (str(page.get("selectedCountry")) != str(country) or
                any(filters.get(key) != "All" for key in ("filter_region", "filter_quadrangle")) or
                any(filters.get(key, "") for key in ("filter_owner", "filter_user"))):
            raise ValueError("dashboard country filter is not confirmed")
        result["pages"] += 1
        return page, requested, observed, headers

    try:
        session = open_session()
        html, actual = fetcher.get(session, BASE, ALLOWED_HOSTS)
        controls = parse_dashboard_html(html, actual)
        action = controls["activitySortUrl"]
        parsed_action = urlsplit(action)
        action_query = parse_qs(parsed_action.query)
        if (parsed_action.scheme != "https" or parsed_action.hostname not in ALLOWED_HOSTS or
                parsed_action.path != "/" or parsed_action.username or parsed_action.password or
                action_query.get("sort") != ["act"] or action_query.get("dir") != ["desc"] or
                "idx" in action_query):
            raise ValueError("dashboard activity filter action is invalid")
        session.close()
        session = None
        for region in active:
            scope = "regional:" + region
            old = deepcopy(scans.get(scope) or {})
            scan_start = _time(old.get("scanStartedAt")) if old.get("inProgress") else None
            cutoff = _time(old.get("indexCoveredThroughAt"))
            cursor = old.get("resumeUrl") if old.get("inProgress") else None
            region_pages_start = result["pages"]
            if logger:
                logger.info("Certitude dashboard region started: region=%s resumed=%s page_limit=%s",
                            region, bool(cursor), page_limit)
            state = deepcopy(old)
            clock_verified = bool(old.get("clockVerified", True)) if old.get("inProgress") else True
            all_known = bool(old.get("allTimesKnown", True)) if old.get("inProgress") else True
            session = open_session()
            visited = set()

            def save_state(*, next_url, status, completed=False, error=None):
                nonlocal state
                through = cutoff
                if completed and clock_verified and all_known:
                    through = max(value for value in (cutoff, scan_start) if value is not None)
                state = {"indexCoveredThroughAt": _iso(through), "inProgress": bool(next_url),
                         "scanStartedAt": _iso(scan_start), "resumeUrl": next_url,
                         "clockVerified": clock_verified, "allTimesKnown": all_known,
                         "lastAttemptAt": _iso(_now()), "status": status, "errorCode": error}
                store.save_activity_scan(scope, state)

            def process_page(bundle, previous_end=None):
                nonlocal scan_start, clock_verified, all_known
                page, requested, observed, headers = bundle
                clock = activity_window(0, _iso(requested), _iso(observed), headers)
                reference = _time(clock["referenceStartAt"])
                scan_start = min(value for value in (scan_start, reference) if value is not None)
                clock_verified = clock_verified and clock["clockVerified"]
                ends = []
                last_end = previous_end
                for row in page["rows"]:
                    window = activity_window(row.get("ageSeconds"), _iso(requested), _iso(observed), headers)
                    end = _time(window["endAt"])
                    if end is None:
                        all_known = False
                    else:
                        if last_end and end > last_end + timedelta(seconds=2):
                            raise ValueError("dashboard activity order changed during pagination")
                        ends.append(end)
                        last_end = end
                    parent = row.get("cacheCode")
                    if not parent or parent.upper() not in project_codes:
                        continue
                    cid = row["checkerId"]
                    previous = checkers.get(cid)
                    should = should_refresh_checker(previous, row, window)
                    record = deepcopy(previous) if previous else {
                        "checkerId": cid, "sourceUrl": row["sourceUrl"], "cacheCode": None,
                        "mappingStatus": "pending", "mappingEvidence": None, "role": "unknown",
                        "stageLabel": None, "answerType": "unknown", "matchingMode": "unknown",
                        "successCount": None, "failureCount": None, "counterObservedAt": None,
                        "publicSolvers": [], "solverListCoverage": "unknown", "solverListObservedAt": None,
                        "solverRowsRetrieved": 0, "solverReportedTotal": None, "publicSolverCount": None,
                        "publishedAt": None, "latestActivityAt": None, "sourceTimezone": None,
                        "crawl": {"status": "not_collected", "lastAttemptAt": None, "lastSuccessAt": None,
                                  "errorCode": None, "fieldCoverage": "unknown", "stale": None}}
                    activity = record.setdefault("activity", {})
                    activity.update(lastSeenAt=_iso(observed), ageSeconds=row.get("ageSeconds"),
                                    windowStartAt=window["startAt"], windowEndAt=window["endAt"], scope=scope,
                                    dashboardSourceUrl=row["sourceUrl"], expectedCacheCode=parent,
                                    pending=bool(activity.get("pending") or should))
                    store.save_checker(record)
                    checkers[cid] = record
                    result["queued" if should else "skipped"] += 1
                reached = bool(page["rows"] and cutoff and all_known and clock_verified and
                               len(ends) == len(page["rows"]) and max(ends) < cutoff)
                return last_end, reached

            try:
                if region not in controls.get("countries", {}):
                    raise ValueError("dashboard country option is unavailable")
                country = controls["countries"][region]
                data = {"filter_owner": "", "filter_user": "", "filter_region": "All", "filter_quadrangle": "All",
                        "filter_country": country}
                head = read_page(session, action, country, data)
                if cursor:
                    # A resumed scan still checks the head for newly arriving activity.
                    # This extra head does not consume the continuation page budget.
                    process_page(head)
                    if not _safe_page_url(cursor):
                        raise ValueError("invalid saved dashboard cursor")
                    save_state(next_url=cursor, status="partial")
                    bundle = read_page(session, cursor, country)
                else:
                    bundle = head
                count, previous_end = 0, None
                while True:
                    previous_end, reached = process_page(bundle, previous_end)
                    count += 1
                    next_url = bundle[0].get("nextUrl")
                    if logger:
                        logger.info("Certitude dashboard page processed: region=%s page=%s rows=%s queued=%s skipped=%s has_next=%s",
                                    region, count, len(bundle[0]["rows"]), result["queued"],
                                    result["skipped"], bool(next_url))
                    if next_url is None or reached:
                        complete = clock_verified and all_known
                        if not complete:
                            result["partialRegions"].append(region)
                        save_state(next_url=None, status="ok" if complete else "partial", completed=complete)
                        break
                    if not _safe_page_url(next_url) or next_url in visited:
                        raise ValueError("invalid or repeated dashboard continuation URL")
                    visited.add(next_url)
                    cursor = next_url
                    # Rows, including their pending flags, have already committed.
                    save_state(next_url=cursor, status="partial")
                    if count >= page_limit:
                        result["partialRegions"].append(region)
                        break
                    bundle = read_page(session, cursor, country)
            except (ValueError, RuntimeError, requests.RequestException) as exc:
                save_state(next_url=cursor, status="error", error=type(exc).__name__)
                result["failedRegions"].append(region)
                if logger:
                    logger.warning("Certitude dashboard scan failed: %s (%s)", region, type(exc).__name__)
            finally:
                if logger:
                    logger.info("Certitude dashboard region finished: region=%s status=%s pages=%s",
                                region, state.get("status", "error"), result["pages"] - region_pages_start)
                session.close()
                session = None
    except (ValueError, RuntimeError, requests.RequestException) as exc:
        for region in active:
            if region not in result["failedRegions"]:
                result["failedRegions"].append(region)
            old = deepcopy(scans.get("regional:" + region) or {})
            old.update(lastAttemptAt=_iso(_now()), status="error", errorCode=type(exc).__name__)
            store.save_activity_scan("regional:" + region, old)
    finally:
        if session is not None:
            session.close()
    return result
