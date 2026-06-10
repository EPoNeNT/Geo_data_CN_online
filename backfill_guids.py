#!/usr/bin/env python3
"""回填数据库中缺失的 owner_guid 和 user_guid。

用法:
  python backfill_guids.py                    # 回填 caches.owner_guid
  python backfill_guids.py --dry-run          # 仅统计，不回填
  python backfill_guids.py --limit 10         # 限制数量
  python backfill_guids.py --cache GCBPR5N    # 指定单个缓存
"""

import os
import re
import sys
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from runtime_utils import connect_postgres, require_env, setup_logging

logger = setup_logging("backfill_guids.log")

NONPREMIUM_COOKIE = (
    os.environ.get("GEOCOOKIE_NONPREMIUM")
    or os.environ.get("GEOCACHING_COOKIE")
    or ""
).strip().strip('"').strip("'")
PREMIUM_COOKIE = (os.environ.get("GEOCOOKIE_PREMIUM") or "").strip().strip('"').strip("'")

session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
session.mount("https://", HTTPAdapter(max_retries=retries))

OWNER_GUID_RE = re.compile(r"/p/\?guid=([a-f0-9-]+)", re.IGNORECASE)


def fetch_owner_guid(code: str) -> Optional[str]:
    """从详情页提取 owner GUID。依次尝试 nonpremium / premium cookie。"""
    urls = [
        f"https://www.geocaching.com/seek/cache_details.aspx?wp={code}",
        f"https://www.geocaching.com/geocache/{code}",
    ]
    cookies = [
        ("nonpremium", NONPREMIUM_COOKIE),
        ("premium", PREMIUM_COOKIE),
    ]

    for url in urls:
        for label, cookie in cookies:
            if not cookie:
                continue
            headers = {
                "accept": "text/html,application/xhtml+xml",
                "cookie": cookie,
                "referer": "https://www.geocaching.com/play/map",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            }
            try:
                resp = session.get(url, headers=headers, timeout=15, allow_redirects=True)
                if resp.status_code != 200:
                    continue
                m = OWNER_GUID_RE.search(resp.text)
                if m:
                    return m.group(1)
            except Exception:
                continue

    return None


def get_caches_missing_owner_guid(database_url: str, limit: Optional[int] = None) -> list[tuple[str, Optional[str]]]:
    conn = connect_postgres(database_url, logger=logger, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            sql = """
                SELECT code, owner_username
                FROM caches
                WHERE owner_guid IS NULL
                  AND country IN ('China', 'Hong Kong', 'Macao', 'Taiwan')
                  AND COALESCE(cache_status, 0) != 2
                ORDER BY code
            """
            if limit:
                sql += f" LIMIT {int(limit)}"
            cur.execute(sql)
            return [(row[0], row[1]) for row in cur.fetchall()]
    finally:
        conn.close()


def get_logs_missing_user_guid_count(database_url: str) -> int:
    conn = connect_postgres(database_url, logger=logger, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM logs WHERE user_guid IS NULL")
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def sync_user_guids_from_db(database_url: str) -> tuple[int, int]:
    """从 caches 和 logs 表同步 GUID 到 user 表。返回 (from_caches, from_logs)。"""
    conn = connect_postgres(database_url, logger=logger, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            # 从 caches 同步
            cur.execute("""
                INSERT INTO "user" (user_name, guid)
                SELECT DISTINCT c.owner_username, c.owner_guid
                FROM caches c
                WHERE c.owner_guid IS NOT NULL
                  AND c.owner_username IS NOT NULL
                ON CONFLICT (user_name) DO UPDATE
                SET guid = EXCLUDED.guid
                WHERE "user".guid IS NULL
            """)
            from_caches = cur.rowcount

            # 从 logs 同步
            cur.execute("""
                INSERT INTO "user" (user_name, guid)
                SELECT DISTINCT l.user_name, l.user_guid
                FROM logs l
                WHERE l.user_guid IS NOT NULL
                  AND l.user_name IS NOT NULL
                ON CONFLICT (user_name) DO UPDATE
                SET guid = EXCLUDED.guid
                WHERE "user".guid IS NULL
            """)
            from_logs = cur.rowcount
        conn.commit()
        return from_caches, from_logs
    except Exception as e:
        logger.warning("同步 user 表 GUID 失败: %s", e)
        conn.rollback()
        return 0, 0
    finally:
        conn.close()


def get_users_missing_guid_count(database_url: str) -> int:
    conn = connect_postgres(database_url, logger=logger, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT COUNT(*) FROM "user" WHERE guid IS NULL')
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def update_owner_guid(database_url: str, code: str, guid: str) -> bool:
    conn = connect_postgres(database_url, logger=logger, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE caches SET owner_guid = %s WHERE code = %s", (guid, code))
        conn.commit()
        return True
    except Exception as e:
        logger.warning("写入失败 %s: %s", code, e)
        conn.rollback()
        return False
    finally:
        conn.close()


def main():
    database_url = require_env("DATABASE_URL")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry_run = "--dry-run" in sys.argv
    limit_arg = next((a.split("=")[1] for a in sys.argv if a.startswith("--limit=")), None)
    cache_code = next((a.split("=")[1] for a in sys.argv if a.startswith("--cache=")), None)

    # --- 统计当前状态 ---
    print("=== 当前缺失统计 ===")
    caches = get_caches_missing_owner_guid(database_url)
    print(f"caches 缺少 owner_guid: {len(caches)}")
    logs_missing = get_logs_missing_user_guid_count(database_url)
    print(f"logs 缺少 user_guid:   {logs_missing}")
    users_missing = get_users_missing_guid_count(database_url)
    print(f"user 缺少 guid:       {users_missing}")
    print()

    # 先从已有的 caches/logs GUID 同步到 user 表
    from_caches, from_logs = sync_user_guids_from_db(database_url)
    if from_caches or from_logs:
        print(f"已从 caches({from_caches}) 和 logs({from_logs}) 同步 GUID 到 user 表")
        users_missing = get_users_missing_guid_count(database_url)
        print(f"user 缺少 guid (同步后): {users_missing}")
        print()

    if cache_code:
        code = cache_code.upper()
        print(f"=== 回填指定缓存: {code} ===")
        guid = fetch_owner_guid(code)
        if guid:
            print(f"  GUID: {guid}")
            if not dry_run:
                update_owner_guid(database_url, code, guid)
                print(f"  已写入数据库")
        else:
            print(f"  未能获取到 GUID")
        return

    if limit_arg:
        caches = caches[:int(limit_arg)]

    if not caches:
        print("无需回填 owner_guid")
        return

    print(f"=== 回填 owner_guid: {len(caches)} 个缓存 ===")
    if dry_run:
        print("(dry-run 模式，不写数据库)")
    print()

    success = 0
    fail = 0

    for i, (code, username) in enumerate(caches):
        print(f"[{i + 1}/{len(caches)}] {code} (owner={username}) ... ", end="", flush=True)
        guid = fetch_owner_guid(code)
        if guid:
            print(f"GUID={guid}", end="")
            if not dry_run:
                if update_owner_guid(database_url, code, guid):
                    print(" ✓", flush=True)
                    success += 1
                else:
                    print(" ✗", flush=True)
                    fail += 1
            else:
                print(" (dry-run)", flush=True)
                success += 1
        else:
            print("未获取到", flush=True)
            fail += 1
        time.sleep(0.3)

    print(f"\n完成: 成功 {success}, 失败 {fail}")


if __name__ == "__main__":
    main()
