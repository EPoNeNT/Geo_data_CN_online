#!/usr/bin/env python3
"""
Geocache Logs 爬虫 - 适配 Neon 数据库
基于 get_data.py 的逻辑重写
"""
import logging
import os
import re
import sys
import time
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
EVENT_CACHE_TYPES = {6, 13, 3653}
FOUND_LOG_TYPE = "Found it"
ATTENDED_LOG_TYPE = "Attended"
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
        "update_coordinates": False,
    },
    "premium": {
        "COOKIES": PREMIUM_COOKIE,
        "page_sleep": 2.0,
        "update_coordinates": True,
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
        """Ensure columns required by current log crawling logic exist."""
        self.cursor.execute(
            """
            ALTER TABLE logs
            ADD COLUMN IF NOT EXISTS log_type TEXT NOT NULL DEFAULT 'Found it'
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
    ) -> List[Tuple[str, Optional[float], Optional[float], bool, Optional[int]]]:
        """获取需要爬取 logs 的全部 cache，并保留 premium 标记。"""
        self.cursor.execute(
            """
            SELECT c.code, c.latitude, c.longitude, c.premium_only,
                   c.geocache_type, c.logs_crawled_at, c.last_found_date
            FROM caches c
            WHERE c.cache_status != 2
            ORDER BY c.code
            """
        )

        results = []
        for row in self.cursor.fetchall():
            code, lat, lng, premium_only, geocache_type, logs_crawled_at, last_found_date = row

            # Convert to date for safe comparison (handle both datetime and date types)
            logs_crawled_date = (
                logs_crawled_at.date() if hasattr(logs_crawled_at, 'date') else logs_crawled_at
            )
            last_found_cmp = (
                last_found_date.date() if hasattr(last_found_date, 'date') else last_found_date
            )

            if logs_crawled_at is None or (
                last_found_date is not None and last_found_cmp >= logs_crawled_date
            ):
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

    def insert_logs(self, logs: List[dict]):
        """批量插入日志。"""
        if not logs:
            return

        max_retries = 3
        for attempt in range(max_retries):
            try:
                execute_batch(
                    self.cursor,
                    """
                    INSERT INTO logs (gc_code, user_name, visited, favorite_point_used, is_ftf, log_type)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    [
                        (
                            log["GCCode"],
                            log["UserName"],
                            log["Visited"],
                            log.get("FavoritePointUsed", False),
                            log.get("IsFTF", False),
                            log.get("LogType", FOUND_LOG_TYPE),
                        )
                        for log in logs
                    ],
                )
                return
            except psycopg2.OperationalError as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"插入 logs 失败，尝试重新连接 ({attempt + 1}/{max_retries})..."
                    )
                    self.reconnect()
                else:
                    logger.error(f"插入 logs 失败: {e}")
                    raise

    def get_existing_logs_for_caches(self, gc_codes: List[str]) -> Dict[Tuple[str, str], dict]:
        """查询指定 caches 的现有日志记录，返回 {(gc_code, user_name): record} 字典。"""
        if not gc_codes:
            return {}

        self.cursor.execute(
            """
            SELECT gc_code, user_name, visited, favorite_point_used, is_ftf, log_type
            FROM logs
            WHERE gc_code = ANY(%s)
            """,
            (gc_codes,),
        )

        existing = {}
        for row in self.cursor.fetchall():
            key = (row[0], row[1])
            existing[key] = {
                "visited": row[2],
                "favorite_point_used": row[3],
                "is_ftf": row[4],
                "log_type": row[5],
            }
        return existing

    def smart_upsert_logs(self, new_logs: List[dict]) -> Tuple[int, int]:
        """
        智能日志更新：比较新旧记录，决定插入或替换。
        
        返回: (inserted_count, updated_count)
        
        逻辑：
        - 如果 (gc_code, user_name) 不存在于数据库 → INSERT
        - 如果存在且所有字段完全相同 → 跳过（去重）
        - 如果存在但有字段不同 → UPDATE（替换旧记录）
        """
        if not new_logs:
            return (0, 0)

        # 批量查询这些 cache 的现有日志
        gc_codes = list(set(log["GCCode"] for log in new_logs))
        existing_logs = self.get_existing_logs_for_caches(gc_codes)

        to_insert = []
        to_update = []

        for log in new_logs:
            gc_code = log["GCCode"]
            user_name = log["UserName"]
            key = (gc_code, user_name)

            new_record = {
                "visited": log["Visited"],
                "favorite_point_used": log.get("FavoritePointUsed", False),
                "is_ftf": log.get("IsFTF", False),
                "log_type": log.get("LogType", FOUND_LOG_TYPE),
            }

            if key not in existing_logs:
                # 新记录，需要插入
                to_insert.append(log)
            else:
                # 已存在，比较字段
                old_record = existing_logs[key]

                # 标准化 visited 字段为字符串进行比较
                db_visited = old_record["visited"]
                if hasattr(db_visited, "isoformat"):
                    db_visited_str = db_visited.isoformat()[:10]
                else:
                    db_visited_str = str(db_visited)[:10]

                api_visited_str = str(new_record["visited"])[:10]

                # 比较所有关键字段
                fields_match = (
                    db_visited_str == api_visited_str
                    and bool(old_record["favorite_point_used"]) == bool(new_record["favorite_point_used"])
                    and bool(old_record["is_ftf"]) == bool(new_record["is_ftf"])
                    and (old_record.get("log_type") or FOUND_LOG_TYPE) == new_record["log_type"]
                )

                if not fields_match:
                    # 字段有变化，需要更新
                    to_update.append(log)

        # 执行批量操作
        inserted_count = 0
        updated_count = 0

        if to_insert:
            self._batch_insert_logs(to_insert)
            inserted_count = len(to_insert)

        if to_update:
            self._batch_update_logs(to_update)
            updated_count = len(to_update)

        return (inserted_count, updated_count)

    def _batch_insert_logs(self, logs: List[dict]):
        """执行批量 INSERT 操作。"""
        execute_batch(
            self.cursor,
            """
            INSERT INTO logs (gc_code, user_name, visited, favorite_point_used, is_ftf, log_type)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            [
                (
                    log["GCCode"],
                    log["UserName"],
                    log["Visited"],
                    log.get("FavoritePointUsed", False),
                    log.get("IsFTF", False),
                    log.get("LogType", FOUND_LOG_TYPE),
                )
                for log in logs
            ],
        )

    def _batch_update_logs(self, logs: List[dict]):
        """执行批量 UPDATE 操作，根据 (gc_code, user_name) 匹配并更新其他字段。"""
        for log in logs:
            self.cursor.execute(
                """
                UPDATE logs
                SET visited = %s,
                    favorite_point_used = %s,
                    is_ftf = %s,
                    log_type = %s
                WHERE gc_code = %s AND user_name = %s
                """,
                (
                    log["Visited"],
                    log.get("FavoritePointUsed", False),
                    log.get("IsFTF", False),
                    log.get("LogType", FOUND_LOG_TYPE),
                    log["GCCode"],
                    log["UserName"],
                ),
            )

    def update_cache_coordinates(self, code: str, lat: float, lng: float):
        """更新 cache 坐标。"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.cursor.execute(
                    """
                    UPDATE caches
                    SET latitude = %s, longitude = %s
                    WHERE code = %s
                    """,
                    (lat, lng, code),
                )
                return
            except psycopg2.OperationalError as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"更新坐标失败，尝试重新连接 ({attempt + 1}/{max_retries})..."
                    )
                    self.reconnect()
                else:
                    logger.error(f"更新坐标失败: {e}")
                    raise

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
    """获取单个 cache 的所有 logs。"""
    if accepted_log_types is None:
        accepted_log_types = {FOUND_LOG_TYPE}

    logs = []
    current_idx = 1
    num_per_page = 100
    max_pages = 100
    max_retries = 3

    headers = make_headers(cookie)
    headers["Referer"] = f"https://www.geocaching.com/seek/geocache_logs.aspx?code={gc_code}"

    for _ in range(max_pages):
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
            if log_type not in accepted_log_types:
                continue

            log_content = item.get("LogText", "")
            logs.append(
                {
                    "GCCode": gc_code,
                    "UserName": item.get("UserName"),
                    "Visited": format_date(item.get("Visited", "")),
                    "FavoritePointUsed": item.get("FavoritePointUsed", False),
                    "IsFTF": is_ftf_log_text(log_content),
                    "LogType": log_type,
                }
            )

        if len(data) < num_per_page:
            return logs, True

        current_idx += 1
        time.sleep(page_sleep)

    logger.error(f"Failed to fetch logs for {gc_code}: exceeded max pages {max_pages}; not marking as crawled")
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


def get_coordinates(gc_code: str, cookie: str) -> Tuple[Optional[float], Optional[float]]:
    """获取 cache 坐标。"""
    api_url = f"https://www.geocaching.com/api/live/v1/search/geocachepreview/{gc_code}"
    headers = make_headers(cookie)
    headers["accept"] = "application/json"

    try:
        resp = session.get(api_url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            lat = data.get("postedCoordinates", {}).get("latitude")
            lng = data.get("postedCoordinates", {}).get("longitude")
            return lat, lng
    except Exception as e:
        logger.error(f"获取坐标失败 {gc_code}: {e}")

    return None, None


def crawl_cache_group(
    db: DatabaseManager,
    group_name: str,
    caches: List[Tuple[str, Optional[float], Optional[float], Optional[int]]],
    today_str: str,
) -> Dict[str, int]:
    """按指定配置爬取一组 cache。"""
    cfg = PROFILES[group_name]
    cookie = cfg["COOKIES"]
    page_sleep = cfg["page_sleep"]
    update_coordinates = cfg["update_coordinates"]

    success_count = 0
    logs_count = 0
    crawled_codes = []
    all_logs = []

    consecutive_token_failures = 0
    MAX_CONSECUTIVE_TOKEN_FAILURES = 5

    logger.info(f"开始处理 {group_name} 组，共 {len(caches)} 个 cache")

    for i, (code, old_lat, old_lng, geocache_type) in enumerate(caches):
        logger.info(f"[{group_name} {i + 1}/{len(caches)}] 处理: {code}")
        accepted_log_types = (
            {ATTENDED_LOG_TYPE}
            if geocache_type in EVENT_CACHE_TYPES
            else {FOUND_LOG_TYPE}
        )

        retry_count = 0
        max_retries = 1
        success = False

        while retry_count <= max_retries and not success:
            try:
                if update_coordinates:
                    new_lat, new_lng = get_coordinates(code, cookie)
                    if new_lat and new_lng:
                        lat_changed = abs(float(new_lat or 0) - float(old_lat or 0)) > 1e-6
                        lng_changed = abs(float(new_lng or 0) - float(old_lng or 0)) > 1e-6
                        if lat_changed or lng_changed:
                            db.update_cache_coordinates(code, new_lat, new_lng)
                            logger.info(f"  更新坐标: ({new_lat}, {new_lng})")
                    time.sleep(1)

                token = get_logbook_token(code, cookie)
                if not token:
                    consecutive_token_failures += 1
                    logger.warning(f"  无法获取 token (连续失败 {consecutive_token_failures}/{MAX_CONSECUTIVE_TOKEN_FAILURES})")

                    if consecutive_token_failures >= MAX_CONSECUTIVE_TOKEN_FAILURES:
                        raise AuthenticationError(
                            f"Authentication failure detected: {MAX_CONSECUTIVE_TOKEN_FAILURES} consecutive caches "
                            f"failed to get logbook token in {group_name} group. Cookie may be invalid."
                        )
                    break

                consecutive_token_failures = 0

                cache_logs, crawl_complete = fetch_logs_for_cache_result(
                    code,
                    token,
                    page_sleep,
                    cookie,
                    accepted_log_types=accepted_log_types,
                )

                if not crawl_complete:
                    retry_count += 1
                    if retry_count <= max_retries:
                        logger.warning(f"  logs 未完整获取，{code} 将在 1 秒后重试...")
                        time.sleep(1)
                        continue
                    logger.error(f"  logs 未完整获取，跳过更新 logs_crawled_at: {code}")
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

        if (i + 1) % 30 == 0:
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

    logger.info(f"{group_name} 组处理完成: 成功 {success_count}, 新增 logs {logs_count}")
    return {"success_count": success_count, "logs_count": logs_count}


def run_logs_crawler():
    """单次运行，先查全量 cache，再按 premium / nonpremium 分组处理。"""
    db = DatabaseManager(DATABASE_URL)
    db.connect()

    try:
        logger.info("加载全部 cache 列表...")
        caches = db.get_all_caches_to_crawl()
        premium_caches = [
            (code, lat, lng, geocache_type)
            for code, lat, lng, premium_only, geocache_type in caches
            if premium_only
        ]
        nonpremium_caches = [
            (code, lat, lng, geocache_type)
            for code, lat, lng, premium_only, geocache_type in caches
            if not premium_only
        ]
        logger.info(
            f"需要处理 {len(caches)} 个 cache，其中 premium {len(premium_caches)} 个，"
            f"nonpremium {len(nonpremium_caches)} 个"
        )

        today_str = datetime.now().strftime("%Y-%m-%d")
        premium_stats = crawl_cache_group(db, "premium", premium_caches, today_str)
        nonpremium_stats = crawl_cache_group(db, "nonpremium", nonpremium_caches, today_str)

        logger.info("=" * 50)
        logger.info(
            "Logs 爬取完成! "
            f"总成功: {premium_stats['success_count'] + nonpremium_stats['success_count']}, "
            f"总新增 logs: {premium_stats['logs_count'] + nonpremium_stats['logs_count']}"
        )
        logger.info(
            f"分组统计: premium 成功 {premium_stats['success_count']} / logs {premium_stats['logs_count']}, "
            f"nonpremium 成功 {nonpremium_stats['success_count']} / logs {nonpremium_stats['logs_count']}"
        )
        logger.info("=" * 50)

    except Exception:
        logger.exception("Logs 爬虫运行出错")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    try:
        run_logs_crawler()
    except Exception:
        logger.exception("crawl_logs failed")
        raise
