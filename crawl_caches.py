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
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Dict, Set, Optional

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
PAGE_SIZE = 1000  # map API 单页返回上限
PAGE_COUNT_LIMIT = 10  # 单网格 skip 分页上限（10 页 = 10000 个，API 硬限制）
PAGE_SLEEP = 4  # 网格之间、skip 分页之间的节流间隔（秒）
CACHE_COUNT_RETRY_MAX = 3  # 网格 0 结果重试次数
CACHE_COUNT_RETRY_DELAY = 10  # 网格 0 结果重试间隔（秒）
ARCHIVED_CHECK_LIMIT = 100  # 潜在归档检查数量上限，超过则跳过（防大量误判）
# 固定爬取网格（大网格 + skip 分页，覆盖中国区全部 cache，实测 total 均 < 10000）
CRAWL_GRIDS = [
    ("G1 36-55N, 72-137E", (55.0, 72.0, 36.0, 137.0)),
    ("G2 17-36N, 72-120.75E", (36.0, 72.0, 17.0, 120.75)),
    ("G3 17-31.25N, 120.75-137E", (31.25, 120.75, 17.0, 137.0)),
    ("G4 31.25-36N, 120.75-134.96875E", (36.0, 120.75, 31.25, 134.96875)),
]
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
                       trackable_count, region, country, attributes, owner_guid
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
                    'region': row[17], 'country': row[18], 'attributes': row[19],
                    'owner_guid': row[20],
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
                trackable_count, region, country, attributes, owner_guid
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
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
                attributes = EXCLUDED.attributes,
                owner_guid = EXCLUDED.owner_guid
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
            json.dumps(cache_data['attributes']) if cache_data['attributes'] else None,
            cache_data.get('owner_guid'),
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
                trackable_count, region, country, attributes, owner_guid
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                attributes = EXCLUDED.attributes,
                owner_guid = EXCLUDED.owner_guid
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
                    json.dumps(cache_data['attributes']) if cache_data['attributes'] else None,
                    cache_data.get('owner_guid'),
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


def safe_fetch(max_lat: float, max_lng: float, min_lat: float, min_lng: float, skip: int = 0) -> Optional[dict]:
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
            full_url = f"{base_url}?box={box_str}&skip={skip}"
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
                    time.sleep(1)
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
                logger.warning(f"[403 Forbidden] Cookie可能失效。等待{attempt + 1}秒...")
                time.sleep(1)
            elif response.status_code == 429:
                logger.warning("[429 Too Many Requests] 触发限流。休眠...")
                time.sleep(60)
            else:
                logger.warning(f"[Error {response.status_code}] 正在重试...")
                time.sleep(1)
        except AuthenticationError:
            raise
        except Exception as e:
            logger.error(f"[网络异常] {e}. 正在重试 ({attempt + 1}/{MAX_RETRIES})...")
            time.sleep(1)
    
    return None


def _page_marks_cache_archived(text: str) -> bool:
    """检测详情页 HTML 是否明确标记 cache 为已归档。

    基于真实归档页面（GC1036Q / GC1039P / GC10435 / GC9ZYE5）验证的特征：
    归档横幅元素 id="ctl00_ContentBody_archivedMessage" 及其内文案
    "This cache has been archived"；有效 cache 页面（GCW5Y9）不含此元素。

    preview API 对已归档 cache 返回 404，但 404 也可能由其它原因导致
    （如部分仍有效的 cache），因此不能仅凭 API 404 判定归档，
    必须以页面上的归档标记为准。
    """
    return bool(
        re.search(r'id="ctl00_ContentBody_archivedMessage"', text, re.IGNORECASE)
        or re.search(r"This cache has been archived", text, re.IGNORECASE)
    )


def check_cache_status_via_detail_page(gc_code: str) -> Optional[int]:
    """直接请求 cache 详情页判断状态（preview API 已失效，详情页为唯一可靠来源）。

    返回: 2=已归档, 404=已删除, 0=有效/其它；全部请求失败返回 None。
    判断依据（实测验证）：
      - HTTP 200 + 归档横幅元素 ctl00_ContentBody_archivedMessage → 已归档
      - HTTP 200 + 无横幅 → 有效
      - HTTP 404 → 已删除

    Premium-only cache 在非 premium 登录态下返回受限页（title 为
    "Premium Member Only Cache"，无横幅），不能据此判定为有效，
    需继续尝试 premium cookie。
    """
    urls = [
        f"https://www.geocaching.com/geocache/{gc_code}",
        f"https://www.geocaching.com/seek/cache_details.aspx?wp={gc_code}",
    ]
    # 收集各 cookie 的判定结果，全部尝试完后优先 premium cookie 的结果
    candidates: dict[str, int] = {}
    for url in urls:
        for label, cookie in (("nonpremium", _RAW_NONPREMIUM_COOKIE), ("premium", _RAW_PREMIUM_COOKIE)):
            if not cookie:
                continue
            try:
                resp = session.get(url, headers={
                    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "cookie": cookie,
                    "referer": "https://www.geocaching.com/play/map",
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                }, timeout=15, allow_redirects=True)
                if resp.status_code == 404:
                    candidates[label] = CACHE_DELETED_STATUS
                    continue
                if resp.status_code != 200 or "geocache" not in resp.url:
                    continue
                if looks_like_login_page(resp.url, resp.text):
                    continue
                # Premium-only 受限页：title 为 "…Premium Member Only Cache"，
                # 无真实内容，不能作为"有效"依据（真实页面 title 为 "GCxxxx …"，不受影响）
                if re.search(r"<title>[^<]*Premium Member Only Cache", resp.text, re.IGNORECASE):
                    continue
                if _page_marks_cache_archived(resp.text):
                    return 2
                candidates[label] = 0
            except Exception:
                continue

    # 优先采用 premium cookie 的判定（能访问受限 cache 的真实状态）
    for label in ("premium", "nonpremium"):
        if label in candidates:
            return candidates[label]
    return None


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
    
    owner = item.get('owner', {}) or {}
    owner_guid = None  # map API does not expose owner GUID; backfilled from detail page

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
        'owner_username': owner.get('username'),
        'owner_guid': owner_guid,
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
            # 与数据库 numeric 列一致的舍入（ROUND_HALF_UP）。
            # Python round() 是银行家舍入，对 .5 边界向下取偶，会与数据库
            # 的四舍五入不一致，导致坐标值永不收敛、每次运行都被判定有更新。
            precision = 6 if field in {'latitude', 'longitude'} else 1
            return float(
                Decimal(str(value)).quantize(
                    Decimal('1e-%d' % precision), rounding=ROUND_HALF_UP
                )
            )
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
        'owner_guid',
    ]

    if not latest.get('premium_only', False):
        compare_keys.extend(['latitude', 'longitude'])

    for key in compare_keys:
        if latest.get(key) is None:
            continue  # API 无此字段，不比较也不覆盖已有值
        if normalize_cache_field(key, latest.get(key)) != normalize_cache_field(key, existing.get(key)):
            return True

    return False


def _extract_owner_guid_from_html(html: str) -> Optional[str]:
    """从详情页 HTML 中提取 owner 的 GUID（从 profile 链接）。"""
    m = re.search(r'/p/\?guid=([a-f0-9-]+)', html, re.IGNORECASE)
    return m.group(1) if m else None


def _extract_owner_guid_from_detail_page(code: str, premium_only: bool = False) -> Optional[str]:
    """请求详情页并提取 owner GUID。premium 缓存优先用 premium cookie。"""
    urls = [
        f"https://www.geocaching.com/seek/cache_details.aspx?wp={code}",
        f"https://www.geocaching.com/geocache/{code}",
    ]
    if premium_only:
        cookie_order = [("premium", _RAW_PREMIUM_COOKIE), ("nonpremium", _RAW_NONPREMIUM_COOKIE)]
    else:
        cookie_order = [("nonpremium", _RAW_NONPREMIUM_COOKIE), ("premium", _RAW_PREMIUM_COOKIE)]

    for url in urls:
        for _label, cookie in cookie_order:
            if not cookie:
                continue
            try:
                resp = session.get(url, headers={
                    "accept": "text/html,application/xhtml+xml",
                    "cookie": cookie,
                    "referer": "https://www.geocaching.com/play/map",
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                }, timeout=10, allow_redirects=True)
                if resp.status_code != 200:
                    continue
                guid = _extract_owner_guid_from_html(resp.text)
                if guid:
                    return guid
            except Exception:
                continue
    return None


def _fetch_detail_page_jsonld(code: str) -> Optional[dict]:
    """访问缓存详情页并提取坐标和 owner GUID。依次尝试 nonpremium / premium cookie。"""
    urls = [
        f"https://www.geocaching.com/geocache/{code}",
        f"https://www.geocaching.com/seek/cache_details.aspx?wp={code}",
    ]
    cookies = [
        ("nonpremium", _RAW_NONPREMIUM_COOKIE),
        ("premium", _RAW_PREMIUM_COOKIE),
    ]

    for url in urls:
        for label, cookie in cookies:
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
                resp = session.get(url, headers=headers, timeout=15, allow_redirects=True)
                if resp.status_code == 404:
                    return None
                if resp.status_code != 200:
                    continue
                if looks_like_login_page(resp.url, resp.text):
                    continue

                jsonld_match = re.search(
                    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                    resp.text, re.DOTALL | re.IGNORECASE,
                )
                if jsonld_match:
                    ld = json.loads(jsonld_match.group(1))
                    if isinstance(ld, dict) and isinstance(ld.get("location"), dict):
                        ld.setdefault("owner_guid", _extract_owner_guid_from_html(resp.text))
                        return ld

                # 旧版 ASP.NET 页面：从 JS 变量提取坐标 (var lat=X, lng=Y)
                js_match = re.search(
                    r"var\s+lat\s*=\s*([\d.]+)\s*,\s*lng\s*=\s*([\d.]+)",
                    resp.text, re.IGNORECASE,
                )
                if js_match:
                    return {
                        "name": "",
                        "location": {
                            "latitude": float(js_match.group(1)),
                            "longitude": float(js_match.group(2)),
                        },
                        "owner_guid": _extract_owner_guid_from_html(resp.text),
                    }
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            except Exception:
                continue

    return None


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
        ld = _fetch_detail_page_jsonld(code)
        if ld is None:
            logger.warning("坐标回填 [%s/%s] %s: 未能获取到坐标", i + 1, len(codes), code)
            time.sleep(random.uniform(0.5, 1.0))
            continue

        loc = ld["location"]
        lat = float(loc["latitude"])
        lng = float(loc["longitude"])
        owner_guid = ld.get("owner_guid")
        if owner_guid:
            db.cursor.execute(
                "UPDATE caches SET latitude = %s, longitude = %s, owner_guid = %s WHERE code = %s",
                (round(lat, 6), round(lng, 6), owner_guid, code),
            )
        else:
            db.cursor.execute(
                "UPDATE caches SET latitude = %s, longitude = %s WHERE code = %s",
                (round(lat, 6), round(lng, 6), code),
            )
        updated += 1
        logger.info("坐标回填 [%s/%s] %s: 坐标更新为 (%s, %s)%s", i + 1, len(codes), code, lat, lng,
                    f" owner_guid={owner_guid}" if owner_guid else "")
        time.sleep(random.uniform(0.5, 1.0))

    if updated:
        db.commit()
    logger.info("坐标回填：完成，成功更新 %s/%s 个缓存", updated, len(codes))
    return updated


def run_crawler():
    """运行爬虫"""
    run_start = time.perf_counter()
    # 连接数据库
    db = DatabaseManager(DATABASE_URL)
    db.connect()

    try:
        # 加载已有数据
        logger.info("加载已有 cache 数据...")
        step_start = time.perf_counter()
        scanned_data = db.get_existing_caches()
        load_elapsed = time.perf_counter() - step_start
        logger.info(f"已加载 {len(scanned_data)} 条记录（耗时 {load_elapsed:.1f}s）")
        
        # 记录原始 code（排除已归档的）
        logger.info("开始筛选原始 code...")
        original_codes = {
            code for code, row in scanned_data.items()
            if row.get('cache_status') != 2
        }
        logger.info(f"记录原始数据中的 code 数量: {len(original_codes)}")
        
        current_crawl_codes = set()
        new_codes = set()
        updated_codes = set()
        crawled_bounds: list[tuple[float, float, float, float]] = []
        loop_start = time.perf_counter()
        loop_stats = {
            'fetch': 0.0, 'process': 0.0, 'upsert': 0.0, 'guid': 0.0,
            'fetch_count': 0, 'process_count': 0, 'upsert_count': 0, 'guid_count': 0,
        }

        # 固定 4 个网格 + skip 分页全量爬取（不依赖数据库进度表，每次全量）
        grid_index = 0
        for grid_name, bounds in CRAWL_GRIDS:
            grid_index += 1
            max_lat, min_lng, min_lat, max_lng = bounds
            grid_start = time.perf_counter()
            logger.info(
                f"[{grid_index}/{len(CRAWL_GRIDS)}] 扫描网格 {grid_name}: "
                f"Lat {min_lat:.4f}~{max_lat:.4f}, Lng {min_lng:.4f}~{max_lng:.4f}"
            )

            skip = 0
            grid_total = None  # API 声明的网格总 cache 数
            grid_crawled = 0  # 本网格已爬取累计
            new_in_grid = 0
            updated_in_grid = 0

            while True:
                fetch_start = time.perf_counter()
                data = safe_fetch(max_lat, max_lng, min_lat, min_lng, skip)
                fetch_elapsed = time.perf_counter() - fetch_start
                loop_stats['fetch'] += fetch_elapsed
                loop_stats['fetch_count'] += 1

                if data:
                    sr = data.get('pageProps', {}).get('searchResults', {})
                    results = sr.get('results', [])
                    # total=0 是限流/异常响应的特征，不覆盖已记录的真实 total
                    if sr.get('total') not in (None, 0):
                        grid_total = sr.get('total')
                else:
                    results = []

                # 网格总 cache 数达到 API 上限（10000）→ 无法取全，让 action 失败以便邮件通知
                if grid_total is not None and grid_total >= PAGE_SIZE * PAGE_COUNT_LIMIT:
                    raise RuntimeError(
                        f"网格 {grid_name} 的 cache 总数达到 {grid_total}（≥ {PAGE_SIZE * PAGE_COUNT_LIMIT}），"
                        f"已超过 API 单查询 10000 上限，无法完整爬取，请拆分网格后重试"
                    )

                # 0 结果重试（可能是网络原因）：sleep 10 后重试，最多 3 次
                retry_count = 0
                while not results and retry_count < CACHE_COUNT_RETRY_MAX:
                    retry_count += 1
                    logger.warning(
                        f"  网格 {grid_name} skip={skip} 返回 0 个结果，"
                        f"{CACHE_COUNT_RETRY_DELAY}s 后重试 ({retry_count}/{CACHE_COUNT_RETRY_MAX})"
                    )
                    time.sleep(CACHE_COUNT_RETRY_DELAY)
                    retry_start = time.perf_counter()
                    data = safe_fetch(max_lat, max_lng, min_lat, min_lng, skip)
                    loop_stats['fetch'] += time.perf_counter() - retry_start
                    loop_stats['fetch_count'] += 1
                    if not data:
                        results = []
                        continue
                    sr = data.get('pageProps', {}).get('searchResults', {})
                    results = sr.get('results', [])
                    if sr.get('total') not in (None, 0):
                        grid_total = sr.get('total')

                if not results:
                    # 重试 3 次后仍为 0：数据不完整，失败退出以便邮件通知
                    raise RuntimeError(
                        f"网格 {grid_name} skip={skip} 重试 {CACHE_COUNT_RETRY_MAX} 次后仍返回 0 个结果，"
                        f"疑似网络异常，请检查后重试"
                    )

                cache_count = len(results)
                grid_crawled += cache_count
                if skip == 0:
                    crawled_bounds.append((max_lat, min_lng, min_lat, max_lng))
                logger.info(
                    f"  [skip={skip}] 获取到 {cache_count} 个结果"
                    f"（累计 {grid_crawled}，网格 total {grid_total}）"
                )

                updated_caches = []
                process_start = time.perf_counter()
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

                        # owner_username 变化 → 缓存可能被转移，从详情页获取新 owner GUID
                        if not is_new and cache_record.get('owner_username') != scanned_data[code].get('owner_username'):
                            new_guid = _extract_owner_guid_from_detail_page(code, cache_record.get('premium_only', False))
                            if new_guid:
                                cache_record['owner_guid'] = new_guid

                        updated_caches.append(cache_record)
                        if is_new:
                            new_in_grid += 1
                            new_codes.add(code)
                        else:
                            updated_in_grid += 1
                            updated_codes.add(code)
                        scanned_data[code] = cache_record

                process_elapsed = time.perf_counter() - process_start
                loop_stats['process'] += process_elapsed
                loop_stats['process_count'] += 1

                # 将这一网格的变更数据批量写入数据库
                upsert_start = time.perf_counter()
                if updated_caches:
                    db.upsert_caches_batch(updated_caches)
                upsert_elapsed = time.perf_counter() - upsert_start
                loop_stats['upsert'] += upsert_elapsed
                loop_stats['upsert_count'] += 1

                # 新 cache 的 owner_guid 在 map API 中不返回，需从详情页提取
                guid_start = time.perf_counter()
                new_without_guid = [c for c in updated_caches if c.get('code') in new_codes and not c.get('owner_guid')]
                if new_without_guid:
                    for cache_record in new_without_guid:
                        code = cache_record['code']
                        guid = _extract_owner_guid_from_detail_page(code, cache_record.get('premium_only', False))
                        if guid:
                            cache_record['owner_guid'] = guid
                            db.cursor.execute(
                                "UPDATE caches SET owner_guid = %s WHERE code = %s AND owner_guid IS NULL",
                                (guid, code))
                    db.conn.commit()
                    logger.info(f"  回填 {len([c for c in new_without_guid if c.get('owner_guid')])}/{len(new_without_guid)} 个新 cache 的 owner GUID")
                guid_elapsed = time.perf_counter() - guid_start
                loop_stats['guid'] += guid_elapsed
                loop_stats['guid_count'] += 1

                if len(results) < PAGE_SIZE:
                    break
                skip += PAGE_SIZE
                # skip 已达上限仍未爬完 → 与 API 10000 上限冲突，失败退出以便通知
                if skip >= PAGE_SIZE * (PAGE_COUNT_LIMIT - 1):
                    raise RuntimeError(
                        f"网格 {grid_name} skip 已达 {skip} 但仍未爬完，超过 API 上限，请拆分网格后重试"
                    )
                time.sleep(PAGE_SLEEP)  # skip 分页间节流

            # 完整性检查：爬取总数与 API total 不一致时告警（total 动态波动，不硬失败）
            if (
                grid_total is not None
                and grid_total < PAGE_SIZE * PAGE_COUNT_LIMIT
                and grid_crawled != grid_total
            ):
                logger.warning(
                    f"  网格 {grid_name} 爬取 {grid_crawled} 与 API total {grid_total} 不一致"
                )

            db.commit()
            grid_elapsed = time.perf_counter() - grid_start
            logger.info(f"  网格 {grid_name} 完成: 爬取 {grid_crawled} 个（total {grid_total}），耗时 {grid_elapsed:.1f}s")
            logger.info(f"  新增: {new_in_grid}, 更新: {updated_in_grid}")
            time.sleep(PAGE_SLEEP)  # 网格间节流

        loop_elapsed = time.perf_counter() - loop_start
        grid_count = max(len(CRAWL_GRIDS), 1)
        logger.info(
            f"[耗时] 主循环网格爬取共 {loop_elapsed:.1f}s（{len(CRAWL_GRIDS)} 个网格，"
            f"平均 {loop_elapsed / grid_count:.2f}s/网格）"
        )
        logger.info(
            f"[耗时] 子步骤累计: fetch={loop_stats['fetch']:.1f}s ({loop_stats['fetch_count']}次), "
            f"process={loop_stats['process']:.1f}s ({loop_stats['process_count']}次), "
            f"upsert={loop_stats['upsert']:.1f}s ({loop_stats['upsert_count']}次), "
            f"guid={loop_stats['guid']:.1f}s ({loop_stats['guid_count']}次)"
        )
        
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

        archive_start = time.perf_counter()
        archive_elapsed = 0.0
        if len(archived_codes) > ARCHIVED_CHECK_LIMIT:
            # 兜底：数量异常大说明本次爬取可能有问题（如大量网格未成功），
            # 跳过归档检查，避免浪费大量请求和误判
            logger.warning(
                f"Potential Archived 数量 {len(archived_codes)} 超过上限 "
                f"{ARCHIVED_CHECK_LIMIT}，跳过归档检查（疑似本次爬取异常）"
            )
        elif archived_codes:
            logger.info("逐个检查潜在归档缓存的最新状态...")

            status_only_updates = []
            unchanged_potential = 0
            failed_preview_count = 0

            for gc_code in archived_codes:
                existing = scanned_data.get(gc_code)
                if existing and existing.get('cache_status') == 404:
                    logger.info(f"  {gc_code} 状态已为 404（已删除），跳过检查")
                    unchanged_potential += 1
                    continue
                status = check_cache_status_via_detail_page(gc_code)
                if status is None:
                    failed_preview_count += 1
                    logger.warning(f"  {gc_code} 详情页检查失败，跳过状态更新")
                    time.sleep(random.uniform(0.5, 1.0))
                    continue

                if status == CACHE_DELETED_STATUS:
                    if existing and existing.get('cache_status') != CACHE_DELETED_STATUS:
                        status_only_updates.append((gc_code, CACHE_DELETED_STATUS))
                        updated_codes.add(gc_code)
                        logger.info(f"  {gc_code} 已确认删除，更新状态为 {CACHE_DELETED_STATUS}")
                    else:
                        unchanged_potential += 1
                        logger.info(f"  {gc_code} 已确认删除，状态无变化")
                elif status == 2:
                    if existing and existing.get('cache_status') != 2:
                        status_only_updates.append((gc_code, 2))
                        updated_codes.add(gc_code)
                        logger.info(f"  {gc_code} 已确认 Archive，更新状态为 2")
                    else:
                        unchanged_potential += 1
                        logger.info(f"  {gc_code} 已确认 Archive，状态无变化")
                else:
                    # 详情页无归档标记 → 有效 cache：若被误标为归档/删除，改回 0
                    if existing and existing.get('cache_status') in (2, CACHE_DELETED_STATUS):
                        status_only_updates.append((gc_code, 0))
                        updated_codes.add(gc_code)
                        logger.info(f"  {gc_code} 详情页无归档标记，将误标状态改回 0")
                    else:
                        unchanged_potential += 1
                        logger.info(f"  {gc_code} 详情页无归档标记，状态无变化")

                time.sleep(random.uniform(0.5, 1.0))

            if status_only_updates:
                logger.info(f"更新 {len(status_only_updates)} 个潜在归档缓存的状态字段...")
                for gc_code, status in status_only_updates:
                    db.update_cache_status(gc_code, status)

            if status_only_updates:
                db.commit()
                logger.info(
                    f"潜在归档缓存状态检查完成：状态更新 {len(status_only_updates)} 个，"
                    f"无变化 {unchanged_potential} 个，检查失败 {failed_preview_count} 个"
                )
            else:
                logger.info(
                    f"潜在归档缓存状态检查完成：无变化 {unchanged_potential} 个，"
                    f"检查失败 {failed_preview_count} 个"
                )
        archive_elapsed = time.perf_counter() - archive_start
        logger.info(f"[耗时] 归档检查阶段: {archive_elapsed:.1f}s")

        backfill_start = time.perf_counter()
        backfill_missing_coordinates(db)
        backfill_elapsed = time.perf_counter() - backfill_start
        logger.info(f"[耗时] 坐标回填阶段: {backfill_elapsed:.1f}s")

        total_elapsed = time.perf_counter() - run_start
        logger.info("=" * 50)
        logger.info(f"本轮爬取完成! 新增: {len(new_codes)}, 更新: {len(updated_codes)}")
        logger.info(
            f"[耗时汇总] 加载数据={load_elapsed:.1f}s | 网格爬取={loop_elapsed:.1f}s "
            f"({len(CRAWL_GRIDS)}网格, 平均{loop_elapsed / grid_count:.2f}s/网格) | "
            f"归档检查={archive_elapsed:.1f}s | 坐标回填={backfill_elapsed:.1f}s | "
            f"全程总计={total_elapsed:.1f}s"
        )
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
