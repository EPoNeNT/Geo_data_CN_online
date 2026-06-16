#!/usr/bin/env python3
"""Fetch Geocaching registration dates for log users missing from the user table."""

import argparse
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import quote

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import psycopg2
from psycopg2.extras import RealDictCursor, execute_batch
import requests

from runtime_utils import (
    connect_postgres,
    looks_like_login_page,
    minimize_cookie_value,
    require_env,
    setup_logging,
)


logger = setup_logging("crawl_user_regdates.log")

DATABASE_URL = require_env("DATABASE_URL")
PROFILE_URL = "https://www.geocaching.com/p/default.aspx"
DEFAULT_TIMEOUT_SECONDS = int(os.getenv("USER_REGDATE_TIMEOUT_SECONDS", "45"))
STREAM_MAX_BYTES = int(os.getenv("USER_REGDATE_STREAM_MAX_BYTES", "204800"))  # 200KB

# nearest.aspx 对美国 cache 只显示州名，需映射为 United States
_US_STATES = frozenset({
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
    "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
    "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming",
})

DEFAULT_MAX_RETRIES = int(os.getenv("USER_REGDATE_MAX_RETRIES", "3"))
DEFAULT_DELAY_SECONDS = float(os.getenv("USER_REGDATE_DELAY_SECONDS", "1.6"))
DEFAULT_BATCH_SIZE = int(os.getenv("USER_REGDATE_BATCH_SIZE", "50"))
DEFAULT_LIMIT = os.getenv("USER_REGDATE_LIMIT", "500")
MIN_LOG_COUNT_FOR_REGDATE_FETCH = 10
MAX_LOGIN_REQUIRED_BEFORE_STOP = int(os.getenv("USER_REGDATE_MAX_LOGIN_REQUIRED", "20"))
AUTH_CHECK_USER = os.getenv("USER_REGDATE_AUTH_CHECK_USER", "asakosachben")
DEBUG_NOT_FOUND_HTML = os.getenv("USER_REGDATE_DEBUG_NOT_FOUND_HTML", "").lower() in {"1", "true", "yes"}
DEBUG_HTML_DIR = Path(os.getenv("USER_REGDATE_DEBUG_HTML_DIR", "test/regdate_debug_pages"))

JOINED_PATTERNS = (
    re.compile(
        r'<span\b[^>]*\bid=["\']ctl00_ProfileHead_ProfileHeader_lblMemberSinceDate["\'][^>]*>\s*Joined\s*(.*?)\s*</span>',
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r'<span\b[^>]*\bid=["\'][^"\']*lblMemberSinceDate["\'][^>]*>\s*Joined\s*(.*?)\s*</span>',
        re.IGNORECASE | re.DOTALL,
    ),
)

PROFILE_MEMBER_PATTERN = re.compile(
    r'<span\b[^>]*\bid=["\']ctl00_ProfileHead_ProfileHeader_lblMemberName["\'][^>]*>(.*?)</span>',
    re.IGNORECASE | re.DOTALL,
)
TITLE_PATTERN = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
MEMBER_SINCE_CONTEXT_PATTERN = re.compile(
    r".{0,120}lblMemberSinceDate.{0,180}",
    re.IGNORECASE | re.DOTALL,
)
JOINED_CONTEXT_PATTERN = re.compile(
    r".{0,120}(lblMemberSinceDate|Joined|Member Since|memberSince|dateJoined).{0,180}",
    re.IGNORECASE | re.DOTALL,
)

DATE_FORMATS = (
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%Y-%m-%d",
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%m.%d.%Y",
    "%d.%m.%Y",
    "%Y. %m. %d.",
    "%Y.%m.%d.",
    "%Y. %m. %d",
    "%Y.%m.%d",
)


def parse_optional_int(value: Optional[str]) -> Optional[int]:
    """Parse an optional positive integer from env or CLI defaults.
    0 or negative means no limit (returns None)."""
    if value is None or not str(value).strip():
        return None

    parsed = int(value)
    if parsed <= 0:
        return None
    return parsed


def normalize_cookie_value(value: str) -> str:
    """Normalize cookie env values from .env files and GitHub Secrets."""
    return minimize_cookie_value(value)


def cookie_candidates() -> list[tuple[str, str]]:
    """Return configured cookie candidates in priority order."""
    candidates = []
    seen_values = set()
    for key in (
        "REG-COOKIE",
        "REG_COOKIE",
        "GEOCOOKIE_NONPREMIUM",
        "GEOCOOKIE_PREMIUM",
    ):
        value = normalize_cookie_value(os.getenv(key) or "")
        if not value or value in seen_values:
            continue
        candidates.append((key, value))
        seen_values.add(value)
    return candidates


def build_session(cookie: Optional[str] = None) -> requests.Session:
    """Create a requests session for Geocaching profile pages."""
    session = requests.Session()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    if cookie is None:
        candidates = cookie_candidates()
        cookie = candidates[0][1] if candidates else None
    if cookie:
        headers["Cookie"] = cookie

    session.headers.update(headers)
    return session


def session_has_profile_access(session: requests.Session, user_name: str, timeout_seconds: int) -> bool:
    """Return True when a session can read a known user's joined date."""
    url = f"{PROFILE_URL}?u={quote(user_name)}"
    response = session.get(url, timeout=timeout_seconds)
    if response.status_code != 200:
        logger.warning("Auth check returned HTTP %s for %s", response.status_code, user_name)
        return False
    if looks_like_login_page(response.url, response.text):
        logger.warning("Auth check returned a login page for %s", user_name)
        return False
    parsed_date, raw_date = parse_registration_date(response.text)
    if parsed_date:
        return True

    summary = summarize_profile_response(response.text)
    logger.warning(
        "Auth check could not read joined date for %s: final_url=%s length=%s title=%r markers=%s context=%r raw=%r",
        user_name,
        response.url,
        len(response.text or ""),
        summary["title"],
        summary["markers"],
        summary["joined_context"],
        raw_date,
    )
    return False


def build_authenticated_session(timeout_seconds: int) -> Optional[requests.Session]:
    """Pick the first configured cookie that can read profile joined dates."""
    candidates = cookie_candidates()
    if not candidates:
        logger.error("No Geocaching cookie env var is configured for registration-date crawling")
        return None

    for key, cookie in candidates:
        session = build_session(cookie)
        try:
            logger.info("Checking %s for registration-date profile access", key)
            if session_has_profile_access(session, AUTH_CHECK_USER, timeout_seconds):
                logger.info("Using %s for registration-date crawling", key)
                return session
            logger.warning("%s failed registration-date auth check", key)
        except requests.RequestException as exc:
            logger.warning("%s auth check request failed: %s", key, exc)
        session.close()

    logger.error("No configured Geocaching cookie can read profile joined dates; stopping this run")
    return None


def clean_html_text(value: str) -> str:
    """Collapse HTML-ish text into one log-safe line."""
    return re.sub(r"\s+", " ", value or "").strip()


def safe_debug_filename(user_name: str) -> str:
    """Build a filesystem-safe debug filename for a profile response."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", user_name).strip("_") or "user"


def summarize_profile_response(html: str) -> dict:
    """Return a concise fingerprint for unexpected profile responses."""
    text = html or ""
    title_match = TITLE_PATTERN.search(text)
    member_match = PROFILE_MEMBER_PATTERN.search(text)
    context_match = MEMBER_SINCE_CONTEXT_PATTERN.search(text) or JOINED_CONTEXT_PATTERN.search(text)
    markers = [
        marker
        for marker in (
            "lblMemberSinceDate",
            "ctl00_ProfileHead_ProfileHeader_lblMemberName",
            "Joined",
            "dateJoined",
            "UserProfile",
            "This user's profile is private",
            "Sorry, the requested user profile was not found",
            "Page Not Found",
        )
        if marker in text
    ]
    return {
        "title": clean_html_text(title_match.group(1)) if title_match else "",
        "profile_name": clean_html_text(member_match.group(1)) if member_match else "",
        "joined_context": clean_html_text(context_match.group(0)) if context_match else "",
        "markers": ",".join(markers),
        "looks_like_profile": bool(member_match) or "UserProfile" in text,
    }


def maybe_save_debug_html(user_name: str, html: str) -> None:
    """Optionally save unexpected profile HTML for action artifact debugging."""
    if not DEBUG_NOT_FOUND_HTML:
        return

    DEBUG_HTML_DIR.mkdir(parents=True, exist_ok=True)
    path = DEBUG_HTML_DIR / f"{safe_debug_filename(user_name)}.html"
    path.write_text(html or "", encoding="utf-8", errors="replace")
    logger.warning("Saved %s profile debug HTML to %s", user_name, path)


def parse_registration_date(html: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (parsed ISO date, raw date text) from a profile page."""
    raw_value = None
    for pattern in JOINED_PATTERNS:
        match = pattern.search(html or "")
        if match:
            raw_value = re.sub(r"\s+", " ", match.group(1)).strip()
            break

    if not raw_value:
        return None, None

    cleaned = re.sub(r"\s+", " ", raw_value.replace(",", ", ")).strip()
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, date_format).date().isoformat(), raw_value
        except ValueError:
            continue

    return None, raw_value


def _stream_profile_html(session: requests.Session, url: str, timeout_seconds: int) -> Tuple[str, requests.Response]:
    """Stream the first STREAM_MAX_BYTES of a profile page and close the connection.

    The registration date and avatar live in the page header (~10 KB).
    Reading only the first chunk avoids downloading multi-MB About sections.
    """
    response = session.get(url, timeout=timeout_seconds, stream=True)
    accumulated = b""
    try:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                accumulated += chunk
                if len(accumulated) >= STREAM_MAX_BYTES:
                    break
    finally:
        response.close()
    return accumulated.decode("utf-8", errors="replace"), response


def fetch_registration_date(
    session: requests.Session,
    user_name: str,
    max_retries: int,
    timeout_seconds: int,
) -> Tuple[Optional[str], Optional[str], str]:
    """Fetch one user's registration date with per-user retries."""
    url = f"{PROFILE_URL}?u={quote(user_name)}"
    last_status = "request_failed"

    for attempt in range(1, max_retries + 1):
        response = None
        try:
            logger.info("Fetching %s attempt %s/%s", user_name, attempt, max_retries)
            text, response = _stream_profile_html(session, url, timeout_seconds)

            if response.status_code != 200:
                last_status = f"http_{response.status_code}"
                logger.warning("%s returned %s", user_name, last_status)
                continue

            if looks_like_login_page(response.url, text):
                logger.warning("%s returned a login page", user_name)
                return None, None, "login_required"

            parsed_date, raw_date = parse_registration_date(text)
            if parsed_date:
                return parsed_date, raw_date, "ok"
            if raw_date:
                return None, raw_date, "parse_failed"

            summary = summarize_profile_response(text)
            logger.warning(
                "%s registration date not found: final_url=%s length=%s title=%r profile_name=%r markers=%s context=%r",
                user_name,
                response.url,
                len(text),
                summary["title"],
                summary["profile_name"],
                summary["markers"],
                summary["joined_context"],
            )
            maybe_save_debug_html(user_name, text)
            if summary["looks_like_profile"] or "lblMemberSinceDate" in summary["markers"]:
                return None, None, "parse_failed"
            return None, None, "not_found"

        except requests.RequestException as exc:
            last_status = "request_failed"
            logger.warning("%s request failed on attempt %s/%s: %s", user_name, attempt, max_retries, exc)

    return None, None, last_status


def fetch_first_find_country(
    session: requests.Session,
    user_name: str,
    timeout_seconds: int,
) -> str | None:
    """Return the country of a user's first found cache, or None.

    Uses the old-style nearest.aspx page which works with any valid cookie
    (no Premium required).  Results are sorted by found date ascending.
    """
    # Geocaching 对查询参数做双重解码，+ 号需双重编码（%2B → %252B）
    url = (
        "https://www.geocaching.com/seek/nearest.aspx"
        f"?ul={quote(user_name).replace('%2B', '%252B')}"
        f"&sort=lastfound&sortdir=asc"
    )

    try:
        html, response = _stream_profile_html(session, url, timeout_seconds)

        if "signin" in response.url.lower() or "login" in response.url.lower():
            logger.warning("%s first-find: redirected to login", user_name)
            return None

        if response.status_code != 200:
            logger.warning("%s first-find: HTTP %s", user_name, response.status_code)
            return None

        # 检查用户是否存在 / finds 是否私密
        html_lower = html.lower()
        if "does not exist" in html_lower:
            logger.info("%s first-find: user does not exist", user_name)
            return None
        if "this content is private" in html_lower:
            logger.info("%s first-find: finds are private", user_name)
            return None

        # nearest.aspx renders cache info in <span class="small"> elements:
        #   by OwnerName | GCXXXXX | CountryName
        for span_match in re.finditer(
            r'<span\s+class="small">(.*?)</span>', html, re.DOTALL
        ):
            text = re.sub(r"<[^>]+>", "", span_match.group(1)).strip()
            parts = [p.strip() for p in text.split("|")]
            if len(parts) >= 3 and re.match(r"GC[A-Z0-9]+$", parts[1]):
                country = parts[2].rsplit(",", 1)[-1].strip()
                if country in _US_STATES:
                    country = "United States"
                if country:
                    logger.info(
                        "%s first-find: %s (%s)", user_name, parts[1], country
                    )
                return country or None

        # No matching cache row found (user may have 0 finds or profile is private)
        logger.info("%s first-find: no cache rows found", user_name)
        return None

    except requests.RequestException as exc:
        logger.warning("%s first-find request failed: %s", user_name, exc)
        return None


def connect_db():
    """Connect to Neon."""
    return connect_postgres(
        DATABASE_URL,
        logger=logger,
        connect_timeout=10,
        cursor_factory=RealDictCursor,
    )


def ensure_user_table(conn) -> None:
    """Create or update the quoted user table used by this crawler."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS "user" (
              user_name TEXT PRIMARY KEY,
              registration_date DATE,
              fetch_status TEXT NOT NULL DEFAULT 'pending'
            );
            """
        )
        cur.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS registration_date DATE;')
        cur.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS fetch_status TEXT NOT NULL DEFAULT \'pending\';')
        cur.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS guid TEXT;')
        cur.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS reg_place TEXT;')
        cur.execute(
            'ALTER TABLE "user" '
            'DROP COLUMN IF EXISTS registration_date_raw, '
            'DROP COLUMN IF EXISTS checked_at, '
            'DROP COLUMN IF EXISTS created_at, '
            'DROP COLUMN IF EXISTS updated_at;'
        )
        cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS user_user_name_unique_idx ON "user" (user_name);')
        cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS user_guid_unique_idx ON "user" (guid);')
    conn.commit()


def get_users_to_fetch(conn, include_non_ok: bool, limit: Optional[int], min_logs: int = MIN_LOG_COUNT_FOR_REGDATE_FETCH) -> tuple[list[dict], list[dict]]:
    """通过 GUID 聚合 caches 和 logs 中的用户。返回 (待抓取列表, 冲突列表)。

    待抓取: [{"guid": ..., "user_name": ..., "log_count": ...}, ...]
    冲突:   [{"guid": ..., "usernames": [...], "count": ...}, ...]
    冲突指同一 GUID 对应多个不同用户名（中间改过名），暂不处理。
    """
    if include_non_ok:
        user_filter = "(u.user_name IS NULL OR COALESCE(u.fetch_status, '') <> 'ok')"
    else:
        user_filter = "u.user_name IS NULL"

    limit_clause = "LIMIT %s" if limit is not None else ""
    params = [min_logs]
    if limit is not None:
        params.append(limit)

    # Step 1: 按 GUID 聚合所有用户数据
    query = f"""
    WITH log_users AS (
      SELECT
        l.user_guid AS guid,
        MAX(TRIM(l.user_name)) AS user_name,
        COUNT(*)::int AS log_count
      FROM logs l
      WHERE l.user_guid IS NOT NULL
        AND TRIM(l.user_name) IS NOT NULL
        AND TRIM(l.user_name) <> ''
      GROUP BY l.user_guid
      HAVING COUNT(*) > %s
    ),
    owner_users AS (
      SELECT
        c.owner_guid AS guid,
        MAX(TRIM(c.owner_username)) AS user_name,
        0::int AS log_count
      FROM caches c
      WHERE c.owner_guid IS NOT NULL
        AND c.owner_guid <> 'no guid'
        AND c.owner_username IS NOT NULL
        AND c.owner_username <> '[DELETED_USER]'
        AND TRIM(c.owner_username) <> ''
        AND COALESCE(c.cache_status, 0) != 404
      GROUP BY c.owner_guid
    ),
    candidates AS (
      SELECT guid, user_name, log_count FROM log_users
      UNION
      SELECT guid, user_name, log_count FROM owner_users
      WHERE guid NOT IN (SELECT guid FROM log_users)
    )
    SELECT candidates.guid, candidates.user_name, candidates.log_count
    FROM candidates
    LEFT JOIN "user" u ON u.guid = candidates.guid
    WHERE {user_filter}
    ORDER BY candidates.log_count DESC, candidates.user_name
    {limit_clause};
    """

    with conn.cursor() as cur:
        cur.execute(query, params)
        all_rows = [dict(row) for row in cur.fetchall()]

    if not all_rows:
        return [], []

    # Step 2: 检测 GUID 是否对应多个不同用户名（改名冲突）
    guids = [r["guid"] for r in all_rows]
    if not guids:
        return [], []

    with conn.cursor() as cur:
        cur.execute(
            """
            WITH guid_usernames AS (
              SELECT l.user_guid AS guid, TRIM(l.user_name) AS user_name
              FROM logs l
              WHERE l.user_guid = ANY(%s)
                AND l.user_guid IS NOT NULL
                AND TRIM(l.user_name) IS NOT NULL
                AND TRIM(l.user_name) <> ''
              UNION
              SELECT c.owner_guid AS guid, TRIM(c.owner_username) AS user_name
              FROM caches c
              WHERE c.owner_guid = ANY(%s)
                AND c.owner_guid IS NOT NULL
                AND c.owner_username IS NOT NULL
                AND TRIM(c.owner_username) <> ''
            )
            SELECT guid, ARRAY_AGG(DISTINCT user_name ORDER BY user_name) AS usernames
            FROM guid_usernames
            GROUP BY guid
            HAVING COUNT(DISTINCT user_name) > 1
            """,
            (guids, guids),
        )
        conflict_map = {row["guid"]: row["usernames"] for row in cur.fetchall()}

    to_fetch = []
    conflicts = []
    for row in all_rows:
        if row["guid"] in conflict_map:
            conflicts.append({
                "guid": row["guid"],
                "usernames": conflict_map[row["guid"]],
                "count": len(conflict_map[row["guid"]]),
            })
        else:
            to_fetch.append(row)

    return to_fetch, conflicts


def get_distinct_log_user_count(conn, min_logs: int = MIN_LOG_COUNT_FOR_REGDATE_FETCH) -> int:
    """通过 GUID 统计唯一候选用户数。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH log_users AS (
              SELECT l.user_guid AS guid
              FROM logs l
              WHERE l.user_guid IS NOT NULL
                AND TRIM(l.user_name) IS NOT NULL
                AND TRIM(l.user_name) <> ''
              GROUP BY l.user_guid
              HAVING COUNT(*) > %s
            ),
            owner_users AS (
              SELECT c.owner_guid AS guid
              FROM caches c
              WHERE c.owner_guid IS NOT NULL
                AND c.owner_guid <> 'no guid'
                AND c.owner_username IS NOT NULL
                AND c.owner_username <> '[DELETED_USER]'
                AND TRIM(c.owner_username) <> ''
                AND COALESCE(c.cache_status, 0) != 404
              GROUP BY c.owner_guid
            )
            SELECT COUNT(*)::int AS count
            FROM (
              SELECT guid FROM log_users
              UNION
              SELECT guid FROM owner_users
            ) sub;
            """,
            (min_logs,),
        )
        return cur.fetchone()["count"] or 0


def upsert_results(conn, results: list[dict]) -> None:
    """Write a batch of user registration results (keyed by user_name)."""
    if not results:
        return

    # 按 user_name 去重（同批次内同一用户名只保留最后一条）
    deduped: dict[str, dict] = {}
    for r in results:
        deduped[r["user_name"]] = r
    results = list(deduped.values())

    query = """
    INSERT INTO "user" (user_name, guid, registration_date, fetch_status, reg_place)
    VALUES (%(user_name)s, %(guid)s, %(registration_date)s, %(fetch_status)s, %(reg_place)s)
    ON CONFLICT (user_name) DO UPDATE
    SET guid = COALESCE("user".guid, EXCLUDED.guid),
        registration_date = COALESCE(EXCLUDED.registration_date, "user".registration_date),
        fetch_status = EXCLUDED.fetch_status,
        reg_place = COALESCE(EXCLUDED.reg_place, "user".reg_place);
    """
    with conn.cursor() as cur:
        execute_batch(cur, query, results)
    conn.commit()


def parse_args():
    parser = argparse.ArgumentParser(description="Fetch registration dates for logs users not ready in the user table.")
    parser.add_argument("--limit", type=lambda v: None if int(v) <= 0 else int(v), default=parse_optional_int(DEFAULT_LIMIT), help="Optional maximum number of users to process. 0 or negative = no limit.")
    parser.add_argument("--missing-only", action="store_true", help="Only fetch users absent from the user table.")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES, help="Per-user request retry count.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Per-request timeout in seconds.")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS, help="Delay between users in seconds.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Database write batch size.")
    parser.add_argument("--min-logs", type=int, default=MIN_LOG_COUNT_FOR_REGDATE_FETCH, help=f"Minimum log count to include (default: {MIN_LOG_COUNT_FOR_REGDATE_FETCH}). 0 = all users.")
    parser.add_argument("--dry-run", action="store_true", help="Only count distinct logs users; do not create table, crawl, or write.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    conn = connect_db()
    pending_results = []
    login_required_count = 0
    all_conflicts: list[dict] = []

    try:
        if args.dry_run:
            total_users = get_distinct_log_user_count(conn, min_logs=args.min_logs)
            logger.info(
                "Candidate users from logs with more than %s logs or cache owners: %s",
                args.min_logs,
                total_users,
            )
            return

        ensure_user_table(conn)
        to_fetch, conflicts = get_users_to_fetch(
            conn,
            include_non_ok=not args.missing_only,
            limit=args.limit,
            min_logs=args.min_logs,
        )
        all_conflicts = conflicts
        logger.info("Users to fetch: %s (skipped %s with username conflicts)", len(to_fetch), len(conflicts))

        session = build_authenticated_session(timeout_seconds=max(1, args.timeout))
        if session is None:
            logger.error("User registration date crawl skipped because profile access is unavailable")
            return

        with session:
            for index, row in enumerate(to_fetch, start=1):
                guid = row["guid"]
                user_name = row["user_name"]
                started_at = time.monotonic()
                registration_date, raw_date, status = fetch_registration_date(
                    session,
                    user_name,
                    max_retries=max(1, args.max_retries),
                    timeout_seconds=max(1, args.timeout),
                )

                reg_place: str | None = None
                if status == "ok":
                    reg_place = fetch_first_find_country(
                        session, user_name, timeout_seconds=max(1, args.timeout)
                    )

                elapsed = time.monotonic() - started_at

                logger.info(
                    "[%s/%s] %s guid=%s status=%s registration_date=%s raw=%s reg_place=%s elapsed=%.1fs",
                    index,
                    len(to_fetch),
                    user_name,
                    guid,
                    status,
                    registration_date,
                    raw_date,
                    reg_place,
                    elapsed,
                )

                if status == "login_required":
                    login_required_count += 1

                pending_results.append(
                    {
                        "guid": guid,
                        "user_name": user_name,
                        "registration_date": registration_date,
                        "fetch_status": status,
                        "reg_place": reg_place,
                    }
                )

                if login_required_count > MAX_LOGIN_REQUIRED_BEFORE_STOP:
                    logger.error(
                        "Stopping user registration crawl after %s login_required responses. "
                        "Authentication may be lost; remaining users will be retried next run.",
                        login_required_count,
                    )
                    break

                if len(pending_results) >= max(1, args.batch_size):
                    upsert_results(conn, pending_results)
                    logger.info("Wrote %s results", len(pending_results))
                    pending_results = []

                if index < len(to_fetch) and args.delay > 0:
                    time.sleep(args.delay)

        if pending_results:
            upsert_results(conn, pending_results)
            logger.info("Wrote final %s results", len(pending_results))

        # 输出冲突报告
        if all_conflicts:
            logger.info("=" * 60)
            logger.info("GUID 对应多个用户名的冲突（跳过，可能是改名）: %s 个", len(all_conflicts))
            for c in all_conflicts:
                logger.info("  guid=%s usernames=%s", c["guid"], c["usernames"])

        logger.info("User registration date crawl complete")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
