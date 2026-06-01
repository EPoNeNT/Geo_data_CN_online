#!/usr/bin/env python3
"""
Geocache 爬虫 - 适配 Neon 数据库
基于 get_caches.py 的逻辑重写
"""
import os
import sys
import json
import time
import random
import re
from datetime import datetime, timezone
from typing import Any, List, Dict, Set, Optional, Tuple

import requests
import psycopg2
from psycopg2.extras import execute_batch
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from runtime_utils import (
    AuthenticationError,
    connect_postgres,
    is_login_url,
    looks_like_login_page,
    optional_cookie,
    require_cookie,
    require_env,
    setup_logging,
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

logger = setup_logging("crawl_caches.log")

# 配置
DATABASE_URL = require_env("DATABASE_URL")
COOKIE = require_cookie("GEOCOOKIE_NONPREMIUM", "GEOCACHING_COOKIE")
PREMIUM_COOKIE = optional_cookie("GEOCOOKIE_PREMIUM")
_RAW_NONPREMIUM_COOKIE = (os.environ.get("GEOCOOKIE_NONPREMIUM") or os.environ.get("GEOCACHING_COOKIE") or "").strip().strip('"').strip("'")
_RAW_PREMIUM_COOKIE = (os.environ.get("GEOCOOKIE_PREMIUM") or "").strip().strip('"').strip("'")
VERSION = "20260403.2.3046"
MAP_VERSION_OVERRIDE = os.environ.get("GEOCACHING_MAP_VERSION")
MAP_PAGE_URL = (
    "https://www.geocaching.com/play/map?"
    "undefined=&lat=22.557646164617534&lng=113.98289150000005&"
    "mlat=22.55679369907677&mlng=113.99826049804688&zoom=12&r=10&"
    "box=22.689418753538202%2C113.7689208984375%2C22.42594516815436%2C114.19670104980469&"
    "st=N+22%C2%B0+33.459%27+E+113%C2%B0+58.973%27&ot=coords"
)
ALLOWED_COUNTRIES = ["China", "Hong Kong", "Taiwan", "Macao"]
MAX_RETRIES = 3
CACHE_DELETED_STATUS = 404
DELETED_PREVIEW_MARKER = "__deleted_cache"
_DETECTED_MAP_VERSION = None

# 设置重试策略
session = requests.Session()
retries = Retry(total=5, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retries))


def normalize_map_version(raw_version: Optional[str]) -> Optional[str]:
    """标准化地图版本号，兼容 release- 前缀。"""
    if not raw_version:
        return None

    version = str(raw_version).strip().strip('"').strip("'")
    if version.startswith("release-"):
        version = version[len("release-"):]
    return version or None


def detect_map_version(force_refresh: bool = False) -> str:
    """自动检测 geocaching 地图页使用的 release 版本。"""
    global _DETECTED_MAP_VERSION

    override_version = normalize_map_version(MAP_VERSION_OVERRIDE)
    if override_version:
        return override_version

    if _DETECTED_MAP_VERSION and not force_refresh:
        return _DETECTED_MAP_VERSION

    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "cache-control": "no-cache",
        "cookie": COOKIE,
        "pragma": "no-cache",
        "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "document",
        "sec-fetch-mode": "navigate",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    }

    try:
        response = session.get(MAP_PAGE_URL, headers=headers, timeout=20)
    except requests.RequestException as exc:
        raise RuntimeError(
            f"Failed to fetch map page while detecting map version: network error: {exc}"
        ) from exc

    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        if response.status_code in {401, 403}:
            raise AuthenticationError(
                f"Authentication failed while detecting map version: HTTP {response.status_code}"
            ) from exc
        raise RuntimeError(
            f"Failed to fetch map page while detecting map version: HTTP {response.status_code}"
        ) from exc

    page_text = response.text

    if looks_like_login_page(response.url, page_text):
        raise AuthenticationError(
            "Authentication failed while detecting map version: redirected to login page"
        )

    patterns = [
        r"/_next/data/release-([^/]+)/en/play/map\.json",
        r'"buildId":"(release-[^"]+)"',
        r'"buildId":"([^"]+)"',
        r"sentry-release=release-([0-9.]+)",
        r"release-([0-9]{8}\.[0-9]+\.[0-9]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, page_text)
        if match:
            version = normalize_map_version(match.group(1))
            if version:
                _DETECTED_MAP_VERSION = version
                logger.info(f"自动检测到地图版本: {version}")
                return version

    raise RuntimeError("Map page structure changed: buildId not found in map page response")


class DatabaseManager:
    """数据库管理类"""
    
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.conn = None
        self.cursor = None
    
    def connect(self):
        """连接数据库，设置 keepalive 参数保持连接"""
        self.conn = connect_postgres(
            self.database_url,
            logger=logger,
            connect_timeout=10,
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5
        )
        self.cursor = self.conn.cursor()
        logger.info("数据库连接成功")
    
    def reconnect(self):
        """重新连接数据库"""
        logger.info("尝试重新连接数据库...")
        self.close()
        self.connect()
        logger.info("数据库重新连接成功")
    
    def close(self):
        """关闭连接"""
        if self.cursor:
            self.cursor.close()
        if self.conn:
            self.conn.close()
        logger.info("数据库连接已关闭")
    
    def get_existing_caches(self) -> Dict[str, dict]:
        """获取已存在的 cache 数据（分批加载避免超时）"""
        batch_size = 2000
        offset = 0
        result = {}

        while True:
            self.cursor.execute("""
                SELECT id, name, code, premium_only, favorite_points,
                       geocache_type, container_type, difficulty, terrain,
                       cache_status, latitude, longitude, details_url,
                       placed_date, owner_username, last_found_date,
                       trackable_count, region, country, attributes
                FROM caches
                ORDER BY code
                LIMIT %s OFFSET %s
            """, (batch_size, offset))

            rows = self.cursor.fetchall()
            if not rows:
                break

            for row in rows:
                result[row[2]] = {
                    'id': row[0], 'name': row[1], 'code': row[2],
                    'premium_only': row[3], 'favorite_points': row[4],
                    'geocache_type': row[5], 'container_type': row[6],
                    'difficulty': row[7], 'terrain': row[8],
                    'cache_status': row[9], 'latitude': row[10],
                    'longitude': row[11], 'details_url': row[12],
                    'placed_date': row[13], 'owner_username': row[14],
                    'last_found_date': row[15], 'trackable_count': row[16],
                    'region': row[17], 'country': row[18], 'attributes': row[19]
                }

            offset += batch_size
            logger.info(f"已加载 {len(result)} 条缓存记录...")

        return result
    
    def upsert_cache(self, cache_data: dict):
        """插入或更新 cache"""
        self.cursor.execute("""
            INSERT INTO caches (
                id, name, code, premium_only, favorite_points,
                geocache_type, container_type, difficulty, terrain,
                cache_status, latitude, longitude, details_url,
                placed_date, owner_username, last_found_date,
                trackable_count, region, country, attributes
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (code) DO UPDATE SET
                name = EXCLUDED.name,
                premium_only = EXCLUDED.premium_only,
                favorite_points = EXCLUDED.favorite_points,
                geocache_type = EXCLUDED.geocache_type,
                container_type = EXCLUDED.container_type,
                difficulty = EXCLUDED.difficulty,
                terrain = EXCLUDED.terrain,
                cache_status = EXCLUDED.cache_status,
                latitude = EXCLUDED.latitude,
                longitude = EXCLUDED.longitude,
                details_url = EXCLUDED.details_url,
                placed_date = EXCLUDED.placed_date,
                owner_username = EXCLUDED.owner_username,
                last_found_date = EXCLUDED.last_found_date,
                trackable_count = EXCLUDED.trackable_count,
                region = EXCLUDED.region,
                country = EXCLUDED.country,
                attributes = EXCLUDED.attributes
        """, (
            cache_data['id'], cache_data['name'], cache_data['code'],
            cache_data['premium_only'], cache_data['favorite_points'],
            cache_data['geocache_type'], cache_data['container_type'],
            cache_data['difficulty'], cache_data['terrain'],
            cache_data['cache_status'], cache_data['latitude'],
            cache_data['longitude'], cache_data['details_url'],
            cache_data['placed_date'], cache_data['owner_username'],
            cache_data['last_found_date'], cache_data['trackable_count'],
            cache_data['region'], cache_data['country'],
            json.dumps(cache_data['attributes']) if cache_data['attributes'] else None
        ))

    def upsert_caches_batch(self, caches: List[dict]):
        """批量插入或更新 cache（分小批次避免超时）"""
        if not caches:
            return

        batch_size = 50  # 减小批次大小
        insert_query = """
            INSERT INTO caches (
                id, name, code, premium_only, favorite_points,
                geocache_type, container_type, difficulty, terrain,
                cache_status, latitude, longitude, details_url,
                placed_date, owner_username, last_found_date,
                trackable_count, region, country, attributes
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (code) DO UPDATE SET
                name = EXCLUDED.name,
                premium_only = EXCLUDED.premium_only,
                favorite_points = EXCLUDED.favorite_points,
                geocache_type = EXCLUDED.geocache_type,
                container_type = EXCLUDED.container_type,
                difficulty = EXCLUDED.difficulty,
                terrain = EXCLUDED.terrain,
                cache_status = EXCLUDED.cache_status,
                latitude = EXCLUDED.latitude,
                longitude = EXCLUDED.longitude,
                details_url = EXCLUDED.details_url,
                placed_date = EXCLUDED.placed_date,
                owner_username = EXCLUDED.owner_username,
                last_found_date = EXCLUDED.last_found_date,
                trackable_count = EXCLUDED.trackable_count,
                region = EXCLUDED.region,
                country = EXCLUDED.country,
                attributes = EXCLUDED.attributes
        """

        # 分批处理
        for i in range(0, len(caches), batch_size):
            batch = caches[i:i + batch_size]
            params = []
            for cache_data in batch:
                params.append((
                    cache_data['id'], cache_data['name'], cache_data['code'],
                    cache_data['premium_only'], cache_data['favorite_points'],
                    cache_data['geocache_type'], cache_data['container_type'],
                    cache_data['difficulty'], cache_data['terrain'],
                    cache_data['cache_status'], cache_data['latitude'],
                    cache_data['longitude'], cache_data['details_url'],
                    cache_data['placed_date'], cache_data['owner_username'],
                    cache_data['last_found_date'], cache_data['trackable_count'],
                    cache_data['region'], cache_data['country'],
                    json.dumps(cache_data['attributes']) if cache_data['attributes'] else None
                ))

            max_retries = 3
            for attempt in range(max_retries):
                try:
                    execute_batch(self.cursor, insert_query, params, page_size=25)
                    break  # 成功则跳出重试循环
                except psycopg2.OperationalError as e:
                    if attempt < max_retries - 1:
                        logger.warning(f"批量插入失败，尝试重新连接 ({attempt + 1}/{max_retries})...")
                        self.reconnect()
                    else:
                        logger.error(f"批量插入失败: {e}")
                        raise

    
    def get_cache_statuses_batch(self, codes: List[str]) -> Dict[str, int]:
        """批量获取缓存状态"""
        if not codes:
            return {}
        
        # 分批查询，避免单次查询过大
        batch_size = 1000
        result = {}
        
        for i in range(0, len(codes), batch_size):
            batch_codes = codes[i:i + batch_size]
            placeholders = ','.join(['%s'] * len(batch_codes))
            
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    self.cursor.execute(f"""
                        SELECT code, cache_status FROM caches 
                        WHERE code IN ({placeholders})
                    """, batch_codes)
                    
                    rows = self.cursor.fetchall()
                    for row in rows:
                        result[row[0]] = row[1]
                    break  # 成功则跳出重试循环
                except psycopg2.OperationalError as e:
                    if attempt < max_retries - 1:
                        logger.warning(f"批量查询缓存状态失败，尝试重新连接 ({attempt + 1}/{max_retries})...")
                        self.reconnect()
                    else:
                        logger.error(f"批量查询缓存状态失败: {e}")
                        raise
        
        return result
    
    def update_cache_status(self, code: str, status: int):
        """更新 cache 状态"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.cursor.execute("""
                    UPDATE caches SET cache_status = %s
                    WHERE code = %s
                """, (status, code))
                return
            except psycopg2.OperationalError as e:
                if attempt < max_retries - 1:
                    logger.warning(f"更新 cache 状态失败，尝试重新连接 ({attempt + 1}/{max_retries})...")
                    self.reconnect()
                else:
                    logger.error(f"更新 cache 状态失败: {e}")
                    raise
    
    def commit(self):
        """提交事务"""
        self.conn.commit()


def get_pending_grids_from_db(db: DatabaseManager) -> List[Tuple[int, List[float]]]:
    """从数据库获取待处理的网格配置 (返回 id 和 bounds 列表)"""
    logger.info("执行 SQL 查询: SELECT id, grid_bounds FROM crawl_progress WHERE status = 'pending'")
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # grid_bounds 现在只存储 bounds 数组，不是整个 grid 对象
            db.cursor.execute("SELECT id, grid_bounds FROM crawl_progress WHERE status = 'pending' ORDER BY id")
            rows = db.cursor.fetchall()
            logger.info(f"SQL 查询完成，返回 {len(rows)} 行数据")
            return [(row[0], row[1]) for row in rows]
        except psycopg2.OperationalError as e:
            if attempt < max_retries - 1:
                logger.warning(f"数据库连接断开，尝试重新连接 ({attempt + 1}/{max_retries})...")
                db.reconnect()
            else:
                logger.error(f"从数据库获取待处理网格失败: {e}")
                return []
        except Exception as e:
            logger.error(f"从数据库获取待处理网格失败: {e}")
            return []
    return []


def mark_grid_completed(db: DatabaseManager, grid_id: int, cache_count: int):
    """标记网格为已完成，并更新 cache_count"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            db.cursor.execute("""
                UPDATE crawl_progress 
                SET status = 'completed', cache_count = %s
                WHERE id = %s
            """, (cache_count, grid_id))
            return
        except psycopg2.OperationalError as e:
            if attempt < max_retries - 1:
                logger.warning(f"数据库连接断开，尝试重新连接 ({attempt + 1}/{max_retries})...")
                db.reconnect()
            else:
                raise


def add_new_grids_to_db(db: DatabaseManager, grids: List[dict]):
    """将新的网格添加到数据库（用于第二轮爬取）
    grids 是 [{"bounds": [...], "count": None}, ...] 格式
    """
    max_retries = 3
    for attempt in range(max_retries):
        try:
            for grid in grids:
                bounds = grid["bounds"]
                count = grid.get("count")
                db.cursor.execute("""
                    INSERT INTO crawl_progress (grid_bounds, cache_count, status)
                    VALUES (%s, %s, 'pending')
                """, (json.dumps(bounds), count))
            logger.info(f"添加了 {len(grids)} 个新网格到数据库")
            return
        except psycopg2.OperationalError as e:
            if attempt < max_retries - 1:
                logger.warning(f"数据库连接断开，尝试重新连接 ({attempt + 1}/{max_retries})...")
                db.reconnect()
            else:
                raise


def reset_all_grids_to_pending(db: DatabaseManager) -> int:
    """将所有已完成的网格重置为 pending 状态，返回重置的数量"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            db.cursor.execute("UPDATE crawl_progress SET status = 'pending'")
            count = db.cursor.rowcount
            db.commit()
            logger.info(f"已将 {count} 个网格重置为待处理状态")
            return count
        except psycopg2.OperationalError as e:
            if attempt < max_retries - 1:
                logger.warning(f"数据库连接断开，尝试重新连接 ({attempt + 1}/{max_retries})...")
                db.reconnect()
            else:
                raise
    return 0


def split_grid(grid_bounds: List[float]) -> List[dict]:
    """将网格切分为四个子网格"""
    max_lat, min_lng, min_lat, max_lng = grid_bounds
    mid_lat = (max_lat + min_lat) / 2
    mid_lng = (max_lng + min_lng) / 2
    
    return [
        {"bounds": [max_lat, min_lng, mid_lat, mid_lng], "count": None},
        {"bounds": [mid_lat, min_lng, min_lat, mid_lng], "count": None},
        {"bounds": [max_lat, mid_lng, mid_lat, max_lng], "count": None},
        {"bounds": [mid_lat, mid_lng, min_lat, max_lng], "count": None},
    ]


def safe_fetch(max_lat: float, max_lng: float, min_lat: float, min_lng: float) -> Optional[dict]:
    """安全地获取 API 数据"""
    box_str = f"{max_lat},{min_lng},{min_lat},{max_lng}"

    headers = {
        "accept": "*/*",
        "cookie": COOKIE,
        "referer": "https://www.geocaching.com/play/map",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "x-nextjs-data": "1"
    }

    version = detect_map_version()
    refresh_attempted = False

    for attempt in range(MAX_RETRIES):
        try:
            base_url = f"https://www.geocaching.com/_next/data/release-{version}/en/play/map.json"
            full_url = f"{base_url}?box={box_str}"
            response = session.get(full_url, headers=headers, timeout=25)
            
            if response.status_code == 200:
                data = response.json()
                redirect_url = data.get("pageProps", {}).get("__N_REDIRECT")
                if redirect_url:
                    if is_login_url(redirect_url):
                        raise AuthenticationError(
                            f"Authentication failed: map API redirected to login page: {redirect_url}"
                        )
                    logger.warning(f"[Redirect] 地图接口返回重定向: {redirect_url}")
                    time.sleep(2 * (attempt + 1))
                    continue
                return data
            elif response.status_code == 404 and not refresh_attempted and not MAP_VERSION_OVERRIDE:
                logger.warning(f"[404 Not Found] 地图版本 {version} 可能已失效，尝试重新检测...")
                version = detect_map_version(force_refresh=True)
                refresh_attempted = True
                continue
            elif response.status_code == 403:
                if attempt == MAX_RETRIES - 1:
                    raise AuthenticationError(
                        "Authentication failed: map API returned HTTP 403 after retries"
                    )
                logger.warning(f"[403 Forbidden] Cookie可能失效。等待{10 * (attempt + 1)}秒...")
                time.sleep(10 * (attempt + 1))
            elif response.status_code == 429:
                logger.warning("[429 Too Many Requests] 触发限流。休眠...")
                time.sleep(60)
            else:
                logger.warning(f"[Error {response.status_code}] 正在重试...")
                time.sleep(5 * (attempt + 1))
        except AuthenticationError:
            raise
        except Exception as e:
            logger.error(f"[网络异常] {e}. 正在重试 ({attempt + 1}/{MAX_RETRIES})...")
            time.sleep(10 * (attempt + 1))
    
    return None


def is_deleted_cache_preview(data: Optional[dict]) -> bool:
    return bool(data and data.get(DELETED_PREVIEW_MARKER))


def fetch_cache_preview(gc_code: str) -> Optional[dict]:
    """按 GC code 获取单个 cache 的预览数据。"""
    api_url = f"https://www.geocaching.com/api/live/v1/search/geocachepreview/{gc_code}"

    cookie_candidates = []
    seen_cookies = set()
    for label, cookie in (("nonpremium", _RAW_NONPREMIUM_COOKIE), ("premium", _RAW_PREMIUM_COOKIE)):
        if not cookie or cookie in seen_cookies:
            continue
        cookie_candidates.append((label, cookie))
        seen_cookies.add(cookie)

    all_candidates_returned_404 = bool(cookie_candidates)

    for label, cookie in cookie_candidates:
        headers = {
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "cache-control": "no-cache",
            "cookie": cookie,
            "pragma": "no-cache",
            "referer": "https://www.geocaching.com/play/map",
            "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            "x-nextjs-data": "1",
        }

        cookie_404_count = 0
        for attempt in range(MAX_RETRIES):
            try:
                response = session.get(api_url, headers=headers, timeout=15)
                if response.status_code == 200:
                    return response.json()
                if response.status_code in {401, 403}:
                    all_candidates_returned_404 = False
                    logger.warning(f"  {gc_code} 预览接口使用 {label} cookie 返回 HTTP {response.status_code}")
                    break
                if response.status_code == CACHE_DELETED_STATUS:
                    cookie_404_count += 1
                    logger.warning(
                        f"  {gc_code} 预览接口使用 {label} cookie 返回 HTTP 404 "
                        f"({cookie_404_count}/{MAX_RETRIES})"
                    )
                    time.sleep(5 * (attempt + 1))
                    continue
                if response.status_code == 429:
                    all_candidates_returned_404 = False
                    time.sleep(60)
                    continue
                all_candidates_returned_404 = False
                logger.warning(f"  {gc_code} 预览接口使用 {label} cookie 返回 HTTP {response.status_code}")
                time.sleep(5 * (attempt + 1))
            except Exception as exc:
                all_candidates_returned_404 = False
                logger.warning(f"  {gc_code} 预览接口使用 {label} cookie 请求失败: {exc}")
                time.sleep(10 * (attempt + 1))

        if cookie_404_count != MAX_RETRIES:
            all_candidates_returned_404 = False

    if len(cookie_candidates) >= 2 and all_candidates_returned_404:
        logger.warning(f"  {gc_code} 两个 cookie 均连续 {MAX_RETRIES} 次返回 404")
        page_data = _fetch_archived_cache_page_data(gc_code)
        if page_data is not None:
            page_data["cacheStatus"] = 2
            fields = [k for k in page_data if k != "code"]
            logger.info(f"  {gc_code} 预览接口返回 404 但网页可访问，从详情页提取 {len(fields)} 个字段")
            return page_data
        logger.warning(f"  {gc_code} 网页也不可访问，本次爬取跳过")

    return None


def _fetch_archived_cache_page_data(gc_code: str) -> Optional[dict]:
    """从 cache 详情页提取已归档 cache 的元数据（preview API 对已归档 cache 返回 404）。

    返回 API 格式的 dict（code, name, cacheStatus, owner->username 等），
    以便下游 process_cache_item 能正常处理。
    """
    detail_url = f"https://www.geocaching.com/geocache/{gc_code}"

    type_names = {
        "traditional": 2, "traditional cache": 2,
        "multi": 3, "multi-cache": 3, "multicache": 3,
        "mystery": 8, "puzzle": 8, "unknown": 8,
        "earthcache": 13, "earth": 13,
        "letterbox": 5, "letterbox hybrid": 5,
        "wherigo": 19,
        "virtual": 4,
        "webcam": 11,
        "event": 6, "cito": 7, "mega": 453, "giga": 4732,
    }
    container_names = {
        "micro": 2, "small": 3, "regular": 4, "large": 5,
        "other": 8, "not chosen": 8, "virtual": 6,
    }

    for label, cookie in (("nonpremium", _RAW_NONPREMIUM_COOKIE), ("premium", _RAW_PREMIUM_COOKIE)):
        if not cookie:
            continue
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "cookie": cookie,
            "referer": "https://www.geocaching.com/play/map",
            "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        }
        try:
            resp = session.get(detail_url, headers=headers, timeout=15, allow_redirects=True)
            if resp.status_code == 404:
                return None
            if resp.status_code != 200 or "geocache" not in resp.url:
                continue

            text = resp.text
            result: dict[str, Any] = {"code": gc_code}

            jsonld_match = re.search(
                r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                text, re.DOTALL | re.IGNORECASE,
            )
            if jsonld_match:
                try:
                    ld = json.loads(jsonld_match.group(1))
                    if isinstance(ld, dict):
                        if ld.get("name"):
                            result["name"] = ld["name"]
                        if ld.get("placedBy"):
                            result["owner"] = {"username": ld["placedBy"]}
                        if ld.get("datePublished"):
                            result["placedDate"] = ld["datePublished"]
                        loc = ld.get("location")
                        if isinstance(loc, dict):
                            lat = loc.get("latitude")
                            lng = loc.get("longitude")
                            if lat is not None and lng is not None:
                                result["postedCoordinates"] = {"latitude": float(lat), "longitude": float(lng)}
                        contained = ld.get("containedInPlace")
                        if isinstance(contained, dict) and contained.get("name"):
                            result["country"] = contained["name"]
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass

            if "owner" not in result:
                author_match = re.search(
                    r'<meta[^>]*name="author"[^>]*content="([^"]*)"',
                    text, re.IGNORECASE,
                )
                if author_match:
                    result["owner"] = {"username": author_match.group(1).strip()}

            if "name" not in result:
                title_match = re.search(
                    r'<title>(?:GC\w+\s*-\s*)?([^<]+)</title>',
                    text, re.IGNORECASE,
                )
                if title_match:
                    result["name"] = title_match.group(1).strip()
                else:
                    og_match = re.search(
                        r'<meta[^>]*property="og:title"[^>]*content="[^"]*-\s*([^"]*)"',
                        text, re.IGNORECASE,
                    )
                    if og_match:
                        result["name"] = og_match.group(1).strip()

            diff_match = re.search(r'[Dd]ifficulty[:\s]*([\d.]+)', text)
            if diff_match:
                try:
                    result["difficulty"] = round(float(diff_match.group(1)), 1)
                except (TypeError, ValueError):
                    pass

            terr_match = re.search(r'[Tt]errain[:\s]*([\d.]+)', text)
            if terr_match:
                try:
                    result["terrain"] = round(float(terr_match.group(1)), 1)
                except (TypeError, ValueError):
                    pass

            type_match = re.search(r'[Tt]ype[:\s]*([^<]+)', text)
            if type_match:
                type_text = type_match.group(1).strip().lower()
                for key, type_id in type_names.items():
                    if key in type_text:
                        result["geocacheType"] = type_id
                        break

            size_match = re.search(r'(?:[Ss]ize|[Cc]ontainer)[:\s]*([^<]+)', text)
            if size_match:
                size_text = size_match.group(1).strip().lower()
                for key, type_id in container_names.items():
                    if key in size_text:
                        result["containerType"] = type_id
                        break

            return result
        except Exception:
            continue
    return None


def check_cache_status(gc_code: str) -> Optional[int]:
    """检查单个 cache 的状态"""
    data = fetch_cache_preview(gc_code)
    if not data:
        return None
    return data.get('cacheStatus')


def process_cache_item(
    item: dict,
    min_lat: Optional[float] = None,
    max_lat: Optional[float] = None,
    min_lng: Optional[float] = None,
    max_lng: Optional[float] = None,
    require_allowed_country: bool = True,
) -> Optional[dict]:
    """处理单个 cache 项"""
    cache_lat = item.get('postedCoordinates', {}).get('latitude')
    cache_lng = item.get('postedCoordinates', {}).get('longitude')
    code = item.get('code')
    country = item.get('country')
    
    # 必须有 code
    if not code:
        return None
    
    # 检查是否是 premium_only
    is_premium = item.get('premiumOnly') == True or item.get('premiumOnly') == 'TRUE'

    has_bounds = all(value is not None for value in (min_lat, max_lat, min_lng, max_lng))
    if has_bounds:
        # 如果不是 premium_only，必须有坐标
        if not is_premium and not all([cache_lat, cache_lng]):
            return None

        # 检查是否在框内或是 premium
        # premium_only 的 cache 没有坐标，所以跳过坐标检查
        if is_premium:
            is_in_box = False
        else:
            is_in_box = (min_lat <= cache_lat <= max_lat) and (min_lng <= cache_lng <= max_lng)

        if not (is_in_box or is_premium):
            return None
    
    if require_allowed_country and country not in ALLOWED_COUNTRIES:
        return None
    
    # 处理属性
    attr_list = item.get('attributes', [])
    filtered_attrs = [{"id": a['id'], "name": a['name']} for a in attr_list if a.get('isApplicable')]
    
    return {
        'id': item.get('id'),
        'name': item.get('name'),
        'code': code,
        'premium_only': is_premium,
        'favorite_points': item.get('favoritePoints'),
        'geocache_type': item.get('geocacheType'),
        'container_type': item.get('containerType'),
        'difficulty': item.get('difficulty'),
        'terrain': item.get('terrain'),
        'cache_status': item.get('cacheStatus'),
        'latitude': cache_lat,
        'longitude': cache_lng,
        'details_url': item.get('detailsUrl'),
        'placed_date': item.get('placedDate'),
        'owner_username': item.get('owner', {}).get('username'),
        'last_found_date': item.get('lastFoundDate'),
        'trackable_count': item.get('trackableCount'),
        'region': item.get('region'),
        'country': country,
        'attributes': filtered_attrs
    }


def normalize_datetime_value(value) -> Optional[str]:
    """归一化时间值，避免数据库和 API 格式差异导致误判。"""
    if value is None:
        return None

    if isinstance(value, datetime):
        dt = value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
        return dt.isoformat(sep=' ', timespec='seconds')

    text = str(value).strip()
    if not text or text.lower() in {'none', 'nan'}:
        return None

    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if parsed.tzinfo:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed.isoformat(sep=' ', timespec='seconds')
    except ValueError:
        return text


def normalize_cache_field(field: str, value):
    """归一化字段值，稳定比较 API 返回值和数据库现有值。"""
    if field == 'attributes':
        raw = value
        if isinstance(raw, str):
            text = raw.strip()
            if text.lower() in {'', 'null', 'none'}:
                raw = []
            else:
                try:
                    raw = json.loads(text)
                except json.JSONDecodeError:
                    return text

        if raw is None:
            raw = []

        if isinstance(raw, list):
            normalized = []
            for item in raw:
                if isinstance(item, dict):
                    normalized.append({
                        'id': item.get('id'),
                        'name': item.get('name'),
                    })
                else:
                    normalized.append(item)
            normalized.sort(key=lambda item: (
                item.get('id') if isinstance(item, dict) else str(item),
                item.get('name') if isinstance(item, dict) else str(item),
            ))
            return json.dumps(normalized, ensure_ascii=False, sort_keys=True)

        return json.dumps(raw, ensure_ascii=False, sort_keys=True)

    if value is None:
        return None

    if field in {'latitude', 'longitude', 'difficulty', 'terrain'}:
        try:
            precision = 6 if field in {'latitude', 'longitude'} else 1
            return round(float(value), precision)
        except (TypeError, ValueError):
            return str(value).strip()

    if field in {'id', 'favorite_points', 'geocache_type', 'container_type', 'cache_status', 'trackable_count'}:
        try:
            return int(value)
        except (TypeError, ValueError):
            return str(value).strip()

    if field == 'premium_only':
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() == 'true'

    if field in {'placed_date', 'last_found_date'}:
        return normalize_datetime_value(value)

    return str(value).strip()


def cache_metadata_changed(existing: dict, latest: dict) -> bool:
    """比较缓存元数据；premium cache 不在这里用空坐标覆盖已有坐标。"""
    compare_keys = [
        'id', 'name', 'code', 'premium_only', 'favorite_points',
        'geocache_type', 'container_type', 'difficulty', 'terrain',
        'cache_status', 'details_url', 'placed_date', 'owner_username',
        'last_found_date', 'trackable_count', 'region', 'country', 'attributes',
    ]

    if not latest.get('premium_only', False):
        compare_keys.extend(['latitude', 'longitude'])

    for key in compare_keys:
        if normalize_cache_field(key, latest.get(key)) != normalize_cache_field(key, existing.get(key)):
            return True

    return False


def backfill_missing_coordinates(db: DatabaseManager) -> int:
    """对缺少坐标的缓存，通过详情页 JSON-LD 获取坐标并回填。"""
    db.cursor.execute(
        """
        SELECT code FROM caches
        WHERE (latitude IS NULL OR longitude IS NULL)
          AND country IN ('China', 'Hong Kong', 'Macao', 'Taiwan')
        ORDER BY code
        """
    )
    codes = [row[0] for row in db.cursor.fetchall()]
    if not codes:
        logger.info("坐标回填：没有缺少坐标的缓存")
        return 0

    logger.info("坐标回填：发现 %s 个缺少坐标的缓存", len(codes))
    updated = 0

    for i, code in enumerate(codes):
        detail_url = f"https://www.geocaching.com/geocache/{code}"
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "cookie": _RAW_PREMIUM_COOKIE or "",
            "referer": "https://www.geocaching.com/play/map",
            "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        }

        try:
            resp = session.get(detail_url, headers=headers, timeout=15, allow_redirects=True)
            if resp.status_code != 200 or "geocache" not in resp.url:
                logger.warning("坐标回填 [%s/%s] %s: HTTP %s 或非详情页", i + 1, len(codes), code, resp.status_code)
                time.sleep(random.uniform(0.5, 1.0))
                continue

            text = resp.text
            jsonld_match = re.search(
                r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                text, re.DOTALL | re.IGNORECASE,
            )
            if not jsonld_match:
                logger.warning("坐标回填 [%s/%s] %s: 未找到 JSON-LD", i + 1, len(codes), code)
                time.sleep(random.uniform(0.5, 1.0))
                continue

            ld = json.loads(jsonld_match.group(1))
            if not isinstance(ld, dict):
                continue

            loc = ld.get("location")
            if not isinstance(loc, dict):
                logger.warning("坐标回填 [%s/%s] %s: JSON-LD 中无 location", i + 1, len(codes), code)
                time.sleep(random.uniform(0.5, 1.0))
                continue

            lat = loc.get("latitude")
            lng = loc.get("longitude")
            if lat is None or lng is None:
                logger.warning("坐标回填 [%s/%s] %s: location 中无坐标", i + 1, len(codes), code)
                time.sleep(random.uniform(0.5, 1.0))
                continue

            lat = float(lat)
            lng = float(lng)
            db.cursor.execute(
                "UPDATE caches SET latitude = %s, longitude = %s WHERE code = %s",
                (round(lat, 6), round(lng, 6), code),
            )
            updated += 1
            logger.info("坐标回填 [%s/%s] %s: 坐标更新为 (%s, %s)", i + 1, len(codes), code, lat, lng)

        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("坐标回填 [%s/%s] %s: 解析失败 - %s", i + 1, len(codes), code, e)
        except Exception as e:
            logger.warning("坐标回填 [%s/%s] %s: 请求失败 - %s", i + 1, len(codes), code, e)

        time.sleep(random.uniform(0.5, 1.0))

    if updated:
        db.commit()
    logger.info("坐标回填：完成，成功更新 %s/%s 个缓存", updated, len(codes))
    return updated


def run_crawler():
    """运行爬虫"""
    # 连接数据库
    db = DatabaseManager(DATABASE_URL)
    db.connect()
    
    try:
        # 加载已有数据
        logger.info("加载已有 cache 数据...")
        scanned_data = db.get_existing_caches()
        logger.info(f"已加载 {len(scanned_data)} 条记录")
        
        # 记录原始 code（排除已归档的）
        logger.info("开始筛选原始 code...")
        original_codes = {
            code for code, row in scanned_data.items()
            if row.get('cache_status') != 2
        }
        logger.info(f"记录原始数据中的 code 数量: {len(original_codes)}")
        
        # 从数据库获取待处理的网格
        logger.info("开始从数据库加载待处理网格...")
        pending_grids = get_pending_grids_from_db(db)
        logger.info(f"从数据库加载了 {len(pending_grids)} 个待处理网格")
        
        if not pending_grids:
            logger.info("没有待处理的网格，自动重置所有网格为待处理状态...")
            reset_count = reset_all_grids_to_pending(db)
            if reset_count > 0:
                pending_grids = get_pending_grids_from_db(db)
                logger.info(f"重新加载了 {len(pending_grids)} 个待处理网格")
            else:
                logger.info("数据库中没有网格数据，请先运行 init_db.py")
                return False
        
        current_crawl_codes = set()
        new_codes = set()
        updated_codes = set()
        crawled_bounds: list[tuple[float, float, float, float]] = []
        commit_counter = 0  # 提交计数器
        COMMIT_INTERVAL = 10  # 每10个网格提交一次
        
        # 处理待处理网格（支持动态添加子网格）
        processed_grids = set()
        grids_to_process = pending_grids.copy()
        
        while grids_to_process:
            # 取出第一个网格进行处理
            grid_id, bounds = grids_to_process.pop(0)
            
            # 检查是否已处理过
            if grid_id in processed_grids:
                continue
            processed_grids.add(grid_id)
            
            # bounds 现在是直接的数组 [max_lat, min_lng, min_lat, max_lng]
            max_lat, min_lng, min_lat, max_lng = bounds
            
            logger.info(
                f"[已处理 {len(processed_grids)} 个 | 队列剩余 {len(grids_to_process)} 个] 扫描网格: "
                f"Lat {min_lat:.4f}~{max_lat:.4f}, Lng {min_lng:.4f}~{max_lng:.4f}"
            )
            
            data = safe_fetch(max_lat, max_lng, min_lat, min_lng)
            if not data:
                logger.warning("无响应，跳过此网格")
                # 标记为已完成（无数据）
                mark_grid_completed(db, grid_id, 0)
                continue
            
            results = data.get('pageProps', {}).get('searchResults', {}).get('results', [])
            cache_count = len(results)
            crawled_bounds.append((max_lat, min_lng, min_lat, max_lng))
            logger.info(f"  获取到 {cache_count} 个结果")
            
            new_in_grid = 0
            updated_in_grid = 0
            
            updated_caches = []
            for item in results:
                cache_data = process_cache_item(item, min_lat, max_lat, min_lng, max_lng)
                if not cache_data:
                    continue

                code = cache_data['code']
                current_crawl_codes.add(code)

                # 检查是否为新数据或已更改
                is_new = code not in scanned_data
                is_different = False

                if not is_new:
                    is_different = cache_metadata_changed(scanned_data[code], cache_data)

                if is_new or is_different:
                    cache_record = dict(cache_data)
                    if not is_new and cache_record.get('premium_only', False):
                        cache_record['latitude'] = scanned_data[code].get('latitude')
                        cache_record['longitude'] = scanned_data[code].get('longitude')

                    updated_caches.append(cache_record)
                    if is_new:
                        new_in_grid += 1
                        new_codes.add(code)
                    else:
                        updated_in_grid += 1
                        updated_codes.add(code)
                    scanned_data[code] = cache_record

            # 将这一网格的变更数据批量写入数据库
            if updated_caches:
                db.upsert_caches_batch(updated_caches)
            
            # 如果缓存数量超过800，需要切分网格
            if cache_count > 800:
                logger.info(f"  网格 cache 数 {cache_count} > 800，需要切分为4个子网格")
                sub_grids = split_grid(bounds)
                
                # 删除原网格（从数据库中真正删除）
                db.cursor.execute("DELETE FROM crawl_progress WHERE id = %s", (grid_id,))
                logger.info(f"  已删除原网格 ID: {grid_id}")
                
                # 添加子网格到数据库并立即处理
                for sub_grid in sub_grids:
                    # 添加子网格到数据库
                    db.cursor.execute("""
                        INSERT INTO crawl_progress (grid_bounds, cache_count, status)
                        VALUES (%s, %s, 'pending')
                    """, (json.dumps(sub_grid["bounds"]), sub_grid.get("count")))
                    
                    # 获取新插入的子网格 ID
                    db.cursor.execute("SELECT lastval()")
                    sub_grid_id = db.cursor.fetchone()[0]
                    
                    # 将子网格添加到当前处理队列
                    grids_to_process.append((sub_grid_id, sub_grid["bounds"]))
                
                logger.info(f"  已添加 {len(sub_grids)} 个子网格到当前爬取队列")
            else:
                # 标记原网格为已完成
                mark_grid_completed(db, grid_id, cache_count)
            
            commit_counter += 1
            
            # 每处理 COMMIT_INTERVAL 个网格提交一次
            if commit_counter >= COMMIT_INTERVAL:
                db.commit()
                logger.info(f"已提交 {commit_counter} 个网格的事务")
                commit_counter = 0
            
            logger.info(f"  新增: {new_in_grid}, 更新: {updated_in_grid}")
            
            # 随机延迟
            time.sleep(random.uniform(5.0, 8.0))
        
        # 处理剩余的提交
        if commit_counter > 0:
            db.commit()
            logger.info(f"已提交 {commit_counter} 个网格的事务")
        
        # 检查本次爬取中未出现的 cache（仅限爬取框内的）
        all_missing = list(original_codes - current_crawl_codes)
        logger.info(f"Caches not found in this crawl: {len(all_missing)}")

        archived_codes: list[str] = []
        skipped_outside_grids = 0
        if crawled_bounds:
            for gc_code in all_missing:
                existing = scanned_data.get(gc_code)
                if not existing:
                    archived_codes.append(gc_code)
                    continue
                lat = existing.get('latitude')
                lng = existing.get('longitude')
                if lat is None or lng is None:
                    archived_codes.append(gc_code)
                    continue
                in_grid = any(
                    min_lat <= lat <= max_lat and min_lng <= lng <= max_lng
                    for max_lat, min_lng, min_lat, max_lng in crawled_bounds
                )
                if in_grid:
                    archived_codes.append(gc_code)
                else:
                    skipped_outside_grids += 1
            if skipped_outside_grids:
                logger.info(f"  跳过 {skipped_outside_grids} 个不在本次爬取框内的 cache")
        else:
            archived_codes = all_missing

        logger.info(f"Potential Archived (within crawled grids): {len(archived_codes)}")
        
        if archived_codes:
            logger.info("逐个检查潜在归档缓存的最新字段...")

            potential_updates = []
            status_only_updates = []
            unchanged_potential = 0
            failed_preview_count = 0

            for gc_code in archived_codes:
                preview_data = fetch_cache_preview(gc_code)
                if not preview_data:
                    failed_preview_count += 1
                    logger.warning(f"  {gc_code} 获取预览数据失败，跳过字段更新")
                    time.sleep(random.uniform(0.5, 1.0))
                    continue

                if is_deleted_cache_preview(preview_data):
                    existing_cache = scanned_data.get(gc_code)
                    if existing_cache and existing_cache.get('cache_status') != CACHE_DELETED_STATUS:
                        status_only_updates.append((gc_code, CACHE_DELETED_STATUS))
                        updated_codes.add(gc_code)
                        logger.info(f"  {gc_code} 已确认删除，仅更新状态为 {CACHE_DELETED_STATUS}")
                    else:
                        unchanged_potential += 1
                        logger.info(f"  {gc_code} 已确认删除，状态无变化")
                    time.sleep(random.uniform(0.5, 1.0))
                    continue

                status = preview_data.get('cacheStatus')
                existing_cache = scanned_data.get(gc_code)
                cache_data = process_cache_item(preview_data, require_allowed_country=False)

                if cache_data and existing_cache:
                    cache_record = dict(cache_data)
                    for key, existing_value in existing_cache.items():
                        if cache_record.get(key) is None and existing_value is not None:
                            cache_record[key] = existing_value

                    if 'attributes' not in preview_data and existing_cache.get('attributes') is not None:
                        cache_record['attributes'] = existing_cache.get('attributes')

                    if (
                        cache_record.get('premium_only', False)
                        or cache_record.get('latitude') is None
                        or cache_record.get('longitude') is None
                    ):
                        cache_record['latitude'] = existing_cache.get('latitude')
                        cache_record['longitude'] = existing_cache.get('longitude')

                    if cache_metadata_changed(existing_cache, cache_record):
                        potential_updates.append(cache_record)
                        updated_codes.add(gc_code)
                        scanned_data[gc_code] = cache_record
                        if cache_record.get('cache_status') == 2:
                            logger.info(f"  {gc_code} 已确认 Archive，字段有更新")
                        else:
                            logger.info(f"  {gc_code} 状态: {status}，字段有更新")
                    else:
                        unchanged_potential += 1
                        if status == 2:
                            logger.info(f"  {gc_code} 已确认 Archive，字段无变化")
                        else:
                            logger.info(f"  {gc_code} 状态: {status}，字段无变化")
                elif status is not None and existing_cache and status != existing_cache.get('cache_status'):
                    status_only_updates.append((gc_code, status))
                    updated_codes.add(gc_code)
                    logger.info(f"  {gc_code} 无法解析完整字段，仅更新状态为 {status}")
                else:
                    unchanged_potential += 1
                    logger.info(f"  {gc_code} 状态: {status}，未发现可更新字段")

                time.sleep(random.uniform(0.5, 1.0))

            if potential_updates:
                logger.info(f"批量更新 {len(potential_updates)} 个潜在归档缓存的完整字段...")
                db.upsert_caches_batch(potential_updates)

            if status_only_updates:
                logger.info(f"更新 {len(status_only_updates)} 个潜在归档缓存的状态字段...")
                for gc_code, status in status_only_updates:
                    db.update_cache_status(gc_code, status)

            if potential_updates or status_only_updates:
                db.commit()
                logger.info(
                    f"潜在归档缓存字段检查完成：完整更新 {len(potential_updates)} 个，"
                    f"仅状态更新 {len(status_only_updates)} 个，无变化 {unchanged_potential} 个，"
                    f"预览失败 {failed_preview_count} 个"
                )
            else:
                logger.info(
                    f"潜在归档缓存字段检查完成：无变化 {unchanged_potential} 个，"
                    f"预览失败 {failed_preview_count} 个"
                )
        
        backfill_missing_coordinates(db)

        logger.info("=" * 50)
        logger.info(f"本轮爬取完成! 新增: {len(new_codes)}, 更新: {len(updated_codes)}")
        logger.info("=" * 50)
        
        # 返回是否还有待处理的网格
    finally:
        db.close()


if __name__ == "__main__":
    try:
        run_crawler()
    except Exception:
        logger.exception("crawl_caches failed")
        raise
