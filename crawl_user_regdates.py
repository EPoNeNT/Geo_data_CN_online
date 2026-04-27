#!/usr/bin/env python3
"""Fetch Geocaching registration dates for log users missing from the user table."""

import argparse
import os
import re
import time
from datetime import datetime
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

from runtime_utils import looks_like_login_page, require_env, setup_logging


logger = setup_logging("crawl_user_regdates.log")

DATABASE_URL = require_env("DATABASE_URL")
PROFILE_URL = "https://www.geocaching.com/p/default.aspx"
DEFAULT_TIMEOUT_SECONDS = int(os.getenv("USER_REGDATE_TIMEOUT_SECONDS", "15"))
DEFAULT_MAX_RETRIES = int(os.getenv("USER_REGDATE_MAX_RETRIES", "3"))
DEFAULT_DELAY_SECONDS = float(os.getenv("USER_REGDATE_DELAY_SECONDS", "1.6"))
DEFAULT_BATCH_SIZE = int(os.getenv("USER_REGDATE_BATCH_SIZE", "50"))
DEFAULT_LIMIT = os.getenv("USER_REGDATE_LIMIT")

JOINED_PATTERNS = (
    re.compile(
        r'id="ctl00_ProfileHead_ProfileHeader_lblMemberSinceDate">\s*Joined\s*(.*?)\s*</span>',
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(
        r'lblMemberSinceDate">\s*Joined\s*(.*?)\s*</span>',
        re.IGNORECASE | re.DOTALL,
    ),
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
)


def parse_optional_int(value: Optional[str]) -> Optional[int]:
    """Parse an optional positive integer from env or CLI defaults."""
    if value is None or not str(value).strip():
        return None

    parsed = int(value)
    if parsed <= 0:
        raise ValueError("limit must be a positive integer")
    return parsed


def build_session() -> requests.Session:
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

    cookie = (
        os.getenv("GEOCACHING_COOKIE")
        or os.getenv("GEOCACHING_COOKIES")
        or os.getenv("GEOCOOKIE_NONPREMIUM")
        or os.getenv("GEOCOOKIE_PREMIUM")
    )
    if cookie:
        headers["Cookie"] = cookie

    session.headers.update(headers)
    return session


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
            return None, None, "not_found"

        except requests.RequestException as exc:
            last_status = "request_failed"
            logger.warning("%s request failed on attempt %s/%s: %s", user_name, attempt, max_retries, exc)

    return None, None, last_status


def connect_db():
    """Connect to Neon."""
    return psycopg2.connect(
        DATABASE_URL,
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
              registration_date_raw TEXT,
              fetch_status TEXT NOT NULL DEFAULT 'pending',
              checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
              updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        cur.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS registration_date DATE;')
        cur.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS registration_date_raw TEXT;')
        cur.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS fetch_status TEXT NOT NULL DEFAULT \'pending\';')
        cur.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW();')
        cur.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();')
        cur.execute('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();')
        cur.execute('CREATE UNIQUE INDEX IF NOT EXISTS user_user_name_unique_idx ON "user" (user_name);')
    conn.commit()


def get_usernames_to_fetch(conn, include_non_ok: bool, limit: Optional[int]) -> list[str]:
    """Read distinct log usernames that need a registration date fetch."""
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
    SELECT DISTINCT TRIM(l.user_name) AS user_name
    FROM logs l
    LEFT JOIN "user" u ON u.user_name = TRIM(l.user_name)
    WHERE l.user_name IS NOT NULL
      AND TRIM(l.user_name) <> ''
      AND {user_filter}
    ORDER BY user_name
    {limit_clause};
    """
    with conn.cursor() as cur:
        cur.execute(query, params)
        return [row["user_name"] for row in cur.fetchall()]


def get_distinct_log_user_count(conn) -> int:
    """Count distinct non-empty log usernames."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(DISTINCT TRIM(user_name))::int AS count
            FROM logs
            WHERE user_name IS NOT NULL
              AND TRIM(user_name) <> '';
            """
        )
        return cur.fetchone()["count"] or 0


def upsert_results(conn, results: list[dict]) -> None:
    """Write a batch of user registration results."""
    if not results:
        return

    query = """
    INSERT INTO "user" (user_name, registration_date, registration_date_raw, fetch_status, checked_at)
    VALUES (%(user_name)s, %(registration_date)s, %(registration_date_raw)s, %(fetch_status)s, NOW())
    ON CONFLICT (user_name) DO UPDATE
    SET registration_date = EXCLUDED.registration_date,
        registration_date_raw = EXCLUDED.registration_date_raw,
        fetch_status = EXCLUDED.fetch_status,
        checked_at = NOW(),
        updated_at = NOW();
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

    try:
        if args.dry_run:
            total_users = get_distinct_log_user_count(conn)
            logger.info("Distinct logs users: %s", total_users)
            return

        ensure_user_table(conn)
        user_names = get_usernames_to_fetch(
            conn,
            include_non_ok=not args.missing_only,
            limit=args.limit,
        )
        logger.info("Users to fetch: %s", len(user_names))

        with build_session() as session:
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

                pending_results.append(
                    {
                        "user_name": user_name,
                        "registration_date": registration_date,
                        "registration_date_raw": raw_date,
                        "fetch_status": status,
                    }
                )

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
