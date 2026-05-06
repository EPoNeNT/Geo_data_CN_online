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
DEFAULT_TIMEOUT_SECONDS = int(os.getenv("USER_REGDATE_TIMEOUT_SECONDS", "15"))
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
    """Parse an optional positive integer from env or CLI defaults."""
    if value is None or not str(value).strip():
        return None

    parsed = int(value)
    if parsed <= 0:
        raise ValueError("limit must be a positive integer")
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
        try:
            logger.info("Fetching %s attempt %s/%s", user_name, attempt, max_retries)
            response = session.get(url, timeout=timeout_seconds)

            if response.status_code != 200:
                last_status = f"http_{response.status_code}"
                logger.warning("%s returned %s", user_name, last_status)
                continue

            if looks_like_login_page(response.url, response.text):
                logger.warning("%s returned a login page", user_name)
                return None, None, "login_required"

            parsed_date, raw_date = parse_registration_date(response.text)
            if parsed_date:
                return parsed_date, raw_date, "ok"
            if raw_date:
                return None, raw_date, "parse_failed"

            summary = summarize_profile_response(response.text)
            logger.warning(
                "%s registration date not found: final_url=%s length=%s title=%r profile_name=%r markers=%s context=%r",
                user_name,
                response.url,
                len(response.text or ""),
                summary["title"],
                summary["profile_name"],
                summary["markers"],
                summary["joined_context"],
            )
            maybe_save_debug_html(user_name, response.text)
            if summary["looks_like_profile"] or "lblMemberSinceDate" in summary["markers"]:
                return None, None, "parse_failed"
            return None, None, "not_found"

        except requests.RequestException as exc:
            last_status = "request_failed"
            logger.warning("%s request failed on attempt %s/%s: %s", user_name, attempt, max_retries, exc)

    return None, None, last_status


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
        cur.execute(
            'ALTER TABLE "user" '
            'DROP COLUMN IF EXISTS registration_date_raw, '
            'DROP COLUMN IF EXISTS checked_at, '
            'DROP COLUMN IF EXISTS created_at, '
            'DROP COLUMN IF EXISTS updated_at;'
        )
        cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS user_user_name_unique_idx ON "user" (user_name);')
    conn.commit()


def get_usernames_to_fetch(conn, include_non_ok: bool, limit: Optional[int]) -> list[str]:
    """Read active log users and all cache owners that need a registration date fetch."""
    if include_non_ok:
        user_filter = "(u.user_name IS NULL OR COALESCE(u.fetch_status, '') <> 'ok')"
    else:
        user_filter = "u.user_name IS NULL"

    limit_clause = ""
    params = []
    if limit is not None:
        limit_clause = "LIMIT %s"
        params.append(limit)

    query = f"""
    WITH log_users AS (
      SELECT TRIM(user_name) AS user_name, COUNT(*)::int AS log_count
      FROM logs
      WHERE user_name IS NOT NULL
        AND TRIM(user_name) <> ''
      GROUP BY TRIM(user_name)
      HAVING COUNT(*) > %s
    ),
    owner_users AS (
      SELECT DISTINCT TRIM(owner_username) AS user_name
      FROM caches
      WHERE owner_username IS NOT NULL
        AND TRIM(owner_username) <> ''
    ),
    candidate_users AS (
      SELECT user_name FROM log_users
      UNION
      SELECT user_name FROM owner_users
    )
    SELECT candidate_users.user_name
    FROM candidate_users
    LEFT JOIN "user" u ON u.user_name = candidate_users.user_name
    WHERE 1 = 1
      AND {user_filter}
      AND candidate_users.user_name <> '[DELETED_USER]'
    ORDER BY candidate_users.user_name
    {limit_clause};
    """
    with conn.cursor() as cur:
        cur.execute(query, [MIN_LOG_COUNT_FOR_REGDATE_FETCH, *params])
        return [row["user_name"] for row in cur.fetchall()]


def get_distinct_log_user_count(conn) -> int:
    """Count candidate users from active log users and all cache owners."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH log_users AS (
              SELECT TRIM(user_name) AS user_name
              FROM logs
              WHERE user_name IS NOT NULL
                AND TRIM(user_name) <> ''
              GROUP BY TRIM(user_name)
              HAVING COUNT(*) > %s
            ),
            owner_users AS (
              SELECT DISTINCT TRIM(owner_username) AS user_name
              FROM caches
              WHERE owner_username IS NOT NULL
                AND TRIM(owner_username) <> ''
            ),
            candidate_users AS (
              SELECT user_name FROM log_users
              UNION
              SELECT user_name FROM owner_users
            )
            SELECT COUNT(*)::int AS count
            FROM candidate_users
            WHERE candidate_users.user_name <> '[DELETED_USER]';
            """,
            (MIN_LOG_COUNT_FOR_REGDATE_FETCH,),
        )
        return cur.fetchone()["count"] or 0


def upsert_results(conn, results: list[dict]) -> None:
    """Write a batch of user registration results."""
    if not results:
        return

    query = """
    INSERT INTO "user" (user_name, registration_date, fetch_status)
    VALUES (%(user_name)s, %(registration_date)s, %(fetch_status)s)
    ON CONFLICT (user_name) DO UPDATE
    SET registration_date = EXCLUDED.registration_date,
        fetch_status = EXCLUDED.fetch_status;
    """
    with conn.cursor() as cur:
        execute_batch(cur, query, results)
    conn.commit()


def parse_args():
    parser = argparse.ArgumentParser(description="Fetch registration dates for logs users not ready in the user table.")
    parser.add_argument("--limit", type=int, default=parse_optional_int(DEFAULT_LIMIT), help="Optional maximum number of users to process.")
    parser.add_argument("--missing-only", action="store_true", help="Only fetch users absent from the user table.")
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES, help="Per-user request retry count.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Per-request timeout in seconds.")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS, help="Delay between users in seconds.")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="Database write batch size.")
    parser.add_argument("--dry-run", action="store_true", help="Only count distinct logs users; do not create table, crawl, or write.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    conn = connect_db()
    pending_results = []
    login_required_count = 0

    try:
        if args.dry_run:
            total_users = get_distinct_log_user_count(conn)
            logger.info(
                "Candidate users from logs with more than %s logs or cache owners: %s",
                MIN_LOG_COUNT_FOR_REGDATE_FETCH,
                total_users,
            )
            return

        ensure_user_table(conn)
        user_names = get_usernames_to_fetch(
            conn,
            include_non_ok=not args.missing_only,
            limit=args.limit,
        )
        logger.info("Users to fetch: %s", len(user_names))

        session = build_authenticated_session(timeout_seconds=max(1, args.timeout))
        if session is None:
            logger.error("User registration date crawl skipped because profile access is unavailable")
            return

        with session:
            for index, user_name in enumerate(user_names, start=1):
                started_at = time.monotonic()
                registration_date, raw_date, status = fetch_registration_date(
                    session,
                    user_name,
                    max_retries=max(1, args.max_retries),
                    timeout_seconds=max(1, args.timeout),
                )
                elapsed = time.monotonic() - started_at

                logger.info(
                    "[%s/%s] %s status=%s registration_date=%s raw=%s elapsed=%.1fs",
                    index,
                    len(user_names),
                    user_name,
                    status,
                    registration_date,
                    raw_date,
                    elapsed,
                )

                if status == "login_required":
                    login_required_count += 1

                pending_results.append(
                    {
                        "user_name": user_name,
                        "registration_date": registration_date,
                        "fetch_status": status,
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

                if index < len(user_names) and args.delay > 0:
                    time.sleep(args.delay)

        if pending_results:
            upsert_results(conn, pending_results)
            logger.info("Wrote final %s results", len(pending_results))

        logger.info("User registration date crawl complete")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
