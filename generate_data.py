#!/usr/bin/env python3
"""
Data Generation Script for GeoCaching CN Analytics
Generates static JSON files from Neon PostgreSQL database.
Runs after crawl_caches and crawl_logs in GitHub Actions workflow.

Output files:
- public/data/overview.json
- public/data/player-rankings.json
- public/data/cache-rankings.json
- public/data/city-rankings.json
- public/data/generated-at.json
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from runtime_utils import (
    connect_postgres,
    looks_like_login_page,
    minimize_cookie_value,
    require_env,
    setup_logging,
)
import psycopg2
from psycopg2.extras import RealDictCursor
import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

logger = setup_logging("generate_data.log")

DATABASE_URL = require_env("DATABASE_URL")
OUTPUT_DIR = "public/data"
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CHINA_CITIES_FILE = os.path.join(PROJECT_ROOT, "china_cities.json")

# Constants
REGIONS = [
    {"key": "china", "name": "中国大陆"},
    {"key": "taiwan", "name": "台湾"},
    {"key": "hong-kong", "name": "香港"},
    {"key": "macao", "name": "澳门"},
]

REGION_COUNTRY_MAP = {
    "china": "China",
    "taiwan": "Taiwan",
    "hong-kong": "Hong Kong",
    "macao": "Macao",
}

DT_VALUES = [1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5]
RANKING_TIME_RANGES = ["30d", "ytd", "active", "all"]
EXCLUDED_CACHE_TYPES = "6, 13, 3653"
EVENT_CACHE_TYPES = "6, 13, 3653"
CACHE_TYPE_LABELS = {
    2: "Tradition",
    3: "Multi",
    4: "Virtual",
    5: "Letterbox",
    6: "Event",
    8: "Mystery",
    11: "Webcam",
    12: "Locationless",
    13: "CITO",
    137: "Earthcache",
    1858: "Wherigo",
    3653: "Community Celebration Event",
}
CACHE_TYPE_FILTERS = {
    "all": None,
    "tradition": 2,
    "multi": 3,
    "virtual": 4,
    "letterbox": 5,
    "mystery": 8,
    "webcam": 11,
    "locationless": 12,
    "earthcache": 137,
    "wherigo": 1858,
}

# Cache filters. Most statistics include archived caches, but D/T matrices and
# active yearly trend counts still exclude them. Deleted caches are excluded from
# every generated dataset.
CACHE_NOT_DELETED_WHERE = "COALESCE(c.cache_status, 0) != 404"
CACHE_NOT_DELETED_JOIN = "COALESCE(c.cache_status, 0) != 404"
EXCLUDE_CACHE_WHERE = f"{CACHE_NOT_DELETED_WHERE} AND c.geocache_type NOT IN ({EXCLUDED_CACHE_TYPES})"
EXCLUDE_CACHE_JOIN = f"{CACHE_NOT_DELETED_JOIN} AND c.geocache_type NOT IN ({EXCLUDED_CACHE_TYPES})"
ACTIVE_CACHE_WHERE = f"COALESCE(c.cache_status, 0) NOT IN (2, 404) AND c.geocache_type NOT IN ({EXCLUDED_CACHE_TYPES})"
ACTIVE_CACHE_JOIN = f"COALESCE(c.cache_status, 0) NOT IN (2, 404) AND c.geocache_type NOT IN ({EXCLUDED_CACHE_TYPES})"
EVENT_CACHE_WHERE = f"{CACHE_NOT_DELETED_WHERE} AND c.geocache_type IN ({EVENT_CACHE_TYPES})"
EVENT_CACHE_JOIN = f"{CACHE_NOT_DELETED_JOIN} AND c.geocache_type IN ({EVENT_CACHE_TYPES})"
OWNER_USERNAME_FILTER = (
    "c.owner_username IS NOT NULL AND c.owner_username <> '' "
    "AND c.owner_username <> '[DELETED_USER]'"
)
CACHE_RANKING_ENTRY_FILTER = (
    "c.code IS NOT NULL AND c.code <> '' "
    "AND c.name IS NOT NULL AND c.name <> '' "
    "AND c.owner_username IS NOT NULL AND c.owner_username <> '' "
    "AND COALESCE(c.cache_status, 0) != 404"
)
COUNTRY_SUBTITLE_MAP = {
    "China": "中国",
    "Taiwan": "台湾",
    "Hong Kong": "香港",
    "Macao": "澳门",
}
PROVINCE_PREFIX_MAP = {
    "11": "北京市",
    "12": "天津市",
    "13": "河北省",
    "14": "山西省",
    "15": "内蒙古自治区",
    "21": "辽宁省",
    "22": "吉林省",
    "23": "黑龙江省",
    "31": "上海市",
    "32": "江苏省",
    "33": "浙江省",
    "34": "安徽省",
    "35": "福建省",
    "36": "江西省",
    "37": "山东省",
    "41": "河南省",
    "42": "湖北省",
    "43": "湖南省",
    "44": "广东省",
    "45": "广西壮族自治区",
    "46": "海南省",
    "50": "重庆市",
    "51": "四川省",
    "52": "贵州省",
    "53": "云南省",
    "54": "西藏自治区",
    "61": "陕西省",
    "62": "甘肃省",
    "63": "青海省",
    "64": "宁夏回族自治区",
    "65": "新疆维吾尔自治区",
}
CITY_PROVINCE_MAP: Optional[Dict[str, str]] = None
DIRECT_ADMIN_CITY_SUBTITLES = {
    "北京市": "北京市",
    "北京": "北京市",
    "上海市": "上海市",
    "上海": "上海市",
    "天津市": "天津市",
    "天津": "天津市",
    "重庆市": "重庆市",
    "重庆": "重庆市",
}
DEFAULT_AVATAR_URL = "https://www.geocaching.com/images/default_avatar.png"
AVATAR_PROFILE_URL = "https://www.geocaching.com/p/default.aspx"
AVATAR_MAX_RETRIES = int(os.getenv("AVATAR_MAX_RETRIES", "3"))
AVATAR_FETCH_DELAY_SECONDS = float(os.getenv("AVATAR_FETCH_DELAY_SECONDS", "0.4"))
AVATAR_REQUEST_TIMEOUT_SECONDS = int(os.getenv("AVATAR_REQUEST_TIMEOUT_SECONDS", "45"))
AVATAR_STREAM_MAX_BYTES = int(os.getenv("AVATAR_STREAM_MAX_BYTES", "102400"))  # 100KB
AVATAR_UPSERT_BATCH_SIZE = int(os.getenv("AVATAR_UPSERT_BATCH_SIZE", "100"))
AVATAR_URL_PATTERNS = [
    re.compile(
        r"profile-image-wrapper.*?url\((?:'|\")?(https://img\.geocaching\.com/user/square250/[^'\")]+?\.(?:jpg|png)(?:\?[^'\")]*)?)(?:'|\")?\)",
        re.DOTALL | re.IGNORECASE,
    ),
    re.compile(
        r"https://img\.geocaching\.com/user/square250/[^'\"\s<>)]*?\.(?:jpg|png)(?:\?[^'\"\s<>)]*)?",
        re.IGNORECASE,
    ),
]
REAL_AVATAR_URL_PATTERN = re.compile(
    r"^https://img\.geocaching\.com/user/square250/.+\.(?:jpg|png)(?:\?.*)?$",
    re.IGNORECASE,
)
NEWBIE_REGISTRATION_FILTER = (
    "u.registration_date >= CURRENT_DATE - INTERVAL '1 year' "
    "AND u.registration_date <= CURRENT_DATE"
)
PREVIOUS_NEWBIE_REGISTRATION_FILTER = (
    "u.registration_date >= CURRENT_DATE - INTERVAL '2 years' "
    "AND u.registration_date < CURRENT_DATE - INTERVAL '1 year'"
)


def sql_literal(value: str) -> str:
    """Return a SQL string literal for trusted generator filters."""
    return "'" + str(value).replace("'", "''") + "'"


def is_real_avatar_url(value: Optional[str]) -> bool:
    """Return True only for real Geocaching square avatar image URLs."""
    return bool(value and REAL_AVATAR_URL_PATTERN.match(value))


def load_city_province_map() -> Dict[str, str]:
    """Load China city-to-province mapping from the bundled city boundary file."""
    global CITY_PROVINCE_MAP
    if CITY_PROVINCE_MAP is not None:
        return CITY_PROVINCE_MAP

    city_provinces: Dict[str, str] = {}
    try:
        with open(CHINA_CITIES_FILE, "r", encoding="utf-8") as f:
            features = json.load(f).get("features", [])
    except Exception as e:
        logger.warning(f"Failed to load China city province map: {e}")
        CITY_PROVINCE_MAP = city_provinces
        return CITY_PROVINCE_MAP

    for feature in features:
        city_id = str(feature.get("id", ""))
        province_name = PROVINCE_PREFIX_MAP.get(city_id[:2])
        city_name = feature.get("properties", {}).get("name")
        if city_name and province_name:
            city_provinces[str(city_name)] = province_name

    city_provinces.update(DIRECT_ADMIN_CITY_SUBTITLES)
    CITY_PROVINCE_MAP = city_provinces
    return CITY_PROVINCE_MAP


class DataGenerator:
    """Generate static JSON data files from database."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.conn = None
        self.cursor = None

    def connect(self):
        """Connect to database."""
        self.conn = connect_postgres(
            self.database_url,
            logger=logger,
            connect_timeout=10,
            cursor_factory=RealDictCursor,
        )
        self.cursor = self.conn.cursor()
        logger.info("Database connection established")

    def close(self):
        """Close database connection."""
        if self.cursor:
            self.cursor.close()
        if self.conn:
            self.conn.close()
        logger.info("Database connection closed")

    def execute_query(self, query: str, params: tuple = None) -> List[Dict]:
        """Execute a query and return results as list of dicts."""
        try:
            self.cursor.execute(query, params)
            return [dict(row) for row in self.cursor.fetchall()]
        except Exception as e:
            logger.error(f"Query execution failed: {e}")
            raise

    def execute_write(self, query: str, params: tuple = None):
        """Execute a write query and commit it."""
        try:
            self.cursor.execute(query, params)
            self.conn.commit()
        except Exception as e:
            if self.conn:
                self.conn.rollback()
            logger.error(f"Write execution failed: {e}")
            raise

    def ensure_user_avatar_table(self):
        """Ensure the user avatar cache table exists."""
        query = """
        CREATE TABLE IF NOT EXISTS user_avatars (
          user_name TEXT PRIMARY KEY,
          avatar_url TEXT NOT NULL,
          avatar_status TEXT NOT NULL DEFAULT 'real',
          checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        ALTER TABLE user_avatars
          ADD COLUMN IF NOT EXISTS avatar_status TEXT NOT NULL DEFAULT 'real';
        ALTER TABLE user_avatars
          ADD COLUMN IF NOT EXISTS checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
        ALTER TABLE user_avatars
          DROP COLUMN IF EXISTS created_at,
          DROP COLUMN IF EXISTS updated_at;
        """
        self.execute_write(query)

    def ensure_live_connection(self):
        """Reconnect before avatar cache writes if the database connection went idle."""
        try:
            if not self.conn or self.conn.closed:
                self.connect()
                return

            self.conn.rollback()
            self.cursor.execute("SELECT 1")
            self.cursor.fetchone()
        except Exception as e:
            logger.warning(f"Database connection stale before avatar cache write, reconnecting: {e}")
            try:
                if self.cursor:
                    self.cursor.close()
            except Exception:
                pass
            try:
                if self.conn:
                    self.conn.close()
            except Exception:
                pass
            self.conn = None
            self.cursor = None
            self.connect()

    def get_avatar_cache_entries(self, user_names: List[str]) -> Dict[str, Dict[str, Any]]:
        """Return cached real/default avatar entries for the given users."""
        unique_names = sorted({name for name in user_names if name})
        if not unique_names:
            return {}

        self.ensure_user_avatar_table()
        query = """
        SELECT
          user_name,
          avatar_url,
          COALESCE(avatar_status, 'real') AS avatar_status,
          checked_at
        FROM user_avatars
        WHERE user_name = ANY(%s);
        """
        rows = self.execute_query(query, (unique_names,))
        return {
            row["user_name"]: {
                "avatar_url": row["avatar_url"],
                "avatar_status": row.get("avatar_status") or "real",
                "checked_at": row.get("checked_at"),
            }
            for row in rows
        }

    def get_cached_avatar_urls(self, user_names: List[str]) -> Dict[str, str]:
        """Return avatar URLs already stored for the given users."""
        entries = self.get_avatar_cache_entries(user_names)
        return {
            user_name: (
                entry["avatar_url"]
                if entry.get("avatar_status") == "real"
                else DEFAULT_AVATAR_URL
            )
            for user_name, entry in entries.items()
            if (
                entry.get("avatar_status") == "real"
                and is_real_avatar_url(entry.get("avatar_url"))
            ) or entry.get("avatar_status") == "default"
        }

    def upsert_avatar_cache_entries(self, avatar_entries: Dict[str, Tuple[str, str]]):
        """Store fetched avatar results in the cache table in batches."""
        rows = []
        for user_name, value in avatar_entries.items():
            avatar_url, avatar_status = value
            if avatar_status == "real" and is_real_avatar_url(avatar_url):
                rows.append((user_name, avatar_url, avatar_status))
            elif avatar_status == "default":
                rows.append((user_name, DEFAULT_AVATAR_URL, avatar_status))
            else:
                continue

        if not rows:
            return

        self.ensure_user_avatar_table()
        self.ensure_live_connection()
        query = """
        INSERT INTO user_avatars (user_name, avatar_url, avatar_status, checked_at)
        VALUES (%s, %s, %s, NOW())
        ON CONFLICT (user_name) DO UPDATE
        SET avatar_url = EXCLUDED.avatar_url,
            avatar_status = EXCLUDED.avatar_status,
            checked_at = NOW();
        """
        try:
            batch_size = max(1, AVATAR_UPSERT_BATCH_SIZE)
            for start in range(0, len(rows), batch_size):
                self.cursor.executemany(query, sorted(rows)[start:start + batch_size])
            self.conn.commit()
        except Exception as e:
            if self.conn:
                self.conn.rollback()
            logger.error(f"Avatar cache upsert failed: {e}")
            raise

    def upsert_avatar_urls(self, avatar_urls: Dict[str, str]):
        """Store fetched real avatar URLs in the cache table."""
        self.upsert_avatar_cache_entries(
            {
                user_name: (avatar_url, "real")
                for user_name, avatar_url in avatar_urls.items()
                if is_real_avatar_url(avatar_url)
            }
        )

    def build_avatar_session(self) -> requests.Session:
        """Create a requests session for avatar profile pages."""
        session = requests.Session()
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        }
        cookie = (
            os.getenv("GEOCOOKIE_PREMIUM")
            or os.getenv("GEOCOOKIE_NONPREMIUM")
        )
        if cookie:
            headers["Cookie"] = minimize_cookie_value(cookie)
        session.headers.update(headers)
        return session

    def parse_avatar_url(self, html: str) -> str:
        """Extract an avatar URL from a Geocaching profile page."""
        for pattern in AVATAR_URL_PATTERNS:
            match = pattern.search(html or "")
            if match:
                return match.group(1) if match.groups() else match.group(0)
        return DEFAULT_AVATAR_URL

    def fetch_avatar_url(self, user_name: str, session: Optional[requests.Session] = None) -> Tuple[str, str]:
        """Fetch one user's avatar URL from their Geocaching profile.

        Streams only the first ~100 KB — the avatar URL is in the page header.
        """
        active_session = session or self.build_avatar_session()

        for attempt in range(AVATAR_MAX_RETRIES):
            response = None
            try:
                if attempt > 0:
                    time.sleep(2)

                logger.info(
                    f"Avatar request start for {user_name} "
                    f"(attempt {attempt + 1}/{AVATAR_MAX_RETRIES})"
                )
                response = active_session.get(
                    AVATAR_PROFILE_URL,
                    params={"u": user_name},
                    timeout=AVATAR_REQUEST_TIMEOUT_SECONDS,
                    stream=True,
                )

                if response.status_code == 200:
                    accumulated = b""
                    try:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                accumulated += chunk
                                if len(accumulated) >= AVATAR_STREAM_MAX_BYTES:
                                    break
                    finally:
                        response.close()

                    html = accumulated.decode("utf-8", errors="replace")
                    if looks_like_login_page(response.url, html):
                        logger.warning(f"Avatar request for {user_name} returned a login page")
                        continue

                    avatar_url = self.parse_avatar_url(html)
                    result = "real" if is_real_avatar_url(avatar_url) else "default"
                    logger.info(
                        f"Avatar request finished for {user_name} "
                        f"(attempt {attempt + 1}/{AVATAR_MAX_RETRIES}, result={result})"
                    )
                    return (avatar_url, result)

                logger.warning(
                    f"Avatar request for {user_name} returned HTTP {response.status_code}"
                )
            except Exception as e:
                logger.warning(f"Avatar request for {user_name} failed: {e}")
            finally:
                if response is not None:
                    response.close()

        return (DEFAULT_AVATAR_URL, "failed")

    def resolve_avatar_urls(self, user_names: List[str]) -> Dict[str, str]:
        """Load cached avatar URLs and fetch missing users."""
        unique_names = sorted({name for name in user_names if name})
        if not unique_names:
            return {}

        cache_entries = self.get_avatar_cache_entries(unique_names)
        cached_urls = {}
        real_cached_count = 0
        default_cached_count = 0
        for name, entry in cache_entries.items():
            avatar_status = entry.get("avatar_status")
            avatar_url = entry.get("avatar_url")
            if avatar_status == "real" and is_real_avatar_url(avatar_url):
                cached_urls[name] = avatar_url
                real_cached_count += 1
            elif avatar_status == "default":
                cached_urls[name] = DEFAULT_AVATAR_URL
                default_cached_count += 1
        missing_names = [
            name
            for name in unique_names
            if name not in cached_urls
        ]

        logger.info(
            "Avatar cache status: "
            f"{real_cached_count} real cached, "
            f"{default_cached_count} default cached, "
            f"{len(missing_names)} uncached/invalid, "
            f"{len(unique_names)} total unique users"
        )

        fetched_urls: Dict[str, str] = {}
        fetched_entries: Dict[str, Tuple[str, str]] = {}
        pending_entries: Dict[str, Tuple[str, str]] = {}
        if missing_names:
            logger.info(f"Fetching avatars for {len(missing_names)} uncached users...")
            with self.build_avatar_session() as session:
                for index, name in enumerate(missing_names, start=1):
                    started_at = time.monotonic()
                    logger.info(f"Fetching avatar {index}/{len(missing_names)}: {name}")
                    fetch_result = self.fetch_avatar_url(name, session=session)
                    if isinstance(fetch_result, tuple):
                        avatar_url, avatar_status = fetch_result
                    else:
                        avatar_url = fetch_result
                        avatar_status = "real" if is_real_avatar_url(avatar_url) else "default"
                    fetched_urls[name] = avatar_url
                    fetched_entries[name] = (avatar_url, avatar_status)
                    pending_entries[name] = (avatar_url, avatar_status)
                    elapsed = time.monotonic() - started_at
                    logger.info(
                        f"Finished avatar {index}/{len(missing_names)}: {name} "
                        f"result={avatar_status} elapsed={elapsed:.1f}s"
                    )
                    if len(pending_entries) >= max(1, AVATAR_UPSERT_BATCH_SIZE):
                        self.upsert_avatar_cache_entries(pending_entries)
                        pending_entries = {}
                    if index < len(missing_names) and AVATAR_FETCH_DELAY_SECONDS > 0:
                        time.sleep(AVATAR_FETCH_DELAY_SECONDS)

            self.upsert_avatar_cache_entries(pending_entries)
            real_count = sum(1 for _, status in fetched_entries.values() if status == "real")
            default_count = sum(1 for _, status in fetched_entries.values() if status == "default")
            failed_count = sum(1 for _, status in fetched_entries.values() if status == "failed")
            logger.info(
                f"Avatar fetch completed: {real_count} real URLs, "
                f"{default_count} default fallbacks, {failed_count} failed requests"
            )

        return {name: cached_urls.get(name) or fetched_urls.get(name, DEFAULT_AVATAR_URL) for name in unique_names}

    def add_avatar_urls_to_rankings(self, rankings: Dict) -> Dict:
        """Attach avatarUrl to all player ranking entries."""
        user_names = []
        for ranking_type in rankings.values():
            for entries in ranking_type.values():
                user_names.extend(entry["name"] for entry in entries if entry.get("name"))

        avatar_urls = self.resolve_avatar_urls(user_names)
        for ranking_type in rankings.values():
            for entries in ranking_type.values():
                for entry in entries:
                    entry["avatarUrl"] = avatar_urls.get(entry.get("name"), DEFAULT_AVATAR_URL)

        return rankings

    def format_city_subtitle(self, city_name: str, country_or_region: Optional[str]) -> str:
        """Format city ranking subtitle as province or Chinese region name."""
        if country_or_region in {"Taiwan", "台湾"}:
            return "台湾"
        if country_or_region in {"Hong Kong", "香港"}:
            return "香港"
        if country_or_region in {"Macao", "澳门"}:
            return "澳门"
        if country_or_region == "China":
            return load_city_province_map().get(city_name, "中国")
        if country_or_region and country_or_region != "None":
            return COUNTRY_SUBTITLE_MAP.get(country_or_region, country_or_region)
        return load_city_province_map().get(city_name, "")

    def ensure_output_dir(self):
        """Ensure output directory exists."""
        os.makedirs(OUTPUT_DIR, exist_ok=True)

    def get_generated_at(self) -> str:
        """Get current timestamp in ISO format."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    # ==================== OVERVIEW.JSON ====================

    def generate_summary_metrics(self, country_filter: Optional[str] = None) -> Dict:
        """Generate summary metrics for a region or overall."""
        if country_filter:
            country_value = sql_literal(country_filter)
            cache_where = f"WHERE {EXCLUDE_CACHE_WHERE} AND c.country = {country_value}"
            log_where = (
                f"WHERE COALESCE(c2.cache_status, 0) != 404 "
                f"AND c2.geocache_type NOT IN ({EXCLUDED_CACHE_TYPES}) "
                f"AND c2.country = {country_value}"
            )
        else:
            cache_where = f"WHERE {EXCLUDE_CACHE_WHERE}"
            log_where = (
                f"WHERE COALESCE(c2.cache_status, 0) != 404 "
                f"AND c2.geocache_type NOT IN ({EXCLUDED_CACHE_TYPES})"
            )

        query = f"""
        WITH cache_scope AS (
          SELECT * FROM caches c
          {cache_where}
        ),
        log_scope AS (
          SELECT l.*
          FROM logs l
          JOIN caches c2 ON c2.code = l.gc_code
          {log_where}
        )
        SELECT
          (SELECT COUNT(*)::int FROM cache_scope) AS total_caches,
          (SELECT COUNT(DISTINCT owner_username)::int FROM cache_scope
           WHERE owner_username IS NOT NULL AND owner_username <> '') AS total_owners,
          (SELECT COUNT(DISTINCT user_name)::int FROM log_scope
           WHERE user_name IS NOT NULL AND user_name <> '') AS total_finders,
          (SELECT COUNT(*)::int FROM log_scope) AS total_logs;
        """

        result = self.execute_query(query)
        if result:
            row = result[0]
            return {
                "totalCaches": row["total_caches"] or 0,
                "totalFinders": row["total_finders"] or 0,
                "totalOwners": row["total_owners"] or 0,
                "totalLogs": row["total_logs"] or 0,
            }
        return {"totalCaches": 0, "totalFinders": 0, "totalOwners": 0, "totalLogs": 0}

    def generate_active_cache_count(self, country_filter: Optional[str] = None) -> int:
        """Generate active cache count for region distribution cards."""
        where_clause = f"WHERE {ACTIVE_CACHE_WHERE}"
        if country_filter:
            where_clause += f" AND c.country = {sql_literal(country_filter)}"

        result = self.execute_query(f"SELECT COUNT(*)::int AS count FROM caches c {where_clause};")
        return result[0]["count"] if result else 0

    def generate_yearly_trend(self, country_filter: Optional[str] = None) -> List[Dict]:
        """Generate yearly trend data."""
        where_clause = f"WHERE {EXCLUDE_CACHE_WHERE}"
        if country_filter:
            where_clause += f" AND c.country = {sql_literal(country_filter)}"

        query = f"""
        WITH years AS (
          SELECT generate_series(
            COALESCE(
              (SELECT MIN(EXTRACT(YEAR FROM placed_date)::int) FROM caches c WHERE placed_date IS NOT NULL AND {EXCLUDE_CACHE_WHERE}),
              EXTRACT(YEAR FROM CURRENT_DATE)::int
            ),
            EXTRACT(YEAR FROM CURRENT_DATE)::int
          ) AS year
        ),
        counts AS (
          SELECT
            EXTRACT(YEAR FROM placed_date)::int AS year,
            COUNT(*) FILTER (WHERE c.cache_status != 2)::int AS total,
            COUNT(*) FILTER (
              WHERE placed_date::date <=
                (date_trunc('year', make_date(EXTRACT(YEAR FROM placed_date)::int, 1, 1))::date
                 + (CURRENT_DATE - date_trunc('year', CURRENT_DATE)::date))
            )::int AS ytd_cache,
            COUNT(*) FILTER (WHERE c.cache_status = 2)::int AS archived
          FROM caches c
          {where_clause}
          AND placed_date IS NOT NULL
          GROUP BY 1
        )
        SELECT
          years.year::text AS year,
          COALESCE(counts.total, 0)::int AS total,
          COALESCE(counts.ytd_cache, 0)::int AS ytdCache,
          COALESCE(counts.archived, 0)::int AS archived
        FROM years
        LEFT JOIN counts USING (year)
        ORDER BY years.year;
        """

        results = self.execute_query(query)
        return [
            {
                "year": str(row["year"]),
                "total": row["total"] or 0,
                "ytdCache": row["ytdcache"] or 0,
                "archived": row["archived"] or 0,
            }
            for row in results
        ]

    def generate_heatmap_period_query(
        self,
        time_range: str,
        country_filter: Optional[str] = None,
    ) -> str:
        """Generate SQL query for overview heatmap points."""
        where_clause = f"WHERE {EXCLUDE_CACHE_JOIN}"
        if country_filter:
            where_clause += f" AND c.country = {sql_literal(country_filter)}"

        if time_range == "30d":
            date_filter = "l.visited::date >= CURRENT_DATE - INTERVAL '30 day'"
        elif time_range == "ytd":
            date_filter = (
                "l.visited::date >= date_trunc('year', CURRENT_DATE)::date "
                "AND l.visited::date <= CURRENT_DATE"
            )
        else:
            raise ValueError(f"Unknown heatmap time range: {time_range}")

        return f"""
        SELECT
          ROUND(c.latitude::numeric, 2)::float8 AS latitude,
          ROUND(c.longitude::numeric, 2)::float8 AS longitude,
          COUNT(*)::int AS count
        FROM logs l
        JOIN caches c ON c.code = l.gc_code
        {where_clause}
          AND c.latitude IS NOT NULL
          AND c.longitude IS NOT NULL
          AND l.visited IS NOT NULL
          AND {date_filter}
        GROUP BY 1, 2
        ORDER BY count DESC, latitude ASC, longitude ASC;
        """

    def generate_heatmap(self, country_filter: Optional[str] = None) -> Dict[str, List[Dict]]:
        """Generate overview heatmap data for recent and YTD find logs."""
        heatmap = {}
        for time_range in ["30d", "ytd"]:
            rows = self.execute_query(
                self.generate_heatmap_period_query(time_range, country_filter)
            )
            heatmap[time_range] = [
                {
                    "latitude": row["latitude"],
                    "longitude": row["longitude"],
                    "count": row["count"] or 0,
                }
                for row in rows
            ]
        return heatmap

    def generate_dt_top_caches_query(
        self,
        country_filter: Optional[str] = None,
        city_filter: Optional[str] = None,
    ) -> str:
        """Generate SQL query for top caches in each D/T matrix cell."""
        where_clause = f"WHERE {ACTIVE_CACHE_WHERE}"
        if country_filter:
            where_clause += f" AND c.country = {sql_literal(country_filter)}"
        if city_filter:
            where_clause += f" AND COALESCE(NULLIF(TRIM(c.city), ''), c.country) = {sql_literal(city_filter)}"

        return f"""
        SELECT
          difficulty,
          terrain,
          code,
          name,
          owner,
          favorite_points
        FROM (
          SELECT
            c.difficulty::float8 AS difficulty,
            c.terrain::float8 AS terrain,
            c.code AS code,
            c.name AS name,
            c.owner_username AS owner,
            COALESCE(c.favorite_points, 0)::int AS favorite_points,
            ROW_NUMBER() OVER (
              PARTITION BY c.difficulty, c.terrain
              ORDER BY COALESCE(c.favorite_points, 0) DESC, c.code ASC
            ) AS rn
          FROM caches c
          {where_clause}
            AND c.difficulty IN ({','.join(map(str, DT_VALUES))})
            AND c.terrain IN ({','.join(map(str, DT_VALUES))})
        ) ranked
        WHERE rn <= 5
        ORDER BY difficulty, terrain, favorite_points DESC, code ASC;
        """

    def generate_dt_matrix(
        self,
        country_filter: Optional[str] = None,
        city_filter: Optional[str] = None,
        include_top_caches: bool = False,
    ) -> List[Dict]:
        """Generate Difficulty/Terrain matrix (9x9 = 81 elements)."""
        where_clause = f"WHERE {ACTIVE_CACHE_WHERE}"
        if country_filter:
            where_clause += f" AND c.country = {sql_literal(country_filter)}"
        if city_filter:
            where_clause += f" AND COALESCE(NULLIF(TRIM(c.city), ''), c.country) = {sql_literal(city_filter)}"

        query = f"""
        SELECT
          c.difficulty::float8 AS difficulty,
          c.terrain::float8 AS terrain,
          COUNT(*)::int AS count
        FROM caches c
        {where_clause}
        AND c.difficulty IN ({','.join(map(str, DT_VALUES))})
          AND c.terrain IN ({','.join(map(str, DT_VALUES))})
        GROUP BY c.difficulty, c.terrain
        ORDER BY c.difficulty, c.terrain;
        """

        results = self.execute_query(query)

        # Create full 81-element matrix
        matrix = {}
        for row in results:
            key = (row["difficulty"], row["terrain"])
            matrix[key] = row["count"] or 0

        top_caches = {}
        if include_top_caches:
            top_cache_results = self.execute_query(
                self.generate_dt_top_caches_query(country_filter, city_filter)
            )
            for row in top_cache_results:
                key = (row["difficulty"], row["terrain"])
                top_caches.setdefault(key, []).append(
                    {
                        "code": row["code"],
                        "name": row["name"],
                        "owner": row["owner"] or "",
                        "favoritePoints": row["favorite_points"] or 0,
                    }
                )

        output = []
        for i, d in enumerate(DT_VALUES):
            for j, t in enumerate(DT_VALUES):
                cell = {
                    "row": i,
                    "col": j,
                    "difficulty": d,
                    "terrain": t,
                    "count": matrix.get((d, t), 0),
                }
                cell_top_caches = top_caches.get((d, t))
                if cell_top_caches:
                    cell["topCaches"] = cell_top_caches
                output.append(cell)

        return output

    def generate_regions_data(self) -> Tuple[List[Dict], Dict]:
        """Generate regions list and region scopes."""
        regions_list = []
        region_scopes = {}

        for region in REGIONS:
            key = region["key"]
            country = REGION_COUNTRY_MAP[key]

            metrics = self.generate_summary_metrics(country)
            yearly_trend = self.generate_yearly_trend(country)
            heatmap = self.generate_heatmap(country)
            dt_matrix = self.generate_dt_matrix(country)

            total_caches = self.generate_active_cache_count(country)

            # Calculate percentage (will be updated after getting total)
            regions_list.append({
                "key": key,
                "name": region["name"],
                "totalCaches": total_caches,
                "percentage": 0,  # Will be calculated
            })

            region_scopes[key] = {
                "metrics": metrics,
                "yearlyTrend": yearly_trend,
                "heatmap": heatmap,
                "dtMatrix": dt_matrix,
            }

        # Calculate total and percentages
        total_all = sum(r["totalCaches"] for r in regions_list)
        for r in regions_list:
            r["percentage"] = round(r["totalCaches"] / total_all * 100, 1) if total_all > 0 else 0

        return regions_list, region_scopes

    def generate_overview_json(self) -> Dict:
        """Generate complete overview.json data."""
        logger.info("Generating overview.json...")

        summary = {
            "metrics": self.generate_summary_metrics(),
            "yearlyTrend": self.generate_yearly_trend(),
            "heatmap": self.generate_heatmap(),
            "dtMatrix": self.generate_dt_matrix(),
        }

        regions_list, region_scopes = self.generate_regions_data()

        return {
            "generatedAt": self.get_generated_at(),
            "summary": summary,
            "regions": regions_list,
            "regionScopes": region_scopes,
        }

    # ==================== PLAYER-RANKINGS.JSON ====================

    def calculate_trend(self, current_rank: int, previous_rank: int) -> Tuple[str, Optional[int]]:
        """Calculate trend direction and delta based on rank change.

        Args:
            current_rank: Current period rank (1-based)
            previous_rank: Previous period rank (1-based), 0 if not ranked

        Returns:
            (trend, trendDelta) where:
            - trend: "up", "down", or "flat"
            - trendDelta: absolute rank change, or None when previous score was zero/not ranked
        """
        if previous_rank == 0:
            # New entry in rankings (was not in previous period)
            return ("up", None)

        delta = previous_rank - current_rank
        if delta > 0:
            # Rank number decreased (e.g., 4->2): improved position
            return ("up", abs(delta))
        elif delta < 0:
            # Rank number increased (e.g., 2->4): dropped position
            return ("down", abs(delta))
        else:
            return ("flat", 0)

    def build_rank_lookup(self, rows: List[Dict], key_field: str = "name") -> Dict[str, int]:
        """Build item-to-rank mapping using the same tied rank rule as output."""
        ranks = {}
        display_rank = 0
        prev_score = None

        for index, row in enumerate(rows):
            score = row["score"] or 0
            if index == 0 or score != prev_score:
                display_rank = index + 1
            ranks[row[key_field]] = display_rank
            prev_score = score

        return ranks

    def generate_ranking_stats_date_condition(
        self,
        time_range: str,
        date_col: str,
        previous: bool = False,
    ) -> str:
        """Generate date condition for rankingStats counts."""
        if not previous:
            if time_range == "30d":
                return f"{date_col} >= CURRENT_DATE - INTERVAL '30 day'"
            if time_range == "ytd":
                return f"{date_col} >= date_trunc('year', CURRENT_DATE)::date"
            if time_range == "active":
                return "TRUE"
            if time_range == "all":
                return "TRUE"
            raise ValueError(f"Unknown time range: {time_range}")

        if time_range == "30d":
            return (
                f"{date_col} >= CURRENT_DATE - INTERVAL '60 day' "
                f"AND {date_col} < CURRENT_DATE - INTERVAL '30 day'"
            )
        if time_range == "ytd":
            return (
                f"{date_col} >= date_trunc('year', CURRENT_DATE - INTERVAL '1 year')::date "
                f"AND {date_col} < date_trunc('year', CURRENT_DATE)::date"
            )
        if time_range in {"active", "all"}:
            return f"{date_col} < date_trunc('year', CURRENT_DATE)::date"
        raise ValueError(f"Unknown time range: {time_range}")

    def generate_ranking_cache_condition(self, time_range: str, join_clause: bool = True) -> str:
        """Return cache scope filter for ranking time ranges."""
        if time_range in {"30d", "ytd", "active"}:
            return ACTIVE_CACHE_JOIN if join_clause else ACTIVE_CACHE_WHERE
        if time_range == "all":
            return EXCLUDE_CACHE_JOIN if join_clause else EXCLUDE_CACHE_WHERE
        raise ValueError(f"Unknown time range: {time_range}")

    def generate_ranking_count_query(
        self,
        ranking_type: str,
        time_range: str,
        previous: bool = False,
        country_filter: Optional[str] = None,
    ) -> str:
        """Generate player count query for rankingStats without leaderboard limits."""
        date_col = "c.placed_date" if ranking_type == "hides" else "l.visited"
        date_condition = self.generate_ranking_stats_date_condition(
            time_range,
            date_col,
            previous=previous,
        )
        cache_join_condition = self.generate_ranking_cache_condition(time_range, join_clause=True)
        cache_where_condition = self.generate_ranking_cache_condition(time_range, join_clause=False)
        country_condition = ""
        if country_filter:
            country_condition = f"AND c.country = {sql_literal(country_filter)}"

        if ranking_type == "finds":
            return f"""
            SELECT COUNT(*)::int AS player_count
            FROM (
              SELECT l.user_name AS name, COUNT(DISTINCT l.gc_code)::int AS score
              FROM logs l
              JOIN caches c ON c.code = l.gc_code
              WHERE l.user_name IS NOT NULL AND l.user_name <> ''
                AND {date_condition}
                AND {cache_join_condition}
                {country_condition}
              GROUP BY l.user_name
              HAVING COUNT(DISTINCT l.gc_code) > 0
            ) ranked;
            """

        if ranking_type == "ftf":
            return f"""
            SELECT COUNT(*)::int AS player_count
            FROM (
              SELECT l.user_name AS name, COUNT(*)::int AS score
              FROM logs l
              JOIN caches c ON c.code = l.gc_code
              WHERE l.user_name IS NOT NULL AND l.user_name <> ''
                AND l.is_ftf IS TRUE
                AND {date_condition}
                AND {cache_join_condition}
                {country_condition}
              GROUP BY l.user_name
              HAVING COUNT(*) > 0
            ) ranked;
            """

        if ranking_type == "hides":
            return f"""
            SELECT COUNT(*)::int AS player_count
            FROM (
              SELECT c.owner_username AS name, COUNT(*)::int AS score
              FROM caches c
              WHERE {OWNER_USERNAME_FILTER}
                AND {date_condition}
                AND {cache_where_condition}
                {country_condition}
              GROUP BY c.owner_username
              HAVING COUNT(*) > 0
            ) ranked;
            """

        if ranking_type == "logs":
            return f"""
            SELECT COUNT(*)::int AS player_count
            FROM (
              SELECT c.owner_username AS name, COUNT(l.*)::int AS score
              FROM caches c
              JOIN logs l ON l.gc_code = c.code
              WHERE {OWNER_USERNAME_FILTER}
                AND l.user_name IS NOT NULL
                AND LOWER(l.user_name) <> LOWER(c.owner_username)
                AND {date_condition}
                AND {cache_join_condition}
                {country_condition}
              GROUP BY c.owner_username
              HAVING COUNT(l.*) > 0
            ) ranked;
            """

        if ranking_type == "favorites":
            return f"""
            SELECT COUNT(*)::int AS player_count
            FROM (
              SELECT c.owner_username AS name, COUNT(l.*)::int AS score
              FROM caches c
              JOIN logs l ON l.gc_code = c.code
              WHERE {OWNER_USERNAME_FILTER}
                AND l.favorite_point_used IS TRUE
                AND {date_condition}
                AND {cache_join_condition}
                {country_condition}
              GROUP BY c.owner_username
              HAVING COUNT(l.*) > 0
            ) ranked;
            """

        raise ValueError(f"Unknown ranking type: {ranking_type}")

    def generate_ranking_query(
        self,
        ranking_type: str,
        time_range: str,
        is_city_ranking: bool = False,
        limit: int = 30,
        country_filter: Optional[str] = None,
    ) -> str:
        """Generate SQL query for rankings based on type and time range."""
        cache_join_condition = self.generate_ranking_cache_condition(time_range, join_clause=True)
        cache_where_condition = self.generate_ranking_cache_condition(time_range, join_clause=False)
        placed_date_condition = self.generate_ranking_stats_date_condition(time_range, "c.placed_date")
        visited_date_condition = self.generate_ranking_stats_date_condition(time_range, "l.visited")

        country_condition = ""
        if country_filter:
            country_condition = f"AND c.country = {sql_literal(country_filter)}"

        if ranking_type == "hides":
            if is_city_ranking:
                return f"""
                SELECT name, subtitle, COUNT(*)::int AS score
                FROM (
                  SELECT
                    COALESCE(NULLIF(TRIM(c.city), ''), c.country) AS name,
                    c.country AS subtitle
                  FROM caches c
                  WHERE COALESCE(NULLIF(TRIM(c.city), ''), c.country) IS NOT NULL
                    AND {placed_date_condition}
                    AND {cache_where_condition}
                    {country_condition}
                ) AS sub
                GROUP BY name, subtitle
                ORDER BY score DESC, name ASC
                LIMIT {limit};
                """
            else:
                return f"""
                SELECT owner_username AS name, COUNT(*)::int AS score
                FROM caches c
                WHERE {OWNER_USERNAME_FILTER}
                  AND {placed_date_condition}
                  AND {cache_where_condition}
                  {country_condition}
                GROUP BY c.owner_username
                ORDER BY score DESC, c.owner_username ASC
                LIMIT {limit};
                """

        elif ranking_type == "finds":
            if is_city_ranking:
                return f"""
                SELECT name, subtitle, COUNT(*)::int AS score
                FROM (
                  SELECT
                    COALESCE(NULLIF(TRIM(c.city), ''), c.country) AS name,
                    c.country AS subtitle
                  FROM logs l
                  JOIN caches c ON c.code = l.gc_code
                  WHERE COALESCE(NULLIF(TRIM(c.city), ''), c.country) IS NOT NULL
                    AND {visited_date_condition}
                    AND {cache_join_condition}
                    {country_condition}
                ) AS sub
                GROUP BY name, subtitle
                ORDER BY score DESC, name ASC
                LIMIT {limit};
                """
            else:
                return f"""
                SELECT user_name AS name, COUNT(DISTINCT gc_code)::int AS score
                FROM logs l
                JOIN caches c ON c.code = l.gc_code
                WHERE l.user_name IS NOT NULL AND l.user_name <> ''
                  AND {visited_date_condition}
                  AND {cache_join_condition}
                  {country_condition}
                GROUP BY l.user_name
                ORDER BY score DESC, l.user_name ASC
                LIMIT {limit};
                """

        elif ranking_type == "ftf":
            if is_city_ranking:
                raise ValueError("FTF ranking is only supported for player rankings")

            return f"""
            SELECT l.user_name AS name, COUNT(*)::int AS score
            FROM logs l
            JOIN caches c ON c.code = l.gc_code
            WHERE l.user_name IS NOT NULL AND l.user_name <> ''
              AND l.is_ftf IS TRUE
              AND {visited_date_condition}
              AND {cache_join_condition}
              {country_condition}
            GROUP BY l.user_name
            ORDER BY score DESC, l.user_name ASC
            LIMIT {limit};
            """

        elif ranking_type == "favorites":
            if is_city_ranking:
                return f"""
                SELECT name, subtitle, COUNT(*)::int AS score
                FROM (
                  SELECT
                    COALESCE(NULLIF(TRIM(c.city), ''), c.country) AS name,
                    c.country AS subtitle
                  FROM logs l
                  JOIN caches c ON c.code = l.gc_code
                  WHERE COALESCE(NULLIF(TRIM(c.city), ''), c.country) IS NOT NULL
                    AND l.favorite_point_used IS TRUE
                    AND {visited_date_condition}
                    AND {cache_join_condition}
                    {country_condition}
                ) AS sub
                GROUP BY name, subtitle
                ORDER BY score DESC, name ASC
                LIMIT {limit};
                """
            else:
                return f"""
                SELECT c.owner_username AS name, COUNT(l.*)::int AS score
                FROM caches c
                JOIN logs l ON l.gc_code = c.code
                WHERE {OWNER_USERNAME_FILTER}
                  AND l.favorite_point_used IS TRUE
                  AND {visited_date_condition}
                  AND {cache_join_condition}
                  {country_condition}
                GROUP BY c.owner_username
                ORDER BY score DESC, c.owner_username ASC
                LIMIT {limit};
                """

        elif ranking_type == "logs":
            if is_city_ranking:
                return f"""
                SELECT name, subtitle, COUNT(*)::int AS score
                FROM (
                  SELECT
                    COALESCE(NULLIF(TRIM(c.city), ''), c.country) AS name,
                    c.country AS subtitle
                  FROM logs l
                  JOIN caches c ON c.code = l.gc_code
                  WHERE COALESCE(NULLIF(TRIM(c.city), ''), c.country) IS NOT NULL
                    AND {visited_date_condition}
                    AND {cache_join_condition}
                  {country_condition}
                ) AS sub
                GROUP BY name, subtitle
                ORDER BY score DESC, name ASC
                LIMIT {limit};
                """
            else:
                return f"""
                SELECT
                  c.owner_username AS name,
                  COUNT(l.*)::int AS score
                FROM caches c
                JOIN logs l ON l.gc_code = c.code
                WHERE {OWNER_USERNAME_FILTER}
                  AND l.user_name IS NOT NULL
                  AND LOWER(l.user_name) <> LOWER(c.owner_username)
                  AND {visited_date_condition}
                  AND {cache_join_condition}
                  {country_condition}
                GROUP BY c.owner_username
                ORDER BY score DESC, c.owner_username ASC
                LIMIT {limit};
                """

        raise ValueError(f"Unknown ranking type: {ranking_type}")

    def generate_previous_period_query(
        self,
        ranking_type: str,
        time_range: str,
        is_city_ranking: bool = False,
        limit: int = 999999,
        country_filter: Optional[str] = None,
    ) -> str:
        """Generate SQL query for previous period (for trend calculation).

        Previous period definitions:
        - 30d: Previous 30-60 days
        - ytd: Previous full calendar year
        - active: Same as all, but still using the active-cache scope
        - all: All time up to end of last year (Dec 31)
        """
        date_col = "c.placed_date" if ranking_type == "hides" else "l.visited"
        date_condition = self.generate_ranking_stats_date_condition(
            time_range,
            date_col,
            previous=True,
        )
        date_filter = f"AND {date_condition}"
        cache_join_condition = self.generate_ranking_cache_condition(time_range, join_clause=True)
        cache_where_condition = self.generate_ranking_cache_condition(time_range, join_clause=False)

        country_condition = ""
        if country_filter:
            country_condition = f"AND c.country = {sql_literal(country_filter)}"

        # Reuse the same query structure but with previous period filter
        if ranking_type == "hides":
            if is_city_ranking:
                return f"""
                SELECT name, subtitle, COUNT(*)::int AS score
                FROM (
                  SELECT
                    COALESCE(NULLIF(TRIM(c.city), ''), c.country) AS name,
                    c.country AS subtitle
                  FROM caches c
                  WHERE COALESCE(NULLIF(TRIM(c.city), ''), c.country) IS NOT NULL
                    {date_filter}
                    AND {cache_where_condition}
                    {country_condition}
                ) AS sub
                GROUP BY name, subtitle
                ORDER BY score DESC, name ASC
                LIMIT {limit};
                """
            else:
                return f"""
                SELECT owner_username AS name, COUNT(*)::int AS score
                FROM caches c
                WHERE {OWNER_USERNAME_FILTER}
                  {date_filter}
                  AND {cache_where_condition}
                  {country_condition}
                GROUP BY c.owner_username
                ORDER BY score DESC, c.owner_username ASC
                LIMIT {limit};
                """

        elif ranking_type == "finds":
            if is_city_ranking:
                return f"""
                SELECT name, subtitle, COUNT(*)::int AS score
                FROM (
                  SELECT
                    COALESCE(NULLIF(TRIM(c.city), ''), c.country) AS name,
                    c.country AS subtitle
                  FROM logs l
                  JOIN caches c ON c.code = l.gc_code
                  WHERE COALESCE(NULLIF(TRIM(c.city), ''), c.country) IS NOT NULL
                    {date_filter}
                    AND {cache_join_condition}
                    {country_condition}
                ) AS sub
                GROUP BY name, subtitle
                ORDER BY score DESC, name ASC
                LIMIT {limit};
                """
            else:
                return f"""
                SELECT user_name AS name, COUNT(DISTINCT gc_code)::int AS score
                FROM logs l
                JOIN caches c ON c.code = l.gc_code
                WHERE l.user_name IS NOT NULL AND l.user_name <> ''
                  {date_filter}
                  AND {cache_join_condition}
                  {country_condition}
                GROUP BY l.user_name
                ORDER BY score DESC, l.user_name ASC
                LIMIT {limit};
                """

        elif ranking_type == "ftf":
            if is_city_ranking:
                raise ValueError("FTF ranking is only supported for player rankings")

            return f"""
            SELECT l.user_name AS name, COUNT(*)::int AS score
            FROM logs l
            JOIN caches c ON c.code = l.gc_code
            WHERE l.user_name IS NOT NULL AND l.user_name <> ''
              AND l.is_ftf IS TRUE
              {date_filter}
              AND {cache_join_condition}
              {country_condition}
            GROUP BY l.user_name
            ORDER BY score DESC, l.user_name ASC
            LIMIT {limit};
            """

        elif ranking_type == "favorites":
            if is_city_ranking:
                return f"""
                SELECT name, subtitle, COUNT(*)::int AS score
                FROM (
                  SELECT
                    COALESCE(NULLIF(TRIM(c.city), ''), c.country) AS name,
                    c.country AS subtitle
                  FROM logs l
                  JOIN caches c ON c.code = l.gc_code
                  WHERE COALESCE(NULLIF(TRIM(c.city), ''), c.country) IS NOT NULL
                    AND l.favorite_point_used IS TRUE
                    {date_filter}
                    AND {cache_join_condition}
                    {country_condition}
                ) AS sub
                GROUP BY name, subtitle
                ORDER BY score DESC, name ASC
                LIMIT {limit};
                """
            else:
                return f"""
                SELECT c.owner_username AS name, COUNT(l.*)::int AS score
                FROM caches c
                JOIN logs l ON l.gc_code = c.code
                WHERE {OWNER_USERNAME_FILTER}
                  AND l.favorite_point_used IS TRUE
                  {date_filter}
                  AND {cache_join_condition}
                  {country_condition}
                GROUP BY c.owner_username
                ORDER BY score DESC, c.owner_username ASC
                LIMIT {limit};
                """

        elif ranking_type == "logs":
            if is_city_ranking:
                return f"""
                SELECT name, subtitle, COUNT(*)::int AS score
                FROM (
                  SELECT
                    COALESCE(NULLIF(TRIM(c.city), ''), c.country) AS name,
                    c.country AS subtitle
                  FROM logs l
                  JOIN caches c ON c.code = l.gc_code
                  WHERE COALESCE(NULLIF(TRIM(c.city), ''), c.country) IS NOT NULL
                    {date_filter}
                    AND {cache_join_condition}
                    {country_condition}
                ) AS sub
                GROUP BY name, subtitle
                ORDER BY score DESC, name ASC
                LIMIT {limit};
                """
            else:
                return f"""
                SELECT
                  c.owner_username AS name,
                  COUNT(l.*)::int AS score
                FROM caches c
                JOIN logs l ON l.gc_code = c.code
                WHERE {OWNER_USERNAME_FILTER}
                  AND l.user_name IS NOT NULL
                  AND LOWER(l.user_name) <> LOWER(c.owner_username)
                  {date_filter}
                  AND {cache_join_condition}
                  {country_condition}
                GROUP BY c.owner_username
                ORDER BY score DESC, c.owner_username ASC
                LIMIT {limit};
                """

        raise ValueError(f"Unknown ranking type: {ranking_type}")

    def generate_rankings(
        self,
        ranking_types: List[str],
        time_ranges: List[str],
        is_city_ranking: bool = False,
        limit: int = 30,
        country_filter: Optional[str] = None,
    ) -> Dict:
        """Generate all rankings data with optimized trend calculation and tied rank support."""
        rankings = {}

        for rtype in ranking_types:
            rankings[rtype] = {}
            for trange in time_ranges:
                logger.debug(f"Generating {rtype}/{trange} ranking...")
                
                # Get current period rankings (fetch more to handle ties at the boundary)
                query = self.generate_ranking_query(
                    rtype,
                    trange,
                    is_city_ranking,
                    limit=limit * 2,
                    country_filter=country_filter,
                )
                results = self.execute_query(query)

                # Filter out zero-score entries
                results = [r for r in results if (r["score"] or 0) > 0]

                # Get previous period rankings once (not per-entry!)
                prev_ranks = {}
                try:
                    prev_query = self.generate_previous_period_query(
                        rtype,
                        trange,
                        is_city_ranking,
                        country_filter=country_filter,
                    )
                    prev_results = self.execute_query(prev_query)
                    prev_results = [r for r in prev_results if (r["score"] or 0) > 0]
                    prev_ranks = self.build_rank_lookup(prev_results)
                    logger.debug(f"  Got {len(prev_results)} previous period entries for {rtype}/{trange}")
                except Exception as e:
                    logger.warning(f"Failed to get previous period data for {rtype}/{trange}: {e}")

                # Calculate tied ranks and apply display limit rules
                ranked_results = []
                display_rank = 0  # The actual displayed rank number
                prev_score = None
                
                for i, row in enumerate(results):
                    score = row["score"] or 0
                    
                    # Calculate display rank with tie handling
                    if i == 0 or score != prev_score:
                        # New score group: advance display rank
                        display_rank = i + 1
                    # If same score as previous: keep same display_rank (ties)
                    
                    # Check if we should include this entry
                    # Rule: Always include if within limit, OR if tied with last included entry
                    should_include = len(ranked_results) < limit
                    
                    if not should_include and ranked_results:
                        # Check if this entry is tied with the last included entry
                        last_included_score = ranked_results[-1]["score"]
                        if score == last_included_score:
                            should_include = True
                    
                    if should_include:
                        entry = {
                            "rank": display_rank,  # Tied rank number
                            "name": row["name"],
                            "score": score,
                        }

                        if is_city_ranking:
                            entry["subtitle"] = self.format_city_subtitle(
                                entry["name"], row.get("subtitle", "")
                            )

                        # Calculate trend based on rank difference
                        previous_rank = prev_ranks.get(entry["name"], 0)  # 0 means not in previous rankings
                        trend, trend_delta = self.calculate_trend(display_rank, previous_rank)
                        entry["trend"] = trend
                        entry["trendDelta"] = trend_delta

                        ranked_results.append(entry)
                    
                    prev_score = score

                logger.debug(f"  Returning {len(ranked_results)} entries for {rtype}/{trange} (limit={limit})")
                rankings[rtype][trange] = ranked_results

        return rankings

    def generate_ranking_stats(
        self,
        ranking_types: List[str],
        time_ranges: List[str],
        country_filter: Optional[str] = None,
    ) -> Dict:
        """Generate player counts and growth percentages for each ranking filter."""
        stats = {}

        for rtype in ranking_types:
            stats[rtype] = {}
            for trange in time_ranges:
                current_query = self.generate_ranking_count_query(
                    rtype,
                    trange,
                    country_filter=country_filter,
                )
                previous_query = self.generate_ranking_count_query(
                    rtype,
                    trange,
                    previous=True,
                    country_filter=country_filter,
                )

                current_result = self.execute_query(current_query)
                previous_result = self.execute_query(previous_query)

                player_count = current_result[0]["player_count"] if current_result else 0
                previous_count = previous_result[0]["player_count"] if previous_result else 0

                growth_pct = 0
                if previous_count > 0:
                    growth_pct = round((player_count - previous_count) / previous_count * 100, 1)

                stats[rtype][trange] = {
                    "playerCount": player_count,
                    "playerCountGrowthPct": growth_pct,
                }

        return stats

    def generate_rankings_by_region(
        self,
        ranking_types: List[str],
        time_ranges: List[str],
        limit: int,
    ) -> Dict[str, Dict]:
        """Generate player rankings for all supported region filters."""
        rankings_by_region = {
            "all": self.generate_rankings(ranking_types, time_ranges, limit=limit)
        }
        for region_key, country in REGION_COUNTRY_MAP.items():
            rankings_by_region[region_key] = self.generate_rankings(
                ranking_types,
                time_ranges,
                limit=limit,
                country_filter=country,
            )
        return rankings_by_region

    def generate_ranking_stats_by_region(
        self,
        ranking_types: List[str],
        time_ranges: List[str],
    ) -> Dict[str, Dict]:
        """Generate ranking stats for all supported region filters."""
        stats_by_region = {
            "all": self.generate_ranking_stats(ranking_types, time_ranges)
        }
        for region_key, country in REGION_COUNTRY_MAP.items():
            stats_by_region[region_key] = self.generate_ranking_stats(
                ranking_types,
                time_ranges,
                country_filter=country,
            )
        return stats_by_region

    def generate_newbie_ranking_query(
        self,
        ranking_type: str,
        limit: int = 50,
        country_filter: Optional[str] = None,
    ) -> str:
        """Generate SQL query for no-time-range newbie rankings."""
        country_condition = ""
        if country_filter:
            country_condition = f"AND c.country = {sql_literal(country_filter)}"

        if ranking_type == "finds":
            return f"""
            SELECT l.user_name AS name, COUNT(DISTINCT l.gc_code)::int AS score
            FROM logs l
            JOIN caches c ON c.code = l.gc_code
            JOIN "user" u ON LOWER(u.user_name) = LOWER(TRIM(l.user_name))
            WHERE l.user_name IS NOT NULL AND l.user_name <> ''
              AND {NEWBIE_REGISTRATION_FILTER}
              AND {EXCLUDE_CACHE_JOIN}
              {country_condition}
            GROUP BY l.user_name
            ORDER BY score DESC, l.user_name ASC
            LIMIT {limit};
            """

        if ranking_type == "hides":
            return f"""
            SELECT c.owner_username AS name, COUNT(*)::int AS score
            FROM caches c
            JOIN "user" u ON LOWER(u.user_name) = LOWER(TRIM(c.owner_username))
            WHERE {OWNER_USERNAME_FILTER}
              AND {NEWBIE_REGISTRATION_FILTER}
              AND {EXCLUDE_CACHE_WHERE}
              {country_condition}
            GROUP BY c.owner_username
            ORDER BY score DESC, c.owner_username ASC
            LIMIT {limit};
            """

        raise ValueError(f"Unknown newbie ranking type: {ranking_type}")

    def generate_newbie_ranking_count_query(
        self,
        ranking_type: str,
        country_filter: Optional[str] = None,
        previous: bool = False,
    ) -> str:
        """Generate player count query for newbie ranking stats."""
        country_condition = ""
        if country_filter:
            country_condition = f"AND c.country = {sql_literal(country_filter)}"
        registration_filter = (
            PREVIOUS_NEWBIE_REGISTRATION_FILTER
            if previous
            else NEWBIE_REGISTRATION_FILTER
        )

        if ranking_type == "finds":
            return f"""
            SELECT COUNT(*)::int AS player_count
            FROM (
              SELECT l.user_name AS name, COUNT(DISTINCT l.gc_code)::int AS score
              FROM logs l
              JOIN caches c ON c.code = l.gc_code
              JOIN "user" u ON LOWER(u.user_name) = LOWER(TRIM(l.user_name))
              WHERE l.user_name IS NOT NULL AND l.user_name <> ''
                AND {registration_filter}
                AND {EXCLUDE_CACHE_JOIN}
                {country_condition}
              GROUP BY l.user_name
              HAVING COUNT(DISTINCT l.gc_code) > 0
            ) ranked;
            """

        if ranking_type == "hides":
            return f"""
            SELECT COUNT(*)::int AS player_count
            FROM (
              SELECT c.owner_username AS name, COUNT(*)::int AS score
              FROM caches c
              JOIN "user" u ON LOWER(u.user_name) = LOWER(TRIM(c.owner_username))
              WHERE {OWNER_USERNAME_FILTER}
                AND {registration_filter}
                AND {EXCLUDE_CACHE_WHERE}
                {country_condition}
              GROUP BY c.owner_username
              HAVING COUNT(*) > 0
            ) ranked;
            """

        raise ValueError(f"Unknown newbie ranking type: {ranking_type}")

    def format_ranked_rows_without_trend_period(self, rows: List[Dict], limit: int) -> List[Dict]:
        """Apply tied ranks for rankings that do not have a meaningful previous period."""
        ranked_results = []
        display_rank = 0
        prev_score = None

        for index, row in enumerate([r for r in rows if (r["score"] or 0) > 0]):
            score = row["score"] or 0
            if index == 0 or score != prev_score:
                display_rank = index + 1

            should_include = len(ranked_results) < limit
            if not should_include and ranked_results:
                should_include = score == ranked_results[-1]["score"]

            if should_include:
                ranked_results.append(
                    {
                        "rank": display_rank,
                        "name": row["name"],
                        "score": score,
                        "trend": "flat",
                        "trendDelta": 0,
                    }
                )

            prev_score = score

        return ranked_results

    def generate_newbie_rankings(
        self,
        limit: int = 50,
        country_filter: Optional[str] = None,
    ) -> Dict:
        """Generate newbie rankings without time-range dimensions."""
        rankings = {}
        for ranking_type in ["finds", "hides"]:
            query = self.generate_newbie_ranking_query(
                ranking_type,
                limit=limit * 2,
                country_filter=country_filter,
            )
            results = self.execute_query(query)
            rankings[ranking_type] = self.format_ranked_rows_without_trend_period(results, limit)
        return rankings

    def generate_newbie_ranking_stats(
        self,
        country_filter: Optional[str] = None,
    ) -> Dict:
        """Generate newbie player counts and growth versus the previous registration year."""
        stats = {}
        for ranking_type in ["finds", "hides"]:
            current_query = self.generate_newbie_ranking_count_query(
                ranking_type,
                country_filter=country_filter,
            )
            previous_query = self.generate_newbie_ranking_count_query(
                ranking_type,
                country_filter=country_filter,
                previous=True,
            )
            current_result = self.execute_query(current_query)
            previous_result = self.execute_query(previous_query)
            player_count = current_result[0]["player_count"] if current_result else 0
            previous_count = previous_result[0]["player_count"] if previous_result else 0
            growth_pct = None
            if previous_count > 0:
                growth_pct = round((player_count - previous_count) / previous_count * 100, 1)

            stats[ranking_type] = {
                "playerCount": player_count,
                "playerCountGrowthPct": growth_pct,
            }
        return stats

    def generate_newbie_rankings_by_region(self, limit: int = 50) -> Dict[str, Dict]:
        """Generate newbie rankings for all supported region filters."""
        rankings_by_region = {
            "all": self.generate_newbie_rankings(limit=limit)
        }
        for region_key, country in REGION_COUNTRY_MAP.items():
            rankings_by_region[region_key] = self.generate_newbie_rankings(
                limit=limit,
                country_filter=country,
            )
        return rankings_by_region

    def generate_newbie_ranking_stats_by_region(self) -> Dict[str, Dict]:
        """Generate newbie ranking stats for all supported region filters."""
        stats_by_region = {
            "all": self.generate_newbie_ranking_stats()
        }
        for region_key, country in REGION_COUNTRY_MAP.items():
            stats_by_region[region_key] = self.generate_newbie_ranking_stats(
                country_filter=country,
            )
        return stats_by_region

    def generate_event_ranking_query(
        self,
        ranking_type: str,
        limit: int = 50,
        country_filter: Optional[str] = None,
        previous_year: bool = False,
        previous_year_window: bool = False,
    ) -> str:
        """Generate SQL query for event rankings."""
        country_condition = ""
        if country_filter:
            country_condition = f"AND c.country = {sql_literal(country_filter)}"

        if ranking_type == "hosts":
            date_condition = ""
            if previous_year_window:
                date_condition = (
                    "AND c.placed_date >= date_trunc('year', CURRENT_DATE - INTERVAL '1 year')::date "
                    "AND c.placed_date < date_trunc('year', CURRENT_DATE)::date"
                )
            elif previous_year:
                date_condition = (
                    "AND c.placed_date < date_trunc('year', CURRENT_DATE)::date"
                )
            return f"""
            SELECT c.owner_username AS name, COUNT(DISTINCT c.code)::int AS score
            FROM caches c
            WHERE {OWNER_USERNAME_FILTER}
              AND {EVENT_CACHE_WHERE}
              {country_condition}
              {date_condition}
            GROUP BY c.owner_username
            ORDER BY score DESC, c.owner_username ASC
            LIMIT {limit};
            """

        if ranking_type == "participants":
            date_condition = ""
            if previous_year_window:
                date_condition = (
                    "AND l.visited >= date_trunc('year', CURRENT_DATE - INTERVAL '1 year')::date "
                    "AND l.visited < date_trunc('year', CURRENT_DATE)::date"
                )
            elif previous_year:
                date_condition = (
                    "AND l.visited < date_trunc('year', CURRENT_DATE)::date"
                )
            return f"""
            SELECT l.user_name AS name, COUNT(DISTINCT l.gc_code)::int AS score
            FROM logs l
            JOIN caches c ON c.code = l.gc_code
            WHERE l.user_name IS NOT NULL AND l.user_name <> ''
              AND l.log_type = 'Attended'
              AND {EVENT_CACHE_JOIN}
              {country_condition}
              {date_condition}
            GROUP BY l.user_name
            ORDER BY score DESC, l.user_name ASC
            LIMIT {limit};
            """

        raise ValueError(f"Unknown event ranking type: {ranking_type}")

    def generate_event_ranking_count_query(
        self,
        ranking_type: str,
        country_filter: Optional[str] = None,
        previous_year: bool = False,
        previous_year_window: bool = False,
    ) -> str:
        """Generate player count query for event ranking stats."""
        ranking_query = self.generate_event_ranking_query(
            ranking_type,
            limit=999999,
            country_filter=country_filter,
            previous_year=previous_year,
            previous_year_window=previous_year_window,
        ).rstrip().rstrip(";")
        return f"""
        SELECT COUNT(*)::int AS player_count
        FROM (
          {ranking_query}
        ) ranked
        WHERE score > 0;
        """

    def format_ranked_rows_with_previous_period(
        self,
        current_rows: List[Dict],
        previous_rows: List[Dict],
        limit: int,
    ) -> List[Dict]:
        """Apply tied ranks and trend calculation for no-time-dimension rankings."""
        current_rows = [r for r in current_rows if (r["score"] or 0) > 0]
        previous_rows = [r for r in previous_rows if (r["score"] or 0) > 0]
        previous_ranks = self.build_rank_lookup(previous_rows)

        ranked_results = []
        display_rank = 0
        prev_score = None

        for index, row in enumerate(current_rows):
            score = row["score"] or 0
            if index == 0 or score != prev_score:
                display_rank = index + 1

            should_include = len(ranked_results) < limit
            if not should_include and ranked_results:
                should_include = score == ranked_results[-1]["score"]

            if should_include:
                trend, trend_delta = self.calculate_trend(
                    display_rank,
                    previous_ranks.get(row["name"], 0),
                )
                ranked_results.append(
                    {
                        "rank": display_rank,
                        "name": row["name"],
                        "score": score,
                        "trend": trend,
                        "trendDelta": trend_delta,
                    }
                )

            prev_score = score

        return ranked_results

    def generate_event_rankings(
        self,
        limit: int = 50,
        country_filter: Optional[str] = None,
    ) -> Dict:
        """Generate event rankings without time-range dimensions."""
        rankings = {}
        for ranking_type in ["hosts", "participants"]:
            current_rows = self.execute_query(
                self.generate_event_ranking_query(
                    ranking_type,
                    limit=limit * 2,
                    country_filter=country_filter,
                )
            )
            previous_rows = self.execute_query(
                self.generate_event_ranking_query(
                    ranking_type,
                    limit=999999,
                    country_filter=country_filter,
                    previous_year=True,
                )
            )
            rankings[ranking_type] = self.format_ranked_rows_with_previous_period(
                current_rows,
                previous_rows,
                limit,
            )
        return rankings

    def generate_event_ranking_stats(
        self,
        country_filter: Optional[str] = None,
    ) -> Dict:
        """Generate event player counts and growth versus cumulative counts before this year."""
        stats = {}
        for ranking_type in ["hosts", "participants"]:
            current_result = self.execute_query(
                self.generate_event_ranking_count_query(
                    ranking_type,
                    country_filter=country_filter,
                )
            )
            previous_result = self.execute_query(
                self.generate_event_ranking_count_query(
                    ranking_type,
                    country_filter=country_filter,
                    previous_year=True,
                )
            )
            player_count = current_result[0]["player_count"] if current_result else 0
            previous_count = previous_result[0]["player_count"] if previous_result else 0
            growth_pct = None
            if previous_count > 0:
                growth_pct = round((player_count - previous_count) / previous_count * 100, 1)

            stats[ranking_type] = {
                "playerCount": player_count,
                "playerCountGrowthPct": growth_pct,
            }
        return stats

    def generate_event_rankings_by_region(self, limit: int = 50) -> Dict[str, Dict]:
        """Generate event rankings for all supported region filters."""
        rankings_by_region = {
            "all": self.generate_event_rankings(limit=limit)
        }
        for region_key, country in REGION_COUNTRY_MAP.items():
            rankings_by_region[region_key] = self.generate_event_rankings(
                limit=limit,
                country_filter=country,
            )
        return rankings_by_region

    def generate_event_ranking_stats_by_region(self) -> Dict[str, Dict]:
        """Generate event ranking stats for all supported region filters."""
        stats_by_region = {
            "all": self.generate_event_ranking_stats()
        }
        for region_key, country in REGION_COUNTRY_MAP.items():
            stats_by_region[region_key] = self.generate_event_ranking_stats(
                country_filter=country,
            )
        return stats_by_region

    def add_avatar_urls_to_rankings_by_region(self, rankings_by_region: Dict[str, Dict]) -> Dict[str, Dict]:
        """Attach avatarUrl to every player ranking entry in every region."""
        user_names = []
        for rankings in rankings_by_region.values():
            for ranking_type in rankings.values():
                for entries in ranking_type.values():
                    user_names.extend(entry["name"] for entry in entries if entry.get("name"))

        avatar_urls = self.resolve_avatar_urls(user_names)
        for rankings in rankings_by_region.values():
            for ranking_type in rankings.values():
                for entries in ranking_type.values():
                    for entry in entries:
                        entry["avatarUrl"] = avatar_urls.get(entry.get("name"), DEFAULT_AVATAR_URL)

        return rankings_by_region

    def add_avatar_urls_to_newbie_rankings_by_region(self, rankings_by_region: Dict[str, Dict]) -> Dict[str, Dict]:
        """Attach avatarUrl to every newbie ranking entry in every region."""
        user_names = []
        for rankings in rankings_by_region.values():
            for entries in rankings.values():
                user_names.extend(entry["name"] for entry in entries if entry.get("name"))

        avatar_urls = self.resolve_avatar_urls(user_names)
        for rankings in rankings_by_region.values():
            for entries in rankings.values():
                for entry in entries:
                    entry["avatarUrl"] = avatar_urls.get(entry.get("name"), DEFAULT_AVATAR_URL)

        return rankings_by_region

    def add_avatar_urls_to_event_rankings_by_region(self, rankings_by_region: Dict[str, Dict]) -> Dict[str, Dict]:
        """Attach avatarUrl to every event ranking entry in every region."""
        return self.add_avatar_urls_to_newbie_rankings_by_region(rankings_by_region)

    def generate_community_stats(self) -> Dict:
        """Generate community statistics."""
        # Active players (last 30 days)
        active_query = f"""
        SELECT COUNT(DISTINCT l.user_name)::int AS count
        FROM logs l
        JOIN caches c ON c.code = l.gc_code
        WHERE l.visited::date >= CURRENT_DATE - INTERVAL '30 day'
          AND {EXCLUDE_CACHE_JOIN};
        """
        active_result = self.execute_query(active_query)
        active_players = active_result[0]["count"] if active_result else 0

        # Previous month active players
        prev_active_query = f"""
        SELECT COUNT(DISTINCT l.user_name)::int AS count
        FROM logs l
        JOIN caches c ON c.code = l.gc_code
        WHERE l.visited::date >= CURRENT_DATE - INTERVAL '60 day'
          AND l.visited::date < CURRENT_DATE - INTERVAL '30 day'
          AND {EXCLUDE_CACHE_JOIN};
        """
        prev_active_result = self.execute_query(prev_active_query)
        prev_active_players = prev_active_result[0]["count"] if prev_active_result else 0

        # Growth calculation
        growth_pct = 0
        if prev_active_players > 0:
            growth_pct = round((active_players - prev_active_players) / prev_active_players * 100, 1)

        # Total caches
        total_query = f"SELECT COUNT(*)::int AS count FROM caches c WHERE {EXCLUDE_CACHE_WHERE};"
        total_result = self.execute_query(total_query)
        total_caches = total_result[0]["count"] if total_result else 0

        # Found cache coverage
        found_query = f"""
        SELECT COUNT(DISTINCT l.gc_code)::int AS total_found
        FROM logs l
        JOIN caches c ON c.code = l.gc_code
        WHERE {EXCLUDE_CACHE_JOIN};
        """
        found_result = self.execute_query(found_query)
        total_found = found_result[0]["total_found"] if found_result else 0

        coverage_pct = 0
        if total_caches > 0:
            coverage_pct = round(total_found / total_caches * 100, 1)

        return {
            "activePlayers": active_players,
            "activePlayersGrowthPct": growth_pct,
            "totalCaches": total_caches,
            "foundCacheCoveragePct": coverage_pct,
        }

    def generate_player_rankings_json(self) -> Dict:
        """Generate complete player-rankings.json data."""
        logger.info("Generating player-rankings.json...")

        ranking_types = ["finds", "ftf", "hides", "logs", "favorites"]
        time_ranges = RANKING_TIME_RANGES
        rankings_by_region = self.generate_rankings_by_region(
            ranking_types,
            time_ranges,
            limit=50,
        )
        ranking_stats_by_region = self.generate_ranking_stats_by_region(
            ranking_types,
            time_ranges,
        )
        newbie_rankings_by_region = self.generate_newbie_rankings_by_region(limit=50)
        newbie_ranking_stats_by_region = self.generate_newbie_ranking_stats_by_region()
        event_rankings_by_region = self.generate_event_rankings_by_region(limit=50)
        event_ranking_stats_by_region = self.generate_event_ranking_stats_by_region()
        community_stats = self.generate_community_stats()
        self.add_avatar_urls_to_rankings_by_region(rankings_by_region)
        self.add_avatar_urls_to_newbie_rankings_by_region(newbie_rankings_by_region)
        self.add_avatar_urls_to_event_rankings_by_region(event_rankings_by_region)
        rankings = rankings_by_region["all"]
        ranking_stats = ranking_stats_by_region["all"]
        newbie_rankings = newbie_rankings_by_region["all"]
        newbie_ranking_stats = newbie_ranking_stats_by_region["all"]
        event_rankings = event_rankings_by_region["all"]
        event_ranking_stats = event_ranking_stats_by_region["all"]

        return {
            "generatedAt": self.get_generated_at(),
            "rankings": rankings,
            "rankingsByRegion": rankings_by_region,
            "newbieRankings": newbie_rankings,
            "newbieRankingsByRegion": newbie_rankings_by_region,
            "eventRankings": event_rankings,
            "eventRankingsByRegion": event_rankings_by_region,
            "rankingStats": ranking_stats,
            "rankingStatsByRegion": ranking_stats_by_region,
            "newbieRankingStats": newbie_ranking_stats,
            "newbieRankingStatsByRegion": newbie_ranking_stats_by_region,
            "eventRankingStats": event_ranking_stats,
            "eventRankingStatsByRegion": event_ranking_stats_by_region,
            "communityStats": community_stats,
        }

    # ==================== CACHE-RANKINGS.JSON ====================

    def generate_cache_ranking_query(
        self,
        ranking_type: str,
        time_range: str,
        previous: bool = False,
        limit: int = 999999,
        country_filter: Optional[str] = None,
        cache_type_filter: Optional[int] = None,
    ) -> str:
        """Generate SQL query for cache rankings."""
        date_condition = self.generate_ranking_stats_date_condition(
            time_range,
            "l.visited",
            previous=previous,
        )
        cache_condition = self.generate_ranking_cache_condition(time_range, join_clause=True)
        country_condition = ""
        if country_filter:
            country_condition = f"AND c.country = {sql_literal(country_filter)}"
        cache_type_condition = ""
        if cache_type_filter is not None:
            cache_type_condition = f"AND c.geocache_type = {int(cache_type_filter)}"

        favorite_condition = ""
        if ranking_type == "favorites":
            favorite_condition = "AND l.favorite_point_used IS TRUE"
        elif ranking_type != "logs":
            raise ValueError(f"Unknown cache ranking type: {ranking_type}")

        return f"""
        SELECT
          c.code,
          c.name,
          c.owner_username AS owner,
          c.geocache_type AS geocache_type,
          COUNT(l.*)::int AS score
        FROM caches c
        JOIN logs l ON l.gc_code = c.code
        WHERE {CACHE_RANKING_ENTRY_FILTER}
          {favorite_condition}
          AND {date_condition}
          AND {cache_condition}
          {cache_type_condition}
          {country_condition}
        GROUP BY c.code, c.name, c.owner_username, c.geocache_type
        ORDER BY score DESC, c.code ASC
        LIMIT {limit};
        """

    def format_cache_type_label(self, geocache_type: Optional[int]) -> str:
        """Format raw geocache_type as a cache ranking type label."""
        if geocache_type is None:
            return "Unknown"
        try:
            type_id = int(geocache_type)
        except (TypeError, ValueError):
            return "Unknown"
        return CACHE_TYPE_LABELS.get(type_id, f"Type {type_id}")

    def generate_cache_ranking_count_query(
        self,
        ranking_type: str,
        time_range: str,
        previous: bool = False,
        country_filter: Optional[str] = None,
        cache_type_filter: Optional[int] = None,
    ) -> str:
        """Generate cache count query for cache ranking stats without leaderboard limits."""
        date_condition = self.generate_ranking_stats_date_condition(
            time_range,
            "l.visited",
            previous=previous,
        )
        cache_condition = self.generate_ranking_cache_condition(time_range, join_clause=True)
        country_condition = ""
        if country_filter:
            country_condition = f"AND c.country = {sql_literal(country_filter)}"
        cache_type_condition = ""
        if cache_type_filter is not None:
            cache_type_condition = f"AND c.geocache_type = {int(cache_type_filter)}"

        favorite_condition = ""
        if ranking_type == "favorites":
            favorite_condition = "AND l.favorite_point_used IS TRUE"
        elif ranking_type != "logs":
            raise ValueError(f"Unknown cache ranking type: {ranking_type}")

        return f"""
        SELECT COUNT(*)::int AS player_count
        FROM (
          SELECT c.code, COUNT(l.*)::int AS score
          FROM caches c
          JOIN logs l ON l.gc_code = c.code
          WHERE {CACHE_RANKING_ENTRY_FILTER}
            {favorite_condition}
            AND {date_condition}
            AND {cache_condition}
            {cache_type_condition}
            {country_condition}
          GROUP BY c.code
          HAVING COUNT(l.*) > 0
        ) ranked;
        """

    def generate_cache_rankings(
        self,
        ranking_types: List[str],
        time_ranges: List[str],
        limit: int = 50,
        country_filter: Optional[str] = None,
        cache_type_filter: Optional[int] = None,
    ) -> Dict:
        """Generate cache rankings with tied ranks and trend calculation."""
        rankings = {}

        for rtype in ranking_types:
            rankings[rtype] = {}
            for trange in time_ranges:
                logger.debug(f"Generating cache {rtype}/{trange} ranking...")

                query = self.generate_cache_ranking_query(
                    rtype,
                    trange,
                    limit=limit * 2,
                    country_filter=country_filter,
                    cache_type_filter=cache_type_filter,
                )
                results = self.execute_query(query)
                results = [r for r in results if (r["score"] or 0) > 0]

                prev_ranks = {}
                try:
                    prev_query = self.generate_cache_ranking_query(
                        rtype,
                        trange,
                        previous=True,
                        country_filter=country_filter,
                        cache_type_filter=cache_type_filter,
                    )
                    prev_results = self.execute_query(prev_query)
                    prev_results = [r for r in prev_results if (r["score"] or 0) > 0]
                    prev_ranks = self.build_rank_lookup(prev_results, key_field="code")
                    logger.debug(
                        f"  Got {len(prev_results)} previous period cache entries for {rtype}/{trange}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to get previous period cache data for {rtype}/{trange}: {e}")

                ranked_results = []
                display_rank = 0
                prev_score = None

                for i, row in enumerate(results):
                    score = row["score"] or 0
                    if i == 0 or score != prev_score:
                        display_rank = i + 1

                    should_include = len(ranked_results) < limit
                    if not should_include and ranked_results:
                        last_included_score = ranked_results[-1]["score"]
                        if score == last_included_score:
                            should_include = True

                    if should_include:
                        previous_rank = prev_ranks.get(row["code"], 0)
                        trend, trend_delta = self.calculate_trend(display_rank, previous_rank)
                        ranked_results.append({
                            "rank": display_rank,
                            "code": row["code"],
                            "name": row["name"],
                            "owner": row["owner"],
                            "typeLabel": self.format_cache_type_label(row.get("geocache_type")),
                            "geocacheType": row.get("geocache_type"),
                            "score": score,
                            "trend": trend,
                            "trendDelta": trend_delta,
                        })

                    prev_score = score

                logger.debug(f"  Returning {len(ranked_results)} cache entries for {rtype}/{trange}")
                rankings[rtype][trange] = ranked_results

        return rankings

    def generate_cache_ranking_stats(
        self,
        ranking_types: List[str],
        time_ranges: List[str],
        country_filter: Optional[str] = None,
        cache_type_filter: Optional[int] = None,
    ) -> Dict:
        """Generate cache counts and growth percentages for each cache ranking filter."""
        stats = {}

        for rtype in ranking_types:
            stats[rtype] = {}
            for trange in time_ranges:
                current_query = self.generate_cache_ranking_count_query(
                    rtype,
                    trange,
                    country_filter=country_filter,
                    cache_type_filter=cache_type_filter,
                )
                previous_query = self.generate_cache_ranking_count_query(
                    rtype,
                    trange,
                    previous=True,
                    country_filter=country_filter,
                    cache_type_filter=cache_type_filter,
                )

                current_result = self.execute_query(current_query)
                previous_result = self.execute_query(previous_query)

                cache_count = current_result[0]["player_count"] if current_result else 0
                previous_count = previous_result[0]["player_count"] if previous_result else 0

                growth_pct = None
                if previous_count > 0:
                    growth_pct = round((cache_count - previous_count) / previous_count * 100, 1)

                stats[rtype][trange] = {
                    "playerCount": cache_count,
                    "playerCountGrowthPct": growth_pct,
                }

        return stats

    def generate_cache_rankings_by_region(
        self,
        ranking_types: List[str],
        time_ranges: List[str],
        limit: int,
    ) -> Dict[str, Dict]:
        """Generate cache rankings for all supported region filters."""
        rankings_by_region = {
            "all": self.generate_cache_rankings(ranking_types, time_ranges, limit=limit)
        }
        for region_key, country in REGION_COUNTRY_MAP.items():
            rankings_by_region[region_key] = self.generate_cache_rankings(
                ranking_types,
                time_ranges,
                limit=limit,
                country_filter=country,
            )
        return rankings_by_region

    def generate_cache_ranking_stats_by_region(
        self,
        ranking_types: List[str],
        time_ranges: List[str],
    ) -> Dict[str, Dict]:
        """Generate cache ranking stats for all supported region filters."""
        stats_by_region = {
            "all": self.generate_cache_ranking_stats(ranking_types, time_ranges)
        }
        for region_key, country in REGION_COUNTRY_MAP.items():
            stats_by_region[region_key] = self.generate_cache_ranking_stats(
                ranking_types,
                time_ranges,
                country_filter=country,
            )
        return stats_by_region

    def generate_cache_rankings_by_cache_type(
        self,
        ranking_types: List[str],
        time_ranges: List[str],
        limit: int,
    ) -> Dict[str, Dict]:
        """Generate cache rankings for all supported cache type filters."""
        rankings_by_cache_type = {}
        for cache_type_key, cache_type_id in CACHE_TYPE_FILTERS.items():
            rankings_by_cache_type[cache_type_key] = self.generate_cache_rankings(
                ranking_types,
                time_ranges,
                limit=limit,
                cache_type_filter=cache_type_id,
            )
        return rankings_by_cache_type

    def generate_cache_ranking_stats_by_cache_type(
        self,
        ranking_types: List[str],
        time_ranges: List[str],
    ) -> Dict[str, Dict]:
        """Generate cache ranking stats for all supported cache type filters."""
        stats_by_cache_type = {}
        for cache_type_key, cache_type_id in CACHE_TYPE_FILTERS.items():
            stats_by_cache_type[cache_type_key] = self.generate_cache_ranking_stats(
                ranking_types,
                time_ranges,
                cache_type_filter=cache_type_id,
            )
        return stats_by_cache_type

    def generate_cache_rankings_by_region_and_cache_type(
        self,
        ranking_types: List[str],
        time_ranges: List[str],
        limit: int,
        rankings_by_region: Dict[str, Dict],
        rankings_by_cache_type: Dict[str, Dict],
    ) -> Dict[str, Dict]:
        """Generate cache rankings for combined region and cache type filters."""
        rankings_by_region_and_cache_type = {
            "all": rankings_by_cache_type,
        }
        for region_key, country in REGION_COUNTRY_MAP.items():
            rankings_by_region_and_cache_type[region_key] = {
                "all": rankings_by_region[region_key],
            }
            for cache_type_key, cache_type_id in CACHE_TYPE_FILTERS.items():
                if cache_type_key == "all":
                    continue
                rankings_by_region_and_cache_type[region_key][cache_type_key] = self.generate_cache_rankings(
                    ranking_types,
                    time_ranges,
                    limit=limit,
                    country_filter=country,
                    cache_type_filter=cache_type_id,
                )
        return rankings_by_region_and_cache_type

    def generate_cache_ranking_stats_by_region_and_cache_type(
        self,
        ranking_types: List[str],
        time_ranges: List[str],
        ranking_stats_by_region: Dict[str, Dict],
        ranking_stats_by_cache_type: Dict[str, Dict],
    ) -> Dict[str, Dict]:
        """Generate cache ranking stats for combined region and cache type filters."""
        stats_by_region_and_cache_type = {
            "all": ranking_stats_by_cache_type,
        }
        for region_key, country in REGION_COUNTRY_MAP.items():
            stats_by_region_and_cache_type[region_key] = {
                "all": ranking_stats_by_region[region_key],
            }
            for cache_type_key, cache_type_id in CACHE_TYPE_FILTERS.items():
                if cache_type_key == "all":
                    continue
                stats_by_region_and_cache_type[region_key][cache_type_key] = self.generate_cache_ranking_stats(
                    ranking_types,
                    time_ranges,
                    country_filter=country,
                    cache_type_filter=cache_type_id,
                )
        return stats_by_region_and_cache_type

    def generate_cache_rankings_json(self) -> Dict:
        """Generate complete cache-rankings.json data."""
        logger.info("Generating cache-rankings.json...")

        ranking_types = ["logs", "favorites"]
        time_ranges = RANKING_TIME_RANGES
        rankings_by_region = self.generate_cache_rankings_by_region(
            ranking_types,
            time_ranges,
            limit=50,
        )
        ranking_stats_by_region = self.generate_cache_ranking_stats_by_region(
            ranking_types,
            time_ranges,
        )
        rankings_by_cache_type = self.generate_cache_rankings_by_cache_type(
            ranking_types,
            time_ranges,
            limit=50,
        )
        ranking_stats_by_cache_type = self.generate_cache_ranking_stats_by_cache_type(
            ranking_types,
            time_ranges,
        )
        rankings_by_region_and_cache_type = self.generate_cache_rankings_by_region_and_cache_type(
            ranking_types,
            time_ranges,
            limit=50,
            rankings_by_region=rankings_by_region,
            rankings_by_cache_type=rankings_by_cache_type,
        )
        ranking_stats_by_region_and_cache_type = self.generate_cache_ranking_stats_by_region_and_cache_type(
            ranking_types,
            time_ranges,
            ranking_stats_by_region=ranking_stats_by_region,
            ranking_stats_by_cache_type=ranking_stats_by_cache_type,
        )

        return {
            "generatedAt": self.get_generated_at(),
            "rankings": rankings_by_region["all"],
            "rankingsByRegion": rankings_by_region,
            "rankingsByCacheType": rankings_by_cache_type,
            "rankingsByRegionAndCacheType": rankings_by_region_and_cache_type,
            "rankingStats": ranking_stats_by_region["all"],
            "rankingStatsByRegion": ranking_stats_by_region,
            "rankingStatsByCacheType": ranking_stats_by_cache_type,
            "rankingStatsByRegionAndCacheType": ranking_stats_by_region_and_cache_type,
        }

    # ==================== CITY-RANKINGS.JSON ====================

    def generate_cache_trend(self, city_filter: Optional[str] = None) -> Dict:
        """Generate cache trend data with monthly (10 months) and yearly (10 years) views."""
        
        # Build WHERE clause for city filter
        where_clause = f"WHERE {EXCLUDE_CACHE_WHERE}"
        if city_filter:
            # City filter: match city or country
            where_clause += f" AND COALESCE(NULLIF(TRIM(c.city), ''), c.country) = {sql_literal(city_filter)}"

        # Generate monthly data (last 10 months including current month)
        monthly_query = f"""
        WITH months AS (
          SELECT generate_series(
            date_trunc('month', CURRENT_DATE - INTERVAL '9 month'),
            date_trunc('month', CURRENT_DATE),
            INTERVAL '1 month'
          ) AS month_start
        ),
        counts AS (
          SELECT date_trunc('month', c.placed_date)::date AS month,
                 COUNT(*) FILTER (WHERE c.cache_status != 2)::int AS count,
                 COUNT(*) FILTER (WHERE c.cache_status = 2)::int AS archived
          FROM caches c
          {where_clause}
            AND c.placed_date IS NOT NULL
            AND c.placed_date >= date_trunc('month', CURRENT_DATE - INTERVAL '9 month')
          GROUP BY 1
        )
        SELECT TO_CHAR(months.month_start, 'YYYY-MM') AS label,
               COALESCE(counts.count, 0)::int AS count,
               COALESCE(counts.archived, 0)::int AS archived
        FROM months
        LEFT JOIN counts ON counts.month = months.month_start::date
        ORDER BY months.month_start;
        """

        monthly_results = self.execute_query(monthly_query)
        monthly = [
            {
                "label": row["label"],
                "count": row["count"] or 0,
                "archived": row["archived"] or 0,
            }
            for row in monthly_results
        ]

        # Generate yearly data (last 10 years including current year)
        yearly_query = f"""
        WITH years AS (
          SELECT generate_series(
            EXTRACT(YEAR FROM CURRENT_DATE)::int - 9,
            EXTRACT(YEAR FROM CURRENT_DATE)::int
          ) AS year
        ),
        counts AS (
          SELECT
            EXTRACT(YEAR FROM placed_date)::int AS year,
            COUNT(*) FILTER (WHERE c.cache_status != 2)::int AS count,
            COUNT(*) FILTER (WHERE c.cache_status = 2)::int AS archived
          FROM caches c
          {where_clause}
            AND c.placed_date IS NOT NULL
            AND EXTRACT(YEAR FROM placed_date)::int >= EXTRACT(YEAR FROM CURRENT_DATE)::int - 9
          GROUP BY 1
        )
        SELECT
          years.year::text AS label,
          COALESCE(counts.count, 0)::int AS count,
          COALESCE(counts.archived, 0)::int AS archived
        FROM years
        LEFT JOIN counts USING (year)
        ORDER BY years.year;
        """

        yearly_results = self.execute_query(yearly_query)
        yearly = [
            {
                "label": str(row["label"]),
                "count": row["count"] or 0,
                "archived": row["archived"] or 0,
            }
            for row in yearly_results
        ]

        # Calculate average growth based on yearly data
        avg_growth_pct = 0
        if len(yearly) >= 2:
            first_count = yearly[0]["count"] + yearly[0]["archived"]
            last_count = yearly[-1]["count"] + yearly[-1]["archived"]
            years_span = len(yearly) - 1
            if first_count > 0:
                avg_growth = ((last_count / first_count) ** (1 / years_span) - 1) * 100
                avg_growth_pct = round(avg_growth, 1)

        return {
            "averageGrowthPct": avg_growth_pct,
            "monthly": monthly,
            "yearly": yearly,
        }

    def generate_city_details(self, rankings_data: Dict) -> Dict:
        """Generate city details data for cities in rankings."""
        logger.info("Generating city details...")
        
        city_details = {}
        
        # Collect all unique city names from rankings
        city_names = set()
        for rtype in rankings_data.values():
            for time_range_data in rtype.values():
                for entry in time_range_data:
                    city_names.add(entry["name"])
        
        logger.debug(f"Found {len(city_names)} unique cities to generate details for")
        
        # Generate details for each city
        for city_name in city_names:
            try:
                cache_trend = self.generate_cache_trend(city_filter=city_name)
                dt_matrix = self.generate_dt_matrix(
                    country_filter=None,
                    city_filter=city_name,
                    include_top_caches=True,
                )
                
                city_details[city_name] = {
                    "cacheTrend": cache_trend,
                    "dtMatrix": dt_matrix,
                }
                
                logger.debug(f"  Generated details for {city_name}")
            
            except Exception as e:
                logger.warning(f"Failed to generate details for {city_name}: {e}")
                continue
        
        return city_details

    def generate_city_rankings_json(self) -> Dict:
        """Generate complete city-rankings.json data."""
        logger.info("Generating city-rankings.json...")

        ranking_types = ["hides", "finds", "favorites"]
        time_ranges = RANKING_TIME_RANGES

        # Generate rankings
        rankings = self.generate_rankings(
            ranking_types, time_ranges, is_city_ranking=True, limit=30
        )

        # Generate global cache trend and dt matrix
        cache_trend = self.generate_cache_trend()
        dt_matrix = self.generate_dt_matrix(include_top_caches=True)

        # Generate city details for cities in rankings
        city_details = self.generate_city_details(rankings)

        return {
            "generatedAt": self.get_generated_at(),
            "rankings": rankings,
            "cacheTrend": cache_trend,
            "dtMatrix": dt_matrix,
            "cityDetails": city_details,
        }

    # ==================== GENERATED-AT.JSON ====================

    def generate_timestamp_json(self) -> Dict:
        """Generate timestamp file."""
        return {
            "generatedAt": self.get_generated_at(),
            "version": "1.0.0",
        }

    # ==================== MAIN GENERATION ====================

    def generate_all(self):
        """Generate all JSON data files."""
        self.ensure_output_dir()

        files_to_generate = [
            ("overview.json", self.generate_overview_json),
            ("player-rankings.json", self.generate_player_rankings_json),
            ("cache-rankings.json", self.generate_cache_rankings_json),
            ("city-rankings.json", self.generate_city_rankings_json),
            ("generated-at.json", self.generate_timestamp_json),
        ]

        generated_files = []
        failed_files = []

        for filename, generator_func in files_to_generate:
            try:
                logger.info(f"Generating {filename}...")
                data = generator_func()

                filepath = os.path.join(OUTPUT_DIR, filename)
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)

                generated_files.append(filename)
                logger.info(f"Successfully generated {filename}")

            except Exception as e:
                logger.error(f"Failed to generate {filename}: {e}")
                failed_files.append(filename)

        return generated_files, failed_files


def main():
    """Main entry point."""
    logger.info("=" * 70)
    logger.info("Starting data generation process")
    logger.info(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 70)

    generator = DataGenerator(DATABASE_URL)

    try:
        generator.connect()

        generated, failed = generator.generate_all()

        logger.info("=" * 70)
        logger.info("Generation Complete!")
        logger.info(f"Successful: {len(generated)} files - {', '.join(generated)}")
        if failed:
            logger.warning(f"Failed: {len(failed)} files - {', '.join(failed)}")
        else:
            logger.info("All files generated successfully!")
        logger.info("=" * 70)

    except Exception as e:
        logger.exception("Data generation failed")
        raise
    finally:
        generator.close()


if __name__ == "__main__":
    main()
