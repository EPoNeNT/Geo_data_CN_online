#!/usr/bin/env python3
"""Build a standalone Greater China Geocaching cohort dashboard.

This script is intentionally independent from ``generate_data.py`` and does
not write to Neon.  It queries the existing database read-only and produces a
self-contained HTML report for local viewing.

Usage:
    python cohort_dashboard.py
    python cohort_dashboard.py --as-of 2026-09-10
    python cohort_dashboard.py --output-dir analysis/cohort-dashboard
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from psycopg2.extras import RealDictCursor

from runtime_utils import connect_postgres, require_env, setup_logging


LOGGER = setup_logging("cohort_dashboard.log")
GREATER_CHINA_COUNTRIES = ("China", "Hong Kong", "Macao", "Taiwan")
PLAYER_ENGAGEMENT_LOG_TYPES = ("Found it", "Didn't find it", "Attended")
DEFAULT_OUTPUT_DIR = Path("analysis") / "cohort-dashboard"
DEFAULT_COHORT_START = date(2017, 1, 1)
CITY_COHORT_TOP_N = 15
MULTI_CITY_LABEL = "同日多城市"
UNKNOWN_CITY_LABEL = "未知城市"
NO_POST_REGISTRATION_ACTIVITY_LABEL = "注册后无有效活动"
CACHE_TYPE_LABELS = {
    2: "Traditional",
    3: "Multi-cache",
    4: "Virtual",
    5: "Letterbox Hybrid",
    6: "Event",
    8: "Mystery/Puzzle",
    11: "Webcam",
    12: "Locationless",
    13: "CITO",
    137: "EarthCache",
    1858: "Wherigo",
    3653: "Community Celebration Event",
}
DATA_ACQUISITION_PRIORITIES = (
    {
        "availability": "现有数据库可直接聚合",
        "priority": "P0",
        "topic": "日志节奏与多日参与",
        "data": "日志日期、Found/DNF/Attended、注册日期",
        "use": "区分单日集中找寻和跨多日形成习惯，定位第二次活动日的转化价值。",
    },
    {
        "availability": "现有数据库可直接聚合",
        "priority": "P0",
        "topic": "缓存供给与空间可达性",
        "data": "缓存经纬度、城市、placed_date、状态、日志覆盖",
        "use": "估算首次活动城市的可见供给、新增供给和移动半径，并与激活及留存对照。",
    },
    {
        "availability": "现有数据库可直接聚合",
        "priority": "P1",
        "topic": "缓存新鲜度与维护状态",
        "data": "placed_date、last_found_date、cache_status、DNF 日志",
        "use": "识别新手失败是否集中于长期无人找到或状态异常的缓存。",
    },
    {
        "availability": "现有数据库可直接聚合",
        "priority": "P1",
        "topic": "缓存属性和类型结构",
        "data": "attributes、类型、容器、难度、地形",
        "use": "形成推荐实验的次级分层，并检查城市或时期的供给结构差异。",
    },
    {
        "availability": "需要新增采集或实验埋点",
        "priority": "P0",
        "topic": "内容渠道归因",
        "data": "来源链接、活动码、落地页曝光与点击、注册来源",
        "use": "区分视频、自然搜索、社群转发和其他渠道，计算真实转化与长期价值。",
    },
    {
        "availability": "需要新增采集或实验埋点",
        "priority": "P0",
        "topic": "产品引导与推荐实验",
        "data": "推荐曝光、点击、推送触达、实验分组、后续日志",
        "use": "验证第二活动日引导、DNF 后替代缓存推荐和 3/5/10 找阶梯任务的因果效果。",
    },
    {
        "availability": "需要新增采集或实验埋点",
        "priority": "P1",
        "topic": "历史供给快照",
        "data": "活动时点的缓存可用状态、收藏点和维护状态",
        "use": "避免用当前快照解释历史行为，提升供给与缓存质量分析的可信度。",
    },
    {
        "availability": "需要新增采集或实验埋点",
        "priority": "P1",
        "topic": "用户位置与出行背景",
        "data": "经同意的常驻城市或粗粒度位置、旅行状态",
        "use": "区分本地长期参与与旅行期间的一次性大中华区日志。",
    },
)

VIDEO_PUBLICATIONS = (
    {
        "bvid": "BV1u14y1D7p6",
        "title": "我玩到了现实版塞尔达！34个宝藏在杭州山里没人挖？",
        "publishedAt": "2023-06-14",
        "views": 951000,
        "viewsAsOf": "2026-09-10",
        "url": "https://www.bilibili.com/video/BV1u14y1D7p6/",
        "creator": "2023年视频UP主",
    },
    {
        "bvid": "BV1wZ4RzjE2P",
        "title": "这片树林，藏着全东莞仅有的两个宝藏",
        "publishedAt": "2025-08-06 11:00:00",
        "views": 4260689,
        "viewsAsOf": "2026-09-11",
        "url": "https://www.bilibili.com/video/BV1wZ4RzjE2P/",
        "creator": "特能斯Terrence",
    },
    {
        "bvid": "BV1B1ttzYEv2",
        "title": "320万人看过，很多人说我错过了一个宝藏？！",
        "publishedAt": "2025-08-09 19:32:31",
        "views": 1461570,
        "viewsAsOf": "2026-09-11",
        "url": "https://www.bilibili.com/video/BV1B1ttzYEv2/",
        "creator": "特能斯Terrence",
    },
    {
        "bvid": "BV1ZivczgEvt",
        "title": "导航失灵+迷路：亲历最复杂的城中村",
        "publishedAt": "2025-08-26 11:09:42",
        "views": 64707,
        "viewsAsOf": "2026-09-11",
        "url": "https://www.bilibili.com/video/BV1ZivczgEvt/",
        "creator": "特能斯Terrence",
    },
    {
        "bvid": "BV1gMaAz4EfT",
        "title": "840万人看过的后续！高手剧透宝藏点！",
        "publishedAt": "2025-09-02 13:28:19",
        "views": 161146,
        "viewsAsOf": "2026-09-11",
        "url": "https://www.bilibili.com/video/BV1gMaAz4EfT/",
        "creator": "特能斯Terrence",
    },
    {
        "bvid": "BV1rxnMzFEZy",
        "title": "我在深圳藏了个大宝藏，看谁能找到…",
        "publishedAt": "2025-09-26 17:30:00",
        "views": 110796,
        "viewsAsOf": "2026-09-11",
        "url": "https://www.bilibili.com/video/BV1rxnMzFEZy/",
        "creator": "特能斯Terrence",
    },
    {
        "bvid": "BV1r2sozyEX5",
        "title": "都来看看是谁！",
        "publishedAt": "2025-10-25 15:49:33",
        "views": 8906,
        "viewsAsOf": "2026-09-11",
        "url": "https://www.bilibili.com/video/BV1r2sozyEX5/",
        "creator": "特能斯Terrence",
    },
    {
        "bvid": "BV1yMSMBXEJy",
        "title": "跑遍广州3个地方，终于解开谜题找到宝藏！",
        "publishedAt": "2025-11-27 19:00:00",
        "views": 30372,
        "viewsAsOf": "2026-09-11",
        "url": "https://www.bilibili.com/video/BV1yMSMBXEJy/",
        "creator": "特能斯Terrence",
    },
)


def sql_literal(value: str) -> str:
    """Quote a fixed SQL string literal without relying on Python repr()."""
    return "'" + value.replace("'", "''") + "'"


def build_cohort_query() -> str:
    """Return the user-level metric query for the fixed Greater China scope."""
    countries = ", ".join(sql_literal(country) for country in GREATER_CHINA_COUNTRIES)
    engagement_types = ", ".join(sql_literal(log_type) for log_type in PLAYER_ENGAGEMENT_LOG_TYPES)
    return f"""
    WITH params AS (
      SELECT %(as_of)s::date AS as_of
    ),
    scoped_log_users AS (
      SELECT DISTINCT l.user_guid AS guid
      FROM logs l
      JOIN caches c ON c.code = l.gc_code
      WHERE c.country IN ({countries})
        AND l.user_guid IS NOT NULL
        AND TRIM(l.user_guid) <> ''
        AND l.user_guid <> 'deleted'
        AND COALESCE(l.log_type, '') <> 'deleted'
    ),
    base AS (
      SELECT
        u.guid,
        u.reg_place,
        u.registration_date,
        date_trunc('month', u.registration_date)::date AS cohort_month
      FROM "user" u
      JOIN scoped_log_users slu ON slu.guid = u.guid
      JOIN params p ON TRUE
      WHERE u.registration_date IS NOT NULL
        AND u.registration_date >= %(cohort_start)s::date
        AND u.registration_date <= p.as_of
    ),
    scoped_events AS (
      SELECT b.guid, b.registration_date, l.visited::date AS visited,
             l.log_type, l.gc_code, c.city, c.difficulty, c.terrain,
             c.geocache_type, c.container_type
      FROM base b
      JOIN logs l ON l.user_guid = b.guid
      JOIN caches c ON c.code = l.gc_code
      JOIN params p ON TRUE
      WHERE c.country IN ({countries})
        AND COALESCE(l.log_type, '') <> 'deleted'
        AND l.visited IS NOT NULL
        AND l.visited::date <= p.as_of
    ),
    first_dnf_dates AS (
      SELECT e.guid, MIN(e.visited) AS first_valid_dnf
      FROM scoped_events e
      WHERE e.log_type = 'Didn''t find it'
        AND e.visited >= e.registration_date
      GROUP BY e.guid
    ),
    first_dnf AS (
      SELECT
        d.guid,
        d.first_valid_dnf,
        COUNT(DISTINCT e.gc_code)::int AS first_valid_dnf_cache_count,
        (ARRAY_AGG(e.gc_code ORDER BY e.gc_code))[1] AS first_valid_dnf_cache,
        (ARRAY_AGG(e.difficulty ORDER BY e.gc_code))[1] AS first_valid_dnf_difficulty,
        (ARRAY_AGG(e.terrain ORDER BY e.gc_code))[1] AS first_valid_dnf_terrain,
        (ARRAY_AGG(e.geocache_type ORDER BY e.gc_code))[1] AS first_valid_dnf_geocache_type,
        (ARRAY_AGG(e.container_type ORDER BY e.gc_code))[1] AS first_valid_dnf_container_type
      FROM first_dnf_dates d
      JOIN scoped_events e
        ON e.guid = d.guid
       AND e.visited = d.first_valid_dnf
       AND e.log_type = 'Didn''t find it'
      GROUP BY d.guid, d.first_valid_dnf
    ),
    first_engagement_dates AS (
      SELECT e.guid, MIN(e.visited) AS first_engagement_date
      FROM scoped_events e
      WHERE e.log_type IN ({engagement_types})
        AND e.visited >= e.registration_date
      GROUP BY e.guid
    ),
    first_engagement_locations AS (
      SELECT
        d.guid,
        d.first_engagement_date,
        CASE
          WHEN COUNT(DISTINCT NULLIF(TRIM(e.city), '')) > 1 THEN {sql_literal(MULTI_CITY_LABEL)}
          ELSE MIN(NULLIF(TRIM(e.city), ''))
        END AS first_engagement_city,
        COUNT(DISTINCT e.gc_code)::int AS first_engagement_cache_count,
        CASE WHEN COUNT(DISTINCT e.gc_code) = 1 THEN MIN(e.gc_code) END
          AS first_engagement_cache,
        CASE WHEN COUNT(DISTINCT e.gc_code) = 1 THEN MIN(e.difficulty) END
          AS first_engagement_difficulty,
        CASE WHEN COUNT(DISTINCT e.gc_code) = 1 THEN MIN(e.terrain) END
          AS first_engagement_terrain,
        CASE WHEN COUNT(DISTINCT e.gc_code) = 1 THEN MIN(e.geocache_type) END
          AS first_engagement_geocache_type,
        CASE WHEN COUNT(DISTINCT e.gc_code) = 1 THEN MIN(e.container_type) END
          AS first_engagement_container_type
      FROM first_engagement_dates d
      JOIN scoped_events e
        ON e.guid = d.guid
       AND e.visited = d.first_engagement_date
       AND e.log_type IN ({engagement_types})
      GROUP BY d.guid, d.first_engagement_date
    ),
    first_engagement_cache_history AS (
      SELECT
        fel.guid,
        c.placed_date::date AS first_engagement_cache_placed_date,
        MAX(h.visited::date) FILTER (
          WHERE h.log_type = 'Found it'
            AND COALESCE(h.log_type, '') <> 'deleted'
            AND h.visited IS NOT NULL
            AND h.visited::date < fel.first_engagement_date
        ) AS first_engagement_cache_prior_found,
        COUNT(*) FILTER (
          WHERE h.log_type = 'Didn''t find it'
            AND COALESCE(h.log_type, '') <> 'deleted'
            AND h.visited IS NOT NULL
            AND h.visited::date >= fel.first_engagement_date - INTERVAL '90 days'
            AND h.visited::date < fel.first_engagement_date
        )::int AS first_engagement_cache_prior_dnf_90
      FROM first_engagement_locations fel
      JOIN caches c ON c.code = fel.first_engagement_cache
      LEFT JOIN logs h ON h.gc_code = fel.first_engagement_cache
      WHERE fel.first_engagement_cache_count = 1
      GROUP BY fel.guid, c.placed_date, fel.first_engagement_date
    )
    SELECT
      b.cohort_month,
      b.registration_date,
      b.reg_place,
      fel.first_engagement_date,
      fel.first_engagement_city,
      fel.first_engagement_cache_count,
      fel.first_engagement_cache,
      fel.first_engagement_difficulty,
      fel.first_engagement_terrain,
      fel.first_engagement_geocache_type,
      fel.first_engagement_container_type,
      feh.first_engagement_cache_placed_date,
      feh.first_engagement_cache_prior_found,
      feh.first_engagement_cache_prior_dnf_90,
      BOOL_OR(
        e.log_type = 'Found it'
        AND e.visited = fel.first_engagement_date
      ) AS first_engagement_found,
      BOOL_OR(
        e.log_type = 'Didn''t find it'
        AND e.visited = fel.first_engagement_date
      ) AS first_engagement_dnf,
      BOOL_OR(
        e.log_type = 'Attended'
        AND e.visited = fel.first_engagement_date
      ) AS first_engagement_attended,
      BOOL_OR(e.log_type = 'Found it' AND e.visited < b.registration_date)
        AS pre_registration_find,
      MIN(e.visited) FILTER (
        WHERE e.log_type = 'Found it'
          AND e.visited >= b.registration_date
      ) AS first_valid_find,
      (ARRAY_AGG(e.gc_code ORDER BY e.visited, e.gc_code) FILTER (
        WHERE e.log_type = 'Found it'
          AND e.visited >= b.registration_date
      ))[1] AS first_valid_find_cache,
      (ARRAY_AGG(e.city ORDER BY e.visited, e.gc_code) FILTER (
        WHERE e.log_type = 'Found it'
          AND e.visited >= b.registration_date
      ))[1] AS first_valid_find_city,
      fd.first_valid_dnf,
      fd.first_valid_dnf_cache,
      fd.first_valid_dnf_cache_count,
      fd.first_valid_dnf_difficulty,
      fd.first_valid_dnf_terrain,
      fd.first_valid_dnf_geocache_type,
      fd.first_valid_dnf_container_type,
      MIN(e.visited) FILTER (
        WHERE e.log_type = 'Found it'
          AND fd.first_valid_dnf IS NOT NULL
          AND e.visited >= fd.first_valid_dnf
      ) AS first_find_on_or_after_dnf,
      (ARRAY_AGG(e.gc_code ORDER BY e.visited, e.gc_code) FILTER (
        WHERE e.log_type = 'Found it'
          AND fd.first_valid_dnf IS NOT NULL
          AND e.visited >= fd.first_valid_dnf
      ))[1] AS first_find_on_or_after_dnf_cache,
      COUNT(*) FILTER (
        WHERE e.log_type = 'Found it'
          AND e.visited >= b.registration_date
          AND e.visited < b.registration_date + INTERVAL '7 days'
      )::int AS finds_d7,
      COUNT(*) FILTER (
        WHERE e.log_type = 'Found it'
          AND e.visited >= b.registration_date
          AND e.visited < b.registration_date + INTERVAL '30 days'
      )::int AS finds_d30,
      COUNT(*) FILTER (
        WHERE e.log_type = 'Didn''t find it'
          AND e.visited >= b.registration_date
          AND e.visited < b.registration_date + INTERVAL '30 days'
      )::int AS dnf_d30,
      COUNT(*) FILTER (
        WHERE e.log_type = 'Attended'
          AND e.visited >= b.registration_date
          AND e.visited < b.registration_date + INTERVAL '30 days'
      )::int AS attended_d30,
      COUNT(DISTINCT e.visited) FILTER (
        WHERE e.log_type = 'Found it'
          AND e.visited >= b.registration_date
          AND e.visited < b.registration_date + INTERVAL '30 days'
      )::int AS find_days_d30,
      COUNT(DISTINCT e.visited) FILTER (
        WHERE e.log_type IN ({engagement_types})
          AND e.visited >= b.registration_date
          AND e.visited < b.registration_date + INTERVAL '30 days'
      )::int AS engagement_days_d30,
      COUNT(*) FILTER (
        WHERE e.log_type = 'Found it'
          AND e.visited >= fel.first_engagement_date
          AND e.visited < fel.first_engagement_date + INTERVAL '30 days'
      )::int AS finds_e30,
      COUNT(*) FILTER (
        WHERE e.log_type = 'Didn''t find it'
          AND e.visited >= fel.first_engagement_date
          AND e.visited < fel.first_engagement_date + INTERVAL '30 days'
      )::int AS dnf_e30,
      COUNT(*) FILTER (
        WHERE e.log_type = 'Attended'
          AND e.visited >= fel.first_engagement_date
          AND e.visited < fel.first_engagement_date + INTERVAL '30 days'
      )::int AS attended_e30,
      COUNT(*) FILTER (
        WHERE e.log_type = 'Found it'
          AND e.visited >= b.registration_date
          AND e.visited < b.registration_date + INTERVAL '90 days'
      )::int AS finds_d90,
      BOOL_OR(
        e.log_type IN ({engagement_types})
        AND e.visited >= b.registration_date + INTERVAL '30 days'
        AND e.visited < b.registration_date + INTERVAL '60 days'
      ) AS active_d30,
      BOOL_OR(
        e.log_type IN ({engagement_types})
        AND e.visited >= b.registration_date + INTERVAL '90 days'
        AND e.visited < b.registration_date + INTERVAL '120 days'
      ) AS active_d90,
      BOOL_OR(
        e.log_type IN ({engagement_types})
        AND e.visited >= b.registration_date + INTERVAL '30 days'
        AND e.visited < b.registration_date + INTERVAL '60 days'
      ) AS active_m1,
      BOOL_OR(
        e.log_type IN ({engagement_types})
        AND e.visited >= b.registration_date + INTERVAL '60 days'
        AND e.visited < b.registration_date + INTERVAL '90 days'
      ) AS active_m2,
      BOOL_OR(
        e.log_type IN ({engagement_types})
        AND e.visited >= b.registration_date + INTERVAL '90 days'
        AND e.visited < b.registration_date + INTERVAL '120 days'
      ) AS active_m3,
      BOOL_OR(
        e.log_type IN ({engagement_types})
        AND e.visited >= b.registration_date + INTERVAL '180 days'
        AND e.visited < b.registration_date + INTERVAL '210 days'
      ) AS active_m6,
      BOOL_OR(
        e.log_type IN ({engagement_types})
        AND e.visited >= b.registration_date + INTERVAL '360 days'
        AND e.visited < b.registration_date + INTERVAL '390 days'
      ) AS active_m12,
      BOOL_OR(
        e.log_type IN ({engagement_types})
        AND e.visited >= fel.first_engagement_date + INTERVAL '30 days'
        AND e.visited < fel.first_engagement_date + INTERVAL '60 days'
      ) AS active_e_m1,
      BOOL_OR(
        e.log_type IN ({engagement_types})
        AND e.visited >= fel.first_engagement_date + INTERVAL '90 days'
        AND e.visited < fel.first_engagement_date + INTERVAL '120 days'
      ) AS active_e_m3,
      BOOL_OR(
        e.log_type IN ({engagement_types})
        AND e.visited >= fel.first_engagement_date + INTERVAL '180 days'
        AND e.visited < fel.first_engagement_date + INTERVAL '210 days'
      ) AS active_e_m6,
      BOOL_OR(
        e.log_type IN ({engagement_types})
        AND e.visited >= fel.first_engagement_date + INTERVAL '360 days'
        AND e.visited < fel.first_engagement_date + INTERVAL '390 days'
      ) AS active_e_m12,
      BOOL_OR(
        e.log_type IN ({engagement_types})
        AND e.visited >= b.registration_date
        AND e.visited > p.as_of - INTERVAL '30 days'
        AND e.visited <= p.as_of
      ) AS active_last30,
      BOOL_OR(
        e.log_type IN ({engagement_types})
        AND e.visited >= b.registration_date
      ) AS has_engagement,
      MAX(e.visited) FILTER (
        WHERE e.log_type IN ({engagement_types})
          AND e.visited >= b.registration_date
      ) AS last_engagement
    FROM base b
    JOIN params p ON TRUE
    LEFT JOIN scoped_events e ON e.guid = b.guid
    LEFT JOIN first_dnf fd ON fd.guid = b.guid
    LEFT JOIN first_engagement_locations fel ON fel.guid = b.guid
    LEFT JOIN first_engagement_cache_history feh ON feh.guid = b.guid
    GROUP BY b.guid, b.reg_place, b.cohort_month, b.registration_date,
             fd.first_valid_dnf, fd.first_valid_dnf_cache,
             fd.first_valid_dnf_cache_count, fd.first_valid_dnf_difficulty,
             fd.first_valid_dnf_terrain, fd.first_valid_dnf_geocache_type,
             fd.first_valid_dnf_container_type,
             fel.first_engagement_date, fel.first_engagement_city,
             fel.first_engagement_cache_count, fel.first_engagement_cache,
             fel.first_engagement_difficulty, fel.first_engagement_terrain,
             fel.first_engagement_geocache_type, fel.first_engagement_container_type,
             feh.first_engagement_cache_placed_date,
             feh.first_engagement_cache_prior_found,
             feh.first_engagement_cache_prior_dnf_90
    ORDER BY b.cohort_month, b.guid;
    """


def build_quality_query() -> str:
    countries = ", ".join(sql_literal(country) for country in GREATER_CHINA_COUNTRIES)
    return f"""
    WITH scoped_log_users AS (
      SELECT DISTINCT l.user_guid AS guid
      FROM logs l
      JOIN caches c ON c.code = l.gc_code
      WHERE c.country IN ({countries})
        AND l.user_guid IS NOT NULL
        AND TRIM(l.user_guid) <> ''
        AND l.user_guid <> 'deleted'
        AND COALESCE(l.log_type, '') <> 'deleted'
    )
    SELECT
      (SELECT COUNT(*)::int FROM scoped_log_users) AS scope_log_users,
      COUNT(*) FILTER (WHERE u.guid IS NOT NULL)::int AS user_dimension_matched,
      COUNT(*) FILTER (WHERE u.registration_date IS NOT NULL)::int AS registration_known,
      COUNT(*) FILTER (WHERE u.registration_date IS NULL)::int AS registration_missing,
      (SELECT COUNT(*)::int FROM caches c
       WHERE c.country IN ({countries}) AND c.logs_crawled_at IS NULL) AS caches_without_completed_logs
    FROM scoped_log_users slu
    LEFT JOIN "user" u ON u.guid = slu.guid;
    """


def fetch_rows(
    database_url: str, as_of: date, cohort_start: date
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Read dashboard inputs without modifying the database."""
    conn = connect_postgres(
        database_url,
        logger=LOGGER,
        connect_timeout=10,
        cursor_factory=RealDictCursor,
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                build_cohort_query(), {"as_of": as_of, "cohort_start": cohort_start}
            )
            rows = [dict(row) for row in cursor.fetchall()]
            cursor.execute(build_quality_query())
            quality = dict(cursor.fetchone())
    finally:
        conn.close()
    return rows, quality


def rate(numerator: int, denominator: int) -> float | None:
    return round(numerator * 100 / denominator, 1) if denominator else None


def filter_rows_by_reg_place(rows: Iterable[dict[str, Any]], value: str) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("reg_place") == value]


def quarter_start(value: date) -> date:
    return date(value.year, ((value.month - 1) // 3) * 3 + 1, 1)


def quarter_label(value: date) -> str:
    return f"{value.year} Q{((value.month - 1) // 3) + 1}"


def shift_quarter(value: date, offset: int) -> date:
    month_index = value.year * 12 + value.month - 1 + offset * 3
    year, zero_based_month = divmod(month_index, 12)
    return date(year, zero_based_month + 1, 1)


def _quarter_rows(
    rows: Iterable[dict[str, Any]], quarter_start_date: date
) -> list[dict[str, Any]]:
    end = shift_quarter(quarter_start_date, 1)
    selected = []
    for source in rows:
        registration = source.get("registration_date")
        if (
            not registration
            or not (quarter_start_date <= registration < end)
            or source.get("pre_registration_find")
        ):
            continue
        row = dict(source)
        row.setdefault("cohort_month", date(registration.year, registration.month, 1))
        for key, default in (
            ("finds_d7", 0), ("finds_d30", 0), ("finds_d90", 0),
            ("active_d30", False), ("active_d90", False),
            ("active_last30", False), ("has_engagement", False),
            ("first_valid_find", None), ("last_engagement", None),
        ):
            row.setdefault(key, default)
        selected.append(row)
    return selected


def _quarter_comparison_item(
    rows: list[dict[str, Any]], quarter_start_date: date, as_of: date
) -> dict[str, Any]:
    total = len(rows)
    month_counts: dict[str, int] = defaultdict(int)
    day_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        registration = row["registration_date"]
        month_counts[f"{registration.year:04d}-{registration.month:02d}"] += 1
        day_counts[registration.isoformat()] += 1
    summary = summarize_cohorts(rows, as_of)
    metrics = dict(summary[0]) if summary else {}
    china_users = sum(row.get("reg_place") == "China" for row in rows)
    no_find_users = sum(row.get("first_valid_find") is None for row in rows)
    peak_month = max(month_counts.values(), default=0)
    peak_day = max(day_counts.values(), default=0)
    return {
        "quarter": quarter_label(quarter_start_date),
        "totalValidUsers": total,
        "chinaUsers": china_users,
        "chinaShare": rate(china_users, total),
        "noFindUsers": no_find_users,
        "noFindShare": rate(no_find_users, total),
        "peakMonthShare": rate(peak_month, total),
        "peakDayShare": rate(peak_day, total),
        "metrics": metrics,
    }


def analyze_focus_quarter(rows: Iterable[dict[str, Any]], quarter_start_date: date, as_of: date) -> dict[str, Any]:
    """Return raw, reproducible diagnostics for one registration quarter."""
    source_rows = list(rows)
    selected = _quarter_rows(source_rows, quarter_start_date)
    def aggregate(subset):
        summary = summarize_cohorts(subset, as_of)
        out = {key: value for key, value in (summary[0].items() if summary else [])}
        return out
    place_counts = defaultdict(int)
    month_counts = defaultdict(int)
    day_counts = defaultdict(int)
    for row in selected:
        place_counts[row.get("reg_place") or "未标注"] += 1
        registration = row["registration_date"]
        month_counts[f"{registration.year:04d}-{registration.month:02d}"] += 1
        day_counts[registration.isoformat()] += 1
    return {"quarter": quarter_label(quarter_start_date), "quarterStart": quarter_start_date.isoformat(),
            "totalValidUsers": len(selected),
            "registrationMonthCounts": dict(sorted(month_counts.items())),
            "registrationDayCounts": dict(sorted(day_counts.items())),
            "regPlaceCounts": dict(sorted(place_counts.items())),
            "cohorts": {"overall": aggregate(selected), "china": aggregate([r for r in selected if r.get("reg_place") == "China"]), "nonChina": aggregate([r for r in selected if r.get("reg_place") != "China"])},
            "comparisonQuarters": [
                _quarter_comparison_item(
                    _quarter_rows(source_rows, shift_quarter(quarter_start_date, offset)),
                    shift_quarter(quarter_start_date, offset),
                    as_of,
                )
                for offset in (-1, 0, 1)
            ]}


INSIGHT_RATE_FIELDS = {
    "firstFindD7Rate": ("firstFindD7", "eligibleD7"),
    "firstFindD30Rate": ("firstFindD30", "eligibleD30"),
    "firstFindD90Rate": ("firstFindD90", "eligibleD90"),
    "finds10D90Rate": ("finds10D90", "eligibleD90"),
    "activeD30Rate": ("activeD30", "eligibleActiveD30"),
    "activeD90Rate": ("activeD90", "eligibleActiveD90"),
    "activeM1Rate": ("activeM1", "eligibleActiveM1"),
    "activeM2Rate": ("activeM2", "eligibleActiveM2"),
    "activeM3Rate": ("activeM3", "eligibleActiveM3"),
    "activeM6Rate": ("activeM6", "eligibleActiveM6"),
    "activeM12Rate": ("activeM12", "eligibleActiveM12"),
    "activeLast30Rate": ("activeLast30", "validUsers"),
}


def _sum_field(rows: Iterable[dict[str, Any]], field: str) -> int:
    return sum(int(row.get(field) or 0) for row in rows)


def _weighted_insight_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "validUsers": _sum_field(rows, "validUsers"),
        "lifecycleEligible": _sum_field(rows, "lifecycleEligible"),
    }
    for output, (numerator, denominator) in INSIGHT_RATE_FIELDS.items():
        numerator_value = _sum_field(rows, numerator)
        denominator_value = _sum_field(rows, denominator)
        result[numerator] = numerator_value
        result[denominator] = denominator_value
        result[output] = rate(numerator_value, denominator_value)

    lifecycle = {
        "zeroInactive": _sum_field(rows, "lifecycleNoFindNoD90"),
        "zeroActive": _sum_field(rows, "lifecycleNoFindD90"),
        "lightInactive": _sum_field(rows, "lifecycleFinds1To9NoD90"),
        "lightActive": _sum_field(rows, "lifecycleFinds1To9D90"),
        "deepInactive": _sum_field(rows, "lifecycleFinds10PlusNoD90"),
        "deepActive": _sum_field(rows, "lifecycleFinds10PlusD90"),
    }
    eligible = result["lifecycleEligible"]
    result.update(lifecycle)
    result["zeroShare"] = rate(lifecycle["zeroInactive"] + lifecycle["zeroActive"], eligible)
    result["lightShare"] = rate(lifecycle["lightInactive"] + lifecycle["lightActive"], eligible)
    result["deepShare"] = rate(lifecycle["deepInactive"] + lifecycle["deepActive"], eligible)
    result["zeroConditionalD90"] = rate(
        lifecycle["zeroActive"], lifecycle["zeroInactive"] + lifecycle["zeroActive"]
    )
    result["lightConditionalD90"] = rate(
        lifecycle["lightActive"], lifecycle["lightInactive"] + lifecycle["lightActive"]
    )
    result["deepConditionalD90"] = rate(
        lifecycle["deepActive"], lifecycle["deepInactive"] + lifecycle["deepActive"]
    )
    return result


def _cohort_year(row: dict[str, Any]) -> int:
    return int(str(row["cohortQuarter"])[:4])


def _year_summary(rows: list[dict[str, Any]], year: int) -> dict[str, Any]:
    return _weighted_insight_summary([row for row in rows if _cohort_year(row) == year])


def _difference_summary(
    overall_rows: list[dict[str, Any]], china_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    fields = {
        "validUsers", "lifecycleEligible",
        *(value for pair in INSIGHT_RATE_FIELDS.values() for value in pair),
        "lifecycleNoFindNoD90", "lifecycleNoFindD90",
        "lifecycleFinds1To9NoD90", "lifecycleFinds1To9D90",
        "lifecycleFinds10PlusNoD90", "lifecycleFinds10PlusD90",
    }
    synthetic = {
        field: _sum_field(overall_rows, field) - _sum_field(china_rows, field)
        for field in fields
    }
    return _weighted_insight_summary([synthetic])


def _behavior_outcome_groups(
    rows: list[dict[str, Any]], classifier: str, as_of: date
) -> list[dict[str, Any]]:
    if classifier == "d7":
        definitions = (
            ("d7_find", "D7 内完成首找", lambda row: int(row.get("finds_d7") or 0) > 0),
            ("no_d7_find", "D7 内未完成首找", lambda row: int(row.get("finds_d7") or 0) == 0),
        )
    elif classifier == "d30_depth":
        definitions = (
            ("no_find", "D30 内 0 找", lambda row: int(row.get("finds_d30") or 0) == 0),
            ("finds_1_to_9", "D30 内 1–9 找", lambda row: 1 <= int(row.get("finds_d30") or 0) < 10),
            ("finds_10_plus", "D30 内 10+ 找", lambda row: int(row.get("finds_d30") or 0) >= 10),
        )
    else:
        raise ValueError(f"Unsupported behavior classifier: {classifier}")

    output = []
    for key, label, predicate in definitions:
        group = [row for row in rows if predicate(row)]
        result: dict[str, Any] = {"key": key, "label": label, "groupUsers": len(group)}
        for output_label, end_day, source_field, fallback_field in (
            ("D90", 120, "active_m3", "active_d90"),
            ("M6", 210, "active_m6", None),
            ("M12", 390, "active_m12", None),
        ):
            eligible = [
                row for row in group
                if row.get("registration_date")
                and row["registration_date"] <= as_of - timedelta(days=end_day)
            ]
            active = 0
            for row in eligible:
                value = row.get(source_field)
                if value is None and fallback_field:
                    value = row.get(fallback_field)
                active += int(bool(value))
            result[f"eligible{output_label}"] = len(eligible)
            result[f"active{output_label}"] = active
            result[f"active{output_label}Rate"] = rate(active, len(eligible))
        output.append(result)
    return output


def analyze_growth_and_onboarding(
    rows: Iterable[dict[str, Any]], as_of: date
) -> dict[str, Any]:
    """Measure observable growth components and early-behavior associations."""
    valid_rows = [dict(row) for row in rows if not row.get("pre_registration_find")]
    summaries = summarize_cohorts(valid_rows, as_of)
    annual = {
        str(year): _year_summary(summaries, year)
        for year in (2024, 2025)
        if any(_cohort_year(row) == year for row in summaries)
    }

    def behavior(subset: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "d7Groups": _behavior_outcome_groups(subset, "d7", as_of),
            "d30DepthGroups": _behavior_outcome_groups(subset, "d30_depth", as_of),
        }

    return {
        "annual": annual,
        "behaviorOutcomes": {
            "overall": behavior(valid_rows),
            "china": behavior([row for row in valid_rows if row.get("reg_place") == "China"]),
        },
    }


def _cadence_activity_summary(
    rows: list[dict[str, Any]], as_of: date
) -> dict[str, Any]:
    result: dict[str, Any] = {"users": len(rows)}
    for label, field, end_day in (
        ("M3", "active_m3", 120),
        ("M6", "active_m6", 210),
        ("M12", "active_m12", 390),
    ):
        eligible = [
            row for row in rows
            if row.get("registration_date")
            and row["registration_date"] <= as_of - timedelta(days=end_day)
        ]
        active = sum(bool(row.get(field)) for row in eligible)
        result[f"eligible{label}"] = len(eligible)
        result[f"active{label}"] = active
        result[f"active{label}Rate"] = rate(active, len(eligible))
    return result


def analyze_engagement_cadence(
    rows: Iterable[dict[str, Any]], as_of: date
) -> dict[str, Any]:
    """Compare single-day and multi-day finding while controlling D30 depth."""
    eligible = [
        dict(row) for row in rows
        if row.get("reg_place") == "China"
        and not row.get("pre_registration_find")
        and row.get("registration_date")
        and row["registration_date"] <= as_of - timedelta(days=30)
    ]

    def period_label(row: dict[str, Any]) -> str:
        year = row["registration_date"].year
        if year <= 2020:
            return "2017–2020"
        if year <= 2023:
            return "2021–2023"
        if year <= 2025:
            return "2024–2025"
        return f"{as_of.year} YTD"

    period_trends = []
    labels = ("2017–2020", "2021–2023", "2024–2025", f"{as_of.year} YTD")
    for label in labels:
        selected = [row for row in eligible if period_label(row) == label]
        finders = [row for row in selected if int(row.get("finds_d30") or 0) > 0]
        multi_day = [row for row in finders if int(row.get("find_days_d30") or 0) >= 2]
        four_plus_days = [row for row in finders if int(row.get("find_days_d30") or 0) >= 4]
        period_trends.append({
            "period": label,
            "eligibleD30": len(selected),
            "findersD30": len(finders),
            "finderRate": rate(len(finders), len(selected)),
            "multiDayFinders": len(multi_day),
            "multiDayShareAmongFinders": rate(len(multi_day), len(finders)),
            "fourPlusDayFinders": len(four_plus_days),
            "fourPlusDayShareAmongFinders": rate(len(four_plus_days), len(finders)),
            "finds3PlusUsers": sum(int(row.get("finds_d30") or 0) >= 3 for row in selected),
            "finds3PlusRate": rate(
                sum(int(row.get("finds_d30") or 0) >= 3 for row in selected), len(selected)
            ),
            "finds10PlusUsers": sum(int(row.get("finds_d30") or 0) >= 10 for row in selected),
            "finds10PlusRate": rate(
                sum(int(row.get("finds_d30") or 0) >= 10 for row in selected), len(selected)
            ),
        })

    recent = [row for row in eligible if row["registration_date"].year >= 2024]
    depth_definitions = (
        ("finds_1_2", "1–2 找", lambda row: 1 <= int(row.get("finds_d30") or 0) <= 2),
        ("finds_3_9", "3–9 找", lambda row: 3 <= int(row.get("finds_d30") or 0) <= 9),
        ("finds_10_plus", "10+ 找", lambda row: int(row.get("finds_d30") or 0) >= 10),
    )
    cadence_definitions = (
        ("single_day", "1 个找寻日", lambda row: int(row.get("find_days_d30") or 0) == 1),
        ("multi_day", "2+ 个找寻日", lambda row: int(row.get("find_days_d30") or 0) >= 2),
    )
    depth_cadence = []
    for depth_key, depth_label, depth_predicate in depth_definitions:
        depth_rows = [row for row in recent if depth_predicate(row)]
        for cadence_key, cadence_label, cadence_predicate in cadence_definitions:
            selected = [row for row in depth_rows if cadence_predicate(row)]
            summary = _cadence_activity_summary(selected, as_of)
            summary.update({
                "depthKey": depth_key,
                "depthLabel": depth_label,
                "cadenceKey": cadence_key,
                "cadenceLabel": cadence_label,
                "depthUsers": len(depth_rows),
                "shareWithinDepth": rate(len(selected), len(depth_rows)),
            })
            depth_cadence.append(summary)

    return {
        "scope": "reg_place = China",
        "eligibleD30Users": len(eligible),
        "recentStartYear": 2024,
        "recentEligibleD30Users": len(recent),
        "periodTrends": period_trends,
        "recentDepthCadence": depth_cadence,
    }


def analyze_cache_historical_freshness(
    rows: Iterable[dict[str, Any]], as_of: date
) -> dict[str, Any]:
    """Compare first-activity outcomes by history known before that activity."""
    selected = [
        dict(row) for row in rows
        if row.get("reg_place") == "China"
        and not row.get("pre_registration_find")
        and row.get("registration_date")
        and row["registration_date"].year >= 2024
        and row.get("first_engagement_date")
        and int(row.get("first_engagement_cache_count") or 0) == 1
    ]

    def summarize(key: str, label: str, group: list[dict[str, Any]]) -> dict[str, Any]:
        eligible_d30 = [
            row for row in group
            if row["registration_date"] <= as_of - timedelta(days=30)
        ]
        eligible_m3 = [
            row for row in group
            if row["registration_date"] <= as_of - timedelta(days=120)
        ]
        first_day_found = sum(bool(row.get("first_engagement_found")) for row in group)
        first_day_dnf_only = sum(
            bool(row.get("first_engagement_dnf"))
            and not bool(row.get("first_engagement_found"))
            for row in group
        )
        d30_found = sum(int(row.get("finds_d30") or 0) > 0 for row in eligible_d30)
        active_m3 = sum(bool(row.get("active_m3")) for row in eligible_m3)
        return {
            "key": key,
            "label": label,
            "users": len(group),
            "firstDayFound": first_day_found,
            "firstDayFoundRate": rate(first_day_found, len(group)),
            "firstDayDnfOnly": first_day_dnf_only,
            "firstDayDnfOnlyRate": rate(first_day_dnf_only, len(group)),
            "eligibleD30": len(eligible_d30),
            "d30Found": d30_found,
            "d30FoundRate": rate(d30_found, len(eligible_d30)),
            "eligibleM3": len(eligible_m3),
            "activeM3": active_m3,
            "activeM3Rate": rate(active_m3, len(eligible_m3)),
        }

    freshness_definitions = (
        ("within_30", "此前 1–30 天有 Found", lambda days: days is not None and days <= 30),
        ("days_31_180", "此前 31–180 天有 Found", lambda days: days is not None and 31 <= days <= 180),
        ("over_180", "此前超过 180 天有 Found", lambda days: days is not None and days > 180),
        ("no_prior_found", "此前无可观察 Found", lambda days: days is None),
    )

    def days_since_prior_found(row: dict[str, Any]) -> int | None:
        prior = row.get("first_engagement_cache_prior_found")
        if not prior:
            return None
        days = (row["first_engagement_date"] - prior).days
        return days if days >= 1 else None

    freshness_bands = []
    for key, label, predicate in freshness_definitions:
        group = [row for row in selected if predicate(days_since_prior_found(row))]
        freshness_bands.append(summarize(key, label, group))

    age_definitions = (
        ("under_1y", "不足 1 年", 0, 365),
        ("years_1_3", "1–3 年", 365, 365 * 3),
        ("years_3_7", "3–7 年", 365 * 3, 365 * 7),
        ("years_7_plus", "7 年及以上", 365 * 7, None),
    )

    def cache_age_days(row: dict[str, Any]) -> int | None:
        placed = row.get("first_engagement_cache_placed_date")
        if not placed:
            return None
        days = (row["first_engagement_date"] - placed).days
        return days if days >= 0 else None

    age_bands = []
    for key, label, minimum, maximum in age_definitions:
        group = [
            row for row in selected
            if cache_age_days(row) is not None
            and cache_age_days(row) >= minimum
            and (maximum is None or cache_age_days(row) < maximum)
        ]
        age_bands.append(summarize(key, label, group))

    prior_dnf_definitions = (
        ("none", "过去 90 天 0 条 DNF", lambda count: count == 0),
        ("one", "过去 90 天 1 条 DNF", lambda count: count == 1),
        ("two_three", "过去 90 天 2–3 条 DNF", lambda count: 2 <= count <= 3),
        ("four_plus", "过去 90 天 4+ 条 DNF", lambda count: count >= 4),
    )
    prior_dnf_bands = []
    for key, label, predicate in prior_dnf_definitions:
        group = [
            row for row in selected
            if predicate(int(row.get("first_engagement_cache_prior_dnf_90") or 0))
        ]
        prior_dnf_bands.append(summarize(key, label, group))

    return {
        "scope": "reg_place = China；注册年份 >= 2024；首次活动日唯一缓存",
        "users": len(selected),
        "eligibleD30": sum(
            row["registration_date"] <= as_of - timedelta(days=30)
            for row in selected
        ),
        "eligibleM3": sum(
            row["registration_date"] <= as_of - timedelta(days=120)
            for row in selected
        ),
        "freshnessBands": freshness_bands,
        "cacheAgeBands": age_bands,
        "priorDnfBands": prior_dnf_bands,
    }


def _city_group_summary(
    label: str,
    rows: list[dict[str, Any]],
    total_valid_users: int,
    as_of: date,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "group": label,
        "city": label,
        "groupUsers": len(rows),
        "groupShare": rate(len(rows), total_valid_users),
    }
    engagement_rows = [row for row in rows if row.get("first_engagement_date")]
    result["firstDayFound"] = sum(bool(row.get("first_engagement_found")) for row in engagement_rows)
    result["firstDayDnf"] = sum(bool(row.get("first_engagement_dnf")) for row in engagement_rows)
    result["firstDayAttended"] = sum(bool(row.get("first_engagement_attended")) for row in engagement_rows)
    result["firstDayDnfWithoutFound"] = sum(
        bool(row.get("first_engagement_dnf")) and not bool(row.get("first_engagement_found"))
        for row in engagement_rows
    )
    result["firstDayFoundAndDnf"] = sum(
        bool(row.get("first_engagement_found")) and bool(row.get("first_engagement_dnf"))
        for row in engagement_rows
    )
    for field in ("firstDayFound", "firstDayDnf", "firstDayAttended"):
        result[f"{field}Rate"] = rate(result[field], len(engagement_rows))
    for field in ("firstDayDnfWithoutFound", "firstDayFoundAndDnf"):
        result[f"{field}Rate"] = rate(result[field], len(engagement_rows))

    eligible_e30 = [
        row for row in engagement_rows
        if row["first_engagement_date"] <= as_of - timedelta(days=30)
    ]
    found_e30 = sum(int(row.get("finds_e30") or 0) > 0 for row in eligible_e30)
    finds10_e30 = sum(int(row.get("finds_e30") or 0) >= 10 for row in eligible_e30)
    result.update({
        "eligibleE30": len(eligible_e30),
        "foundE30": found_e30,
        "foundE30Rate": rate(found_e30, len(eligible_e30)),
        "finds10E30": finds10_e30,
        "finds10E30Rate": rate(finds10_e30, len(eligible_e30)),
        "e30Depth": [
            {
                "key": key,
                "label": depth_label,
                "users": sum(
                    minimum <= int(row.get("finds_e30") or 0)
                    and (maximum is None or int(row.get("finds_e30") or 0) <= maximum)
                    for row in eligible_e30
                ),
            }
            for key, depth_label, minimum, maximum in (
                ("finds_0", "0找", 0, 0),
                ("finds_1_2", "1–2找", 1, 2),
                ("finds_3_9", "3–9找", 3, 9),
                ("finds_10_plus", "10+找", 10, None),
            )
        ],
    })
    for depth in result["e30Depth"]:
        depth["share"] = rate(depth["users"], len(eligible_e30))

    for metric, end_day, source_field in (
        ("EM1", 60, "active_e_m1"),
        ("EM3", 120, "active_e_m3"),
        ("EM6", 210, "active_e_m6"),
        ("EM12", 390, "active_e_m12"),
    ):
        eligible = [
            row for row in engagement_rows
            if row["first_engagement_date"] <= as_of - timedelta(days=end_day)
        ]
        active = sum(bool(row.get(source_field)) for row in eligible)
        result[f"eligible{metric}"] = len(eligible)
        result[f"active{metric}"] = active
        result[f"active{metric}Rate"] = rate(active, len(eligible))

    for metric, end_day, predicate in (
        ("FirstFindD7", 7, lambda row: int(row.get("finds_d7") or 0) > 0),
        ("FirstFindD30", 30, lambda row: int(row.get("finds_d30") or 0) > 0),
        ("Finds10D30", 30, lambda row: int(row.get("finds_d30") or 0) >= 10),
        ("FirstFindD90", 90, lambda row: int(row.get("finds_d90") or 0) > 0),
        ("Finds10D90", 90, lambda row: int(row.get("finds_d90") or 0) >= 10),
    ):
        eligible = [
            row for row in rows
            if row.get("registration_date")
            and row["registration_date"] <= as_of - timedelta(days=end_day)
        ]
        count = sum(bool(predicate(row)) for row in eligible)
        result[f"eligible{metric.removeprefix('FirstFind').removeprefix('Finds10')}"] = len(eligible)
        result[metric[0].lower() + metric[1:]] = count
        result[metric[0].lower() + metric[1:] + "Rate"] = rate(count, len(eligible))

    for metric, end_day, source_field, fallback_field in (
        ("M1", 60, "active_m1", "active_d30"),
        ("M3", 120, "active_m3", "active_d90"),
        ("M6", 210, "active_m6", None),
        ("M12", 390, "active_m12", None),
    ):
        eligible = [
            row for row in rows
            if row.get("registration_date")
            and row["registration_date"] <= as_of - timedelta(days=end_day)
        ]
        active = 0
        for row in eligible:
            value = row.get(source_field)
            if value is None and fallback_field:
                value = row.get(fallback_field)
            active += int(bool(value))
        result[f"eligible{metric}"] = len(eligible)
        result[f"active{metric}"] = active
        result[f"active{metric}Rate"] = rate(active, len(eligible))
    return result


def analyze_city_cohorts(rows: Iterable[dict[str, Any]], as_of: date) -> dict[str, Any]:
    """Compare China users by their first post-registration engagement city."""
    valid_rows = [
        dict(row) for row in rows
        if row.get("reg_place") == "China" and not row.get("pre_registration_find")
    ]
    def city_label(row: dict[str, Any]) -> str:
        if not row.get("first_engagement_date"):
            return NO_POST_REGISTRATION_ACTIVITY_LABEL
        city = str(row.get("first_engagement_city") or "").strip()
        return city or UNKNOWN_CITY_LABEL

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in valid_rows:
        grouped[city_label(row)].append(row)

    special_labels = {
        MULTI_CITY_LABEL,
        UNKNOWN_CITY_LABEL,
        NO_POST_REGISTRATION_ACTIVITY_LABEL,
    }
    concrete_labels = sorted(
        (label for label in grouped if label not in special_labels),
        key=lambda label: (-len(grouped[label]), label),
    )
    top_labels = concrete_labels[:CITY_COHORT_TOP_N]
    remaining_rows = [
        row for label in concrete_labels[CITY_COHORT_TOP_N:] for row in grouped[label]
    ]
    display_groups = [
        _city_group_summary(label, grouped[label], len(valid_rows), as_of)
        for label in top_labels
    ]
    if remaining_rows:
        display_groups.append(
            _city_group_summary("其他城市", remaining_rows, len(valid_rows), as_of)
        )
    display_groups.extend(
        _city_group_summary(label, grouped[label], len(valid_rows), as_of)
        for label in (MULTI_CITY_LABEL, UNKNOWN_CITY_LABEL, NO_POST_REGISTRATION_ACTIVITY_LABEL)
        if grouped.get(label)
    )

    all_groups = [
        _city_group_summary(label, grouped[label], len(valid_rows), as_of)
        for label in sorted(grouped, key=lambda label: (-len(grouped[label]), label))
    ]
    exact_city_users = sum(
        len(grouped[label]) for label in concrete_labels
    )
    comparison_labels = concrete_labels[:6]

    def period_summary(
        key: str,
        label: str,
        selected: list[dict[str, Any]],
        start: str,
        end: str,
    ) -> dict[str, Any]:
        period_groups = [
            _city_group_summary(
                city,
                [row for row in selected if city_label(row) == city],
                len(selected),
                as_of,
            )
            for city in comparison_labels
        ]
        return {
            "key": key,
            "label": label,
            "start": start,
            "end": end,
            "totalUsers": len(selected),
            "summary": _city_group_summary(label, selected, len(selected), as_of),
            "groups": period_groups,
        }

    year_comparisons = [
        period_summary(
            str(year),
            f"{year} 年",
            [row for row in valid_rows if row["registration_date"].year == year],
            f"{year}-01-01",
            min(as_of, date(year, 12, 31)).isoformat(),
        )
        for year in (2023, 2024, 2025, 2026)
    ]
    video_rows = [
        row for row in valid_rows
        if date(2025, 8, 6) <= row["registration_date"] <= date(2025, 8, 13)
    ]
    adjacent_rows = [
        row for row in valid_rows
        if date(2025, 5, 1) <= row["registration_date"] <= date(2025, 11, 30)
        and row["registration_date"].month != 8
    ]
    event_comparisons = {
        "2025_video_wave": period_summary(
            "2025_video_wave", "2025 高播放视频合并窗口", video_rows,
            "2025-08-06", "2025-08-13",
        ),
        "2025_adjacent": period_summary(
            "2025_adjacent", "2025 年 5–7 月及 9–11 月", adjacent_rows,
            "2025-05-01", "2025-11-30（不含8月）",
        ),
    }
    return {
        "scope": "reg_place = China",
        "attribution": "注册后首次有效活动所在城市",
        "validLogTypes": list(PLAYER_ENGAGEMENT_LOG_TYPES),
        "totalValidUsers": len(valid_rows),
        "exactCityUsers": exact_city_users,
        "exactCityCoverage": rate(exact_city_users, len(valid_rows)),
        "uniqueExactCities": len(concrete_labels),
        "topN": CITY_COHORT_TOP_N,
        "displayCityLabels": comparison_labels,
        "groups": display_groups,
        "allGroups": all_groups,
        "yearComparisons": year_comparisons,
        "eventComparisons": event_comparisons,
    }


DT_BUCKETS = (
    ("1.0_1.5", "1.0–1.5", 1.0, 1.5),
    ("2.0_2.5", "2.0–2.5", 2.0, 2.5),
    ("3.0_3.5", "3.0–3.5", 3.0, 3.5),
    ("4.0_5.0", "4.0–5.0", 4.0, 5.0),
)


def _dt_bucket(value: Any) -> tuple[str, str] | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not 1.0 <= number <= 5.0:
        return None
    for key, label, minimum, maximum in DT_BUCKETS:
        if minimum <= number <= maximum:
            return key, label
    return None


def _cache_type_label(value: Any) -> str:
    try:
        type_id = int(value)
    except (TypeError, ValueError):
        return "未知类型"
    return CACHE_TYPE_LABELS.get(type_id, f"类型 {type_id}")


def _container_type_label(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return "未知容器类型"
    try:
        return f"容器类型 {int(value)}"
    except (TypeError, ValueError):
        return f"容器类型 {value}"


def analyze_cache_attributes(
    rows: Iterable[dict[str, Any]], as_of: date
) -> dict[str, Any]:
    """Compare cache attributes using only unambiguous single-cache dates."""
    valid_rows = [
        dict(row) for row in rows
        if row.get("reg_place") == "China" and not row.get("pre_registration_find")
    ]
    engagement_rows = [row for row in valid_rows if row.get("first_engagement_date")]
    exact_engagement = [
        row for row in engagement_rows
        if int(row.get("first_engagement_cache_count") or 0) == 1
    ]

    def engagement_summary(
        key: str, label: str, selected: list[dict[str, Any]]
    ) -> dict[str, Any]:
        eligible_e30 = [
            row for row in selected
            if row["first_engagement_date"] <= as_of - timedelta(days=30)
        ]
        eligible_em3 = [
            row for row in selected
            if row["first_engagement_date"] <= as_of - timedelta(days=120)
        ]
        found = sum(bool(row.get("first_engagement_found")) for row in selected)
        dnf_without_found = sum(
            bool(row.get("first_engagement_dnf"))
            and not bool(row.get("first_engagement_found"))
            for row in selected
        )
        found_and_dnf = sum(
            bool(row.get("first_engagement_found"))
            and bool(row.get("first_engagement_dnf"))
            for row in selected
        )
        return {
            "key": key,
            "label": label,
            "users": len(selected),
            "found": found,
            "foundRate": rate(found, len(selected)),
            "dnfWithoutFound": dnf_without_found,
            "dnfWithoutFoundRate": rate(dnf_without_found, len(selected)),
            "foundAndDnf": found_and_dnf,
            "foundAndDnfRate": rate(found_and_dnf, len(selected)),
            "eligibleE30": len(eligible_e30),
            "finds10E30": sum(int(row.get("finds_e30") or 0) >= 10 for row in eligible_e30),
            "finds10E30Rate": rate(
                sum(int(row.get("finds_e30") or 0) >= 10 for row in eligible_e30),
                len(eligible_e30),
            ),
            "eligibleEM3": len(eligible_em3),
            "activeEM3": sum(bool(row.get("active_e_m3")) for row in eligible_em3),
            "activeEM3Rate": rate(
                sum(bool(row.get("active_e_m3")) for row in eligible_em3),
                len(eligible_em3),
            ),
        }

    def grouped_summaries(
        selected: list[dict[str, Any]],
        field: str,
        mode: str,
        summarize,
    ) -> tuple[list[dict[str, Any]], int]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        unknown = 0
        for row in selected:
            value = row.get(field)
            if mode == "dt":
                bucket = _dt_bucket(value)
            elif mode == "cache_type":
                if value is None:
                    bucket = None
                else:
                    bucket = (str(int(value)), _cache_type_label(value))
            else:
                if value is None or str(value).strip() == "":
                    bucket = None
                else:
                    bucket = (str(value), _container_type_label(value))
            if bucket is None:
                unknown += 1
            else:
                grouped[bucket].append(row)
        items = [
            summarize(key, label, group)
            for (key, label), group in grouped.items()
        ]
        if mode != "dt" and any(item["users"] >= 30 for item in items):
            small_keys = {item["key"] for item in items if item["users"] < 30}
            if small_keys:
                small_rows = [
                    row for (key, _), group in grouped.items()
                    if key in small_keys for row in group
                ]
                items = [item for item in items if item["users"] >= 30]
                items.append(summarize("other", "其他", small_rows))
        items.sort(key=lambda item: (-item["users"], item["label"]))
        return items, unknown

    engagement_dimensions: dict[str, list[dict[str, Any]]] = {}
    engagement_unknown: dict[str, int] = {}
    for output, field, mode in (
        ("difficulty", "first_engagement_difficulty", "dt"),
        ("terrain", "first_engagement_terrain", "dt"),
        ("geocacheType", "first_engagement_geocache_type", "cache_type"),
        ("containerType", "first_engagement_container_type", "container_type"),
    ):
        engagement_dimensions[output], engagement_unknown[output] = grouped_summaries(
            exact_engagement, field, mode, engagement_summary
        )

    dnf_rows = [
        row for row in valid_rows
        if row.get("first_valid_dnf")
        and row.get("registration_date")
        and row["registration_date"] <= row["first_valid_dnf"]
        and row["first_valid_dnf"] < row["registration_date"] + timedelta(days=30)
    ]
    newcomer_failure = [
        row for row in dnf_rows
        if not row.get("first_valid_find")
        or row["first_valid_dnf"] <= row["first_valid_find"]
    ]
    exact_dnf = [
        row for row in newcomer_failure
        if int(row.get("first_valid_dnf_cache_count") or 0) == 1
    ]

    def recovery_delay(row: dict[str, Any]) -> int | None:
        recovered = row.get("first_find_on_or_after_dnf")
        first_dnf = row.get("first_valid_dnf")
        if not recovered or not first_dnf:
            return None
        days = (recovered - first_dnf).days
        return days if days >= 0 else None

    def recovery_summary(
        key: str, label: str, selected: list[dict[str, Any]]
    ) -> dict[str, Any]:
        eligible = [
            row for row in selected
            if row["first_valid_dnf"] <= as_of - timedelta(days=30)
        ]
        recovered = [
            row for row in eligible
            if recovery_delay(row) is not None and recovery_delay(row) <= 30
        ]
        same_day = [row for row in recovered if recovery_delay(row) == 0]
        delayed = [row for row in recovered if 1 <= recovery_delay(row) <= 30]
        comparable = [
            row for row in recovered
            if row.get("first_valid_dnf_cache")
            and row.get("first_find_on_or_after_dnf_cache")
        ]
        same_cache = sum(
            row["first_valid_dnf_cache"] == row["first_find_on_or_after_dnf_cache"]
            for row in comparable
        )
        delays = [recovery_delay(row) for row in delayed]
        return {
            "key": key,
            "label": label,
            "users": len(selected),
            "eligibleRecovery30": len(eligible),
            "recoveredWithin30": len(recovered),
            "recoveredWithin30Rate": rate(len(recovered), len(eligible)),
            "sameDayFound": len(same_day),
            "sameDayFoundRate": rate(len(same_day), len(eligible)),
            "recoveredDays1To30": len(delayed),
            "recoveredDays1To30Rate": rate(len(delayed), len(eligible)),
            "medianRecoveryDays1To30": round(float(median(delays)), 1) if delays else None,
            "cacheComparableRecoveries": len(comparable),
            "sameCacheRecoveries": same_cache,
            "sameCacheRecoveryShare": rate(same_cache, len(comparable)),
        }

    recovery_dimensions: dict[str, list[dict[str, Any]]] = {}
    recovery_unknown: dict[str, int] = {}
    for output, field, mode in (
        ("difficulty", "first_valid_dnf_difficulty", "dt"),
        ("terrain", "first_valid_dnf_terrain", "dt"),
        ("geocacheType", "first_valid_dnf_geocache_type", "cache_type"),
        ("containerType", "first_valid_dnf_container_type", "container_type"),
    ):
        recovery_dimensions[output], recovery_unknown[output] = grouped_summaries(
            exact_dnf, field, mode, recovery_summary
        )

    overall_recovery = recovery_summary("all", "全部唯一缓存首 DNF", exact_dnf)

    def period_result(
        key: str,
        label: str,
        predicate,
    ) -> dict[str, Any]:
        period_engagement = [row for row in exact_engagement if predicate(row)]
        period_dnf = [row for row in exact_dnf if predicate(row)]
        period_engagement_dimensions: dict[str, list[dict[str, Any]]] = {}
        period_recovery_dimensions: dict[str, list[dict[str, Any]]] = {}
        for output, engagement_field, recovery_field, mode in (
            ("difficulty", "first_engagement_difficulty", "first_valid_dnf_difficulty", "dt"),
            ("terrain", "first_engagement_terrain", "first_valid_dnf_terrain", "dt"),
            ("geocacheType", "first_engagement_geocache_type", "first_valid_dnf_geocache_type", "cache_type"),
            ("containerType", "first_engagement_container_type", "first_valid_dnf_container_type", "container_type"),
        ):
            period_engagement_dimensions[output], _ = grouped_summaries(
                period_engagement, engagement_field, mode, engagement_summary
            )
            period_recovery_dimensions[output], _ = grouped_summaries(
                period_dnf, recovery_field, mode, recovery_summary
            )
        return {
            "key": key,
            "label": label,
            "firstEngagementUsers": len(period_engagement),
            "firstEngagementSummary": engagement_summary(
                "all", "全部唯一缓存首次活动", period_engagement
            ),
            "firstEngagementDimensions": period_engagement_dimensions,
            "firstDnfUsers": len(period_dnf),
            "firstDnfRecoverySummary": recovery_summary(
                "all", "全部唯一缓存首 DNF", period_dnf
            ),
            "firstDnfRecoveryDimensions": period_recovery_dimensions,
        }

    period_comparisons = {
        "2025_video_wave": period_result(
            "2025_video_wave",
            "2025 高播放视频合并窗口",
            lambda row: date(2025, 8, 6) <= row["registration_date"] <= date(2025, 8, 13),
        ),
        "2025_adjacent": period_result(
            "2025_adjacent",
            "2025 年 5–7 月及 9–11 月",
            lambda row: date(2025, 5, 1) <= row["registration_date"] <= date(2025, 11, 30)
            and row["registration_date"].month != 8,
        ),
    }
    return {
        "scope": "reg_place = China；排除注册前 Found 异常",
        "attribution": "主分析仅使用首次活动日或首次 DNF 日恰有一个缓存的用户",
        "minimumDisplayedCategoricalGroupUsers": 30,
        "firstEngagement": {
            "allEngagementUsers": len(engagement_rows),
            "exactSingleCacheUsers": len(exact_engagement),
            "ambiguousMultiCacheUsers": len(engagement_rows) - len(exact_engagement),
            "dimensions": engagement_dimensions,
            "unknownAttributeUsers": engagement_unknown,
        },
        "firstDnfRecovery": {
            "newcomerFailureUsers": len(newcomer_failure),
            "exactSingleCacheUsers": len(exact_dnf),
            "ambiguousMultiCacheUsers": len(newcomer_failure) - len(exact_dnf),
            **{key: value for key, value in overall_recovery.items() if key not in {"key", "label", "users"}},
            "dimensions": recovery_dimensions,
            "unknownAttributeUsers": recovery_unknown,
        },
        "periodComparisons": period_comparisons,
    }


EARLY_PATHWAY_DEFINITIONS = (
    ("none", "30 天内无记录", False, False, False),
    ("dnf_only", "仅 DNF", False, True, False),
    ("attended_only", "仅 Attended", False, False, True),
    ("dnf_attended", "DNF + Attended，无找到", False, True, True),
    ("found_only", "仅找到", True, False, False),
    ("found_dnf", "找到 + DNF", True, True, False),
    ("found_attended", "找到 + Attended", True, False, True),
    ("found_dnf_attended", "找到 + DNF + Attended", True, True, True),
)


def _early_pathway_groups(rows: list[dict[str, Any]], as_of: date) -> list[dict[str, Any]]:
    mature_d30 = [
        row for row in rows
        if not row.get("pre_registration_find")
        and row.get("registration_date")
        and row["registration_date"] <= as_of - timedelta(days=30)
    ]
    output = []
    for key, label, has_find, has_dnf, has_attended in EARLY_PATHWAY_DEFINITIONS:
        group = [
            row for row in mature_d30
            if (int(row.get("finds_d30") or 0) > 0) == has_find
            and (int(row.get("dnf_d30") or 0) > 0) == has_dnf
            and (int(row.get("attended_d30") or 0) > 0) == has_attended
        ]
        result: dict[str, Any] = {
            "key": key,
            "label": label,
            "groupUsers": len(group),
            "groupShare": rate(len(group), len(mature_d30)),
        }
        for output_label, end_day, source_field, fallback_field in (
            ("M3", 120, "active_m3", "active_d90"),
            ("M6", 210, "active_m6", None),
            ("M12", 390, "active_m12", None),
        ):
            eligible = [
                row for row in group
                if row["registration_date"] <= as_of - timedelta(days=end_day)
            ]
            active = 0
            for row in eligible:
                value = row.get(source_field)
                if value is None and fallback_field:
                    value = row.get(fallback_field)
                active += int(bool(value))
            result[f"eligible{output_label}"] = len(eligible)
            result[f"active{output_label}"] = active
            result[f"active{output_label}Rate"] = rate(active, len(eligible))
        output.append(result)
    return output


def _dnf_within_depth_groups(rows: list[dict[str, Any]], as_of: date) -> list[dict[str, Any]]:
    mature_d30 = [
        row for row in rows
        if not row.get("pre_registration_find")
        and row.get("registration_date")
        and row["registration_date"] <= as_of - timedelta(days=30)
    ]
    definitions = (
        ("finds_1_to_9_no_dnf", "1–9 找，无 DNF", 1, 9, False),
        ("finds_1_to_9_with_dnf", "1–9 找，有 DNF", 1, 9, True),
        ("finds_10_plus_no_dnf", "10+ 找，无 DNF", 10, None, False),
        ("finds_10_plus_with_dnf", "10+ 找，有 DNF", 10, None, True),
    )
    output = []
    for key, label, minimum, maximum, has_dnf in definitions:
        depth_base = [
            row for row in mature_d30
            if int(row.get("finds_d30") or 0) >= minimum
            and (maximum is None or int(row.get("finds_d30") or 0) <= maximum)
        ]
        group = [
            row for row in depth_base
            if (int(row.get("dnf_d30") or 0) > 0) == has_dnf
        ]
        result: dict[str, Any] = {
            "key": key,
            "label": label,
            "groupUsers": len(group),
            "withinDepthShare": rate(len(group), len(depth_base)),
        }
        for output_label, end_day, source_field, fallback_field in (
            ("M3", 120, "active_m3", "active_d90"),
            ("M6", 210, "active_m6", None),
            ("M12", 390, "active_m12", None),
        ):
            eligible = [
                row for row in group
                if row["registration_date"] <= as_of - timedelta(days=end_day)
            ]
            active = 0
            for row in eligible:
                value = row.get(source_field)
                if value is None and fallback_field:
                    value = row.get(fallback_field)
                active += int(bool(value))
            result[f"eligible{output_label}"] = len(eligible)
            result[f"active{output_label}"] = active
            result[f"active{output_label}Rate"] = rate(active, len(eligible))
        output.append(result)
    return output


def _first_find_timing_groups(rows: list[dict[str, Any]], as_of: date) -> list[dict[str, Any]]:
    mature_d30 = [
        row for row in rows
        if not row.get("pre_registration_find")
        and row.get("registration_date")
        and row["registration_date"] <= as_of - timedelta(days=30)
    ]

    def find_delay(row: dict[str, Any]) -> int | None:
        first_find = row.get("first_valid_find")
        registration = row.get("registration_date")
        if not first_find or not registration:
            return None
        return (first_find - registration).days

    definitions = (
        ("day_0", "注册当日首找", lambda delay: delay == 0),
        ("days_1_2", "第 1–2 天首找", lambda delay: delay is not None and 1 <= delay <= 2),
        ("days_3_6", "第 3–6 天首找", lambda delay: delay is not None and 3 <= delay <= 6),
        ("days_7_29", "第 7–29 天首找", lambda delay: delay is not None and 7 <= delay <= 29),
        ("days_30_89", "第 30–89 天首找", lambda delay: delay is not None and 30 <= delay <= 89),
        ("days_90_plus", "第 90 天后首找", lambda delay: delay is not None and delay >= 90),
        ("no_find_record", "截至数据日仍无首找", lambda delay: delay is None),
    )
    output = []
    for key, label, predicate in definitions:
        group = [row for row in mature_d30 if predicate(find_delay(row))]
        result: dict[str, Any] = {
            "key": key,
            "label": label,
            "groupUsers": len(group),
            "groupShare": rate(len(group), len(mature_d30)),
        }
        for output_label, end_day, source_field, fallback_field in (
            ("M3", 120, "active_m3", "active_d90"),
            ("M6", 210, "active_m6", None),
            ("M12", 390, "active_m12", None),
        ):
            eligible = [
                row for row in group
                if row["registration_date"] <= as_of - timedelta(days=end_day)
            ]
            active = 0
            for row in eligible:
                value = row.get(source_field)
                if value is None and fallback_field:
                    value = row.get(fallback_field)
                active += int(bool(value))
            result[f"eligible{output_label}"] = len(eligible)
            result[f"active{output_label}"] = active
            result[f"active{output_label}Rate"] = rate(active, len(eligible))
        output.append(result)
    return output


def analyze_early_pathways(rows: Iterable[dict[str, Any]], as_of: date) -> dict[str, Any]:
    """Compare mutually exclusive first-30-day log-type paths with later activity."""
    valid_rows = [dict(row) for row in rows if not row.get("pre_registration_find")]
    china_rows = [row for row in valid_rows if row.get("reg_place") == "China"]
    recent_rows = [
        row for row in valid_rows
        if row.get("registration_date") and row["registration_date"].year in (2024, 2025)
    ]
    recent_china_rows = [row for row in recent_rows if row.get("reg_place") == "China"]
    return {
        "overall": _early_pathway_groups(valid_rows, as_of),
        "china": _early_pathway_groups(china_rows, as_of),
        "dnfWithinDepth": {
            "overall": _dnf_within_depth_groups(valid_rows, as_of),
            "china": _dnf_within_depth_groups(china_rows, as_of),
            "recentOverall": _dnf_within_depth_groups(recent_rows, as_of),
            "recentChina": _dnf_within_depth_groups(recent_china_rows, as_of),
        },
        "firstFindTiming": {
            "overall": _first_find_timing_groups(valid_rows, as_of),
            "china": _first_find_timing_groups(china_rows, as_of),
        },
    }


def _video_behavior_summary(rows: list[dict[str, Any]], as_of: date) -> dict[str, Any]:
    total = len(rows)

    def share(count: int) -> float | None:
        return rate(count, total)

    def retention(days: int, field: str) -> tuple[int, int, float | None]:
        eligible = [
            row for row in rows
            if row.get("registration_date")
            and row["registration_date"] <= as_of - timedelta(days=days)
        ]
        active = sum(bool(row.get(field)) for row in eligible)
        return len(eligible), active, rate(active, len(eligible))

    eligible_m3, active_m3, active_m3_rate = retention(120, "active_m3")
    eligible_m6, active_m6, active_m6_rate = retention(210, "active_m6")
    eligible_m12, active_m12, active_m12_rate = retention(390, "active_m12")
    found = [row for row in rows if int(row.get("finds_d30") or 0) > 0]
    dnf = [row for row in rows if int(row.get("dnf_d30") or 0) > 0]
    first_find_d2 = sum(
        bool(row.get("first_valid_find"))
        and 0 <= (row["first_valid_find"] - row["registration_date"]).days <= 2
        for row in rows
    )
    return {
        "users": total,
        "firstFindD2Rate": share(first_find_d2),
        "firstFindD7Rate": share(sum(int(row.get("finds_d7") or 0) > 0 for row in rows)),
        "firstFindD30Rate": share(len(found)),
        "finds10D30Rate": share(sum(int(row.get("finds_d30") or 0) >= 10 for row in rows)),
        "dnfD30Rate": share(len(dnf)),
        "noFoundOrDnfRate": share(sum(
            int(row.get("finds_d30") or 0) == 0 and int(row.get("dnf_d30") or 0) == 0
            for row in rows
        )),
        "dnfOnlyRate": share(sum(
            int(row.get("finds_d30") or 0) == 0 and int(row.get("dnf_d30") or 0) > 0
            for row in rows
        )),
        "foundOnlyRate": share(sum(
            int(row.get("finds_d30") or 0) > 0 and int(row.get("dnf_d30") or 0) == 0
            for row in rows
        )),
        "foundDnfRate": share(sum(
            int(row.get("finds_d30") or 0) > 0 and int(row.get("dnf_d30") or 0) > 0
            for row in rows
        )),
        "eligibleM3": eligible_m3,
        "activeM3": active_m3,
        "activeM3Rate": active_m3_rate,
        "eligibleM6": eligible_m6,
        "activeM6": active_m6,
        "activeM6Rate": active_m6_rate,
        "eligibleM12": eligible_m12,
        "activeM12": active_m12,
        "activeM12Rate": active_m12_rate,
    }


def _video_conversion_funnel(rows: list[dict[str, Any]], as_of: date) -> dict[str, Any]:
    """Build a nested D30 conversion funnel and later activity by find depth."""
    eligible = [
        row for row in rows
        if row.get("registration_date")
        and row["registration_date"] <= as_of - timedelta(days=30)
    ]
    stage_definitions = (
        ("any_action", "D30内有Found、DNF或Attended", lambda row: (
            int(row.get("finds_d30") or 0)
            + int(row.get("dnf_d30") or 0)
            + int(row.get("attended_d30") or 0)
        ) > 0),
        ("found_1_plus", "D30内至少1找", lambda row: int(row.get("finds_d30") or 0) >= 1),
        ("found_3_plus", "D30内至少3找", lambda row: int(row.get("finds_d30") or 0) >= 3),
        ("found_5_plus", "D30内至少5找", lambda row: int(row.get("finds_d30") or 0) >= 5),
        ("found_10_plus", "D30内至少10找", lambda row: int(row.get("finds_d30") or 0) >= 10),
    )
    stages = []
    prior_count = len(eligible)
    for key, label, predicate in stage_definitions:
        count = sum(predicate(row) for row in eligible)
        stages.append({
            "key": key,
            "label": label,
            "users": count,
            "cohortRate": rate(count, len(eligible)),
            "priorStepRate": rate(count, prior_count),
        })
        prior_count = count

    depth_definitions = (
        ("finds_0", "0找", 0, 0),
        ("finds_1_2", "1–2找", 1, 2),
        ("finds_3_4", "3–4找", 3, 4),
        ("finds_5_9", "5–9找", 5, 9),
        ("finds_10_plus", "10+找", 10, None),
    )
    depth_groups = []
    for key, label, minimum, maximum in depth_definitions:
        selected = [
            row for row in eligible
            if int(row.get("finds_d30") or 0) >= minimum
            and (maximum is None or int(row.get("finds_d30") or 0) <= maximum)
        ]
        result: dict[str, Any] = {
            "key": key,
            "label": label,
            "users": len(selected),
            "cohortShare": rate(len(selected), len(eligible)),
        }
        for suffix, days, field in (
            ("M3", 120, "active_m3"),
            ("M6", 210, "active_m6"),
            ("M12", 390, "active_m12"),
        ):
            mature = [
                row for row in selected
                if row["registration_date"] <= as_of - timedelta(days=days)
            ]
            active = sum(bool(row.get(field)) for row in mature)
            result[f"eligible{suffix}"] = len(mature)
            result[f"active{suffix}"] = active
            result[f"active{suffix}Rate"] = rate(active, len(mature))
        depth_groups.append(result)
    return {
        "registeredUsers": len(rows),
        "eligibleD30": len(eligible),
        "stages": stages,
        "findDepthRetention": depth_groups,
    }


def _first_dnf_recovery_summary(rows: list[dict[str, Any]], as_of: date) -> dict[str, Any]:
    """Describe recovery after a first DNF recorded in the first 30 days."""
    dnf_rows = [
        row for row in rows
        if row.get("first_valid_dnf")
        and row.get("registration_date")
        and row["registration_date"] <= row["first_valid_dnf"]
        and row["first_valid_dnf"] < row["registration_date"] + timedelta(days=30)
    ]
    newcomer_failure = [
        row for row in dnf_rows
        if not row.get("first_valid_find")
        or row["first_valid_dnf"] <= row["first_valid_find"]
    ]
    found_before_dnf = [
        row for row in dnf_rows
        if row.get("first_valid_find")
        and row["first_valid_find"] < row["first_valid_dnf"]
    ]

    def delay(row: dict[str, Any]) -> int | None:
        recovered = row.get("first_find_on_or_after_dnf")
        first_dnf = row.get("first_valid_dnf")
        if not recovered or not first_dnf:
            return None
        value = (recovered - first_dnf).days
        return value if value >= 0 else None

    bucket_definitions = (
        ("same_day", "同日有Found（顺序未知）", lambda value: value == 0),
        ("days_1_7", "1–7天恢复", lambda value: value is not None and 1 <= value <= 7),
        ("days_8_30", "8–30天恢复", lambda value: value is not None and 8 <= value <= 30),
        ("days_31_90", "31–90天恢复", lambda value: value is not None and 31 <= value <= 90),
        ("after_90", "90天后恢复", lambda value: value is not None and value > 90),
        ("no_observed_recovery", "截至数据日未观察到恢复", lambda value: value is None),
    )
    recovery_buckets = []
    for key, label, predicate in bucket_definitions:
        selected = [row for row in newcomer_failure if predicate(delay(row))]
        bucket: dict[str, Any] = {
            "key": key,
            "label": label,
            "users": len(selected),
            "share": rate(len(selected), len(newcomer_failure)),
        }
        for suffix, days, field in (
            ("M3", 120, "active_m3"),
            ("M6", 210, "active_m6"),
            ("M12", 390, "active_m12"),
        ):
            mature = [
                row for row in selected
                if row["registration_date"] <= as_of - timedelta(days=days)
            ]
            active = sum(bool(row.get(field)) for row in mature)
            bucket[f"eligible{suffix}"] = len(mature)
            bucket[f"active{suffix}Rate"] = rate(active, len(mature))
        recovery_buckets.append(bucket)

    eligible_30 = [
        row for row in newcomer_failure
        if row["first_valid_dnf"] <= as_of - timedelta(days=30)
    ]
    eligible_90 = [
        row for row in newcomer_failure
        if row["first_valid_dnf"] <= as_of - timedelta(days=90)
    ]
    recovered_30 = sum(delay(row) is not None and delay(row) <= 30 for row in eligible_30)
    recovered_90 = sum(delay(row) is not None and delay(row) <= 90 for row in eligible_90)
    recovered_rows = [row for row in newcomer_failure if delay(row) is not None]
    comparable_cache_rows = [
        row for row in recovered_rows
        if row.get("first_valid_dnf_cache")
        and row.get("first_find_on_or_after_dnf_cache")
    ]
    same_cache = sum(
        row["first_valid_dnf_cache"] == row["first_find_on_or_after_dnf_cache"]
        for row in comparable_cache_rows
    )
    return {
        "dnfUsers": len(dnf_rows),
        "newcomerFailureUsers": len(newcomer_failure),
        "newcomerFailureShareOfDnf": rate(len(newcomer_failure), len(dnf_rows)),
        "foundBeforeDnfUsers": len(found_before_dnf),
        "eligibleRecovery30": len(eligible_30),
        "recoveredWithin30": recovered_30,
        "recoveredWithin30Rate": rate(recovered_30, len(eligible_30)),
        "eligibleRecovery90": len(eligible_90),
        "recoveredWithin90": recovered_90,
        "recoveredWithin90Rate": rate(recovered_90, len(eligible_90)),
        "cacheComparableRecoveries": len(comparable_cache_rows),
        "sameCacheRecoveries": same_cache,
        "differentCacheRecoveries": len(comparable_cache_rows) - same_cache,
        "sameCacheRecoveryShare": rate(same_cache, len(comparable_cache_rows)),
        "recoveryBuckets": recovery_buckets,
    }


def analyze_video_influx(rows: Iterable[dict[str, Any]], as_of: date) -> dict[str, Any]:
    """Compare known Bilibili exposure periods with observed registration cohorts."""
    valid_rows = [dict(row) for row in rows if not row.get("pre_registration_find")]

    def first_find_spread(selected: list[dict[str, Any]]) -> dict[str, Any]:
        cache_counts: dict[str, int] = defaultdict(int)
        city_counts: dict[str, int] = defaultdict(int)
        for row in selected:
            cache_code = row.get("first_valid_find_cache")
            city = row.get("first_valid_find_city")
            if cache_code:
                cache_counts[str(cache_code)] += 1
            if city:
                city_counts[str(city)] += 1
        cache_users = sum(cache_counts.values())
        city_users = sum(city_counts.values())
        max_cache = max(cache_counts.values(), default=0)
        max_city = max(city_counts.values(), default=0)
        return {
            "uniqueCaches": len(cache_counts),
            "uniqueCities": len(city_counts),
            "maxSameCacheUsers": max_cache,
            "maxSameCacheShare": rate(max_cache, cache_users),
            "maxSameCityUsers": max_city,
            "maxSameCityShare": rate(max_city, city_users),
        }
    specs = (
        ("2022_same30", "2022 历史同期30天", date(2022, 6, 14), date(2022, 7, 13)),
        ("2023_pre30", "2023 发布前30天", date(2023, 5, 15), date(2023, 6, 13)),
        ("2023_wave", "2023 发布后0–6天", date(2023, 6, 14), date(2023, 6, 20)),
        ("2023_tail", "2023 发布后7–29天", date(2023, 6, 21), date(2023, 7, 13)),
        ("2024_same30", "2024 历史同期30天", date(2024, 6, 14), date(2024, 7, 13)),
        ("2023_aug", "2023年8月", date(2023, 8, 1), date(2023, 8, 31)),
        ("2024_aug", "2024年8月", date(2024, 8, 1), date(2024, 8, 31)),
        ("2025_july", "2025年7月", date(2025, 7, 1), date(2025, 7, 31)),
        ("2025_aug", "2025年8月", date(2025, 8, 1), date(2025, 8, 31)),
        ("2025_pre8", "2025 主视频前8天 7月29日–8月5日", date(2025, 7, 29), date(2025, 8, 5)),
        ("2025_wave1", "2025 两条高播放视频合并窗口 8月6–13日", date(2025, 8, 6), date(2025, 8, 13)),
        ("2025_main_before_followup", "主视频发布至第二条发布前 8月6–8日", date(2025, 8, 6), date(2025, 8, 8)),
        ("2025_followup_overlap", "第二条高播放视频发布后 8月9–13日", date(2025, 8, 9), date(2025, 8, 13)),
        ("2025_middle", "高播放视频长尾 8月14–25日", date(2025, 8, 14), date(2025, 8, 25)),
        ("2025_wave2", "8月26日低播放相关视频后0–6天", date(2025, 8, 26), date(2025, 9, 1)),
        ("2025_sep2_followup", "9月2日后续视频后0–6天", date(2025, 9, 2), date(2025, 9, 8)),
        ("2025_sep26_followup", "9月26日后续视频后0–6天", date(2025, 9, 26), date(2025, 10, 2)),
        ("2025_oct25_followup", "10月25日后续视频后0–6天", date(2025, 10, 25), date(2025, 10, 31)),
        ("2025_nov27_followup", "11月27日后续视频后0–6天", date(2025, 11, 27), date(2025, 12, 3)),
        ("2025_september", "2025年9月", date(2025, 9, 1), date(2025, 9, 30)),
    )
    periods: dict[str, Any] = {}
    for key, label, start, end in specs:
        selected = [row for row in valid_rows if start <= row["registration_date"] <= end]
        china = [row for row in selected if row.get("reg_place") == "China"]
        periods[key] = {
            "label": label,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "allUsers": len(selected),
            "chinaUsers": len(china),
            "chinaShare": rate(len(china), len(selected)),
            "chinaBehavior": _video_behavior_summary(china, as_of),
            "chinaFirstFindSpread": first_find_spread(china),
            "chinaConversionFunnel": _video_conversion_funnel(china, as_of),
            "chinaFirstDnfRecovery": _first_dnf_recovery_summary(china, as_of),
        }

    adjacent_2025 = [
        row for row in valid_rows
        if date(2025, 5, 1) <= row["registration_date"] <= date(2025, 11, 30)
        and row["registration_date"].month != 8
    ]
    adjacent_2025_china = [row for row in adjacent_2025 if row.get("reg_place") == "China"]
    periods["2025_adjacent"] = {
        "label": "2025年5–7月及9–11月",
        "start": "2025-05-01",
        "end": "2025-11-30（不含8月）",
        "allUsers": len(adjacent_2025),
        "chinaUsers": len(adjacent_2025_china),
        "chinaShare": rate(len(adjacent_2025_china), len(adjacent_2025)),
        "chinaBehavior": _video_behavior_summary(adjacent_2025_china, as_of),
        "chinaFirstFindSpread": first_find_spread(adjacent_2025_china),
        "chinaConversionFunnel": _video_conversion_funnel(adjacent_2025_china, as_of),
        "chinaFirstDnfRecovery": _first_dnf_recovery_summary(adjacent_2025_china, as_of),
    }

    def daily_peaks(start: date, end: date, limit: int) -> list[dict[str, Any]]:
        counts: dict[date, list[int]] = defaultdict(lambda: [0, 0])
        for row in valid_rows:
            registration = row.get("registration_date")
            if registration and start <= registration <= end:
                counts[registration][0] += 1
                counts[registration][1] += int(row.get("reg_place") == "China")
        ordered = sorted(counts.items(), key=lambda item: (-item[1][0], item[0]))[:limit]
        return [
            {"date": day.isoformat(), "allUsers": values[0], "chinaUsers": values[1],
             "chinaShare": rate(values[1], values[0])}
            for day, values in ordered
        ]

    pre = periods["2023_pre30"]
    post_2023_all = periods["2023_wave"]["allUsers"] + periods["2023_tail"]["allUsers"]
    post_2023_china = periods["2023_wave"]["chinaUsers"] + periods["2023_tail"]["chinaUsers"]
    august_2025 = periods["2025_aug"]
    adjacent_all = (periods["2025_july"]["allUsers"] + periods["2025_september"]["allUsers"]) / 2
    adjacent_china = (periods["2025_july"]["chinaUsers"] + periods["2025_september"]["chinaUsers"]) / 2
    historical_aug_all = (periods["2023_aug"]["allUsers"] + periods["2024_aug"]["allUsers"]) / 2
    historical_aug_china = (periods["2023_aug"]["chinaUsers"] + periods["2024_aug"]["chinaUsers"]) / 2
    videos = [dict(item) for item in VIDEO_PUBLICATIONS]
    video_responses = []
    prior_publish_date: date | None = None
    for video in videos:
        publish_date = date.fromisoformat(video["publishedAt"][:10])
        pre_start = publish_date - timedelta(days=7)
        pre_end = publish_date - timedelta(days=1)
        post_end = publish_date + timedelta(days=6)
        pre_rows = [row for row in valid_rows if pre_start <= row["registration_date"] <= pre_end]
        post_rows = [row for row in valid_rows if publish_date <= row["registration_date"] <= post_end]
        same_day_rows = [row for row in valid_rows if row["registration_date"] == publish_date]
        pre_china = sum(row.get("reg_place") == "China" for row in pre_rows)
        post_china = sum(row.get("reg_place") == "China" for row in post_rows)
        same_day_china = sum(row.get("reg_place") == "China" for row in same_day_rows)
        video_responses.append({
            "bvid": video["bvid"],
            "publishedDate": publish_date.isoformat(),
            "pre7Start": pre_start.isoformat(),
            "pre7End": pre_end.isoformat(),
            "post7End": post_end.isoformat(),
            "pre7AllUsers": len(pre_rows),
            "post7AllUsers": len(post_rows),
            "postVsPreRatio": round(len(post_rows) / len(pre_rows), 1) if pre_rows else None,
            "pre7ChinaUsers": pre_china,
            "post7ChinaUsers": post_china,
            "post7ChinaShare": rate(post_china, len(post_rows)),
            "sameDayAllUsers": len(same_day_rows),
            "sameDayChinaUsers": same_day_china,
            "overlapsPriorVideo": bool(
                prior_publish_date and (publish_date - prior_publish_date).days <= 6
            ),
        })
        prior_publish_date = publish_date

    return {
        "events": {
            "2023": {"knownPublishDate": "2023-06-14", "reportedViews": "95万"},
            "2025": {
                "knownPublishDates": [item["publishedAt"] for item in videos if item["publishedAt"].startswith("2025-")],
                "reportedViews": "426.1万、146.2万及后续低播放相关视频",
                "exactPublishDatesKnown": True,
            },
        },
        "videos": videos,
        "videoResponses": video_responses,
        "periods": periods,
        "dailyPeaks": {
            "2023": daily_peaks(date(2023, 6, 1), date(2023, 7, 13), 10),
            "2025": daily_peaks(date(2025, 8, 1), date(2025, 8, 31), 12),
        },
        "excess": {
            "2023VsPrior30All": post_2023_all - pre["allUsers"],
            "2023VsPrior30China": post_2023_china - pre["chinaUsers"],
            "2025VsAdjacentMonthsAll": round(august_2025["allUsers"] - adjacent_all),
            "2025VsAdjacentMonthsChina": round(august_2025["chinaUsers"] - adjacent_china),
            "2025VsHistoricalAugustAll": round(august_2025["allUsers"] - historical_aug_all),
            "2025VsHistoricalAugustChina": round(august_2025["chinaUsers"] - historical_aug_china),
        },
    }


def _metric_cells(summary: dict[str, Any]) -> str:
    fields = (
        "firstFindD7Rate", "firstFindD30Rate", "firstFindD90Rate",
        "finds10D90Rate", "activeD30Rate", "activeD90Rate",
    )
    return "".join(f"<td>{_fmt_percent(summary.get(field))}</td>" for field in fields)


def _relative_change(current: int, previous: int) -> float | None:
    return round((current - previous) * 100 / previous, 1) if previous else None


def _fmt_signed_percent(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+.1f}%"


def _point_comparison(current: float | None, reference: float | None, label: str) -> str:
    if current is None or reference is None:
        return f"与 {label} 无法比较"
    difference = round(current - reference, 1)
    if difference == 0:
        return f"与 {label} 持平"
    direction = "高" if difference > 0 else "低"
    return f"比 {label} {direction} {abs(difference):.1f} 个百分点"


def _growth_conclusions_html(growth: dict[str, Any] | None) -> str:
    if not growth:
        return ""
    annual = growth.get("annual") or {}
    earlier = annual.get("2024") or {}
    later = annual.get("2025") or {}
    volume_change = _relative_change(
        int(later.get("validUsers") or 0), int(earlier.get("validUsers") or 0)
    )
    growth_summary = (
        f"在当前可观察样本中，2025 年有效用户规模相对 2024 年为 "
        f"{_fmt_signed_percent(volume_change)}；D7 首找率"
        f"{_point_comparison(later.get('firstFindD7Rate'), earlier.get('firstFindD7Rate'), '2024 年')}，"
        f"M1 活跃率{_point_comparison(later.get('activeM1Rate'), earlier.get('activeM1Rate'), '2024 年')}。"
    )

    annual_rows = "".join(
        f"<tr><td>{html.escape(year)}</td><td>{_fmt_int(summary.get('validUsers'))}</td>"
        f"<td>{_fmt_percent(summary.get('firstFindD7Rate'))}</td>"
        f"<td>{_fmt_percent(summary.get('firstFindD90Rate'))}</td>"
        f"<td>{_fmt_percent(summary.get('finds10D90Rate'))}</td>"
        f"<td>{_fmt_percent(summary.get('activeM1Rate'))}</td>"
        f"<td>{_fmt_percent(summary.get('activeM6Rate'))}</td>"
        f"<td>{_fmt_percent(summary.get('activeM12Rate'))}</td></tr>"
        for year, summary in sorted(annual.items())
    )

    behavior = growth.get("behaviorOutcomes") or {}
    behavior_rows = []
    for scope_key, scope_label in (("overall", "全量"), ("china", "China")):
        scope = behavior.get(scope_key) or {}
        for groups in (scope.get("d7Groups") or [], scope.get("d30DepthGroups") or []):
            for item in groups:
                behavior_rows.append(
                    f"<tr><td>{scope_label}</td><td>{html.escape(str(item.get('label') or '—'))}</td>"
                    f"<td>{_fmt_int(item.get('groupUsers'))}</td>"
                    f"<td>{_fmt_int(item.get('eligibleD90'))}</td>"
                    f"<td>{_fmt_percent(item.get('activeD90Rate'))}</td>"
                    f"<td>{_fmt_percent(item.get('activeM6Rate'))}</td>"
                    f"<td>{_fmt_percent(item.get('activeM12Rate'))}</td></tr>"
                )

    overall = behavior.get("overall") or {}
    d7_by_key = {item.get("key"): item for item in overall.get("d7Groups") or []}
    depth_by_key = {item.get("key"): item for item in overall.get("d30DepthGroups") or []}
    d7_find = d7_by_key.get("d7_find") or {}
    no_d7_find = d7_by_key.get("no_d7_find") or {}
    deep = depth_by_key.get("finds_10_plus") or {}
    no_find = depth_by_key.get("no_find") or {}
    light = depth_by_key.get("finds_1_to_9") or {}
    d7_rate = d7_find.get("activeD90Rate")
    no_d7_rate = no_d7_find.get("activeD90Rate")
    d7_gap = (
        round(float(d7_rate) - float(no_d7_rate), 1)
        if d7_rate is not None and no_d7_rate is not None else None
    )
    if d7_gap is not None and abs(d7_gap) < 2:
        d7_interpretation = (
            "两组仅相差 " + f"{abs(d7_gap):.1f} 个百分点，"
            "当前结果不能把 D7 首找当作独立留存杠杆；它更适合作为新手漏斗的过程指标。"
        )
    elif d7_gap is not None and d7_gap > 0:
        d7_interpretation = (
            f"完成首找组高 {d7_gap:.1f} 个百分点，但仍需实验验证是否为引导造成。"
        )
    elif d7_gap is not None:
        d7_interpretation = (
            f"完成首找组低 {abs(d7_gap):.1f} 个百分点，不能据此把 D7 首找视为留存驱动。"
        )
    else:
        d7_interpretation = "当前样本不足以判断 D7 首找与后续活跃的关系。"

    deep_m12 = deep.get("activeM12Rate")
    comparison_m12 = max(
        [float(value) for value in (no_find.get("activeM12Rate"), light.get("activeM12Rate"))
         if value is not None],
        default=None,
    )
    if deep_m12 is not None and comparison_m12 is not None:
        depth_interpretation = (
            f"10+ 找组比其余分组中的较高值仍高 "
            f"{float(deep_m12) - comparison_m12:.1f} 个百分点，是当前最强的行为关联信号。"
        )
    else:
        depth_interpretation = "D30 找寻深度仍需更多成熟样本验证。"
    evidence = (
        f"D7 内完成首找用户的 D90 活跃率为 {_fmt_percent(d7_find.get('activeD90Rate'))}，"
        f"未完成者为 {_fmt_percent(no_d7_find.get('activeD90Rate'))}；"
        f"D30 内达到 10+ 找用户的 M12 活跃率为 {_fmt_percent(deep.get('activeM12Rate'))}，"
        f"D30 内 0 找用户为 {_fmt_percent(no_find.get('activeM12Rate'))}。"
        f"{d7_interpretation}{depth_interpretation}"
    )

    return f"""
      <section class="insight-block"><h3>结论与行动建议</h3>
        <h4>增长由什么构成</h4>
        <div class="insight-callout">{growth_summary}</div>
        <p>当前数据支持把增长拆成三部分：被观察到的新 cohort 规模、注册后 7–90 天的激活效率、以及 M1–M12 的持续或回流活动。报告没有渠道、广告、推荐来源、活动曝光或缓存供给数据，因此不能确认究竟是哪一种外部因素造成新增增长。</p>
        <div class="table-wrap"><table><thead><tr><th>年份</th><th>有效用户</th><th>D7 首找</th><th>D90 首找</th><th>D90 达10找</th><th>M1</th><th>M6</th><th>M12</th></tr></thead><tbody>{annual_rows}</tbody></table></div>
        <p class="caption">首找与达 10 找只累计 Found it；M1、M6、M12 分别观察注册后第 30–59、180–209、360–389 天的 Found it、DNF 或 Attended。每列使用对应窗口成熟用户作分母。</p>
        <h4>早期行为与后续活跃</h4>
        <p>{evidence}这些差异是筛选后的相关关系，既包含用户自身兴趣差异，也可能包含引导效果，不能直接证明因果。</p>
        <div class="table-wrap"><table><thead><tr><th>范围</th><th>早期行为</th><th>分组用户</th><th>D90 成熟用户</th><th>D90 活跃</th><th>M6 活跃</th><th>M12 活跃</th></tr></thead><tbody>{''.join(behavior_rows)}</tbody></table></div>
        <p class="caption">“D90 成熟用户”是分组中已完整经过注册后 120 天的人数；D90、M6、M12 活跃率的分母分别是该早期行为组中对应窗口的成熟用户，而不是全部 cohort 用户。</p>
        <h4>新用户引导优先级</h4>
        <ol>
          <li><strong>优先验证 D30 深度：</strong>围绕 3 找、5 找和 10 找设计阶梯任务；对已达到 1–9 找的用户推荐下一处低摩擦缓存。10+ 找是当前与 M6/M12 活跃差异最大的早期行为，但是否具有因果作用仍需实验。</li>
          <li><strong>把 D7 首找作为漏斗诊断，不作为唯一成功指标：</strong>继续降低第一次找寻的难度，但同时观察用户能否在 30 天内形成多次找寻；当前数据没有显示 D7 首找带来明显的独立留存提升。</li>
          <li><strong>拆分无首找、DNF 和活动参与：</strong>0 找用户的后续活跃可能来自晚激活、DNF 或 Attended。应根据首次失败原因推荐维护状态更好、距离更近的缓存，并单独衡量各路径。</li>
          <li><strong>在 M2/M3 前主动召回：</strong>结合附近新缓存、周末路线和活动提醒触达；M6/M12 使用季节性活动或纪念节点承接可能的回流。</li>
          <li><strong>补齐增长归因数据：</strong>记录注册渠道、活动或邀请标识、首次推荐缓存及曝光点击，才能进一步判断哪些因素真正带来新增和长期留存。</li>
        </ol>
        <p class="caption">行动建议的优先级来自当前行为关联强弱；上线时应采用分组实验，并同时观察首找率、DNF、30 天找寻深度和 M3/M6 留存，避免只优化短期指标。</p>
      </section>
    """


def _early_pathways_html(pathways: dict[str, Any] | None) -> str:
    if not pathways:
        return ""
    rows = []
    for scope_key, scope_label in (("overall", "全量"), ("china", "China")):
        for item in pathways.get(scope_key) or []:
            rows.append(
                f"<tr><td>{scope_label}</td><td>{html.escape(str(item.get('label') or '—'))}</td>"
                f"<td>{_fmt_int(item.get('groupUsers'))}</td>"
                f"<td>{_fmt_percent(item.get('groupShare'))}</td>"
                f"<td>{_fmt_int(item.get('eligibleM3'))}</td>"
                f"<td>{_fmt_percent(item.get('activeM3Rate'))}</td>"
                f"<td>{_fmt_percent(item.get('activeM6Rate'))}</td>"
                f"<td>{_fmt_percent(item.get('activeM12Rate'))}</td></tr>"
            )

    stable = [
        item for item in pathways.get("overall") or []
        if int(item.get("eligibleM12") or 0) >= 50
        and item.get("activeM12Rate") is not None
    ]
    if stable:
        best = max(stable, key=lambda item: float(item["activeM12Rate"]))
        narrative = (
            f"在 M12 成熟样本不少于 50 人的全量路径中，"
            f"“{html.escape(str(best.get('label') or '—'))}”的 M12 活跃率最高，"
            f"为 {_fmt_percent(best.get('activeM12Rate'))}。"
        )
    else:
        narrative = "当前没有足够的 M12 成熟样本用于稳定比较。"

    overall_by_key = {
        item.get("key"): item for item in pathways.get("overall") or []
    }
    dnf_only = overall_by_key.get("dnf_only") or {}
    found_only = overall_by_key.get("found_only") or {}
    found_dnf = overall_by_key.get("found_dnf") or {}
    dnf_read = ""
    if all(
        item.get("activeM12Rate") is not None
        for item in (dnf_only, found_only, found_dnf)
    ):
        dnf_read = (
            f"仅 DNF、仅找到、找到 + DNF 三条路径的 M12 活跃率分别为 "
            f"{_fmt_percent(dnf_only.get('activeM12Rate'))}、"
            f"{_fmt_percent(found_only.get('activeM12Rate'))} 和 "
            f"{_fmt_percent(found_dnf.get('activeM12Rate'))}。"
            "DNF 本身不能被统一解释为流失信号：仅 DNF 路径应优先提供失败救援；"
            "已经找到且出现 DNF 的用户更可能代表较广的探索或更高的活动强度。"
        )
    attended_users = sum(
        int((overall_by_key.get(key) or {}).get("groupUsers") or 0)
        for key in ("attended_only", "dnf_attended", "found_attended", "found_dnf_attended")
    )
    attended_read = (
        f"前 30 天出现 Attended 的路径合计仅 {_fmt_int(attended_users)} 人，"
        "当前不足以稳定判断活动参与的长期影响。"
    )
    depth_rows = []
    depth_data = pathways.get("dnfWithinDepth") or {}
    for scope_key, scope_label in (
        ("overall", "全量 2017+"),
        ("china", "China 2017+"),
        ("recentOverall", "全量 2024–2025"),
        ("recentChina", "China 2024–2025"),
    ):
        for item in depth_data.get(scope_key) or []:
            depth_rows.append(
                f"<tr><td>{scope_label}</td><td>{html.escape(str(item.get('label') or '—'))}</td>"
                f"<td>{_fmt_int(item.get('groupUsers'))}</td>"
                f"<td>{_fmt_percent(item.get('withinDepthShare'))}</td>"
                f"<td>{_fmt_int(item.get('eligibleM12'))}</td>"
                f"<td>{_fmt_percent(item.get('activeM3Rate'))}</td>"
                f"<td>{_fmt_percent(item.get('activeM6Rate'))}</td>"
                f"<td>{_fmt_percent(item.get('activeM12Rate'))}</td></tr>"
            )
    depth_by_key = {
        item.get("key"): item for item in depth_data.get("overall") or []
    }
    controlled_reads = []
    for prefix, label in (("finds_1_to_9", "1–9 找"), ("finds_10_plus", "10+ 找")):
        without = depth_by_key.get(f"{prefix}_no_dnf") or {}
        with_dnf = depth_by_key.get(f"{prefix}_with_dnf") or {}
        without_rate = without.get("activeM12Rate")
        with_rate = with_dnf.get("activeM12Rate")
        if without_rate is not None and with_rate is not None:
            gap = round(float(with_rate) - float(without_rate), 1)
            direction = "高" if gap >= 0 else "低"
            controlled_reads.append(
                f"在 {label} 内，有 DNF 用户的 M12 活跃率为 {_fmt_percent(with_rate)}，"
                f"无 DNF 用户为 {_fmt_percent(without_rate)}，前者{direction} {abs(gap):.1f} 个百分点。"
            )
    controlled_text = "".join(controlled_reads)
    recent_by_key = {
        item.get("key"): item for item in depth_data.get("recentOverall") or []
    }
    recent_reads = []
    for prefix, label in (("finds_1_to_9", "1–9 找"), ("finds_10_plus", "10+ 找")):
        without = recent_by_key.get(f"{prefix}_no_dnf") or {}
        with_dnf = recent_by_key.get(f"{prefix}_with_dnf") or {}
        without_rate = without.get("activeM12Rate")
        with_rate = with_dnf.get("activeM12Rate")
        if without_rate is not None and with_rate is not None:
            gap = round(float(with_rate) - float(without_rate), 1)
            direction = "高" if gap >= 0 else "低"
            recent_reads.append(
                f"2024–2025 年 {label} 内，有 DNF 用户的 M12 活跃率"
                f"比无 DNF 用户{direction} {abs(gap):.1f} 个百分点。"
            )
    recent_text = "".join(recent_reads)
    depth_section = ""
    if depth_rows:
        depth_section = f"""
        <h4>控制 D30 找寻深度后的 DNF 对照</h4>
        <p>{controlled_text}{recent_text}该分层减少了找寻深度和历史时期差异，但仍未控制用户动机、可用时间和缓存供给，结果仍是相关关系。</p>
        <div class="table-wrap"><table><thead><tr><th>范围</th><th>找寻深度与 DNF</th><th>用户数</th><th>深度内占比</th><th>M12 成熟用户</th><th>M3 活跃</th><th>M6 活跃</th><th>M12 活跃</th></tr></thead><tbody>{''.join(depth_rows)}</tbody></table></div>
        <p class="caption">找寻深度按注册后第 0–29 天累计 Found it 数量划分；“深度内占比”以对应深度组为分母。M3/M6/M12 仍以注册日锚定，活跃率只使用各组对应窗口的成熟用户。</p>
        <h4>路径对应的运营动作</h4>
        <ul>
          <li><strong>仅 DNF：</strong>识别为“尝试但未成功”，优先提供提示、维护状态可靠的替代缓存和失败后的再次尝试入口。</li>
          <li><strong>已有找到并伴随 DNF：</strong>不要直接标记为流失风险；在相同找寻深度内，这类用户的后续活跃仍更高，更适合推荐相似路线或不同难度的下一处缓存。</li>
          <li><strong>1–9 找且无 DNF：</strong>重点验证是否属于低探索、一次性使用或记录不完整；通过连续任务和邻近路线推动形成重复行为。</li>
          <li><strong>Attended 路径：</strong>当前人数过少，暂不据此制定大规模活动策略，应先扩大样本或补充活动曝光数据。</li>
        </ul>
        """
    timing_rows = []
    timing_data = pathways.get("firstFindTiming") or {}
    for scope_key, scope_label in (("overall", "全量"), ("china", "China")):
        for item in timing_data.get(scope_key) or []:
            timing_rows.append(
                f"<tr><td>{scope_label}</td><td>{html.escape(str(item.get('label') or '—'))}</td>"
                f"<td>{_fmt_int(item.get('groupUsers'))}</td>"
                f"<td>{_fmt_percent(item.get('groupShare'))}</td>"
                f"<td>{_fmt_int(item.get('eligibleM12'))}</td>"
                f"<td>{_fmt_percent(item.get('activeM3Rate'))}</td>"
                f"<td>{_fmt_percent(item.get('activeM6Rate'))}</td>"
                f"<td>{_fmt_percent(item.get('activeM12Rate'))}</td></tr>"
            )
    timing_stable = [
        item for item in timing_data.get("overall") or []
        if int(item.get("eligibleM12") or 0) >= 50
        and item.get("activeM12Rate") is not None
    ]
    timing_narrative = ""
    timing_guidance = ""
    if timing_stable:
        best_timing = max(timing_stable, key=lambda item: float(item["activeM12Rate"]))
        timing_narrative = (
            f"全量成熟样本中，{html.escape(str(best_timing.get('label') or '—'))}组的 "
            f"M12 活跃率最高，为 {_fmt_percent(best_timing.get('activeM12Rate'))}。"
        )
        if best_timing.get("key") == "days_3_6":
            timing_guidance = (
                "不要把注册当日首找设为唯一目标：注册当日帮助用户收藏和规划，"
                "第 3–7 天重点引导完成首找；之后仍保留低频召回和晚激活入口。"
            )
        else:
            timing_guidance = (
                f"可优先围绕“{html.escape(str(best_timing.get('label') or '—'))}”设计触达实验，"
                "并与注册当日引导进行随机对照。"
            )
    timing_section = ""
    if timing_rows:
        timing_section = f"""
        <h4>首找时机与后续活跃</h4>
        <p>{timing_narrative}该比较用于确定引导触达时点；首找更快也可能源于用户本身意愿更强，不能解释为提前首找必然提高留存。</p>
        <div class="insight-callout">{timing_guidance}</div>
        <div class="table-wrap"><table><thead><tr><th>范围</th><th>首找时机</th><th>用户数</th><th>占比</th><th>M12 成熟用户</th><th>M3 活跃</th><th>M6 活跃</th><th>M12 活跃</th></tr></thead><tbody>{''.join(timing_rows)}</tbody></table></div>
        <p class="caption">“首找”只统计 Found it；各首找时机档互斥，“占比”以同一范围内全部有效用户为分母。“截至数据日仍无首找”表示未观察到 Found it，不代表没有 DNF、Attended 或其他行动。M3/M6/M12 活跃分别以该时机组中已完整经过第 90–119、180–209、360–389 天窗口的用户为分母；第 90 天后首找可能发生在这些观察窗口之后，因此只能用于描述晚激活，不能作为早期预测变量。</p>
        """

    return f"""
      <section class="insight-block"><h3>注册后 30 天行为路径</h3>
        <div class="insight-callout">{narrative}</div>
        <p>按注册后前 30 天是否出现 Found it、Didn't find it 与 Attended，将用户划分为八个互斥路径，再观察后续独立窗口中的活动。</p>
        <p>{dnf_read}{attended_read}</p>
        <div class="table-wrap"><table><thead><tr><th>范围</th><th>D30 路径</th><th>用户数</th><th>路径占比</th><th>M3 成熟用户</th><th>M3 活跃</th><th>M6 活跃</th><th>M12 活跃</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
        {depth_section}
        {timing_section}
        <p class="caption">路径只区分日志类型是否出现，不区分次数和发生顺序。China 是分析用地域归属，包含首找位于 China 的用户，以及网站 Find 为 0 且数据库内 DNF 全部位于 China 的用户；小样本路径应结合人数判断，不能单凭百分比排序。</p>
      </section>
    """


def _video_conversion_and_dnf_html(periods: dict[str, Any]) -> str:
    comparison_keys = (
        ("2023_wave", "2023发布后0–6天"),
        ("2025_wave1", "2025高播放合并窗口"),
        ("2025_adjacent", "2025相邻月份"),
    )

    funnels = {
        key: (periods.get(key) or {}).get("chinaConversionFunnel") or {}
        for key, _ in comparison_keys
    }
    stage_keys = ("any_action", "found_1_plus", "found_3_plus", "found_5_plus", "found_10_plus")
    stage_lookup = {
        key: {item.get("key"): item for item in (funnel.get("stages") or [])}
        for key, funnel in funnels.items()
    }
    funnel_rows = []
    for stage_key in stage_keys:
        label = next(
            (
                str(stage_lookup[key][stage_key].get("label"))
                for key, _ in comparison_keys if stage_key in stage_lookup[key]
            ),
            stage_key,
        )
        cells = []
        for key, _ in comparison_keys:
            item = stage_lookup[key].get(stage_key) or {}
            cells.append(
                f"<td>{_fmt_int(item.get('users'))}</td>"
                f"<td>{_fmt_percent(item.get('cohortRate'))}</td>"
                f"<td>{_fmt_percent(item.get('priorStepRate'))}</td>"
            )
        funnel_rows.append(f"<tr><td>{html.escape(label)}</td>{''.join(cells)}</tr>")

    depth_rows = []
    for key, period_label in comparison_keys:
        for item in funnels[key].get("findDepthRetention") or []:
            depth_rows.append(
                f"<tr><td>{period_label}</td><td>{html.escape(str(item.get('label') or '—'))}</td>"
                f"<td>{_fmt_int(item.get('users'))}</td>"
                f"<td>{_fmt_percent(item.get('cohortShare'))}</td>"
                f"<td>{_fmt_percent(item.get('activeM3Rate'))} (n={_fmt_int(item.get('eligibleM3'))})</td>"
                f"<td>{_fmt_percent(item.get('activeM6Rate'))} (n={_fmt_int(item.get('eligibleM6'))})</td>"
                f"<td>{_fmt_percent(item.get('activeM12Rate'))} (n={_fmt_int(item.get('eligibleM12'))})</td></tr>"
            )

    recoveries = {
        key: (periods.get(key) or {}).get("chinaFirstDnfRecovery") or {}
        for key, _ in comparison_keys
    }
    recovery_overview_rows = []
    for key, period_label in comparison_keys:
        item = recoveries[key]
        recovery_overview_rows.append(
            f"<tr><td>{period_label}</td><td>{_fmt_int(item.get('dnfUsers'))}</td>"
            f"<td>{_fmt_int(item.get('newcomerFailureUsers'))}</td>"
            f"<td>{_fmt_percent(item.get('newcomerFailureShareOfDnf'))}</td>"
            f"<td>{_fmt_int(item.get('foundBeforeDnfUsers'))}</td>"
            f"<td>{_fmt_int(item.get('recoveredWithin30'))} / {_fmt_int(item.get('eligibleRecovery30'))}</td>"
            f"<td>{_fmt_percent(item.get('recoveredWithin30Rate'))}</td>"
            f"<td>{_fmt_percent(item.get('recoveredWithin90Rate'))}</td>"
            f"<td>{_fmt_percent(item.get('sameCacheRecoveryShare'))}</td></tr>"
        )

    recovery_bucket_rows = []
    for key, period_label in comparison_keys:
        for item in recoveries[key].get("recoveryBuckets") or []:
            recovery_bucket_rows.append(
                f"<tr><td>{period_label}</td><td>{html.escape(str(item.get('label') or '—'))}</td>"
                f"<td>{_fmt_int(item.get('users'))}</td><td>{_fmt_percent(item.get('share'))}</td>"
                f"<td>{_fmt_percent(item.get('activeM3Rate'))}</td>"
                f"<td>{_fmt_percent(item.get('activeM6Rate'))}</td>"
                f"<td>{_fmt_percent(item.get('activeM12Rate'))} (n={_fmt_int(item.get('eligibleM12'))})</td></tr>"
            )

    event_funnel = stage_lookup.get("2025_wave1") or {}
    control_funnel = stage_lookup.get("2025_adjacent") or {}
    event_action = event_funnel.get("any_action") or {}
    event_first = event_funnel.get("found_1_plus") or {}
    event_ten = event_funnel.get("found_10_plus") or {}
    control_first = control_funnel.get("found_1_plus") or {}
    control_ten = control_funnel.get("found_10_plus") or {}
    event_recovery = recoveries.get("2025_wave1") or {}
    control_recovery = recoveries.get("2025_adjacent") or {}

    return f"""
        <h4>China视频用户30天转化漏斗</h4>
        <div class="insight-callout"><strong>主要流失点：</strong>2025高播放合并窗口中，完成D30观察的China用户有 {_fmt_int(funnels.get('2025_wave1', {}).get('eligibleD30'))} 人；其中 {_fmt_percent(event_action.get('cohortRate'))} 在30天内留下Found、DNF或Attended，但只有 {_fmt_percent(event_first.get('cohortRate'))} 完成至少1找、{_fmt_percent(event_ten.get('cohortRate'))} 达到10找。相邻月份的至少1找和10找比例分别为 {_fmt_percent(control_first.get('cohortRate'))}、{_fmt_percent(control_ten.get('cohortRate'))}。这把问题定位为“尝试后未成功或首找后没有形成连续找寻”，而不只是注册后完全没有行动。</div>
        <div class="table-wrap"><table><thead><tr><th rowspan="2">D30阶段</th><th colspan="3">2023发布后0–6天</th><th colspan="3">2025高播放合并窗口</th><th colspan="3">2025相邻月份</th></tr><tr><th>人数</th><th>占成熟用户</th><th>上一步转化</th><th>人数</th><th>占成熟用户</th><th>上一步转化</th><th>人数</th><th>占成熟用户</th><th>上一步转化</th></tr></thead><tbody>{''.join(funnel_rows)}</tbody></table></div>
        <p class="caption">漏斗只使用已经完整经过注册后 30 天的用户，所有阶段均累计第 0–29 天。“占成熟用户”以该时期 D30 成熟人数为分母；“上一步转化”以上一阶段人数为分母。Found、DNF 或 Attended 构成“有行动”；后续 1 找、3 找、5 找、10 找均只计算 Found it，因此各阶段严格递减。</p>
        <h4>D30找寻深度与后续活跃</h4>
        <div class="table-wrap"><table><thead><tr><th>时期</th><th>D30找寻深度</th><th>用户数</th><th>占比</th><th>M3活跃</th><th>M6活跃</th><th>M12活跃</th></tr></thead><tbody>{''.join(depth_rows)}</tbody></table></div>
        <p class="caption">D30 深度按注册后第 0–29 天累计 Found it 划分为 0、1–2、3–4、5–9、10+。M3、M6、M12 仍以注册日锚定，是独立观察窗口，不属于上述严格漏斗；括号内 n 为该深度组中已经完整经过相应窗口的成熟用户数。</p>
        <h4>首次DNF后的恢复路径</h4>
        <div class="insight-callout"><strong>恢复判断：</strong>2025视频窗口中，注册后30天内出现DNF且DNF早于或同日于首次Found的China用户有 {_fmt_int(event_recovery.get('newcomerFailureUsers'))} 人；其中30天内恢复成功 {_fmt_percent(event_recovery.get('recoveredWithin30Rate'))}，相邻月份为 {_fmt_percent(control_recovery.get('recoveredWithin30Rate'))}。已恢复且缓存代码可比较的用户中，{_fmt_percent(event_recovery.get('sameCacheRecoveryShare'))} 在首次DNF的同一缓存上留下Found，其余通过其他缓存恢复。</div>
        <div class="table-wrap"><table><thead><tr><th>时期</th><th>D30有DNF</th><th>DNF早于/同日首找</th><th>占DNF用户</th><th>先Found后DNF</th><th>30天恢复/成熟</th><th>30天恢复率</th><th>90天恢复率</th><th>同缓存恢复占比</th></tr></thead><tbody>{''.join(recovery_overview_rows)}</tbody></table></div>
        <div class="table-wrap"><table><thead><tr><th>时期</th><th>恢复时间</th><th>用户数</th><th>占新手失败用户</th><th>M3活跃</th><th>M6活跃</th><th>M12活跃</th></tr></thead><tbody>{''.join(recovery_bucket_rows)}</tbody></table></div>
        <p class="caption">“新手失败”限定为注册后 30 天内首次 DNF 早于首次 Found，或与首次 Found 同日；由于日志只有日期，不能推断当天先后。30/90 天恢复率分别以已完整经过首 DNF 后 30/90 天的新手失败用户为分母；恢复指首 DNF 当日或之后观察到 Found it。“同缓存恢复占比”只以恢复者中前后缓存代码可比较的用户为分母。</p>
    """


def _video_influx_html(video: dict[str, Any] | None) -> str:
    if not video:
        return ""
    periods = video.get("periods") or {}
    peaks = video.get("dailyPeaks") or {}
    excess = video.get("excess") or {}
    videos = video.get("videos") or []
    responses = {item.get("bvid"): item for item in (video.get("videoResponses") or [])}

    def format_views(value: Any) -> str:
        try:
            count = int(value)
        except (TypeError, ValueError):
            return "—"
        return f"{count / 10000:.1f}万" if count >= 10000 else f"{count:,}"

    video_rows = []
    for item in videos:
        response = responses.get(item.get("bvid")) or {}
        overlap = "传播窗口重叠，不能独立归因" if response.get("overlapsPriorVideo") else "独立窗口对照"
        ratio = response.get("postVsPreRatio")
        ratio_text = f"{ratio:.1f}倍" if isinstance(ratio, (int, float)) else "—"
        url = html.escape(str(item.get("url") or "#"), quote=True)
        title = html.escape(str(item.get("title") or item.get("bvid") or "—"))
        video_rows.append(
            f'<tr><td><a href="{url}">{title}</a><br><span class="caption">{html.escape(str(item.get("bvid") or ""))}</span></td>'
            f"<td>{html.escape(str(item.get('publishedAt') or '—'))}</td>"
            f"<td>{format_views(item.get('views'))}</td>"
            f"<td>{_fmt_int(response.get('sameDayAllUsers'))} / {_fmt_int(response.get('sameDayChinaUsers'))}</td>"
            f"<td>{_fmt_int(response.get('pre7AllUsers'))} / {_fmt_int(response.get('pre7ChinaUsers'))}</td>"
            f"<td>{_fmt_int(response.get('post7AllUsers'))} / {_fmt_int(response.get('post7ChinaUsers'))}</td>"
            f"<td>{ratio_text}</td><td>{overlap}</td></tr>"
        )

    volume_keys = (
        "2022_same30", "2023_pre30", "2023_wave", "2023_tail", "2024_same30",
        "2023_aug", "2024_aug", "2025_july", "2025_aug", "2025_september",
    )
    volume_rows = "".join(
        f"<tr><td>{html.escape(str(item.get('label') or key))}</td>"
        f"<td>{_fmt_int(item.get('allUsers'))}</td>"
        f"<td>{_fmt_int(item.get('chinaUsers'))}</td>"
        f"<td>{_fmt_percent(item.get('chinaShare'))}</td></tr>"
        for key in volume_keys if (item := periods.get(key))
    )

    behavior_keys = (
        "2023_pre30", "2023_wave", "2023_tail", "2025_pre8",
        "2025_wave1", "2025_main_before_followup", "2025_followup_overlap",
        "2025_middle", "2025_wave2", "2025_sep2_followup", "2025_sep26_followup",
        "2025_oct25_followup", "2025_nov27_followup", "2025_adjacent",
    )
    behavior_rows = []
    for key in behavior_keys:
        item = periods.get(key)
        if not item:
            continue
        behavior = item.get("chinaBehavior") or {}
        m12 = (
            f"{_fmt_percent(behavior.get('activeM12Rate'))} "
            f"(n={_fmt_int(behavior.get('eligibleM12'))})"
        )
        behavior_rows.append(
            f"<tr><td>{html.escape(str(item.get('label') or key))}</td>"
            f"<td>{_fmt_int(item.get('chinaUsers'))}</td>"
            f"<td>{_fmt_percent(behavior.get('firstFindD2Rate'))}</td>"
            f"<td>{_fmt_percent(behavior.get('firstFindD7Rate'))}</td>"
            f"<td>{_fmt_percent(behavior.get('firstFindD30Rate'))}</td>"
            f"<td>{_fmt_percent(behavior.get('finds10D30Rate'))}</td>"
            f"<td>{_fmt_percent(behavior.get('dnfD30Rate'))}</td>"
            f"<td>{_fmt_percent(behavior.get('foundDnfRate'))}</td>"
            f"<td>{_fmt_percent(behavior.get('activeM3Rate'))}</td>"
            f"<td>{_fmt_percent(behavior.get('activeM6Rate'))}</td>"
            f"<td>{m12}</td></tr>"
        )

    peak_rows = []
    for event_year, label in (("2023", "2023事件附近"), ("2025", "2025年8月")):
        for item in (peaks.get(event_year) or [])[:8]:
            peak_rows.append(
                f"<tr><td>{label}</td><td>{html.escape(str(item.get('date') or '—'))}</td>"
                f"<td>{_fmt_int(item.get('allUsers'))}</td>"
                f"<td>{_fmt_int(item.get('chinaUsers'))}</td>"
                f"<td>{_fmt_percent(item.get('chinaShare'))}</td></tr>"
            )

    wave_2023 = periods.get("2023_wave") or {}
    tail_2023 = periods.get("2023_tail") or {}
    pre_2023 = periods.get("2023_pre30") or {}
    august_2025 = periods.get("2025_aug") or {}
    wave1 = (periods.get("2025_wave1") or {}).get("chinaBehavior") or {}
    wave2 = (periods.get("2025_wave2") or {}).get("chinaBehavior") or {}
    adjacent_behavior = (periods.get("2025_adjacent") or {}).get("chinaBehavior") or {}
    spread_2023 = (periods.get("2023_wave") or {}).get("chinaFirstFindSpread") or {}
    spread_wave1 = (periods.get("2025_wave1") or {}).get("chinaFirstFindSpread") or {}
    spread_wave2 = (periods.get("2025_wave2") or {}).get("chinaFirstFindSpread") or {}
    post_2023_all = int(wave_2023.get("allUsers") or 0) + int(tail_2023.get("allUsers") or 0)
    post_2023_china = int(wave_2023.get("chinaUsers") or 0) + int(tail_2023.get("chinaUsers") or 0)
    pre_all = int(pre_2023.get("allUsers") or 0)
    pre_china = int(pre_2023.get("chinaUsers") or 0)
    ratio_2023_all = post_2023_all / pre_all if pre_all else None
    ratio_2023_china = post_2023_china / pre_china if pre_china else None
    ratio_text = (
        f"，分别是发布前同长度窗口的 {ratio_2023_all:.1f} 倍和 {ratio_2023_china:.1f} 倍"
        if ratio_2023_all is not None and ratio_2023_china is not None else ""
    )

    return f"""
      <section class="insight-block"><h3>B站视频与用户涌入事件对照</h3>
        <div class="insight-callout"><strong>判断：</strong>两次涌入与已知视频时间在数据上高度吻合。2023-06-14 视频发布后的 30 天观察到 {_fmt_int(post_2023_all)} 名用户，其中 China {_fmt_int(post_2023_china)} 名{ratio_text}；注册峰值出现在 2023-06-16。2025 年 8 月观察到 {_fmt_int(august_2025.get('allUsers'))} 名用户，其中 China {_fmt_int(august_2025.get('chinaUsers'))} 名；相对相邻月份均值分别多约 {_fmt_int(excess.get('2025VsAdjacentMonthsAll'))} 名和 {_fmt_int(excess.get('2025VsAdjacentMonthsChina'))} 名。</div>
        <p>2025 年两条高播放视频分别发布于 8 月 6 日和 8 月 9 日，实际处于同一个传播波峰，而不是两轮相互独立的流量。后续相关投稿延续至 11 月，但播放量主要在 1–16 万之间；下表用统一的发布前后 7 天作描述性对照。8 月 9 日的视频与主视频传播窗口重叠，不能把其后新增单独归因给第二条视频。</p>
        <h4>已核实视频时间线与注册响应</h4>
        <div class="table-wrap"><table><thead><tr><th>视频</th><th>发布时间</th><th>页面播放量</th><th>发布日 全量/China</th><th>前7天 全量/China</th><th>后7天 全量/China</th><th>后/前</th><th>归因说明</th></tr></thead><tbody>{''.join(video_rows)}</tbody></table></div>
        <p class="caption">按注册日期自然日统计：前 7 天为发布日前第 7–1 天，不含发布日；后 7 天为发布日到第 6 天，包含发布日；“后/前”是两段全量用户人数之比。播放量为公开页面在 2026-09-10 至 2026-09-11 的快照，会继续变化。注册响应不是 B 站点击到注册的直接转化。</p>
        <h4>注册规模与 China 构成</h4>
        <div class="table-wrap"><table><thead><tr><th>时期</th><th>全量用户</th><th>China 用户</th><th>China 占比</th></tr></thead><tbody>{volume_rows}</tbody></table></div>
        <p class="caption">全量用户是注册日期落在该时期、且通过主分析有效性筛选的用户；China 用户是其中分析归属为 China 的人数，China 占比以该时期全量用户为分母。China 是日志推导的分析归属，不代表注册地或居住地。</p>
        <h4>日级峰值</h4>
        <div class="table-wrap"><table><thead><tr><th>事件</th><th>注册日期</th><th>全量用户</th><th>China 用户</th><th>China 占比</th></tr></thead><tbody>{''.join(peak_rows)}</tbody></table></div>
        <p class="caption">日级峰值按用户注册日期计数；每行是一日的注册 cohort，不是视频播放日的点击量。“China 占比”以该日全量有效用户为分母。</p>
        <h4>事件期间 China 用户行为</h4>
        <div class="table-wrap"><table><thead><tr><th>注册时期</th><th>China 用户</th><th>D2 首找</th><th>D7 首找</th><th>D30 首找</th><th>D30 达10找</th><th>D30 有DNF</th><th>找到+DNF</th><th>M3 活跃</th><th>M6 活跃</th><th>M12 活跃</th></tr></thead><tbody>{''.join(behavior_rows)}</tbody></table></div>
        <p class="caption">D2/D7/D30 均从每位用户注册日累计，只统计 Found it；“D30 有 DNF”表示第 0–29 天至少一条 DNF，“找到+DNF”表示同一第 0–29 天窗口内既有至少一条 Found it、又有至少一条 DNF。两列不是互斥分类：“找到+DNF”用户也包含在“D30 有 DNF”中。M3/M6/M12 分别观察注册后第 90–119、180–209、360–389 天是否至少有一条 Found it、DNF 或 Attended，括号内 n 为对应成熟分母。</p>
        <div class="insight-callout"><strong>行为特征：</strong>2025 两条高播放视频合并窗口内，China 用户的 D7、D30 首找率分别为 {_fmt_percent(wave1.get('firstFindD7Rate'))}、{_fmt_percent(wave1.get('firstFindD30Rate'))}，低于相邻六个月 China 用户的 {_fmt_percent(adjacent_behavior.get('firstFindD7Rate'))}、{_fmt_percent(adjacent_behavior.get('firstFindD30Rate'))}；D30 达10找仅 {_fmt_percent(wave1.get('finds10D30Rate'))}，同时 D30 有 DNF 达 {_fmt_percent(wave1.get('dnfD30Rate'))}，高于相邻月份的 {_fmt_percent(adjacent_behavior.get('dnfD30Rate'))}。更新 DNF-only 地域归属后，结论更清楚：视频显著扩大了入口，但吸引了大量尚未完成首次成功、深度转化较弱的新手。</div>
        {_video_conversion_and_dnf_html(periods)}
        <p>首次找到并未集中在视频中的少数目标：2023立即响应组覆盖 {_fmt_int(spread_2023.get('uniqueCaches'))} 个不同缓存和 {_fmt_int(spread_2023.get('uniqueCities'))} 个城市，单一缓存最多仅 {_fmt_int(spread_2023.get('maxSameCacheUsers'))} 人；2025两条高播放视频合并窗口覆盖 {_fmt_int(spread_wave1.get('uniqueCaches'))} 个缓存和 {_fmt_int(spread_wave1.get('uniqueCities'))} 个城市，8月26日低播放相关视频后的窗口覆盖 {_fmt_int(spread_wave2.get('uniqueCaches'))} 个缓存和 {_fmt_int(spread_wave2.get('uniqueCities'))} 个城市。这更支持视频激发了全国范围的玩法尝试，而不只是观众集中寻找视频展示的宝藏。</p>
        <p>留存判断以 reg_place=China 为主，降低境外用户仅在旅行期间留下大中华区日志造成的误判。2023 立即响应组的 China 样本为 {_fmt_int(wave_2023.get('chinaUsers'))} 人，其 M12 活跃率为 {_fmt_percent((wave_2023.get('chinaBehavior') or {}).get('activeM12Rate'))}；2025 第一轮为 {_fmt_percent(wave1.get('activeM12Rate'))}。这表明视频可以显著扩大入口，但长期价值取决于能否把首找用户继续推进到多次、跨日找寻。</p>
        <p class="caption">这是一项自然事件对照：时间响应、量级异常和 China 构成同时支持“视频是重要来源”的解释，但不能单凭时间重合证明因果，也无法排除同期传播、其他平台转发或数据覆盖变化。较低播放量的后续视频没有形成与8月6日同量级的独立注册峰值。</p>
      </section>
    """


def _yearly_china_trend_chart(
    trend_summaries: list[tuple[int, str, dict[str, Any]]],
    as_of: date,
) -> str:
    """Render annual China activation and activity rates with YTD visually separated."""
    if not trend_summaries:
        return ""

    series = (
        ("firstFindD7Rate", "D7 首找", "#2563eb"),
        ("firstFindD30Rate", "D30 首找", "#0f766e"),
        ("firstFindD90Rate", "D90 首找", "#9333ea"),
        ("finds10D90Rate", "D90 达10找", "#dc2626"),
        ("activeD30Rate", "D30窗口活跃", "#d97706"),
        ("activeD90Rate", "D90窗口活跃", "#0891b2"),
    )
    width, height = 920, 390
    left, right, top, bottom = 62, 18, 76, 52
    plot_width, plot_height = width - left - right, height - top - bottom
    values = [
        float(summary[field])
        for _year, _label, summary in trend_summaries
        for field, _series_label, _color in series
        if summary.get(field) is not None
    ]
    y_max = _percent_axis_ceiling(max(values, default=0))
    tick_values = [round(y_max * index / 4, 1) for index in range(5)]
    count = len(trend_summaries)

    def x_at(index: int) -> float:
        return left + (index / max(1, count - 1)) * plot_width

    def y_at(value: float) -> float:
        return top + plot_height - value / y_max * plot_height

    grid = []
    for value in tick_values:
        y = y_at(value)
        grid.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" class="grid"/>')
        grid.append(f'<text x="{left-10}" y="{y+4:.1f}" text-anchor="end" class="axis">{value:.1f}%</text>')

    ytd_band = ""
    if trend_summaries[-1][0] == as_of.year:
        last_x = x_at(count - 1)
        previous_x = x_at(max(0, count - 2))
        band_left = (last_x + previous_x) / 2 if count > 1 else left
        ytd_band = (
            f'<rect x="{band_left:.1f}" y="{top}" width="{width-right-band_left:.1f}" '
            f'height="{plot_height}" class="ytd-band"/>'
        )

    labels = []
    for index, (_year, label, _summary) in enumerate(trend_summaries):
        anchor = "end" if index == count - 1 else ("start" if index == 0 else "middle")
        labels.append(
            f'<text x="{x_at(index):.1f}" y="{height-18}" text-anchor="{anchor}" '
            f'class="axis">{html.escape(label)}</text>'
        )

    paths = []
    legend = []
    for series_index, (field, label, color) in enumerate(series):
        points = []
        for row_index, (year, year_label, summary) in enumerate(trend_summaries):
            value = summary.get(field)
            if value is None:
                continue
            points.append((row_index, x_at(row_index), y_at(float(value)), float(value), year, year_label))

        historical_points = [point for point in points if point[4] < as_of.year]
        if historical_points:
            path = " ".join(
                ("M" if index == 0 else "L") + f" {x:.1f} {y:.1f}"
                for index, (_row, x, y, _value, _year, _year_label) in enumerate(historical_points)
            )
            paths.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.4"/>')

        ytd_points = [point for point in points if point[4] == as_of.year]
        if historical_points and ytd_points:
            _row1, x1, y1, _value1, _year1, _label1 = historical_points[-1]
            _row2, x2, y2, _value2, _year2, _label2 = ytd_points[0]
            paths.append(
                f'<path d="M {x1:.1f} {y1:.1f} L {x2:.1f} {y2:.1f}" fill="none" '
                f'stroke="{color}" stroke-width="2.4" stroke-dasharray="6 5"/>'
            )

        for _row, x, y, value, _year, year_label in points:
            tooltip = html.escape(f"{year_label}｜{label}: {value:.1f}%")
            paths.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="{color}"><title>{tooltip}</title></circle>'
            )

        legend_x = 20 + (series_index % 3) * 295
        legend_y = 18 + (series_index // 3) * 25
        legend.append(
            f'<line x1="{legend_x}" y1="{legend_y}" x2="{legend_x+18}" y2="{legend_y}" '
            f'stroke="{color}" stroke-width="3"/>'
        )
        legend.append(
            f'<text x="{legend_x+25}" y="{legend_y+4}" class="legend">{html.escape(label)}</text>'
        )

    return f"""
        <div class="yearly-trend-chart-wrap">
          <h4>China 年度激活与活跃趋势</h4>
          <svg class="yearly-trend-chart" viewBox="0 0 {width} {height}" role="img" aria-label="China 用户按注册年份的激活、找寻深度与活跃率折线图">
            <rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" class="frame"/>
            {ytd_band}{''.join(grid)}{''.join(labels)}{''.join(paths)}{''.join(legend)}
          </svg>
        </div>
        <p class="caption">纵轴为成熟用户占比。2017–2025 为完整自然年；虚线连接 2026 YTD，表示截至 {as_of.isoformat()} 的未完整年度，且每项指标只使用已经经过相应观察窗口的成熟用户，不应直接作为完整年度终值。</p>
    """


def _insights_html(
    cohorts: list[dict[str, Any]],
    china_cohorts: list[dict[str, Any]],
    as_of: date,
    focus: dict[str, Any] | None,
    growth: dict[str, Any] | None = None,
    pathways: dict[str, Any] | None = None,
    video: dict[str, Any] | None = None,
) -> str:
    if not cohorts:
        if not focus and not growth and not pathways and not video:
            return ""
        empty = _weighted_insight_summary([])
        cohorts = []
    else:
        empty = _weighted_insight_summary([])

    mature_rows = [
        row for row in cohorts
        if row.get("validUsers") and row.get("lifecycleEligible") == row.get("validUsers")
    ]
    latest_mature = mature_rows[-1] if mature_rows else None

    china_years = sorted({_cohort_year(row) for row in china_cohorts})
    trend_rows = []
    trend_summaries: list[tuple[int, str, dict[str, Any]]] = []

    def percent_with_n(summary: dict[str, Any], rate_field: str) -> str:
        _numerator, denominator = INSIGHT_RATE_FIELDS[rate_field]
        denominator_value = int(summary.get(denominator) or 0)
        valid_users = int(summary.get("validUsers") or 0)
        suffix = (
            f" <span class=\"caption\">(n={_fmt_int(denominator_value)})</span>"
            if denominator_value != valid_users
            else ""
        )
        return f"{_fmt_percent(summary.get(rate_field))}{suffix}"

    for year in china_years:
        label = f"{year} YTD" if year == as_of.year else str(year)
        trend_summaries.append((year, label, _year_summary(china_cohorts, year)))
    for _year, label, summary in trend_summaries:
        trend_rows.append(
            f"<tr><td>{html.escape(label)}</td><td>{_fmt_int(summary['validUsers'])}</td>"
            f"<td>{percent_with_n(summary, 'firstFindD7Rate')}</td>"
            f"<td>{percent_with_n(summary, 'firstFindD30Rate')}</td>"
            f"<td>{percent_with_n(summary, 'firstFindD90Rate')}</td>"
            f"<td>{percent_with_n(summary, 'finds10D90Rate')}</td>"
            f"<td>{percent_with_n(summary, 'activeD30Rate')}</td>"
            f"<td>{percent_with_n(summary, 'activeD90Rate')}</td></tr>"
        )

    narrative = []
    complete_years = [item for item in trend_summaries if item[0] < as_of.year]
    if len(complete_years) >= 2:
        lowest = min(complete_years, key=lambda item: int(item[2]["validUsers"] or 0))
        highest = max(complete_years, key=lambda item: int(item[2]["validUsers"] or 0))
        counts = [int(item[2]["validUsers"] or 0) for item in complete_years]
        monotonic = all(later >= earlier for earlier, later in zip(counts, counts[1:]))
        narrative.append(
            "China 分析归属用户规模"
            f"{'逐年不下降' if monotonic else '并非逐年递增'}：完整年度最低为 {lowest[1]} 年的 "
            f"{_fmt_int(lowest[2]['validUsers'])} 人，最高为 {highest[1]} 年的 "
            f"{_fmt_int(highest[2]['validUsers'])} 人，期间存在明显回落与阶段性跃升。"
        )
        for metric, name in (("firstFindD7Rate", "D7 首找"), ("firstFindD90Rate", "D90 首找")):
            comparable = [item for item in complete_years if item[2].get(metric) is not None]
            if comparable:
                low = min(comparable, key=lambda item: float(item[2][metric]))
                high = max(comparable, key=lambda item: float(item[2][metric]))
                narrative.append(
                    f"{name}在完整年度间波动于 {low[1]} 年的 {_fmt_percent(low[2][metric])} "
                    f"至 {high[1]} 年的 {_fmt_percent(high[2][metric])}，不属于稳定线性上升。"
                )
    if trend_summaries and trend_summaries[-1][0] == as_of.year:
        narrative.append(
            f"{as_of.year} 为截至 {as_of.isoformat()} 的 YTD 数据，各指标仅使用已成熟用户，"
            "不能与完整自然年直接比较。"
        )

    period_specs = ((2017, 2020, "2017–2020"), (2021, 2023, "2021–2023"), (2024, 2025, "2024–2025"))
    lifecycle_rows = []
    retention_rows = []
    retention_summaries = []
    for start, end, label in period_specs:
        selected = [row for row in cohorts if start <= _cohort_year(row) <= end]
        summary = _weighted_insight_summary(selected)
        retention_summaries.append((label, summary))
        retention_rows.append(
            f"<tr><td>全量</td><td>{label}</td>"
            f"<td>{_fmt_percent(summary['activeM1Rate'])}</td>"
            f"<td>{_fmt_percent(summary['activeM2Rate'])}</td>"
            f"<td>{_fmt_percent(summary['activeM3Rate'])}</td>"
            f"<td>{_fmt_percent(summary['activeM6Rate'])}</td>"
            f"<td>{_fmt_percent(summary['activeM12Rate'])}</td></tr>"
        )
        if not summary["lifecycleEligible"]:
            continue
        lifecycle_rows.append(
            f"<tr><td>{label}</td><td>{_fmt_int(summary['lifecycleEligible'])}</td>"
            f"<td>{_fmt_percent(summary['zeroShare'])}</td><td>{_fmt_percent(summary['lightShare'])}</td>"
            f"<td>{_fmt_percent(summary['deepShare'])}</td><td>{_fmt_percent(summary['zeroConditionalD90'])}</td>"
            f"<td>{_fmt_percent(summary['lightConditionalD90'])}</td>"
            f"<td>{_fmt_percent(summary['deepConditionalD90'])}</td></tr>"
        )
    if latest_mature:
        latest = _weighted_insight_summary([latest_mature])
        lifecycle_rows.append(
            f"<tr><td>{html.escape(str(latest_mature['cohortQuarter']))}</td>"
            f"<td>{_fmt_int(latest['lifecycleEligible'])}</td><td>{_fmt_percent(latest['zeroShare'])}</td>"
            f"<td>{_fmt_percent(latest['lightShare'])}</td><td>{_fmt_percent(latest['deepShare'])}</td>"
            f"<td>{_fmt_percent(latest['zeroConditionalD90'])}</td>"
            f"<td>{_fmt_percent(latest['lightConditionalD90'])}</td>"
            f"<td>{_fmt_percent(latest['deepConditionalD90'])}</td></tr>"
        )

    for year in (2024, 2025):
        selected = [row for row in china_cohorts if _cohort_year(row) == year]
        if not selected:
            continue
        summary = _weighted_insight_summary(selected)
        retention_rows.append(
            f"<tr><td>China</td><td>{year}</td>"
            f"<td>{_fmt_percent(summary['activeM1Rate'])}</td>"
            f"<td>{_fmt_percent(summary['activeM2Rate'])}</td>"
            f"<td>{_fmt_percent(summary['activeM3Rate'])}</td>"
            f"<td>{_fmt_percent(summary['activeM6Rate'])}</td>"
            f"<td>{_fmt_percent(summary['activeM12Rate'])}</td></tr>"
        )

    retention_narrative = ""
    if len(retention_summaries) >= 2:
        first_label, first_retention = retention_summaries[0]
        last_label, last_retention = retention_summaries[-1]
        retention_narrative = (
            f"全量加权结果中，M1 从 {html.escape(first_label)} 的 "
            f"{_fmt_percent(first_retention['activeM1Rate'])} 提高到 "
            f"{html.escape(last_label)} 的 {_fmt_percent(last_retention['activeM1Rate'])}；"
            f"M12 从 {_fmt_percent(first_retention['activeM12Rate'])} 提高到 "
            f"{_fmt_percent(last_retention['activeM12Rate'])}。"
        )

    comparison_rows = []
    for year in (2024, 2025):
        overall_period = [row for row in cohorts if _cohort_year(row) == year]
        china_period = [row for row in china_cohorts if _cohort_year(row) == year]
        if not overall_period:
            continue
        for label, summary in (
            (f"{year} 全量", _weighted_insight_summary(overall_period)),
            (f"{year} China", _weighted_insight_summary(china_period)),
            (f"{year} 非 China", _difference_summary(overall_period, china_period)),
        ):
            comparison_rows.append(
                f"<tr><td>{label}</td><td>{_fmt_int(summary['validUsers'])}</td>{_metric_cells(summary)}</tr>"
            )

    focus_html = ""
    if focus:
        total = int(focus.get("totalValidUsers") or 0)
        month_rows = "".join(
            f"<tr><td>{html.escape(month)}</td><td>{_fmt_int(count)}</td>"
            f"<td>{_fmt_percent(rate(int(count), total))}</td></tr>"
            for month, count in focus.get("registrationMonthCounts", {}).items()
        )
        ordered_days = sorted(
            focus.get("registrationDayCounts", {}).items(), key=lambda item: (-int(item[1]), item[0])
        )
        day_rows = "".join(
            f"<tr><td>{html.escape(day)}</td><td>{_fmt_int(count)}</td>"
            f"<td>{_fmt_percent(rate(int(count), total))}</td></tr>"
            for day, count in ordered_days[:10]
        )
        ordered_places = sorted(
            focus.get("regPlaceCounts", {}).items(), key=lambda item: (-int(item[1]), str(item[0]))
        )
        visible_places = ordered_places[:8]
        remaining = sum(int(count) for _place, count in ordered_places[8:])
        if remaining:
            visible_places.append(("其他", remaining))
        place_rows = "".join(
            f"<tr><td>{html.escape(str(place))}</td><td>{_fmt_int(count)}</td>"
            f"<td>{_fmt_percent(rate(int(count), total))}</td></tr>"
            for place, count in visible_places
        )
        focus_metrics = focus.get("cohorts", {})
        focus_metric_rows = "".join(
            f"<tr><td>{label}</td><td>{_fmt_int((focus_metrics.get(key) or {}).get('validUsers'))}</td>"
            f"{_metric_cells(focus_metrics.get(key) or empty)}</tr>"
            for key, label in (("overall", "全量"), ("china", "China"), ("nonChina", "非 China"))
        )
        adjacent = focus.get("comparisonQuarters") or []
        adjacent_rows = "".join(
            f"<tr><td>{html.escape(str(item.get('quarter') or '—'))}</td>"
            f"<td>{_fmt_int(item.get('totalValidUsers'))}</td>"
            f"<td>{_fmt_percent(item.get('chinaShare'))}</td>"
            f"<td>{_fmt_percent(item.get('noFindShare'))}</td>"
            f"<td>{_fmt_percent(item.get('peakMonthShare'))}</td>"
            f"<td>{_fmt_percent(item.get('peakDayShare'))}</td>"
            f"<td>{_fmt_percent((item.get('metrics') or {}).get('firstFindD7Rate'))}</td>"
            f"<td>{_fmt_percent((item.get('metrics') or {}).get('firstFindD90Rate'))}</td>"
            f"<td>{_fmt_percent((item.get('metrics') or {}).get('finds10D90Rate'))}</td>"
            f"<td>{_fmt_percent((item.get('metrics') or {}).get('activeD90Rate'))}</td></tr>"
            for item in adjacent
        )
        adjacent_callout = ""
        if len(adjacent) == 3:
            previous, current, following = adjacent
            previous_change = _relative_change(
                int(current.get("totalValidUsers") or 0),
                int(previous.get("totalValidUsers") or 0),
            )
            following_change = _relative_change(
                int(following.get("totalValidUsers") or 0),
                int(current.get("totalValidUsers") or 0),
            )
            previous_label = str(previous.get("quarter") or "前季度")
            following_label = str(following.get("quarter") or "后季度")
            current_metrics = current.get("metrics") or {}
            previous_metrics = previous.get("metrics") or {}
            following_metrics = following.get("metrics") or {}
            structural_read = ""
            if (
                (current.get("peakMonthShare") or 0) > (previous.get("peakMonthShare") or 0)
                and (current.get("peakMonthShare") or 0) > (following.get("peakMonthShare") or 0)
                and (current.get("noFindShare") or 0) > (previous.get("noFindShare") or 0)
                and (current.get("noFindShare") or 0) > (following.get("noFindShare") or 0)
            ):
                structural_read = (
                    " 该季度同时具有最高的单月集中度和无首找占比，"
                    "更符合时间集中与用户结构变化叠加，而不是单一日期带来的规模峰值。"
                )
            adjacent_callout = (
                f"<p>规模变化：{html.escape(str(current.get('quarter') or '本季度'))} "
                f"相对 {html.escape(previous_label)} 为 "
                f"{_fmt_signed_percent(previous_change)}；"
                f"{html.escape(following_label)} 相对本季度为 "
                f"{_fmt_signed_percent(following_change)}。</p>"
                f"<div class=\"insight-callout\"><strong>结构信号：</strong>China 占比"
                f"{_point_comparison(current.get('chinaShare'), previous.get('chinaShare'), previous_label)}、"
                f"{_point_comparison(current.get('chinaShare'), following.get('chinaShare'), following_label)}；"
                f"无首找占比{_point_comparison(current.get('noFindShare'), previous.get('noFindShare'), previous_label)}、"
                f"{_point_comparison(current.get('noFindShare'), following.get('noFindShare'), following_label)}。"
                f"D90 首找率{_point_comparison(current_metrics.get('firstFindD90Rate'), previous_metrics.get('firstFindD90Rate'), previous_label)}、"
                f"{_point_comparison(current_metrics.get('firstFindD90Rate'), following_metrics.get('firstFindD90Rate'), following_label)}；"
                f"D90 留存率{_point_comparison(current_metrics.get('activeD90Rate'), previous_metrics.get('activeD90Rate'), previous_label)}、"
                f"{_point_comparison(current_metrics.get('activeD90Rate'), following_metrics.get('activeD90Rate'), following_label)}。"
                f"{structural_read}</div>"
            )
        china_count = int(focus.get("regPlaceCounts", {}).get("China") or 0)
        peak_month, peak_month_count = max(
            focus.get("registrationMonthCounts", {}).items(),
            key=lambda item: int(item[1]),
            default=("—", 0),
        )
        peak_day, peak_day_count = max(
            focus.get("registrationDayCounts", {}).items(),
            key=lambda item: int(item[1]),
            default=("—", 0),
        )
        focus_html = f"""
        <section class="insight-block"><h3>{html.escape(str(focus.get('quarter') or '2025 Q3'))} 专项诊断</h3>
          <p>该季度共有 {_fmt_int(total)} 名有效观察用户，其中 reg_place = China 为 {_fmt_int(china_count)} 人，占 {_fmt_percent(rate(china_count, total))}。下面拆分注册月份与分析地域归属构成，用于判断异常来自规模集中还是群体差异。</p>
          <div class="insight-callout">集中度信号：{html.escape(str(peak_month))} 注册 {_fmt_int(peak_month_count)} 人，占 {_fmt_percent(rate(int(peak_month_count), total))}；峰值日期 {html.escape(str(peak_day))} 注册 {_fmt_int(peak_day_count)} 人，占 {_fmt_percent(rate(int(peak_day_count), total))}。</div>
          <h4>相邻季度对照</h4>{adjacent_callout}<div class="table-wrap"><table><thead><tr><th>季度</th><th>有效用户</th><th>China 占比</th><th>无首找占比</th><th>最高月份占比</th><th>最高单日占比</th><th>D7 首找</th><th>D90 首找</th><th>D90 达10找</th><th>D90 留存</th></tr></thead><tbody>{adjacent_rows}</tbody></table></div>
          <p class="caption">“无首找占比”是该季度有效用户中截至数据日仍未观察到任何 Found it 的比例，不等于没有 DNF、Attended 或其他行动。“最高月份占比”和“最高单日占比”分别以该季度注册用户为分母，统计注册量最高的自然月和自然日所占比例。D7/D90 首找与 D90 达10找只累计 Found it；D90 留存观察注册后第 90–119 天的 Found it、DNF 或 Attended。</p>
          <div class="insight-grid"><div><h4>注册月份构成</h4><table class="compact-table"><thead><tr><th>月份</th><th>人数</th><th>占比</th></tr></thead><tbody>{month_rows}</tbody></table></div>
          <div><h4>注册日期 Top 10</h4><table class="compact-table"><thead><tr><th>日期</th><th>人数</th><th>占比</th></tr></thead><tbody>{day_rows}</tbody></table></div></div>
          <h4>reg_place 构成</h4><table class="compact-table"><thead><tr><th>分析地域/状态</th><th>人数</th><th>占比</th></tr></thead><tbody>{place_rows}</tbody></table>
          <h4>群体指标差异</h4><div class="table-wrap"><table><thead><tr><th>群体</th><th>有效用户</th><th>D7 首找</th><th>D30 首找</th><th>D90 首找</th><th>D90 达10找</th><th>D30 留存</th><th>D90 留存</th></tr></thead><tbody>{focus_metric_rows}</tbody></table></div>
          <p class="caption">专项结果只能定位构成和差异，不能仅凭本表确定活动、产品变化或数据回填是异常原因。</p>
        </section>"""

    return f"""
    <section class="insights"><h2>数据洞察</h2>
      <div class="insight-callout"><strong>核心判断：</strong>{' '.join(narrative) if narrative else '当前数据不足以形成跨期趋势判断。'}</div>
      <p>以下比例均使用对应成熟用户人数加权，不直接平均季度百分比。结果是描述性关联，不代表因果关系。</p>
      <section class="insight-block"><h3>China 分析归属用户逐年趋势</h3>
        <p>每一行包含注册日期位于该自然年、且 <code>reg_place = China</code> 的有效用户。这里的 China 是日志推导的分析归属，不是真实账号注册地、居住地或国籍。</p>
        <div class="table-wrap"><table><thead><tr><th>注册年份</th><th>China 有效用户</th><th>D7 首找</th><th>D30 首找</th><th>D90 首找</th><th>D90 达10找</th><th>D30窗口活跃</th><th>D90窗口活跃</th></tr></thead><tbody>{''.join(trend_rows)}</tbody></table></div>
        {_yearly_china_trend_chart(trend_summaries, as_of)}
        <p class="caption">首找和达 10 找是注册日起累计窗口，只统计 Found it；D30/D90 窗口活跃分别观察第 30–59、90–119 天的 Found it、DNF 或 Attended。仅当指标成熟分母与该年 China 有效用户数不同时显示 n；未显示 n 表示两者相同。{as_of.year} YTD 只统计截至 {as_of.isoformat()} 已观察到的注册用户，不与完整年度人数直接比较。</p>
      </section>
      <section class="insight-block"><h3>生命周期结构与条件 D90 活跃</h3>
        <div class="table-wrap"><table><thead><tr><th>时期</th><th>成熟用户</th><th>0找占比</th><th>1–9找占比</th><th>10+找占比</th><th>0找后D90活跃</th><th>1–9找后D90活跃</th><th>10+找后D90活跃</th></tr></thead><tbody>{''.join(lifecycle_rows)}</tbody></table></div>
        <p>“0 找后 D90 活跃”可能表示晚激活、仅 DNF 或仅 Attended；它不等于从未参与。10+ 找与后续活跃高度相关，但不能据此认定达到 10 找会因果性地提高留存。</p>
        <div class="metric-note"><strong>指标说明：</strong><ul><li>0 找后 D90 活跃：注册后第 0–89 天没有 Found it，但第 90–119 天至少有 1 条 Found it、DNF 或 Attended。</li><li>1–9 找后 D90 活跃和10+ 找后 D90 活跃：先按第 0–89 天累计 Found it 数量分为 1–9 找、10+ 找，再计算各组第 90–119 天的活跃率。</li><li>这里的“后”表示先按前 90 天 Found it 数量分组，再计算该组在第 90–119 天的活跃率；不是“完成某次找到之后立即活跃”。每个条件活跃率的分母都是对应深度组中已完整经过注册后 120 天的用户。</li><li>三个找寻深度组互斥并覆盖全部成熟用户；组内的 D90 活跃与不活跃也是互斥状态。</li></ul></div>
      </section>
      <section class="insight-block"><h3>固定窗口留存洞察</h3>
        <p>{retention_narrative}</p>
        <div class="table-wrap"><table><thead><tr><th>群体</th><th>时期</th><th>M1</th><th>M2</th><th>M3</th><th>M6</th><th>M12</th></tr></thead><tbody>{''.join(retention_rows)}</tbody></table></div>
        <p class="caption">M1、M2、M3、M6、M12 分别观察注册后第 30–59、60–89、90–119、180–209、360–389 天是否至少有一条 Found it、DNF 或 Attended。它们是彼此独立的 30 天窗口，不要求连续活跃；每列以已完整经过该窗口的用户为分母。较晚窗口高于较早窗口可以反映回流，不能把各列直接解释为同一批用户逐月流失。</p>
      </section>
      <section class="insight-block"><h3>全量与非 China 辅助对照</h3>
        <div class="table-wrap"><table><thead><tr><th>群体</th><th>有效用户</th><th>D7 首找</th><th>D30 首找</th><th>D90 首找</th><th>D90 达10找</th><th>D30 留存</th><th>D90 留存</th></tr></thead><tbody>{''.join(comparison_rows)}</tbody></table></div>
        <p class="caption">D7/D30/D90 首找分别表示注册后第 0–6、0–29、0–89 天至少一条 Found it；D90 达10找表示第 0–89 天累计至少 10 条 Found it。D30/D90 留存分别观察第 30–59、90–119 天是否至少有一条 Found it、DNF 或 Attended，各比例使用对应成熟用户作分母。reg_place 是日志推导的分析地域归属，不代表注册地、居住地或国籍。</p>
      </section>
      {focus_html}
      {_video_influx_html(video)}
      {_growth_conclusions_html(growth)}
      {_early_pathways_html(pathways)}
      <p class="caption">截至数据日 30 日内活动会随 cohort 年龄机械性变化，最新季度的高值不能与历史 cohort 直接解释为长期留存改善。</p>
    </section>
    """


def _new_summary(cohort_quarter: date) -> dict[str, Any]:
    return {
        "cohortQuarter": quarter_label(cohort_quarter),
        "cohortQuarterStart": cohort_quarter.isoformat(),
        "observedUsers": 0,
        "validUsers": 0,
        "excludedPreRegistrationFind": 0,
        "eligibleD7": 0,
        "firstFindD7": 0,
        "eligibleD30": 0,
        "firstFindD30": 0,
        "finds10D30": 0,
        "eligibleD90": 0,
        "firstFindD90": 0,
        "finds10D90": 0,
        "eligibleActiveD30": 0,
        "activeD30": 0,
        "eligibleActiveD90": 0,
        "activeD90": 0,
        "eligibleActiveM1": 0, "activeM1": 0,
        "eligibleActiveM2": 0, "activeM2": 0,
        "eligibleActiveM3": 0, "activeM3": 0,
        "eligibleActiveM6": 0, "activeM6": 0,
        "eligibleActiveM12": 0, "activeM12": 0,
        "activeLast30": 0,
        "eligibleChurn30": 0,
        "churn30": 0,
        "lifecycleEligible": 0,
        "lifecycleNoFindNoD90": 0, "lifecycleNoFindD90": 0,
        "lifecycleFinds1To9NoD90": 0, "lifecycleFinds1To9D90": 0,
        "lifecycleFinds10PlusNoD90": 0, "lifecycleFinds10PlusD90": 0,
    }


def summarize_cohorts(rows: Iterable[dict[str, Any]], as_of: date) -> list[dict[str, Any]]:
    """Aggregate user rows into reproducible, maturity-aware cohort metrics."""
    summaries: dict[date, dict[str, Any]] = {}
    for row in rows:
        cohort_quarter = quarter_start(row["cohort_month"])
        summary = summaries.setdefault(cohort_quarter, _new_summary(cohort_quarter))
        summary["observedUsers"] += 1
        if row["pre_registration_find"]:
            summary["excludedPreRegistrationFind"] += 1
            continue

        summary["validUsers"] += 1
        registration_date = row["registration_date"]

        if registration_date <= as_of - timedelta(days=7):
            summary["eligibleD7"] += 1
            summary["firstFindD7"] += int(row["first_valid_find"] is not None and row["finds_d7"] > 0)
        if registration_date <= as_of - timedelta(days=30):
            summary["eligibleD30"] += 1
            summary["firstFindD30"] += int(row["first_valid_find"] is not None and row["finds_d30"] > 0)
            summary["finds10D30"] += int((row["finds_d30"] or 0) >= 10)
        if registration_date <= as_of - timedelta(days=90):
            summary["eligibleD90"] += 1
            summary["firstFindD90"] += int(row["first_valid_find"] is not None and row["finds_d90"] > 0)
            summary["finds10D90"] += int((row["finds_d90"] or 0) >= 10)
        if registration_date <= as_of - timedelta(days=60):
            summary["eligibleActiveD30"] += 1
            summary["activeD30"] += int(bool(row["active_d30"]))
        if registration_date <= as_of - timedelta(days=120):
            summary["eligibleActiveD90"] += 1
            summary["activeD90"] += int(bool(row["active_d90"]))
            summary["lifecycleEligible"] += 1
            finds = row.get("finds_d90") or 0
            bucket = "NoFind" if finds == 0 else ("Finds1To9" if finds < 10 else "Finds10Plus")
            suffix = "D90" if row.get("active_d90") else "NoD90"
            summary[f"lifecycle{bucket}{suffix}"] += 1
        for label, start_day, end_day, source_field, fallback_field in (
            ("M1", 30, 60, "active_m1", "active_d30"),
            ("M2", 60, 90, "active_m2", None),
            ("M3", 90, 120, "active_m3", "active_d90"),
            ("M6", 180, 210, "active_m6", None),
            ("M12", 360, 390, "active_m12", None),
        ):
            if registration_date <= as_of - timedelta(days=end_day):
                summary[f"eligibleActive{label}"] += 1
                value = row.get(source_field)
                if value is None and fallback_field:
                    value = row.get(fallback_field)
                summary[f"active{label}"] += int(bool(value))
        summary["activeLast30"] += int(bool(row.get("active_last30")))
        if registration_date <= as_of - timedelta(days=30) and row["has_engagement"]:
            summary["eligibleChurn30"] += 1
            last_engagement = row["last_engagement"]
            summary["churn30"] += int(last_engagement is not None and last_engagement <= as_of - timedelta(days=30))

    output = []
    metric_pairs = {
        "firstFindD7Rate": ("firstFindD7", "eligibleD7"),
        "firstFindD30Rate": ("firstFindD30", "eligibleD30"),
        "finds10D30Rate": ("finds10D30", "eligibleD30"),
        "firstFindD90Rate": ("firstFindD90", "eligibleD90"),
        "finds10D90Rate": ("finds10D90", "eligibleD90"),
        "activeD30Rate": ("activeD30", "eligibleActiveD30"),
        "activeD90Rate": ("activeD90", "eligibleActiveD90"),
        "activeM1Rate": ("activeM1", "eligibleActiveM1"),
        "activeM2Rate": ("activeM2", "eligibleActiveM2"),
        "activeM3Rate": ("activeM3", "eligibleActiveM3"),
        "activeM6Rate": ("activeM6", "eligibleActiveM6"),
        "activeM12Rate": ("activeM12", "eligibleActiveM12"),
        "activeLast30Rate": ("activeLast30", "validUsers"),
        "churn30Rate": ("churn30", "eligibleChurn30"),
    }
    for cohort_month in sorted(summaries):
        summary = summaries[cohort_month]
        for field, (numerator, denominator) in metric_pairs.items():
            summary[field] = rate(summary[numerator], summary[denominator])
        for numerator, denominator in (
            ("firstFindD7", "eligibleD7"),
            ("firstFindD30", "eligibleD30"),
            ("finds10D30", "eligibleD30"),
            ("firstFindD90", "eligibleD90"),
            ("finds10D90", "eligibleD90"),
            ("activeD30", "eligibleActiveD30"),
            ("activeD90", "eligibleActiveD90"),
            ("activeLast30", "validUsers"),
            ("churn30", "eligibleChurn30"),
        ):
                summary[f"{numerator}Display"] = (
                summary[numerator] if summary[denominator] else None
            )
        for field in ("lifecycleNoFindNoD90", "lifecycleNoFindD90", "lifecycleFinds1To9NoD90", "lifecycleFinds1To9D90", "lifecycleFinds10PlusNoD90", "lifecycleFinds10PlusD90"):
            summary[f"{field}Rate"] = rate(summary[field], summary["lifecycleEligible"])
        output.append(summary)
    return output


def _fmt_percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"


def _fmt_int(value: int | None) -> str:
    return f"{value or 0:,}"


def _count_axis_ceiling(value: float) -> int:
    if value <= 0:
        return 1
    magnitude = 10 ** max(0, int(math.floor(math.log10(value))) - 1)
    return int(math.ceil(value / (4 * magnitude)) * 4 * magnitude)


def _percent_axis_ceiling(value: float) -> float:
    return max(5.0, math.ceil(value / 5.0) * 5.0)


CHART_METRIC_DEFINITIONS = {
    "validUsers": "注册人数：注册日期已知、在大中华区缓存日志中被观察到，且通过异常排除的用户数。",
    "firstFindD7": "D7 首找：注册日起第 0–6 天至少有 1 条 Found it。只有 Found it 算首找；只有 DNF、Attended 或其他记录，都不算首找。",
    "firstFindD30": "D30 首找：注册日起第 0–29 天至少有 1 条 Found it。只有 Found it 算首找；只有 DNF、Attended 或其他记录，都不算首找。",
    "firstFindD90": "D90 首找：注册日起第 0–89 天至少有 1 条 Found it。只有 Found it 算首找；只有 DNF、Attended 或其他记录，都不算首找。",
    "finds10D30": "D30 达 10 找：注册日起第 0–29 天累计至少 10 条 Found it。DNF 与 Attended 不计入找寻次数。",
    "finds10D90": "D90 达 10 找：注册日起第 0–89 天累计至少 10 条 Found it。DNF 与 Attended 不计入找寻次数。",
    "activeD30": "D30 留存：注册后第 30–59 天至少有 1 条 Found it、Didn't find it 或 Attended。",
    "activeD90": "D90 留存：注册后第 90–119 天至少有 1 条 Found it、Didn't find it 或 Attended。",
    "activeLast30": "截至数据日 30 日内活动：数据截止日前 30 天内至少有 1 次上述有效参与。",
    "activeM1": "M1：注册后第 30–59 天至少有 1 次有效参与，与本报告的 D30 留存口径相同。",
    "activeM2": "M2：注册后第 60–89 天至少有 1 次有效参与。",
    "activeM3": "M3：注册后第 90–119 天至少有 1 次有效参与，与本报告的 D90 留存口径相同。",
    "activeM6": "M6：注册后第 180–209 天至少有 1 次有效参与。",
    "activeM12": "M12：注册后第 360–389 天至少有 1 次有效参与。",
}


def _chart_metric_note(series: list[tuple[str, str, str]]) -> str:
    """Explain every plotted metric and the mature-denominator rule."""
    definitions = []
    seen = set()
    for field, _label, _color in series:
        base_field = field.removesuffix("Rate").removesuffix("Display")
        definition = CHART_METRIC_DEFINITIONS.get(base_field)
        if definition and definition not in seen:
            definitions.append(f"<li>{html.escape(definition)}</li>")
            seen.add(definition)
    definitions.append(
        "<li>成熟用户：数据截止日已经完整经过对应观察窗口的用户；比例只以成熟用户为分母，未成熟用户显示为空值，不按 0 处理。</li>"
    )
    return (
        '<div class="metric-note"><strong>指标说明：</strong><ul>'
        + "".join(definitions)
        + "</ul></div>"
    )


def _metric_glossary_html() -> str:
    return """
    <section class="metric-glossary">
      <h2>指标释义速查</h2>
      <div class="definition-alert"><strong>先看这条：首找只看 Found it。</strong>例如 D7 首找表示注册日起第 0–6 天至少出现 1 条 Found it。只有 DNF、Attended 或其他记录，都不算首找；若同一天既有 Found it 又有 DNF，则因为存在 Found it，仍算完成首找。</div>
      <div class="glossary-grid">
        <div><strong>Cohort</strong><p>按注册时间组成的用户群。本报告的主图按注册季度分组。</p></div>
        <div><strong>D7 / D30 / D90</strong><p>D 表示从注册日开始计时。用于“首找、达 10 找”时是累计窗口：第 0–6、0–29、0–89 天；用于“D30/D90 留存”时分别指第 30–59、90–119 天的独立活动窗口。</p></div>
        <div><strong>M1 / M2 / M3 / M6 / M12</strong><p>从注册日起算的独立 30 天活动窗口，分别为第 30–59、60–89、90–119、180–209、360–389 天；不要求连续活跃。</p></div>
        <div><strong>E30 / EM1 / EM3 / EM6 / EM12</strong><p>E 表示改以“首次有效活动日”为时间零点。E30 是其后第 0–29 天累计结果；EM1、EM3、EM6、EM12 是首次活动后的对应独立 30 天窗口。</p></div>
        <div><strong>Found it / DNF / Attended</strong><p>Found it 表示找到缓存；DNF 是 Didn't find it，表示未找到；Attended 表示参加活动。首找、找寻次数、达 3/10 找只统计 Found it；“有效参与/活跃”才统计这三种日志中的任意一种。</p></div>
        <div><strong>观察窗口成熟</strong><p>用户从时间零点到数据截止日已完整经过该指标所需窗口。比例只使用成熟用户作分母；“—”通常表示尚未成熟或分母为 0。</p></div>
        <div><strong>首次活动与首日仅 DNF</strong><p>首次活动是注册后最早的 Found it、DNF 或 Attended 日期；“首日仅 DNF”表示该日有 DNF、没有 Found it，同日日志没有小时顺序。</p></div>
        <div><strong>恢复率</strong><p>新手首次 DNF 当日或之后出现 Found it 的比例。“同日 Found”与第 1–30 天跨日恢复分开呈现，同日先后顺序不可知。</p></div>
        <div><strong>China / 城市归属</strong><p>都是基于已观察日志的分析归属，不代表用户真实注册地、居住地、国籍或常驻城市。</p></div>
        <div><strong>D / T</strong><p>缓存难度 Difficulty 与地形 Terrain，通常取 1–5。报告将 0 和范围外数值归为未知；它们是缓存属性，不是用户能力评分。</p></div>
        <div><strong>比例、占比与 n</strong><p>“占比”通常以当前群体全部用户为分母；窗口指标以对应成熟用户为分母；n 表示实际成熟分母。漏斗“上一步转化”则以上一阶段人数为分母。</p></div>
        <div><strong>视频事件窗口</strong><p>均按注册日期的自然日划分。发布前 7 天不含发布日；发布后 0–6 天包含发布日。2025 高播放合并窗口为 8 月 6–13 日，对照期为 5–7 月与 9–11 月。</p></div>
        <div><strong>唯一缓存与原缓存恢复</strong><p>“唯一缓存”表示目标日期仅涉及一个可识别缓存；“原缓存恢复占比”只在已经恢复且前后缓存代码都可比较的用户中计算。</p></div>
      </div>
    </section>
    """


def _chart(
    title: str,
    description: str,
    series: list[tuple[str, str, str]],
    rows: list[dict[str, Any]],
    value_mode: str = "percent",
    auto_percent_axis: bool = False,
) -> str:
    """Render a compact SVG line chart without browser-side dependencies."""
    if value_mode not in {"percent", "count"}:
        raise ValueError(f"Unsupported chart value mode: {value_mode}")
    plot_rows = rows
    width, height = 920, 310
    left, right, top, bottom = 62, 18, 42, 48
    plot_width, plot_height = width - left - right, height - top - bottom
    values = [
        float(row[field])
        for field, _label, _color in series
        for row in plot_rows
        if row.get(field) is not None
    ]
    if value_mode == "percent":
        y_max = _percent_axis_ceiling(max(values, default=0)) if auto_percent_axis else 100
        tick_values = [round(y_max * i / 4, 1) for i in range(5)]
    else:
        y_max = _count_axis_ceiling(max(values, default=0))
        tick_values = [round(y_max * i / 4) for i in range(5)]
    grid = []
    for value in tick_values:
        y = top + plot_height - value / y_max * plot_height
        tick_label = f"{value:.1f}%" if value_mode == "percent" else f"{_fmt_int(value)} 人"
        grid.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" class="grid"/>')
        grid.append(f'<text x="{left-10}" y="{y+4:.1f}" text-anchor="end" class="axis">{tick_label}</text>')
    labels = []
    count = len(plot_rows)
    for index, row in enumerate(plot_rows):
        if count <= 1 or index % max(1, (count - 1) // 5) == 0 or index == count - 1:
            x = left + (index / max(1, count - 1)) * plot_width
            labels.append(f'<text x="{x:.1f}" y="{height-16}" text-anchor="middle" class="axis">{row["cohortQuarter"]}</text>')
    paths = []
    legend = []
    for index, (field, label, color) in enumerate(series):
        points = []
        for row_index, row in enumerate(plot_rows):
            value = row.get(field)
            if value is None:
                continue
            x = left + (row_index / max(1, count - 1)) * plot_width
            y = top + plot_height - value / y_max * plot_height
            points.append((x, y, value, row["cohortQuarter"]))
        if points:
            path = " ".join(("M" if i == 0 else "L") + f" {x:.1f} {y:.1f}" for i, (x, y, _, _) in enumerate(points))
            paths.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2.5"/>')
            for x, y, value, month in points:
                formatted_value = f"{value:.1f}%" if value_mode == "percent" else f"{_fmt_int(int(value))} 人"
                tooltip = html.escape(f"{label}: {formatted_value} ({month})")
                paths.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{color}"><title>{tooltip}</title></circle>')
        lx = 22 + index * 220
        legend.append(f'<line x1="{lx}" y1="21" x2="{lx+18}" y2="21" stroke="{color}" stroke-width="3"/>')
        legend.append(f'<text x="{lx+25}" y="25" class="legend">{html.escape(label)}</text>')
    return f"""
    <section class="chart-section">
      <h2>{html.escape(title)}</h2>
      <p class="chart-description">{html.escape(description)}</p>
      <svg viewBox="0 0 {width} {height}" role="img" aria-label="注册季度 cohort 图表">
        <rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" class="frame"/>
        {''.join(grid)}{''.join(labels)}{''.join(paths)}{''.join(legend)}
      </svg>
      {_chart_metric_note(series)}
      <p class="caption">展示 2017 年起的全部注册季度 cohort；观察期未成熟的指标以空值处理。</p>
    </section>
    """


def _overlaid_bar_chart(
    title: str,
    description: str,
    series: list[tuple[str, str, str]],
    rows: list[dict[str, Any]],
) -> str:
    """Render one quarter bar with independent retention counts overlaid."""
    width, height = max(920, 100 + len(rows) * 28), 380
    left, right, top, bottom = 72, 18, 42, 48
    plot_width, plot_height = width - left - right, height - top - bottom
    max_value = max(
        (float(row.get(field) or 0) for row in rows for field, _label, _color in series),
        default=0,
    )
    sqrt_max = _count_axis_ceiling(max_value)
    tick_values = [round(sqrt_max * index / 4) for index in range(5)]

    def sqrt_y(value: float) -> float:
        return top + plot_height - math.sqrt(value) / math.sqrt(sqrt_max) * plot_height

    grid = []
    for value in tick_values:
        y = sqrt_y(value)
        grid.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" class="grid"/>')
        grid.append(f'<text x="{left-10}" y="{y+4:.1f}" text-anchor="end" class="axis">{_fmt_int(value)} 人</text>')

    column_step = plot_width / max(1, len(rows))
    bar_width = column_step * 0.64
    labels = []
    bars = []
    for row_index, row in enumerate(rows):
        x = left + row_index * column_step + (column_step - bar_width) / 2
        visible_series = series
        if row_index == len(rows) - 1:
            visible_series = [
                item for item in series
                if item[0] in {"validUsers", "activeLast30Display"}
            ]
        for field, label, color in visible_series:
            value = row.get(field)
            if value is None:
                continue
            y = sqrt_y(float(value))
            bar_height = top + plot_height - y
            tooltip = html.escape(f"{row['cohortQuarter']}｜{label}: {_fmt_int(int(value))} 人")
            bars.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}" fill="{color}"><title>{tooltip}</title></rect>'
            )
        if len(rows) <= 12 or row_index % max(1, (len(rows) - 1) // 5) == 0 or row_index == len(rows) - 1:
            center_x = x + bar_width / 2
            labels.append(f'<text x="{center_x:.1f}" y="{height-16}" text-anchor="middle" class="axis">{row["cohortQuarter"]}</text>')

    legend = []
    for index, (_field, label, color) in enumerate(series):
        x = 18 + index * 220
        legend.append(f'<rect x="{x}" y="14" width="14" height="14" fill="{color}"/>')
        legend.append(f'<text x="{x+20}" y="25" class="legend">{html.escape(label)}</text>')
    return f"""
    <section class="chart-section">
      <h2>{html.escape(title)}</h2>
      <p class="chart-description">{html.escape(description)}</p>
      <div class="bar-chart-wrap"><svg class="bar-chart" style="width:{width}px" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}，每个注册季度一根重叠人数柱">
        <rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" class="frame"/>
        {''.join(grid)}{''.join(legend)}{''.join(labels)}{''.join(bars)}
      </svg></div>
      {_chart_metric_note(series)}
      <p class="caption">纵轴使用平方根刻度，以在保留 cohort 规模差异的同时呈现小规模留存的变化；同一季度的各柱以相同宽度、相同底部重叠绘制，柱高表示各指标的独立用户人数，不表示严格用户分层。最新注册季度仅显示注册人数和截至数据日 30 日内活动人数。</p>
    </section>
    """


def _table(rows: list[dict[str, Any]]) -> str:
    table_rows = []
    for row in rows:
        table_rows.append(
            "<tr>"
            f"<td>{row['cohortQuarter']}</td>"
            f"<td>{_fmt_int(row['validUsers'])}</td>"
            f"<td>{_fmt_percent(row['firstFindD7Rate'])}</td>"
            f"<td>{_fmt_percent(row['firstFindD30Rate'])}</td>"
            f"<td>{_fmt_percent(row['finds10D30Rate'])}</td>"
            f"<td>{_fmt_percent(row['firstFindD90Rate'])}</td>"
            f"<td>{_fmt_percent(row['finds10D90Rate'])}</td>"
            f"<td>{_fmt_percent(row['activeD30Rate'])}</td>"
            f"<td>{_fmt_percent(row['activeD90Rate'])}</td>"
            f"<td>{_fmt_percent(row['activeLast30Rate'])}</td>"
            "</tr>"
        )
    return f"""
    <section>
      <h2>2017 年起的注册季度 cohort</h2>
      <div class="table-wrap"><table>
        <thead><tr><th>注册季度</th><th>有效观察用户</th><th>D7 首找</th><th>D30 首找</th><th>D30 达 10 找</th><th>D90 首找</th><th>D90 达 10 找</th><th>D30 留存</th><th>D90 留存</th><th>截至数据日 30 日内活动</th></tr></thead>
        <tbody>{''.join(table_rows)}</tbody>
      </table></div>
      <p class="caption">D7/D30/D90 首找和“达 10 找”是从注册日起累计的第 0–6、0–29、0–89 天窗口；D30/D90 留存却分别是第 30–59、90–119 天的独立活动窗口。所有比例使用各自已经完整经过窗口的成熟用户为分母。</p>
    </section>
    """


LIFECYCLE_SERIES = (
    ("lifecycleNoFindNoD90", "0 找，D90 不活跃", "#94a3b8"),
    ("lifecycleNoFindD90", "0 找，D90 活跃", "#2563eb"),
    ("lifecycleFinds1To9NoD90", "1–9 找，D90 不活跃", "#f59e0b"),
    ("lifecycleFinds1To9D90", "1–9 找，D90 活跃", "#0f766e"),
    ("lifecycleFinds10PlusNoD90", "10+ 找，D90 不活跃", "#ef4444"),
    ("lifecycleFinds10PlusD90", "10+ 找，D90 活跃", "#9333ea"),
)


def _stacked_lifecycle_chart(
    title: str,
    description: str,
    rows: list[dict[str, Any]],
    normalized: bool,
) -> str:
    """Render additive lifecycle cells as stacked quarterly bars."""
    width, height = max(920, 100 + len(rows) * 28), 420
    left, right, top, bottom = 72, 18, 76, 48
    plot_width, plot_height = width - left - right, height - top - bottom
    if normalized:
        y_max = 100.0
        tick_values = [0, 25, 50, 75, 100]
    else:
        y_max = float(_count_axis_ceiling(max((row["lifecycleEligible"] for row in rows), default=0)))
        tick_values = [round(y_max * index / 4) for index in range(5)]

    grid = []
    for value in tick_values:
        y = top + plot_height - float(value) / y_max * plot_height
        label = f"{value:.0f}%" if normalized else f"{_fmt_int(value)} 人"
        grid.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" class="grid"/>')
        grid.append(f'<text x="{left-10}" y="{y+4:.1f}" text-anchor="end" class="axis">{label}</text>')

    column_step = plot_width / max(1, len(rows))
    bar_width = column_step * 0.68
    bars = []
    labels = []
    for row_index, row in enumerate(rows):
        denominator = int(row["lifecycleEligible"])
        x = left + row_index * column_step + (column_step - bar_width) / 2
        cumulative = 0.0
        if denominator:
            for field, label, color in LIFECYCLE_SERIES:
                count = int(row[field])
                value = count * 100.0 / denominator if normalized else float(count)
                if value <= 0:
                    continue
                y_top = top + plot_height - (cumulative + value) / y_max * plot_height
                y_bottom = top + plot_height - cumulative / y_max * plot_height
                percentage = count * 100.0 / denominator
                tooltip = html.escape(
                    f"{row['cohortQuarter']}｜{label}: {_fmt_int(count)} 人（{percentage:.1f}%）"
                )
                bars.append(
                    f'<rect class="lifecycle-stack" x="{x:.1f}" y="{y_top:.1f}" '
                    f'width="{bar_width:.1f}" height="{y_bottom-y_top:.1f}" fill="{color}">'
                    f'<title>{tooltip}</title></rect>'
                )
                cumulative += value
        if len(rows) <= 12 or row_index % max(1, (len(rows) - 1) // 5) == 0 or row_index == len(rows) - 1:
            labels.append(
                f'<text x="{x+bar_width/2:.1f}" y="{height-16}" text-anchor="middle" '
                f'class="axis">{row["cohortQuarter"]}</text>'
            )

    legend = []
    for index, (_field, label, color) in enumerate(LIFECYCLE_SERIES):
        legend_x = 18 + (index % 3) * 300
        legend_y = 16 + (index // 3) * 24
        legend.append(f'<rect x="{legend_x}" y="{legend_y}" width="14" height="14" fill="{color}"/>')
        legend.append(
            f'<text x="{legend_x+20}" y="{legend_y+11}" class="legend">{html.escape(label)}</text>'
        )

    return f"""
    <section class="chart-section">
      <h2>{html.escape(title)}</h2>
      <p class="chart-description">{html.escape(description)}</p>
      <div class="bar-chart-wrap"><svg class="bar-chart" style="width:{width}px" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">
        <rect x="{left}" y="{top}" width="{plot_width}" height="{plot_height}" class="frame"/>
        {''.join(grid)}{''.join(legend)}{''.join(labels)}{''.join(bars)}
      </svg></div>
      <div class="metric-note"><strong>指标说明：</strong><ul><li>先按注册日起第 0–89 天累计 Found it 数量分为 0、1–9、10+ 三个互斥找寻深度组。</li><li>再在每个深度组内判断 D90 活跃：注册后第 90–119 天至少有 1 条 Found it、DNF 或 Attended；没有上述日志则为 D90 不活跃。</li><li>例如“0 找，D90 活跃”表示前 90 天没有 Found it，但第 90–119 天出现过上述任一有效参与；它不表示前 90 天完全没有 DNF 或 Attended。</li><li>成熟用户指截至数据日已完整经过注册后 120 天；六种状态互斥且合计为全部成熟用户。</li></ul></div>
      <p class="caption">仅纳入注册后已满 120 天的有效用户；没有任何成熟用户的季度不绘制生命周期柱。</p>
    </section>
    """


def _lifecycle_section(cohorts: list[dict[str, Any]]) -> str:
    chart = _stacked_lifecycle_chart(
        "生命周期 100% 堆叠分布",
        "按注册季度展示六种互斥生命周期状态占成熟用户的比例。",
        cohorts,
        normalized=True,
    )
    count_chart = _stacked_lifecycle_chart(
        "生命周期人数",
        "按注册季度展示六种互斥生命周期状态的实际人数。",
        cohorts,
        normalized=False,
    )
    rows = []
    for row in cohorts:
        cells = "".join(
            f"<td>{_fmt_int(row[field])}</td><td>{_fmt_percent(row[field+'Rate'])}</td>"
            for field, _label, _color in LIFECYCLE_SERIES
        )
        rows.append(f"<tr><td>{row['cohortQuarter']}</td><td>{_fmt_int(row['lifecycleEligible'])}</td>{cells}</tr>")
    headers = "".join(f"<th>{label}</th><th>比例</th>" for _field, label, _color in LIFECYCLE_SERIES)
    table = f"<section><h2>生命周期明细</h2><div class=\"table-wrap\"><table><thead><tr><th>注册季度</th><th>成熟用户</th>{headers}</tr></thead><tbody>{''.join(rows)}</tbody></table></div></section>"
    caption = '<p class="caption">分母为注册后已满 120 天的有效用户。前 90 天的 <code>Found it</code> 数量分为 0、1–9、10+；D90 活跃表示注册后第 90–119 天内至少一次有效参与，即 [注册日 + 90 天，注册日 + 120 天)。六个单元格互斥且相加等于成熟用户数，未成熟用户不纳入。</p>'
    return '<section><h2>生命周期分析</h2>' + chart + count_chart + table + caption + '</section>'


def _analysis_charts(cohorts: list[dict[str, Any]]) -> str:
    return "".join([
        _chart("首次找寻转化", "按注册季度比较用户在 7、30、90 天内首次完成大中华区缓存找寻的比例，用于判断激活发生得有多快。", [
            ("firstFindD7Rate", "D7 首次找寻", "#2563eb"),
            ("firstFindD30Rate", "D30 首次找寻", "#0f766e"),
            ("firstFindD90Rate", "D90 首次找寻", "#9333ea"),
        ], cohorts),
        _chart("首次找寻用户数", "与上图统计口径相同，但显示各季度在对应窗口内完成首次找寻的实际用户数，用于观察 cohort 规模对结果的影响。", [
            ("firstFindD7Display", "D7 首次找寻人数", "#2563eb"),
            ("firstFindD30Display", "D30 首次找寻人数", "#0f766e"),
            ("firstFindD90Display", "D90 首次找寻人数", "#9333ea"),
        ], cohorts, value_mode="count"),
        _chart("达到 10 次找寻", "展示各注册季度 cohort 在 30、90 天内完成 10 次找寻的比例，用于识别从首次尝试走向稳定参与的转化强弱。", [
            ("finds10D30Rate", "D30 达 10 找", "#d97706"),
            ("finds10D90Rate", "D90 达 10 找", "#dc2626"),
        ], cohorts, auto_percent_axis=True),
        _chart("达到 10 次找寻用户数", "与上图统计口径相同，但显示各季度在对应窗口内达到 10 次找寻的实际用户数，用于区分比例变化和 cohort 规模变化。", [
            ("finds10D30Display", "D30 达 10 找人数", "#d97706"),
            ("finds10D90Display", "D90 达 10 找人数", "#dc2626"),
        ], cohorts, value_mode="count"),
        _chart("区域参与留存与近期活动", "按注册季度展示用户在注册后的大中华区参与情况；截至数据日 30 日内活动表示在截止日前 30 天内至少有一次有效参与的用户。", [
            ("activeD30Rate", "D30 留存", "#0f766e"),
            ("activeD90Rate", "D90 留存", "#2563eb"),
            ("activeLast30Rate", "截至数据日 30 日内活动", "#dc2626"),
        ], cohorts),
        _overlaid_bar_chart("区域参与留存人数", "每个注册季度以一根重叠柱展示注册人数、D30 留存、D90 留存和截至数据日 30 日内活动的实际人数；柱高用于直接比较各阶段的用户规模。", [
            ("validUsers", "注册人数", "#475569"),
            ("activeD30Display", "D30 留存人数", "#0f766e"),
            ("activeD90Display", "D90 留存人数", "#2563eb"),
            ("activeLast30Display", "截至数据日 30 日内活动人数", "#dc2626"),
        ], cohorts),
    ])


def _retention_curve_section(cohorts: list[dict[str, Any]]) -> str:
    early = _chart(
        "早期固定窗口留存",
        "比较注册后第 30–59、60–89、90–119 天内是否有大中华区有效参与，分别记为 M1、M2、M3。",
        [
            ("activeM1Rate", "M1（第 30–59 天）", "#0f766e"),
            ("activeM2Rate", "M2（第 60–89 天）", "#2563eb"),
            ("activeM3Rate", "M3（第 90–119 天）", "#9333ea"),
        ],
        cohorts,
    )
    long_term = _chart(
        "中长期固定窗口留存",
        "比较注册后第 180–209 天和第 360–389 天内是否有大中华区有效参与，分别记为 M6、M12。",
        [
            ("activeM6Rate", "M6（第 180–209 天）", "#d97706"),
            ("activeM12Rate", "M12（第 360–389 天）", "#dc2626"),
        ],
        cohorts,
    )
    return (
        '<section><h2>固定窗口留存曲线</h2>'
        '<p>各点均表示对应 30 天窗口内至少一次有效参与，占已经完整经过该窗口的 cohort 用户比例。'
        '它们是独立窗口，不要求用户连续活跃，也不构成严格漏斗。</p>'
        f'{early}{long_term}</section>'
    )


def _china_kpis(china_cohorts: list[dict[str, Any]], overall_valid_users: int) -> str:
    valid_users = sum(row["validUsers"] for row in china_cohorts)
    d90_eligible = sum(row["eligibleD90"] for row in china_cohorts)
    lifecycle_eligible = sum(row["lifecycleEligible"] for row in china_cohorts)
    excluded = sum(row["excludedPreRegistrationFind"] for row in china_cohorts)
    return f"""
    <div class="kpis">
      <div class="kpi"><span>China 有效观察用户</span><b>{_fmt_int(valid_users)}</b></div>
      <div class="kpi"><span>占全量有效用户</span><b>{_fmt_percent(rate(valid_users, overall_valid_users))}</b></div>
      <div class="kpi"><span>生命周期成熟用户</span><b>{_fmt_int(lifecycle_eligible)}</b></div>
      <div class="kpi"><span>D90 已成熟 / 异常排除</span><b>{_fmt_int(d90_eligible)} / {_fmt_int(excluded)}</b></div>
    </div>
    """


def _city_cohort_html(city_analysis: dict[str, Any] | None) -> str:
    if not city_analysis:
        return ""
    groups = city_analysis.get("groups") or []
    def depth_share(item: dict[str, Any], keys: set[str]) -> float | None:
        depth = item.get("e30Depth") or []
        values = [entry.get("share") for entry in depth if entry.get("key") in keys]
        return round(sum(float(value or 0) for value in values), 1) if values else None

    engagement_rows = "".join(
        f"<tr><td>{html.escape(str(item.get('city') or '—'))}</td>"
        f"<td>{_fmt_int(item.get('groupUsers'))}</td>"
        f"<td>{_fmt_percent(item.get('groupShare'))}</td>"
        f"<td>{_fmt_percent(item.get('firstDayFoundRate'))}</td>"
        f"<td>{_fmt_percent(item.get('firstDayDnfWithoutFoundRate'))}</td>"
        f"<td>{_fmt_int(item.get('eligibleE30'))}</td>"
        f"<td>{_fmt_percent(item.get('foundE30Rate'))}</td>"
        f"<td>{_fmt_percent(depth_share(item, {'finds_3_9', 'finds_10_plus'}))}</td>"
        f"<td>{_fmt_percent(item.get('finds10E30Rate'))}</td>"
        f"<td>{_fmt_percent(item.get('activeEM1Rate'))}</td>"
        f"<td>{_fmt_percent(item.get('activeEM3Rate'))}</td>"
        f"<td>{_fmt_percent(item.get('activeEM6Rate'))}</td>"
        f"<td>{_fmt_percent(item.get('activeEM12Rate'))}</td></tr>"
        for item in groups
    )
    registration_rows = "".join(
        f"<tr><td>{html.escape(str(item.get('city') or '—'))}</td>"
        f"<td>{_fmt_int(item.get('eligibleD30'))}</td>"
        f"<td>{_fmt_percent(item.get('firstFindD30Rate'))}</td>"
        f"<td>{_fmt_percent(item.get('finds10D30Rate'))}</td>"
        f"<td>{_fmt_int(item.get('eligibleM3'))}</td>"
        f"<td>{_fmt_percent(item.get('activeM3Rate'))}</td>"
        f"<td>{_fmt_percent(item.get('activeM6Rate'))}</td>"
        f"<td>{_fmt_percent(item.get('activeM12Rate'))}</td></tr>"
        for item in groups
    )
    comparable = [
        item for item in groups
        if item.get("city") not in {
            "其他城市", MULTI_CITY_LABEL, UNKNOWN_CITY_LABEL,
            NO_POST_REGISTRATION_ACTIVITY_LABEL,
        }
        and int(item.get("eligibleE30") or 0) >= 100
    ]
    strongest = max(
        comparable,
        key=lambda item: float(item.get("foundE30Rate") or -1),
        default=None,
    )
    comparison = (
        f"在表中展示且首次活动后已完整观察 30 天的人数至少 100 的城市中，"
        f"{html.escape(str(strongest.get('city')))} 的 E30 成功率最高，为 "
        f"{_fmt_percent(strongest.get('foundE30Rate'))}。"
        if strongest else "当前没有 E30 成熟人数达到 100 的城市可作稳定比较。"
    )

    def weighted_rate(items: list[dict[str, Any]], numerator: str, denominator: str) -> float | None:
        total_numerator = sum(int(item.get(numerator) or 0) for item in items)
        total_denominator = sum(int(item.get(denominator) or 0) for item in items)
        return rate(total_numerator, total_denominator)

    all_by_city = {
        str(item.get("city")): item for item in city_analysis.get("allGroups") or []
    }
    top_six = [
        all_by_city[label]
        for label in city_analysis.get("displayCityLabels") or []
        if label in all_by_city
    ]
    top_six_users = sum(int(item.get("groupUsers") or 0) for item in top_six)
    top_six_summary = (
        f"北京、上海、广州、深圳、成都、杭州共 {_fmt_int(top_six_users)} 人，"
        f"占 China 有效用户 {_fmt_percent(rate(top_six_users, int(city_analysis.get('totalValidUsers') or 0)))}；"
        f"E30 有 Found 为 {_fmt_percent(weighted_rate(top_six, 'foundE30', 'eligibleE30'))}，"
        f"E30 达 10 找为 {_fmt_percent(weighted_rate(top_six, 'finds10E30', 'eligibleE30'))}，"
        f"EM3 活跃为 {_fmt_percent(weighted_rate(top_six, 'activeEM3', 'eligibleEM3'))}。"
    ) if top_six else "当前缺少主要城市的可比较数据。"

    event_comparisons = city_analysis.get("eventComparisons") or {}
    video_period = event_comparisons.get("2025_video_wave") or {}
    adjacent_period = event_comparisons.get("2025_adjacent") or {}
    video_summary = video_period.get("summary") or {}
    adjacent_summary = adjacent_period.get("summary") or {}
    event_summary = (
        f"2025 高播放视频窗口的首日仅 DNF 无 Found 为 "
        f"{_fmt_percent(video_summary.get('firstDayDnfWithoutFoundRate'))}，"
        f"对照期为 {_fmt_percent(adjacent_summary.get('firstDayDnfWithoutFoundRate'))}；"
        f"E30 有 Found 为 {_fmt_percent(video_summary.get('foundE30Rate'))} 对 "
        f"{_fmt_percent(adjacent_summary.get('foundE30Rate'))}，E30 达 10 找为 "
        f"{_fmt_percent(video_summary.get('finds10E30Rate'))} 对 "
        f"{_fmt_percent(adjacent_summary.get('finds10E30Rate'))}，EM3 活跃为 "
        f"{_fmt_percent(video_summary.get('activeEM3Rate'))} 对 "
        f"{_fmt_percent(adjacent_summary.get('activeEM3Rate'))}。"
    ) if video_summary and adjacent_summary else "当前缺少视频窗口与对照期数据。"

    guangzhou = all_by_city.get("广州市") or {}
    hangzhou = all_by_city.get("杭州市") or {}
    xian = all_by_city.get("西安市") or {}
    city_pattern_summary = (
        f"广州在大样本城市中同时表现出较高的 E30 成功（{_fmt_percent(guangzhou.get('foundE30Rate'))}）、"
        f"E30 达 10 找（{_fmt_percent(guangzhou.get('finds10E30Rate'))}）和 EM3 活跃"
        f"（{_fmt_percent(guangzhou.get('activeEM3Rate'))}）。杭州 E30 成功为 "
        f"{_fmt_percent(hangzhou.get('foundE30Rate'))}，但 E30 达 10 找仅 "
        f"{_fmt_percent(hangzhou.get('finds10E30Rate'))}；西安 E30 成功为 "
        f"{_fmt_percent(xian.get('foundE30Rate'))}，EM3 活跃仅 "
        f"{_fmt_percent(xian.get('activeEM3Rate'))}。这说明首次成功、深度形成和后续活跃是三个不同阶段。"
    ) if guangzhou and hangzhou and xian else "城市间的首次成功、深度形成和后续活跃需要分阶段判断。"
    year_rows = "".join(
        f"<tr><td>{html.escape(str(period.get('label') or '—'))}</td>"
        f"<td>{html.escape(str(item.get('city') or '—'))}</td>"
        f"<td>{_fmt_int(item.get('groupUsers'))}</td>"
        f"<td>{_fmt_int(item.get('eligibleE30'))}</td>"
        f"<td>{_fmt_percent(item.get('foundE30Rate'))}</td>"
        f"<td>{_fmt_percent(item.get('finds10E30Rate'))}</td>"
        f"<td>{_fmt_int(item.get('eligibleEM3'))}</td>"
        f"<td>{_fmt_percent(item.get('activeEM3Rate'))}</td></tr>"
        for period in city_analysis.get("yearComparisons") or []
        for item in period.get("groups") or []
        if int(item.get("groupUsers") or 0) > 0
    )
    event_rows = "".join(
        f"<tr><td>{html.escape(str(period.get('label') or '—'))}</td>"
        f"<td>{html.escape(str(item.get('city') or '—'))}</td>"
        f"<td>{_fmt_int(item.get('groupUsers'))}</td>"
        f"<td>{_fmt_percent(item.get('groupShare'))}</td>"
        f"<td>{_fmt_percent(item.get('firstDayDnfWithoutFoundRate'))}</td>"
        f"<td>{_fmt_percent(item.get('foundE30Rate'))}</td>"
        f"<td>{_fmt_percent(item.get('finds10E30Rate'))}</td>"
        f"<td>{_fmt_percent(item.get('activeEM3Rate'))}</td></tr>"
        for period in (city_analysis.get("eventComparisons") or {}).values()
        for item in period.get("groups") or []
        if int(item.get("groupUsers") or 0) > 0
    )
    return f"""
    <section><h2>首次有效活动城市比较</h2>
      <p>仅分析 <code>reg_place = China</code> 的有效用户，并按注册后首次 <code>Found it</code>、<code>Didn't find it</code> 或 <code>Attended</code> 所在城市分组。该字段是事后行为归属，不代表居住城市、注册城市或常驻城市。</p>
      <div class="kpis">
        <div class="kpi"><span>China 有效用户</span><b>{_fmt_int(city_analysis.get('totalValidUsers'))}</b></div>
        <div class="kpi"><span>可归入明确城市</span><b>{_fmt_int(city_analysis.get('exactCityUsers'))}</b></div>
        <div class="kpi"><span>明确城市覆盖率</span><b>{_fmt_percent(city_analysis.get('exactCityCoverage'))}</b></div>
        <div class="kpi"><span>不同明确城市</span><b>{_fmt_int(city_analysis.get('uniqueExactCities'))}</b></div>
      </div>
      <div class="insight-callout">{comparison}这是描述性差异，可能同时受到 cohort 年份、当地缓存供给、活动组织和用户构成影响，不能解释为城市本身造成转化差异。</div>
      <section class="insight-block"><h3>完整结论</h3>
        <ol>
          <li><strong>入口集中：</strong>{top_six_summary}</li>
          <li><strong>城市差异不是单一漏斗：</strong>{city_pattern_summary}</li>
          <li><strong>渠道构成会重写城市表象：</strong>{event_summary}同一方向在多个主要城市出现，因此不能把 2025 年的下降简单归因于当地缓存环境。</li>
          <li><strong>可行动边界：</strong>优先把首日仅 DNF 无 Found、E30 仍为 0 找和 E30 停留在 1–2 找的人群分别作为失败救援、首次成功和深度培养对象；城市仅用于调整本地缓存推荐与活动供给，不应用作用户质量标签。</li>
        </ol>
      </section>
      <h3>首次活动后转化与留存</h3>
      <p>以首次活动日作为城市 cohort 的时间零点。E30 表示首次活动日起 30 天内，EM1/EM3/EM6/EM12 表示首次活动后相应的独立 30 天窗口。这样先确定城市，再观察后续行为，避免用未来城市归属直接解释注册后早期结果。</p>
      <div class="table-wrap"><table><thead><tr><th>首次活动城市</th><th>用户</th><th>占比</th><th>首日Found</th><th>首日仅DNF无Found</th><th>E30成熟</th><th>E30有Found</th><th>E30达3找</th><th>E30达10找</th><th>EM1</th><th>EM3</th><th>EM6</th><th>EM12</th></tr></thead><tbody>{engagement_rows}</tbody></table></div>
      <p class="caption">首日 Found：首次活动日至少有 1 条 Found it。首日仅 DNF 无 Found：首次活动日有 DNF、但没有 Found it；若同日两者都有则不计入此列。E30 有 Found：首次活动日起第 0–29 天至少有 1 条 Found it；E30 达3找、达10找分别表示同期累计至少 3、10 条 Found it。E30 成熟是已完整经过这 30 天的人数。EM1/EM3/EM6/EM12 分别观察首次活动后第 30–59、90–119、180–209、360–389 天是否至少有一条 Found it、DNF 或 Attended，各率以对应城市和窗口的成熟用户为分母。</p>
      <h3>注册日起算敏感性对照</h3>
      <p>下表保留原有注册日起算口径，主要用于检查“从注册到首次活动的等待时间”是否改变城市排序。它不应单独用于判断城市造成了激活差异。</p>
      <div class="table-wrap"><table><thead><tr><th>首次活动城市</th><th>D30成熟</th><th>D30首找</th><th>D30达10找</th><th>M3成熟</th><th>M3活跃</th><th>M6活跃</th><th>M12活跃</th></tr></thead><tbody>{registration_rows}</tbody></table></div>
      <p class="caption">“首次活动城市”是注册后最早一日出现 Found it、DNF 或 Attended 的缓存城市分析归属，不是注册地或居住地。D30 首找和达10找从注册日累计第 0–29 天 Found it；M3/M6/M12 分别观察注册后第 90–119、180–209、360–389 天的 Found it、DNF 或 Attended，各率以对应成熟用户为分母。</p>
      <h3>注册年份稳健性</h3>
      <p>对全时期用户数最多的 6 个明确城市按注册年份拆分。若某个差异只在单一年份出现，或成熟人数很小，不应视为稳定城市特征。</p>
      <div class="table-wrap"><table><thead><tr><th>注册年份</th><th>首次活动城市</th><th>用户</th><th>E30成熟</th><th>E30有Found</th><th>E30达10找</th><th>EM3成熟</th><th>EM3活跃</th></tr></thead><tbody>{year_rows}</tbody></table></div>
      <p class="caption">E30 以首次活动日为第 0 天：E30 有 Found 表示第 0–29 天至少 1 条 Found it，E30 达10找表示同期累计至少 10 条 Found it。EM3 活跃观察首次活动后第 90–119 天是否有 Found it、DNF 或 Attended；成熟人数是各率的实际分母。</p>
      <h3>2025 视频窗口城市构成</h3>
      <p>比较 2025 年 8 月 6–13 日高播放视频合并窗口与 5–7 月、9–11 月对照期。该表用于判断全时期城市差异是否被特殊流量窗口改变，不能把组间差异解释为视频的独立因果效果。</p>
      <div class="table-wrap"><table><thead><tr><th>时期</th><th>首次活动城市</th><th>用户</th><th>时期内占比</th><th>首日仅DNF无Found</th><th>E30有Found</th><th>E30达10找</th><th>EM3活跃</th></tr></thead><tbody>{event_rows}</tbody></table></div>
      <p class="caption">表格按首次活动用户数展示 Top {int(city_analysis.get('topN') or CITY_COHORT_TOP_N)} 明确城市，其余合并为“其他城市”；城市字段缺失列为“未知城市”，同日多城市不强行归属。“时期内占比”以该时期全部可分析用户为分母。“首日仅 DNF 无 Found”表示首次活动日有 DNF 但没有 Found it；E30 有 Found 与达10找分别统计首次活动日起第 0–29 天至少 1 条、累计至少 10 条 Found it；EM3 活跃观察第 90–119 天的 Found it、DNF 或 Attended，各率以对应成熟用户为分母。</p>
    </section>
    """


def _cache_attribute_html(analysis: dict[str, Any] | None) -> str:
    if not analysis:
        return ""
    engagement = analysis.get("firstEngagement") or {}
    recovery = analysis.get("firstDnfRecovery") or {}
    engagement_dimensions = engagement.get("dimensions") or {}
    recovery_dimensions = recovery.get("dimensions") or {}
    period_comparisons = analysis.get("periodComparisons") or {}

    def engagement_rows(items: list[dict[str, Any]]) -> str:
        return "".join(
            "<tr>"
            f"<td>{html.escape(str(item.get('label') or item.get('key') or '—'))}</td>"
            f"<td>{_fmt_int(item.get('users'))}</td>"
            f"<td>{_fmt_percent(item.get('foundRate'))}</td>"
            f"<td>{_fmt_percent(item.get('dnfWithoutFoundRate'))}</td>"
            f"<td>{_fmt_int(item.get('eligibleE30'))}</td>"
            f"<td>{_fmt_percent(item.get('finds10E30Rate'))}</td>"
            f"<td>{_fmt_int(item.get('eligibleEM3'))}</td>"
            f"<td>{_fmt_percent(item.get('activeEM3Rate'))}</td>"
            "</tr>"
            for item in items
        ) or '<tr><td colspan="8">无可用样本</td></tr>'

    def recovery_rows(items: list[dict[str, Any]]) -> str:
        def format_days(value: Any) -> str:
            return "—" if value is None else f"{float(value):.1f}"

        return "".join(
            "<tr>"
            f"<td>{html.escape(str(item.get('label') or item.get('key') or '—'))}</td>"
            f"<td>{_fmt_int(item.get('users'))}</td>"
            f"<td>{_fmt_int(item.get('eligibleRecovery30'))}</td>"
            f"<td>{_fmt_percent(item.get('recoveredWithin30Rate'))}</td>"
            f"<td>{_fmt_percent(item.get('sameDayFoundRate'))}</td>"
            f"<td>{_fmt_percent(item.get('recoveredDays1To30Rate'))}</td>"
            f"<td>{format_days(item.get('medianRecoveryDays1To30'))}</td>"
            f"<td>{_fmt_int(item.get('cacheComparableRecoveries'))}</td>"
            f"<td>{_fmt_percent(item.get('sameCacheRecoveryShare'))}</td>"
            "</tr>"
            for item in items
        ) or '<tr><td colspan="9">无可用样本</td></tr>'

    def stable_range(
        items: list[dict[str, Any]], metric: str, denominator: str, noun: str
    ) -> str:
        stable = [
            item for item in items
            if item.get(metric) is not None and int(item.get(denominator) or 0) >= 30
        ]
        if len(stable) < 2:
            return f"{noun}的稳定样本不足以比较。"
        low = min(stable, key=lambda item: item[metric])
        high = max(stable, key=lambda item: item[metric])
        return (
            f"{noun}从 {html.escape(str(low['label']))} 的 {_fmt_percent(low[metric])} "
            f"到 {html.escape(str(high['label']))} 的 {_fmt_percent(high[metric])}。"
        )

    exact_engagement = int(engagement.get("exactSingleCacheUsers") or 0)
    all_engagement = int(engagement.get("allEngagementUsers") or 0)
    ambiguous_engagement = int(engagement.get("ambiguousMultiCacheUsers") or 0)
    exact_dnf = int(recovery.get("exactSingleCacheUsers") or 0)
    newcomer_dnf = int(recovery.get("newcomerFailureUsers") or 0)
    difficulty_unknown = int(
        (engagement.get("unknownAttributeUsers") or {}).get("difficulty") or 0
    )
    type_groups = engagement_dimensions.get("geocacheType") or []
    leading_type = max(type_groups, key=lambda item: item.get("users") or 0, default=None)
    leading_type_text = (
        f"{html.escape(str(leading_type['label']))} 占唯一缓存样本的 "
        f"{_fmt_percent(rate(int(leading_type.get('users') or 0), exact_engagement))}，"
        if leading_type else "缓存类型样本为空，"
    )
    dnf_difficulty_range = stable_range(
        engagement_dimensions.get("difficulty") or [],
        "dnfWithoutFoundRate", "users", "难度分组的首日仅 DNF"
    )
    dnf_terrain_range = stable_range(
        engagement_dimensions.get("terrain") or [],
        "dnfWithoutFoundRate", "users", "地形分组的首日仅 DNF"
    )
    recovery_difficulty_range = stable_range(
        recovery_dimensions.get("difficulty") or [],
        "recoveredDays1To30Rate", "eligibleRecovery30", "难度分组的 1–30 天后续恢复率"
    )

    dimension_specs = (
        ("difficulty", "难度 D"),
        ("terrain", "地形 T"),
        ("geocacheType", "缓存类型"),
        ("containerType", "容器类型原始值"),
    )
    engagement_tables = "".join(
        f"<h3>首次活动：{title}</h3>"
        '<div class="table-wrap"><table><thead><tr><th>属性组</th><th>用户</th><th>首日Found</th><th>首日仅DNF无Found</th><th>E30成熟</th><th>E30达10找</th><th>EM3成熟</th><th>EM3活跃</th></tr></thead>'
        f"<tbody>{engagement_rows(engagement_dimensions.get(key) or [])}</tbody></table></div>"
        '<p class="caption">首日 Found：首次活动日至少有 1 条 Found it。首日仅 DNF 无 Found：首次活动日有 DNF、但没有 Found it。E30 达10找表示首次活动日起第 0–29 天累计至少 10 条 Found it；EM3 活跃表示首次活动后第 90–119 天至少有一条 Found it、DNF 或 Attended。E30/EM3 比例分别以该属性组对应窗口的成熟用户为分母。</p>'
        for key, title in dimension_specs
    )
    recovery_tables = "".join(
        f"<h3>首次 DNF 后恢复：{title}</h3>"
        '<div class="table-wrap"><table><thead><tr><th>属性组</th><th>失败用户</th><th>30天成熟</th><th>30天内含同日Found</th><th>同日Found</th><th>1–30天后续恢复</th><th>后续恢复中位天数</th><th>缓存可比较恢复</th><th>原缓存恢复占比</th></tr></thead>'
        f"<tbody>{recovery_rows(recovery_dimensions.get(key) or [])}</tbody></table></div>"
        '<p class="caption">30 天内含同日 Found：在首次 DNF 当日至之后第 30 天内观察到 Found it。同日 Found 只统计首次 DNF 当日也有 Found it，日期级日志无法判断先后。1–30 天后续恢复：首次 DNF 后第 1–30 天出现 Found it，与同日 Found 互斥。前三项均以已完整经过首 DNF 后 30 天的失败用户为分母。“后续恢复中位天数”只计算跨日恢复者；“原缓存恢复占比”以已恢复且前后缓存代码可比较的用户为分母。</p>'
        for key, title in dimension_specs
    )

    def period_rows() -> str:
        rows = []
        for period in period_comparisons.values():
            first = period.get("firstEngagementSummary") or {}
            period_recovery = period.get("firstDnfRecoverySummary") or {}
            rows.append(
                "<tr>"
                f"<td>{html.escape(str(period.get('label') or period.get('key') or '—'))}</td>"
                f"<td>{_fmt_int(period.get('firstEngagementUsers'))}</td>"
                f"<td>{_fmt_percent(first.get('foundRate'))}</td>"
                f"<td>{_fmt_percent(first.get('dnfWithoutFoundRate'))}</td>"
                f"<td>{_fmt_percent(first.get('finds10E30Rate'))}</td>"
                f"<td>{_fmt_percent(first.get('activeEM3Rate'))}</td>"
                f"<td>{_fmt_int(period_recovery.get('eligibleRecovery30'))}</td>"
                f"<td>{_fmt_percent(period_recovery.get('recoveredWithin30Rate'))}</td>"
                f"<td>{_fmt_percent(period_recovery.get('sameDayFoundRate'))}</td>"
                f"<td>{_fmt_percent(period_recovery.get('recoveredDays1To30Rate'))}</td>"
                "</tr>"
            )
        return "".join(rows) or '<tr><td colspan="10">无可用样本</td></tr>'

    video_period = period_comparisons.get("2025_video_wave") or {}
    adjacent_period = period_comparisons.get("2025_adjacent") or {}
    video_first = video_period.get("firstEngagementSummary") or {}
    adjacent_first = adjacent_period.get("firstEngagementSummary") or {}
    video_recovery = video_period.get("firstDnfRecoverySummary") or {}
    adjacent_recovery = adjacent_period.get("firstDnfRecoverySummary") or {}
    period_insight = ""
    if video_first and adjacent_first and video_recovery and adjacent_recovery:
        period_insight = (
            "视频窗口的唯一缓存首次活动中，首日 Found 为 "
            f"{_fmt_percent(video_first.get('foundRate'))}，对照期为 "
            f"{_fmt_percent(adjacent_first.get('foundRate'))}；首日仅 DNF 为 "
            f"{_fmt_percent(video_first.get('dnfWithoutFoundRate'))} 对 "
            f"{_fmt_percent(adjacent_first.get('dnfWithoutFoundRate'))}。"
            "跨日 1–30 天恢复率则为 "
            f"{_fmt_percent(video_recovery.get('recoveredDays1To30Rate'))} 对 "
            f"{_fmt_percent(adjacent_recovery.get('recoveredDays1To30Rate'))}，"
            "差距远小于含同日 Found 的恢复率差距，说明渠道窗口的主要异常集中在首次活动日。"
        )
    return f"""
    <section class="insights">
      <h2>缓存属性与新手成功、失败恢复</h2>
      <p>本节只分析 <code>reg_place = China</code> 且无注册前 Found 异常的用户。主分析要求首次活动日或首次 DNF 日是<strong>当天唯一缓存</strong>；同日多个缓存无法确定顺序与单一暴露，因而排除。</p>
      <div class="kpis">
        <div class="kpi"><span>首次活动用户</span><b>{_fmt_int(all_engagement)}</b></div>
        <div class="kpi"><span>唯一缓存活动日</span><b>{_fmt_int(exact_engagement)}</b></div>
        <div class="kpi"><span>多缓存日排除</span><b>{_fmt_int(ambiguous_engagement)}</b></div>
        <div class="kpi"><span>D/T 无效或未知</span><b>{_fmt_int(difficulty_unknown)}</b></div>
      </div>
      <div class="insight-callout">这些比例描述用户主动提交的<strong>上报日志结果</strong>，不是所有实际尝试的完整记录；未记录 DNF、缓存供给、城市、年份与渠道构成均可能造成选择偏差，因此不能解释为真实成功概率或缓存属性的因果效果。</div>
      <section class="insight-block"><h3>完整结论</h3><ol>
        <li><strong>可识别边界：</strong>首次活动 {all_engagement} 人中，{exact_engagement} 人可唯一归属缓存，{ambiguous_engagement} 人因同日多缓存排除；新手首 DNF {newcomer_dnf} 人中，{exact_dnf} 人可唯一归属失败缓存。</li>
        <li><strong>首日报告结果：</strong>{dnf_difficulty_range}{dnf_terrain_range}</li>
        <li><strong>失败后的恢复：</strong>{recovery_difficulty_range}总体唯一缓存首 DNF 的 30 天内 Found 为 {_fmt_percent(recovery.get('recoveredWithin30Rate'))}，其中同日 Found {_fmt_percent(recovery.get('sameDayFoundRate'))}；更可解释为失败后恢复的 1–30 天 Found 为 {_fmt_percent(recovery.get('recoveredDays1To30Rate'))}。</li>
        <li><strong>渠道敏感性：</strong>{period_insight or '当前没有足够的时期对照数据。'}</li>
        <li><strong>属性解释力有限：</strong>常见 D/T 区间的首日结果与跨日恢复差异较小；缓存类型又高度集中于 Traditional。现有数据更支持把渠道流量质量、首次活动日体验与后续推荐流程作为主要诊断层，把缓存属性作为次级分层。</li>
        <li><strong>结构性限制：</strong>{leading_type_text}类型之间的表面差异容易被样本集中与使用场景混杂。D/T、类型和容器值适合用于推荐规则分层与实验假设，不应直接当作用户质量标签。</li>
      </ol></section>
      <h3>首次活动日的 Found、失败、深度与 EM3</h3>
      <p>Found 和仅 DNF 均以首次活动日为准；同日 Found+DNF 单独保留在 JSON 中，表中“首日仅 DNF 无 Found”不会重复计入。E30 与 EM3 均按首次活动日锚定，并使用完整成熟窗口。</p>
      {engagement_tables}
      <h3>首次 DNF 后 30 天恢复</h3>
      <p>只纳入注册后前 30 天发生、且不晚于首次 Found 的首个 DNF。恢复包括同日 Found，但同日先后顺序未知；恢复缓存是否相同只在缓存代码可比较时计算。</p>
      {recovery_tables}
      <h3>2025 视频窗口敏感性对照</h3>
      <p>比较 2025 年 8 月 6–13 日高播放视频合并窗口与 5–7 月、9–11 月对照期。该对照检验全时期属性差异是否可能由渠道构成推动；它不是随机实验。</p>
      <div class="table-wrap"><table><thead><tr><th>时期</th><th>唯一缓存首次活动</th><th>首日Found</th><th>首日仅DNF</th><th>E30达10找</th><th>EM3活跃</th><th>恢复成熟</th><th>30天内含同日Found</th><th>同日Found</th><th>1–30天后续恢复</th></tr></thead><tbody>{period_rows()}</tbody></table></div>
      <p class="caption">“首日 Found”与“首日仅 DNF”按首次活动日判定；E30 达10找累计首次活动日起第 0–29 天 Found it，EM3 活跃观察第 90–119 天的 Found it、DNF 或 Attended。“30天内含同日 Found”包含首 DNF 当日，“同日 Found”与“1–30天后续恢复”互斥，后者只统计首 DNF 后第 1–30 天出现 Found it。恢复指标以恢复窗口成熟用户为分母。D/T 分别是缓存难度与地形，0 值及 1–5 之外数值归为未知；少于 30 人的类别合并为“其他”，所有差异均为观察性关联。</p>
    </section>
    """


def _data_acquisition_plan_html() -> str:
    sections = []
    for availability in ("现有数据库可直接聚合", "需要新增采集或实验埋点"):
        rows = []
        for item in DATA_ACQUISITION_PRIORITIES:
            if item["availability"] != availability:
                continue
            rows.append(
                f"<tr><td>{html.escape(item['priority'])}</td>"
                f"<td>{html.escape(item['topic'])}</td>"
                f"<td>{html.escape(item['data'])}</td>"
                f"<td>{html.escape(item['use'])}</td></tr>"
            )
        sections.append(
            f"<h3>{html.escape(availability)}</h3>"
            f"<div class=\"table-wrap\"><table><thead><tr><th>优先级</th>"
            f"<th>方向</th><th>所需数据</th><th>预期用途</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
    return f"""
    <section class="insights">
      <h2>新增数据获取优先级</h2>
      <p>先使用现有数据库可直接形成的聚合指标；需要新增埋点的项目用于后续验证因果和渠道价值。当前快照字段不能替代历史时点记录。</p>
      {''.join(sections)}
    </section>
    """


def _cadence_analysis_html(analysis: dict[str, Any] | None) -> str:
    if not analysis:
        return ""
    period_rows = []
    for item in analysis.get("periodTrends", []):
        period_rows.append(
            f"<tr><td>{html.escape(str(item.get('period') or ''))}</td>"
            f"<td>{_fmt_int(item.get('eligibleD30'))}</td>"
            f"<td>{_fmt_percent(item.get('finderRate'))}</td>"
            f"<td>{_fmt_percent(item.get('multiDayShareAmongFinders'))}</td>"
            f"<td>{_fmt_percent(item.get('fourPlusDayShareAmongFinders'))}</td>"
            f"<td>{_fmt_percent(item.get('finds3PlusRate'))}</td>"
            f"<td>{_fmt_percent(item.get('finds10PlusRate'))}</td></tr>"
        )

    cadence_rows = []
    cadence_lookup = {}
    for item in analysis.get("recentDepthCadence", []):
        cadence_lookup[(item.get("depthKey"), item.get("cadenceKey"))] = item
        cadence_rows.append(
            f"<tr><td>{html.escape(str(item.get('depthLabel') or ''))}</td>"
            f"<td>{html.escape(str(item.get('cadenceLabel') or ''))}</td>"
            f"<td>{_fmt_int(item.get('users'))}</td>"
            f"<td>{_fmt_percent(item.get('shareWithinDepth'))}</td>"
            f"<td>{_fmt_percent(item.get('activeM3Rate'))} (n={_fmt_int(item.get('eligibleM3'))})</td>"
            f"<td>{_fmt_percent(item.get('activeM6Rate'))} (n={_fmt_int(item.get('eligibleM6'))})</td>"
            f"<td>{_fmt_percent(item.get('activeM12Rate'))} (n={_fmt_int(item.get('eligibleM12'))})</td></tr>"
        )

    comparisons = []
    for depth_key, label in (("finds_1_2", "1–2 找"), ("finds_3_9", "3–9 找")):
        single = cadence_lookup.get((depth_key, "single_day"), {})
        multi = cadence_lookup.get((depth_key, "multi_day"), {})
        if single.get("activeM3Rate") is None or multi.get("activeM3Rate") is None:
            continue
        comparisons.append(
            f"{label}用户的 M3 活跃从单日找寻的 {_fmt_percent(single.get('activeM3Rate'))} "
            f"提高到多日找寻的 {_fmt_percent(multi.get('activeM3Rate'))}"
        )
    comparison_text = "；".join(comparisons) or "当前样本不足以稳定比较单日和多日找寻。"
    return f"""
    <section class="insights">
      <h2>多日找寻节奏与后续活跃</h2>
      <div class="insight-callout"><strong>稳定信号：</strong>在 2024 年以来的 China 用户中控制 D30 找寻深度后，{comparison_text}。该差异在多个历史时期重复出现，说明“是否跨到第二个找寻日”比单纯增加同一天的找寻次数更适合作为引导目标。</div>
      <h3>China 用户的 D30 找寻节奏趋势</h3>
      <div class="table-wrap"><table><thead><tr><th>注册时期</th><th>D30 成熟用户</th><th>D30 有 Found</th><th>找到用户中 2+ 找寻日</th><th>找到用户中 4+ 找寻日</th><th>D30 达3找</th><th>D30 达10找</th></tr></thead><tbody>{''.join(period_rows)}</tbody></table></div>
      <p class="caption">每一行按注册日期分期；D30 成熟用户已经完整经过注册后第 0–29 天。多日占比只以 D30 内至少有 1 条 Found it 的用户为分母。</p>
      <h3>控制找寻深度后的活跃差异（2024 年以来）</h3>
      <div class="table-wrap"><table><thead><tr><th>D30 找寻深度</th><th>D30 找寻节奏</th><th>用户数</th><th>深度组内占比</th><th>M3 活跃</th><th>M6 活跃</th><th>M12 活跃</th></tr></thead><tbody>{''.join(cadence_rows)}</tbody></table></div>
      <p class="caption">找寻日是第 0–29 天内至少有一条 Found it 的不同自然日；同一天多条 Found it 只算 1 个找寻日。1 个找寻日与 2+ 个找寻日互斥。M3/M6/M12 分别观察注册后第 90–119、180–209、360–389 天的 Found it、DNF 或 Attended，括号内 n 为对应成熟分母。结果为观察性关联，跨多日参与也可能反映用户原始兴趣更强。</p>
    </section>
    """


def _cache_freshness_analysis_html(analysis: dict[str, Any] | None) -> str:
    if not analysis:
        return ""
    freshness = analysis.get("freshnessBands") or []
    by_key = {str(item.get("key")): item for item in freshness}
    recent = by_key.get("within_30", {})
    stale = by_key.get("over_180", {})
    callout = (
        f"首次活动缓存此前 30 天内有人 Found 时，首日成功率为 "
        f"{_fmt_percent(recent.get('firstDayFoundRate'))}；此前超过 180 天没有 "
        f"Found 时为 {_fmt_percent(stale.get('firstDayFoundRate'))}。"
        f"两组首日仅 DNF 分别为 {_fmt_percent(recent.get('firstDayDnfOnlyRate'))} "
        f"和 {_fmt_percent(stale.get('firstDayDnfOnlyRate'))}。"
    )

    rows = []
    for item in freshness:
        rows.append(
            f"<tr><td>{html.escape(str(item.get('label') or ''))}</td>"
            f"<td>{_fmt_int(item.get('users'))}</td>"
            f"<td>{_fmt_percent(item.get('firstDayFoundRate'))}</td>"
            f"<td>{_fmt_percent(item.get('firstDayDnfOnlyRate'))}</td>"
            f"<td>{_fmt_int(item.get('eligibleD30'))}</td>"
            f"<td>{_fmt_percent(item.get('d30FoundRate'))}</td>"
            f"<td>{_fmt_int(item.get('eligibleM3'))}</td>"
            f"<td>{_fmt_percent(item.get('activeM3Rate'))}</td></tr>"
        )

    age_by_key = {
        str(item.get("key")): item for item in analysis.get("cacheAgeBands") or []
    }
    young = age_by_key.get("under_1y", {})
    old = age_by_key.get("years_7_plus", {})
    age_check = (
        f"缓存年龄本身没有同样清晰的梯度：不足 1 年的缓存首日成功率为 "
        f"{_fmt_percent(young.get('firstDayFoundRate'))}（{_fmt_int(young.get('users'))} 人），"
        f"7 年及以上缓存为 {_fmt_percent(old.get('firstDayFoundRate'))}"
        f"（{_fmt_int(old.get('users'))} 人）。M3 活跃没有形成同样稳定的梯度，"
        f"因此该信号适合用于提高首次成功率，不能直接解释长期留存。"
    )
    return f"""
    <section class="insights">
      <h2>首次活动缓存的历史新鲜度</h2>
      <div class="insight-callout"><strong>明确趋势：</strong>{callout}近期真实 Found 记录比缓存年龄或城市缓存总量更适合作为新手推荐信号。</div>
      <div class="table-wrap"><table><thead><tr><th>活动前最近一次 Found</th><th>用户数</th><th>首日 Found</th><th>首日仅 DNF</th><th>D30 成熟</th><th>D30 有 Found</th><th>M3 成熟</th><th>M3 活跃</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
      <p>{age_check}</p>
      <div class="metric-note"><strong>指标说明：</strong><ul><li>范围是 2024 年以来、分析归属为 China、没有注册前找寻异常、且首次有效活动日只能归属到一个缓存的用户。</li><li>“此前 N 天有 Found”只使用首次活动日之前该缓存的历史 Found it 日志；不会使用活动日之后的日志，也不使用当前 <code>last_found_date</code> 或 <code>cache_status</code> 快照。</li><li>首日 Found 与首日仅 DNF 按首次活动日判定；后者要求当天有 DNF 且没有 Found it。D30 有 Found 统计注册日起第 0–29 天至少一条 Found it；M3 活跃观察注册后第 90–119 天至少一条 Found it、DNF 或 Attended。</li><li>“此前无可观察 Found”表示数据库中没有活动日前的 Found it，不等于缓存当时不可用。结果是观察性关联，仍可能受城市、用户意愿和缓存选择影响。</li></ul></div>
    </section>
    """


def build_html(
    cohorts: list[dict[str, Any]],
    quality: dict[str, Any],
    as_of: date,
    china_cohorts: list[dict[str, Any]] | None = None,
    focus_analysis: dict[str, Any] | None = None,
    growth_analysis: dict[str, Any] | None = None,
    pathway_analysis: dict[str, Any] | None = None,
    video_analysis: dict[str, Any] | None = None,
    city_analysis: dict[str, Any] | None = None,
    cache_attribute_analysis: dict[str, Any] | None = None,
    cadence_analysis: dict[str, Any] | None = None,
    cache_freshness_analysis: dict[str, Any] | None = None,
) -> str:
    valid_users = sum(row["validUsers"] for row in cohorts)
    excluded = sum(row["excludedPreRegistrationFind"] for row in cohorts)
    d90_eligible = sum(row["eligibleD90"] for row in cohorts)
    registration_known = int(quality.get("registration_known") or 0)
    scope_log_users = int(quality.get("scope_log_users") or 0)
    coverage = rate(registration_known, scope_log_users)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    charts = _analysis_charts(cohorts) + _retention_curve_section(cohorts)
    china_cohorts = cohorts if china_cohorts is None else china_cohorts
    china_charts = _analysis_charts(china_cohorts) + _retention_curve_section(china_cohorts)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Geo Data｜大中华区 Cohort 分析</title>
<style>
  :root {{ color-scheme: light; --ink:#18212f; --muted:#5a6575; --line:#d9e0e8; --panel:#f7f9fc; }}
  * {{ box-sizing:border-box; }} body {{ margin:0; background:#fff; color:var(--ink); font:15px/1.55 Arial,"Microsoft YaHei",sans-serif; }}
  main {{ max-width:1180px; margin:0 auto; padding:44px 28px 64px; }} h1 {{ font-size:30px; margin:0 0 4px; }} h2 {{ font-size:19px; margin:42px 0 12px; }} h3 {{ font-size:17px; margin:28px 0 10px; }} h4 {{ font-size:14px; margin:12px 0 7px; }} p {{ margin:6px 0; }} .muted,.caption {{ color:var(--muted); }}
  .meta {{ margin-top:20px; padding:14px 16px; border-left:4px solid #2563eb; background:var(--panel); }}
  .kpis {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-top:22px; }} .kpi {{ background:var(--panel); padding:16px; border:1px solid var(--line); }} .kpi b {{ display:block; font-size:25px; margin-top:5px; }} .kpi span {{ color:var(--muted); font-size:13px; }}
  .chart-section {{ margin-top:28px; }} svg {{ width:100%; height:auto; display:block; overflow:visible; }} .bar-chart-wrap {{ overflow-x:auto; }} .bar-chart-wrap svg.bar-chart {{ max-width:none; }} .frame {{ fill:#fff; stroke:var(--line); }} .grid {{ stroke:var(--line); stroke-width:1; }} .axis,.legend {{ fill:var(--muted); font-size:12px; }} .legend {{ fill:var(--ink); }} .caption {{ font-size:13px; }}
  .yearly-trend-chart-wrap {{ margin-top:20px; overflow-x:auto; }} .yearly-trend-chart-wrap h4 {{ margin-bottom:8px; }} .yearly-trend-chart {{ min-width:760px; }} .yearly-trend-chart .ytd-band {{ fill:#f1f5f9; }}
  .metric-note {{ margin:10px 0 6px; padding:10px 14px; background:#f8fafc; border-left:3px solid #94a3b8; color:#475569; font-size:13px; }} .metric-note ul {{ margin:5px 0 0; padding-left:20px; }} .metric-note li {{ margin:2px 0; }}
  .metric-glossary {{ margin:28px 0 34px; padding:20px; border:1px solid var(--line); background:#fff; }} .metric-glossary h2 {{ margin-top:0; }} .definition-alert {{ margin:0 0 14px; padding:13px 15px; background:#eff6ff; border-left:4px solid #2563eb; }} .glossary-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }} .glossary-grid>div {{ padding:12px 14px; background:var(--panel); border:1px solid var(--line); }} .glossary-grid p {{ margin:4px 0 0; color:var(--muted); font-size:13px; }}
  .table-wrap {{ overflow-x:auto; border:1px solid var(--line); }} table {{ width:100%; border-collapse:collapse; min-width:960px; font-size:13px; }} th,td {{ padding:9px 10px; border-bottom:1px solid var(--line); text-align:right; white-space:nowrap; }} th {{ background:var(--panel); color:var(--muted); font-weight:600; }} th:first-child,td:first-child {{ text-align:left; }}
  .insights {{ margin-top:38px; padding:24px; background:var(--panel); border:1px solid var(--line); }} .insights>h2 {{ margin-top:0; }} .insight-block {{ margin-top:26px; }} .insight-callout {{ padding:14px 16px; background:#fff; border-left:4px solid #0f766e; }} .insight-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; }} .compact-table {{ min-width:0; background:#fff; border:1px solid var(--line); }}
  .method {{ margin-top:40px; padding-top:18px; border-top:1px solid var(--line); }} ul {{ padding-left:20px; }}
  @media (max-width:760px) {{ main {{ padding:28px 16px 44px; }} h1 {{ font-size:25px; }} .kpis {{ grid-template-columns:repeat(2,1fr); }} .insights {{ padding:18px; }} .insight-grid,.glossary-grid {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body><main>
  <header><h1>Geo Data｜大中华区 Cohort 分析</h1><p class="muted">注册季度 cohort 的观察型分析看板</p></header>
  <div class="meta"><b>数据截止日：{as_of.isoformat()}</b><br>生成时间：{generated_at}。注册 cohort 从 2017 年 Q1 起；地理范围：大陆、香港、澳门、台湾；分析仅反映在该范围缓存日志中被观察到的用户活动。</div>
  <div class="kpis">
    <div class="kpi"><span>有效观察用户</span><b>{_fmt_int(valid_users)}</b></div>
    <div class="kpi"><span>注册日期覆盖率</span><b>{_fmt_percent(coverage)}</b></div>
    <div class="kpi"><span>D90 已成熟用户</span><b>{_fmt_int(d90_eligible)}</b></div>
    <div class="kpi"><span>注册前找寻异常排除</span><b>{_fmt_int(excluded)}</b></div>
  </div>
  {_insights_html(cohorts, china_cohorts, as_of, focus_analysis, growth_analysis, pathway_analysis, video_analysis)}
  {_data_acquisition_plan_html()}
  {_cadence_analysis_html(cadence_analysis)}
  {_cache_freshness_analysis_html(cache_freshness_analysis)}
  {_city_cohort_html(city_analysis)}
  {_cache_attribute_html(cache_attribute_analysis)}
  {charts}
  {_table(cohorts)}
  {_lifecycle_section(cohorts)}
  <section><h2>reg_place = China</h2><p>此处 China 是分析用归属，包括首个 <code>Found it</code> 位于 China 的用户，以及网站 Find 为 0 且数据库内 DNF 全部位于 China 的用户；它不代表注册地、居住地或国籍。活动指标仍只统计大中华区缓存日志。</p>{_china_kpis(china_cohorts, valid_users)}{china_charts}{_table(china_cohorts)}{_lifecycle_section(china_cohorts)}</section>
  <section class="method"><h2>口径与限制</h2><ul>
    <li>分母为注册日期已知、且曾在大中华区缓存日志中被观察到的用户；不是平台全部注册用户，也不代表用户居住地。</li>
    <li>首次找寻和 10 次找寻仅统计 <code>Found it</code>。D7、D30、D90 使用 [注册日，注册日 + N 天) 窗口。</li>
    <li>留存统计 <code>Found it</code>、<code>Didn't find it</code> 与 <code>Attended</code>；D30 为注册后第 30–59 天至少一次参与，D90 为第 90–119 天至少一次参与。</li>
    <li>截至数据日 30 日内活动：在数据截止日前 30 天内至少有一次有效参与的用户，占该季度有效观察用户的比例。</li>
    <li>首个 <code>Found it</code> 早于注册日期的用户不纳入主指标。未成熟观察窗口显示为“—”。</li>
  </ul></section>
</main></body></html>"""


def serialize(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Cannot serialize {type(value)!r}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the local Greater China cohort dashboard.")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today(), help="Data cutoff date, YYYY-MM-DD (default: today).")
    parser.add_argument("--cohort-start", type=date.fromisoformat, default=DEFAULT_COHORT_START, help="Earliest registration date to display, YYYY-MM-DD (default: 2017-01-01).")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Local directory for the standalone report.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    database_url = require_env("DATABASE_URL")
    rows, quality = fetch_rows(database_url, args.as_of, args.cohort_start)
    cohorts = summarize_cohorts(rows, args.as_of)
    china_cohorts = summarize_cohorts(filter_rows_by_reg_place(rows, "China"), args.as_of)
    focus_analysis = analyze_focus_quarter(rows, date(2025, 7, 1), args.as_of)
    growth_analysis = analyze_growth_and_onboarding(rows, args.as_of)
    pathway_analysis = analyze_early_pathways(rows, args.as_of)
    video_analysis = analyze_video_influx(rows, args.as_of)
    city_analysis = analyze_city_cohorts(rows, args.as_of)
    cache_attribute_analysis = analyze_cache_attributes(rows, args.as_of)
    cadence_analysis = analyze_engagement_cadence(rows, args.as_of)
    cache_freshness_analysis = analyze_cache_historical_freshness(rows, args.as_of)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "dataAsOf": args.as_of.isoformat(),
        "cohortStart": args.cohort_start.isoformat(),
        "scope": list(GREATER_CHINA_COUNTRIES),
        "quality": quality,
        "cohorts": cohorts,
        "chinaCohorts": china_cohorts,
        "focusAnalysis": focus_analysis,
        "growthAnalysis": growth_analysis,
        "earlyPathwayAnalysis": pathway_analysis,
        "videoInfluxAnalysis": video_analysis,
        "cityCohortAnalysis": city_analysis,
        "cacheAttributeAnalysis": cache_attribute_analysis,
        "engagementCadenceAnalysis": cadence_analysis,
        "cacheFreshnessAnalysis": cache_freshness_analysis,
    }
    (args.output_dir / "cohort-dashboard-data.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=serialize), encoding="utf-8"
    )
    (args.output_dir / "cohort-dashboard.html").write_text(
        build_html(
            cohorts, quality, args.as_of, china_cohorts, focus_analysis, growth_analysis,
            pathway_analysis, video_analysis, city_analysis, cache_attribute_analysis,
            cadence_analysis, cache_freshness_analysis,
        ), encoding="utf-8"
    )
    LOGGER.info("Created %s", args.output_dir / "cohort-dashboard.html")


if __name__ == "__main__":
    main()
