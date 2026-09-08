#!/usr/bin/env python3
"""
Geocache Logs 爬虫 - 适配 Neon 数据库
基于 get_data.py 的逻辑重写
"""
import argparse
import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
import psycopg2
import requests
from psycopg2.extras import execute_batch
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from runtime_utils import (
    AuthenticationError,
    connect_postgres,
    is_login_url,
    looks_like_login_page,
    require_cookie,
    require_env,
    setup_logging,
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

logger = setup_logging("crawl_logs.log")


DATABASE_URL = require_env("DATABASE_URL")
MAX_RETRIES = 3
MAX_LOGBOOK_PAGES = int(os.getenv("LOGBOOK_MAX_PAGES", "1000"))
FOUND_LOG_TYPE = "Found it"
ATTENDED_LOG_TYPE = "Attended"
SINGLETON_LOG_TYPES = {FOUND_LOG_TYPE, ATTENDED_LOG_TYPE}
FTF_MARKER_RE = re.compile(
    r"[\{\[\(\uFF08]\s*\*?\s*ftf\s*\*?\s*[\}\]\)\uFF09]",
    re.IGNORECASE,
)

NONPREMIUM_COOKIE = require_cookie("GEOCOOKIE_NONPREMIUM", "GEOCACHING_COOKIE")
PREMIUM_COOKIE = require_cookie("GEOCOOKIE_PREMIUM")


session = requests.Session()
retries = Retry(total=5, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
session.mount("https://", HTTPAdapter(max_retries=retries))


PROFILES = {
    "nonpremium": {
        "COOKIES": NONPREMIUM_COOKIE,
        "page_sleep": 0.4,
    },
    "premium": {
        "COOKIES": PREMIUM_COOKIE,
        "page_sleep": 2.0,
    },
}


def format_date(raw_date: str) -> str:
    """格式化日期为 YYYY-MM-DD 格式。"""
    if not raw_date:
        return raw_date
    try:
        return pd.to_datetime(raw_date).strftime("%Y-%m-%d")
    except Exception:
        return raw_date


def normalize_log_date_for_compare(value) -> str:
    """Normalize DB/API log dates to YYYY-MM-DD when possible."""
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    formatted = format_date(str(value))
    return str(formatted)[:10]


def log_date_sort_key(value) -> str:
    """Return a sortable YYYY-MM-DD key; invalid dates sort first."""
    normalized = normalize_log_date_for_compare(value)
    if re.match(r"^\d{4}-\d{2}-\d{2}$", normalized):
        return normalized
    return "0001-01-01"


def normalize_log_id(value) -> Optional[int]:
    """Return a positive numeric LogID, or None when the API value is unusable."""
    try:
        log_id = int(value)
    except (TypeError, ValueError):
        return None
    return log_id if log_id > 0 else None


def deduplicate_logs_by_log_id(logs: List[dict]) -> List[dict]:
    """Keep one copy of each API log event without collapsing distinct events."""
    deduped: Dict[int, dict] = {}
    for log in logs:
        log_id = normalize_log_id(log.get("LogID"))
        if log_id is None:
            raise ValueError(f"Missing valid LogID for {log.get('GCCode')}")
        normalized = dict(log)
        normalized["LogID"] = log_id
        deduped.setdefault(log_id, normalized)
    return list(deduped.values())


def select_storage_events(logs: List[dict]) -> List[dict]:
    """Keep all event logs except superseded Found it and Attended observations.

    Those two types are singleton observations per cache/account/type in this
    project. When an API response contains multiple such events, retain the
    most recently visited one, breaking same-day ties by the larger LogID.
    ``logs`` must already have valid, normalized LogID values.
    """
    latest_singletons = {}
    non_singleton_logs = []
    for log in logs:
        key = legacy_log_key(log)
        if not key or key[2] not in SINGLETON_LOG_TYPES:
            non_singleton_logs.append(log)
            continue
        current = latest_singletons.get(key)
        if (
            current is None
            or log_date_sort_key(log["Visited"]) > log_date_sort_key(current["Visited"])
            or (
                log_date_sort_key(log["Visited"]) == log_date_sort_key(current["Visited"])
                and log["LogID"] > current["LogID"]
            )
        ):
            latest_singletons[key] = log
    return non_singleton_logs + list(latest_singletons.values())


def legacy_log_key(log: dict) -> Optional[Tuple[str, str, str]]:
    """Key used only to attach a LogID to records collected before event IDs existed."""
    gc_code = log.get("GCCode") or log.get("gc_code")
    user_guid = log.get("AccountGuid") or log.get("user_guid")
    log_type = log.get("LogType") or log.get("log_type")
    if not gc_code or not user_guid or not log_type:
        return None
    return str(gc_code), str(user_guid), str(log_type)


def legacy_log_matches(existing: dict, incoming: dict) -> bool:
    """Return whether an old ID-less row represents an incoming API event.

    The legacy table has no event identifier, so a match requires its original
    cache/user/type key plus all stored event fields. User name is deliberately
    excluded because account names can change while AccountGuid remains stable.
    """
    if legacy_log_key(existing) != legacy_log_key(incoming):
        return False
    return (
        normalize_log_date_for_compare(existing.get("visited"))
        == normalize_log_date_for_compare(incoming.get("Visited"))
        and bool(existing.get("favorite_point_used"))
        == bool(incoming.get("FavoritePointUsed", False))
        and bool(existing.get("is_ftf"))
        == bool(incoming.get("IsFTF", False))
    )


def is_ftf_log_text(log_content: str) -> bool:
    """Return True when log text contains an FTF marker inside brackets."""
    return bool(FTF_MARKER_RE.search(log_content or ""))


class DatabaseManager:
    """数据库管理类。"""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.conn = None
        self.cursor = None

    def connect(self):
        """连接数据库。"""
        self.conn = connect_postgres(
            self.database_url,
            logger=logger,
            connect_timeout=10,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
        )
        self.cursor = self.conn.cursor()
        self.ensure_logs_schema()
        logger.info("数据库连接成功")

    def ensure_logs_schema(self):
        """Migrate legacy cache/user rows to event-level LogID storage."""
        self.cursor.execute(
            """
            ALTER TABLE logs
            ADD COLUMN IF NOT EXISTS log_type TEXT NOT NULL DEFAULT 'Found it'
            """
        )
        self.cursor.execute(
            """
            ALTER TABLE logs
            ADD COLUMN IF NOT EXISTS user_guid TEXT
            """
        )
        self.cursor.execute(
            """
            ALTER TABLE logs
            ADD COLUMN IF NOT EXISTS log_id BIGINT
            """
        )
        # The two legacy constraints collapse multiple event logs from the same
        # account. LogID is now the authoritative event identity.
        self.cursor.execute("DROP INDEX IF EXISTS logs_gc_user_guid_unique")
        self.cursor.execute(
            "ALTER TABLE logs DROP CONSTRAINT IF EXISTS logs_gc_code_user_name_visited_key"
        )
        self.cursor.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS logs_log_id_unique
            ON logs(log_id) WHERE log_id IS NOT NULL
            """
        )
        self.cursor.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS logs_gc_user_guid_find_attend_unique
            ON logs(gc_code, user_guid, log_type)
            WHERE user_guid IS NOT NULL
              AND user_guid <> 'deleted'
              AND log_type IN ('Found it', 'Attended')
            """
        )
        self.conn.commit()

    def reconnect(self):
        """重新连接数据库。"""
        logger.info("尝试重新连接数据库...")
        self.close()
        self.connect()
        logger.info("数据库重新连接成功")

    def close(self):
        """关闭数据库连接。"""
        if self.cursor:
            try:
                self.cursor.close()
            except Exception:
                pass
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
        logger.info("数据库连接已关闭")

    def get_all_caches_to_crawl(
        self,
        full: bool = False,
        include_archived: bool = False,
    ) -> List[Tuple[str, Optional[float], Optional[float], bool, Optional[int]]]:
        """Return incremental caches, active caches, or all non-deleted caches."""
        status_filter = (
            "COALESCE(c.cache_status, 0) <> 404"
            if full and include_archived
            else "COALESCE(c.cache_status, 0) NOT IN (2, 404)"
        )
        self.cursor.execute(
            f"""
            SELECT c.code, c.latitude, c.longitude, c.premium_only,
                   c.geocache_type, c.logs_crawled_at, c.last_found_date, c.placed_date
            FROM caches c
            WHERE {status_filter}
            ORDER BY c.code
            """
        )

        results = []
        for row in self.cursor.fetchall():
            code, lat, lng, premium_only, geocache_type, logs_crawled_at, last_found_date, placed_date = row

            if full:
                results.append((code, lat, lng, bool(premium_only), geocache_type))
                continue

            logs_crawled_date = (
                logs_crawled_at.date() if hasattr(logs_crawled_at, 'date') else logs_crawled_at
            )
            last_found_cmp = (
                last_found_date.date() if hasattr(last_found_date, 'date') else last_found_date
            )
            placed_cmp = (
                placed_date.date() if hasattr(placed_date, 'date') else placed_date
            )

            # 从未爬取过
            if logs_crawled_at is None:
                results.append((code, lat, lng, bool(premium_only), geocache_type))
            # 有新的 find 日志（last_found >= 上次爬取时间）
            elif last_found_date is not None and last_found_cmp >= logs_crawled_date:
                results.append((code, lat, lng, bool(premium_only), geocache_type))
            # last_found <= placed_date 且是真实日期（排除 0001-01-01 这种"无 find"默认值）
            elif (last_found_date is not None and placed_date is not None
                  and last_found_cmp <= placed_cmp
                  and last_found_cmp.year > 1):
                results.append((code, lat, lng, bool(premium_only), geocache_type))
        return results

    def batch_update_logs_crawled_at(self, codes: List[str], crawl_date: str):
        """批量更新 logs_crawled_at。"""
        if not codes:
            return

        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.cursor.execute(
                    """
                    UPDATE caches SET logs_crawled_at = %s WHERE code = ANY(%s)
                    """,
                    (crawl_date, codes),
                )
                return
            except psycopg2.OperationalError as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"批量更新 logs_crawled_at 失败，尝试重新连接 ({attempt + 1}/{max_retries})..."
                    )
                    self.reconnect()
                else:
                    logger.error(f"批量更新 logs_crawled_at 失败: {e}")
                    raise

    def get_existing_logs_for_caches(self, gc_codes: List[str]) -> List[dict]:
        """Load event rows and ID-less legacy rows for the incoming cache batch."""
        if not gc_codes:
            return []
        self.cursor.execute(
            """
            SELECT id, gc_code, user_name, visited, favorite_point_used, is_ftf,
                   log_type, user_guid, log_id
            FROM logs
            WHERE gc_code = ANY(%s)
            """,
            (gc_codes,),
        )
        return [
            {
                "id": row[0], "gc_code": row[1], "user_name": row[2],
                "visited": row[3], "favorite_point_used": row[4],
                "is_ftf": row[5], "log_type": row[6], "user_guid": row[7],
                "log_id": row[8],
            }
            for row in self.cursor.fetchall()
        ]

    @staticmethod
    def _event_fields_match(existing: dict, incoming: dict) -> bool:
        return (
            legacy_log_matches(existing, incoming)
            and existing.get("user_name") == (incoming.get("UserName") or "deleted")
        )

    @staticmethod
    def _log_values(log: dict) -> Tuple:
        return (
            log["GCCode"], log.get("UserName") or "deleted", log["Visited"],
            bool(log.get("FavoritePointUsed", False)), bool(log.get("IsFTF", False)),
            log.get("LogType") or "Unknown", log.get("AccountGuid"),
            normalize_log_id(log.get("LogID")),
        )

    def smart_upsert_logs(self, new_logs: List[dict]) -> Tuple[int, int]:
        """Upsert API events by LogID and backfill matching legacy rows.

        ID-less rows are matched by ``(gc_code, user_guid, log_type)`` and all
        stored event fields. For Found it and Attended, the project keeps one
        row per cache/account/type and retains the most recent visited date.
        """
        if not new_logs:
            return 0, 0
        deduped_logs = deduplicate_logs_by_log_id(new_logs)
        if len(deduped_logs) != len(new_logs):
            logger.info("Deduplicated %s rows to %s LogID events", len(new_logs), len(deduped_logs))

        deduped_logs = select_storage_events(deduped_logs)

        existing = self.get_existing_logs_for_caches(list({log["GCCode"] for log in deduped_logs}))
        existing_by_log_id = {row["log_id"]: row for row in existing if row["log_id"] is not None}
        legacy_by_key = defaultdict(list)
        singleton_by_key = {}
        for row in existing:
            if row["log_id"] is None:
                key = legacy_log_key(row)
                if key:
                    legacy_by_key[key].append(row)
            key = legacy_log_key(row)
            if key and key[2] in SINGLETON_LOG_TYPES:
                current = singleton_by_key.get(key)
                if current is None or log_date_sort_key(row["visited"]) > log_date_sort_key(current["visited"]):
                    singleton_by_key[key] = row

        to_insert, to_update, legacy_backfills = [], [], []
        consumed_legacy_ids = set()
        for log in deduped_logs:
            event_row = existing_by_log_id.get(log["LogID"])
            if event_row is not None:
                if not self._event_fields_match(event_row, log):
                    to_update.append((event_row["id"], log))
                continue

            key = legacy_log_key(log)
            matched_legacy = next(
                (
                    row for row in legacy_by_key.get(key, [])
                    if row["id"] not in consumed_legacy_ids and legacy_log_matches(row, log)
                ),
                None,
            )
            if matched_legacy is not None:
                consumed_legacy_ids.add(matched_legacy["id"])
                legacy_backfills.append((matched_legacy["id"], log))
                matched_legacy.update({
                    "user_name": log.get("UserName") or "deleted",
                    "visited": log["Visited"],
                    "favorite_point_used": bool(log.get("FavoritePointUsed", False)),
                    "is_ftf": bool(log.get("IsFTF", False)),
                    "log_type": log.get("LogType") or "Unknown",
                    "user_guid": log.get("AccountGuid"),
                    "log_id": log["LogID"],
                })
                continue

            singleton_row = singleton_by_key.get(key)
            if singleton_row is not None:
                if log_date_sort_key(log["Visited"]) >= log_date_sort_key(singleton_row["visited"]):
                    to_update.append((singleton_row["id"], log))
                    singleton_row.update({
                        "user_name": log.get("UserName") or "deleted",
                        "visited": log["Visited"],
                        "favorite_point_used": bool(log.get("FavoritePointUsed", False)),
                        "is_ftf": bool(log.get("IsFTF", False)),
                        "log_type": log.get("LogType") or "Unknown",
                        "user_guid": log.get("AccountGuid"),
                        "log_id": log["LogID"],
                    })
                continue

            to_insert.append(log)

        if to_insert:
            execute_batch(
                self.cursor,
                """
                INSERT INTO logs (gc_code, user_name, visited, favorite_point_used, is_ftf, log_type, user_guid, log_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (log_id) WHERE log_id IS NOT NULL DO UPDATE
                SET user_name = EXCLUDED.user_name,
                    visited = EXCLUDED.visited,
                    favorite_point_used = EXCLUDED.favorite_point_used,
                    is_ftf = EXCLUDED.is_ftf,
                    log_type = EXCLUDED.log_type,
                    user_guid = EXCLUDED.user_guid
                """,
                [self._log_values(log) for log in to_insert],
            )

        updates = to_update + legacy_backfills
        if updates:
            execute_batch(
                self.cursor,
                """
                UPDATE logs
                SET user_name = %s, visited = %s, favorite_point_used = %s,
                    is_ftf = %s, log_type = %s, user_guid = %s, log_id = %s
                WHERE id = %s
                """,
                [self._log_values(log)[1:] + (row_id,) for row_id, log in updates],
            )
        return len(to_insert), len(updates)

    def commit(self):
        """提交事务。"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.conn.commit()
                return
            except psycopg2.OperationalError as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"提交事务失败，尝试重新连接 ({attempt + 1}/{max_retries})..."
                    )
                    self.reconnect()
                else:
                    logger.error(f"提交事务失败: {e}")
                    raise


def make_headers(cookie: str) -> dict:
    """构造请求头。"""
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/144.0.0.0 Safari/537.36"
        ),
        "Cookie": cookie,
        "X-Requested-With": "XMLHttpRequest",
    }


def looks_like_public_geocaching_page(text: str) -> bool:
    """Return True when Geocaching served a public page instead of an authenticated one."""
    lowered = text or ""
    compact = re.sub(r"\s+", "", lowered)
    if '"isAuthenticated":false' in compact:
        return True
    if re.search(r"userInfo\s*=\s*\{\s*ID\s*:\s*0\s*\}", text or ""):
        return True

    match = re.search(
        r'"pageInfo"\s*:\s*\{\s*"idx"\s*:\s*1,\s*"size"\s*:\s*(\d+),\s*"totalRows"\s*:\s*(\d+)',
        text or "",
    )
    if match:
        page_size = int(match.group(1))
        total_rows = int(match.group(2))
        if page_size <= 5 and total_rows > page_size:
            return True

    return False


def is_public_logbook_auth_error(error: Exception) -> bool:
    """Return True when token fetch got a public logbook page instead of an authenticated one."""
    return "public logbook page returned" in str(error)


def get_logbook_token(gc_code: str, cookie: str) -> Optional[str]:
    """获取 logbook token。"""
    url = f"https://www.geocaching.com/seek/cache_details.aspx?wp={gc_code}"
    headers = make_headers(cookie)

    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, headers=headers, timeout=15)

            if resp.status_code == 200:
                if looks_like_login_page(resp.url, resp.text):
                    raise AuthenticationError(
                        f"Authentication failed while getting token for {gc_code}: redirected to login page"
                    )
                if looks_like_public_geocaching_page(resp.text):
                    raise AuthenticationError(
                        f"Authentication failed while getting token for {gc_code}: public logbook page returned"
                    )

                match = re.search(r"userToken\s*=\s*['\"](.*?)['\"]", resp.text)
                if match:
                    return match.group(1)

            elif resp.status_code in [401, 403]:
                if attempt == MAX_RETRIES - 1:
                    raise AuthenticationError(
                        f"Authentication failed while getting token for {gc_code}: HTTP {resp.status_code}"
                    )
                time.sleep(10 * (attempt + 1))

            elif resp.status_code == 429:
                time.sleep(10 * (attempt + 1))

        except AuthenticationError:
            raise
        except Exception as e:
            logger.error(f"获取 token 失败 {gc_code}: {e}")
            time.sleep(5)

    return None


def fetch_logs_for_cache_result(
    gc_code: str,
    token: str,
    page_sleep: float,
    cookie: str,
    accepted_log_types: Optional[set] = None,
) -> Tuple[List[dict], bool]:
    """Fetch every log event for one cache unless a caller explicitly filters."""

    logs = []
    current_idx = 1
    num_per_page = 100
    max_retries = 3

    headers = make_headers(cookie)
    headers["Referer"] = f"https://www.geocaching.com/seek/geocache_logs.aspx?code={gc_code}"

    for _ in range(MAX_LOGBOOK_PAGES):
        url = (
            "https://www.geocaching.com/seek/geocache.logbook?"
            f"tkn={token}&idx={current_idx}&num={num_per_page}"
            "&sp=false&sf=false&showOwnerOnly=false&decrypt=false"
        )

        success = False
        data = None
        for attempt in range(max_retries):
            try:
                resp = session.get(url, headers=headers, timeout=20)
                if resp.status_code == 200:
                    data = resp.json().get("data", [])
                    success = True
                    break
                logger.warning(
                    f"获取 logs 失败 {gc_code}: 状态码 {resp.status_code}，重试中({attempt + 1})..."
                )
            except (
                requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
            ) as e:
                logger.warning(f"网络异常: {gc_code} {e}，正在进行第 {attempt + 1} 次重试...")
                time.sleep(2)
            except (requests.RequestException, ValueError) as e:
                logger.warning(f"Failed to fetch logs for {gc_code}: {e}; retrying ({attempt + 1})...")
                time.sleep(2)

        if not success:
            logger.error(
                f"获取 logs 失败 {gc_code}: 在重试 {max_retries} 次后依然无法连接服务器"
            )
            return logs, False

        if not data:
            return logs, True

        for item in data:
            log_type = item.get("LogType")
            if accepted_log_types is not None and log_type not in accepted_log_types:
                continue

            log_id = normalize_log_id(item.get("LogID"))
            if log_id is None:
                logger.error("Logbook row has no usable LogID: cache=%s type=%s", gc_code, log_type)
                return logs, False

            # LogText is read only for the existing FTF marker heuristic and is
            # intentionally omitted from the returned record and database.
            log_content = item.get("LogText", "")
            logs.append(
                {
                    "GCCode": gc_code,
                    "UserName": item.get("UserName") or "deleted",
                    "Visited": format_date(item.get("Visited", "")),
                    "FavoritePointUsed": item.get("FavoritePointUsed", False),
                    "IsFTF": log_type == FOUND_LOG_TYPE and is_ftf_log_text(log_content),
                    "LogType": log_type or "Unknown",
                    "AccountGuid": item.get("AccountGuid"),
                    "LogID": log_id,
                }
            )

        if len(data) < num_per_page:
            return logs, True

        current_idx += 1
        time.sleep(page_sleep)

    logger.error(f"Failed to fetch logs for {gc_code}: exceeded max pages {MAX_LOGBOOK_PAGES}; not marking as crawled")
    return logs, False


def fetch_logs_for_cache(
    gc_code: str,
    token: str,
    page_sleep: float,
    cookie: str,
    accepted_log_types: Optional[set] = None,
) -> List[dict]:
    """Fetch logs for one cache. Kept for diagnostics and older callers."""
    logs, _ = fetch_logs_for_cache_result(
        gc_code,
        token,
        page_sleep,
        cookie,
        accepted_log_types=accepted_log_types,
    )
    return logs


def crawl_cache_group(
    db: DatabaseManager,
    group_name: str,
    caches: List[Tuple[str, Optional[int]]],
    today_str: str,
) -> Dict[str, int]:
    """按指定配置爬取一组 cache。"""
    cfg = PROFILES[group_name]
    cookie = cfg["COOKIES"]
    page_sleep = cfg["page_sleep"]

    success_count = 0
    logs_count = 0
    crawled_codes = []
    all_logs = []
    failed_caches = []
    LOG_UPLOAD_THRESHOLD = 2000

    consecutive_token_failures = 0
    MAX_CONSECUTIVE_TOKEN_FAILURES = 5
    MAX_PUBLIC_PAGE_RETRIES = 3
    consecutive_public_page_failures = 0
    MAX_CONSECUTIVE_PUBLIC_PAGE_FAILURES = 5

    logger.info(f"开始处理 {group_name} 组，共 {len(caches)} 个 cache")

    for i, (code, _geocache_type) in enumerate(caches):
        logger.info(f"[{group_name} {i + 1}/{len(caches)}] 处理: {code}")

        retry_count = 0
        max_retries = 1
        public_page_retries = 0
        success = False

        while retry_count <= max_retries and not success:
            try:
                try:
                    token = get_logbook_token(code, cookie)
                except AuthenticationError as e:
                    if not is_public_logbook_auth_error(e):
                        raise

                    public_page_retries += 1
                    if public_page_retries < MAX_PUBLIC_PAGE_RETRIES:
                        logger.warning(
                            f"  返回游客页，2 秒后重试: {code} "
                            f"({public_page_retries}/{MAX_PUBLIC_PAGE_RETRIES})"
                        )
                        time.sleep(2)
                        continue

                    consecutive_public_page_failures += 1
                    logger.error(
                        f"  返回游客页，已重试 {MAX_PUBLIC_PAGE_RETRIES} 次，跳过: {code} "
                        f"(连续 {consecutive_public_page_failures}/{MAX_CONSECUTIVE_PUBLIC_PAGE_FAILURES})"
                    )
                    if consecutive_public_page_failures >= MAX_CONSECUTIVE_PUBLIC_PAGE_FAILURES:
                        raise AuthenticationError(
                            f"Authentication failure detected: {consecutive_public_page_failures} consecutive "
                            f"caches returned public logbook page in {group_name} group. Cookie may be expired."
                        )
                    failed_caches.append(
                        {
                            "code": code,
                            "group": group_name,
                            "reason": "public_logbook_page",
                            "message": str(e),
                        }
                    )
                    break

                if not token:
                    consecutive_token_failures += 1
                    logger.warning(f"  无法获取 token (连续失败 {consecutive_token_failures}/{MAX_CONSECUTIVE_TOKEN_FAILURES})")

                    if consecutive_token_failures >= MAX_CONSECUTIVE_TOKEN_FAILURES:
                        raise AuthenticationError(
                            f"Authentication failure detected: {MAX_CONSECUTIVE_TOKEN_FAILURES} consecutive caches "
                            f"failed to get logbook token in {group_name} group. Cookie may be invalid."
                        )
                    failed_caches.append(
                        {
                            "code": code,
                            "group": group_name,
                            "reason": "token_missing",
                            "message": "get_logbook_token returned no token",
                        }
                    )
                    break

                consecutive_token_failures = 0
                consecutive_public_page_failures = 0

                cache_logs, crawl_complete = fetch_logs_for_cache_result(
                    code,
                    token,
                    page_sleep,
                    cookie,
                )

                if not crawl_complete:
                    retry_count += 1
                    if retry_count <= max_retries:
                        logger.warning(f"  logs 未完整获取，{code} 将在 1 秒后重试...")
                        time.sleep(1)
                        continue
                    logger.error(f"  logs 未完整获取，跳过更新 logs_crawled_at: {code}")
                    failed_caches.append(
                        {
                            "code": code,
                            "group": group_name,
                            "reason": "incomplete_logs",
                            "message": "logbook pages were not fetched completely",
                        }
                    )
                    break

                if cache_logs:
                    all_logs.extend(cache_logs)
                    logger.info(f"  获取 {len(cache_logs)} 条 logs")
                    success_count += 1
                else:
                    logger.info("  无 logs 数据")

                crawled_codes.append(code)
                success = True

            except AuthenticationError:
                raise
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                retry_count += 1
                if retry_count <= max_retries:
                    logger.warning(f"  数据库连接出错: {e}，尝试重新连接...")
                    try:
                        db.reconnect()
                    except Exception:
                        pass
                    time.sleep(2)
                else:
                    logger.error(f"  数据库连接出错，重试次数已用完: {e}")
            except Exception as e:
                retry_count += 1
                if retry_count <= max_retries:
                    logger.warning(f"  出错: {e}，1 秒后重试...")
                    time.sleep(1)
                else:
                    logger.error(f"  出错: {e}，重试次数已用完")

        if len(all_logs) >= LOG_UPLOAD_THRESHOLD:
            if all_logs:
                inserted, updated = db.smart_upsert_logs(all_logs)
                logs_count += (inserted + updated)
                logger.info(f"{group_name} 组处理 {len(all_logs)} 条原始 logs: 新增 {inserted} 条, 更新 {updated} 条 (净增 {inserted+updated} 条)")
                all_logs = []
            db.batch_update_logs_crawled_at(crawled_codes, today_str)
            crawled_codes = []
            db.commit()

    if all_logs:
        inserted, updated = db.smart_upsert_logs(all_logs)
        logs_count += (inserted + updated)
        logger.info(f"{group_name} 组处理 {len(all_logs)} 条原始 logs: 新增 {inserted} 条, 更新 {updated} 条 (净增 {inserted+updated} 条)")
    if crawled_codes:
        db.batch_update_logs_crawled_at(crawled_codes, today_str)

    db.commit()

    logger.info(f"{group_name} 组处理完成: {success_count}个cache, 新增 logs {logs_count}")
    return {
        "success_count": success_count,
        "logs_count": logs_count,
        "failed_caches": failed_caches,
    }


def run_logs_crawler(full: bool = False, include_archived: bool = False):
    """Run incremental, active-full, or all-non-deleted event-log refreshes."""
    db = DatabaseManager(DATABASE_URL)
    db.connect()

    try:
        refresh_scope = "全量（含归档）" if include_archived else ("全量活跃" if full else "增量")
        logger.info("加载%s cache 列表...", refresh_scope)
        caches = db.get_all_caches_to_crawl(full=full, include_archived=include_archived)
        premium_caches = [
            (code, geocache_type)
            for code, _lat, _lng, premium_only, geocache_type in caches
            if premium_only
        ]
        nonpremium_caches = [
            (code, geocache_type)
            for code, _lat, _lng, premium_only, geocache_type in caches
            if not premium_only
        ]
        logger.info(
            f"需要处理 {len(caches)} 个 cache，其中 premium {len(premium_caches)} 个，"
            f"nonpremium {len(nonpremium_caches)} 个"
        )

        today_str = datetime.now().strftime("%Y-%m-%d")
        premium_stats = crawl_cache_group(db, "premium", premium_caches, today_str)
        nonpremium_stats = crawl_cache_group(db, "nonpremium", nonpremium_caches, today_str)
        failed_caches = (
            premium_stats.get("failed_caches", [])
            + nonpremium_stats.get("failed_caches", [])
        )

        logger.info("=" * 50)
        logger.info(
            "Logs 爬取完成! "
            f"总计: {premium_stats['success_count'] + nonpremium_stats['success_count']}个cache, "
            f"总新增 logs: {premium_stats['logs_count'] + nonpremium_stats['logs_count']}"
        )
        logger.info(
            f"分组统计: premium {premium_stats['success_count']}个cache / logs {premium_stats['logs_count']}, "
            f"nonpremium {nonpremium_stats['success_count']}个cache / logs {nonpremium_stats['logs_count']}"
        )
        if failed_caches:
            logger.warning(f"Failed caches: {len(failed_caches)}")
            for item in failed_caches:
                logger.warning(
                    "Failed cache "
                    f"group={item.get('group')} code={item.get('code')} "
                    f"reason={item.get('reason')} message={item.get('message')}"
                )
        else:
            logger.info("Failed caches: 0")
        logger.info("=" * 50)

    except Exception:
        logger.exception("Logs 爬虫运行出错")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Crawl Geocaching logbook events.")
    refresh_group = parser.add_mutually_exclusive_group()
    refresh_group.add_argument(
        "--full",
        action="store_true",
        help="Recrawl every non-deleted cache, including archived caches.",
    )
    refresh_group.add_argument(
        "--full-active",
        action="store_true",
        help="Recrawl every active cache, excluding archived and unavailable caches.",
    )
    args = parser.parse_args()
    try:
        run_logs_crawler(
            full=args.full or args.full_active,
            include_archived=args.full,
        )
    except Exception:
        logger.exception("crawl_logs failed")
        raise
