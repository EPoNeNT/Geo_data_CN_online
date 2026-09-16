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
    GROUP BY b.guid, b.reg_place, b.cohort_month, b.registration_date,
             fd.first_valid_dnf, fd.first_valid_dnf_cache,
             fd.first_valid_dnf_cache_count, fd.first_valid_dnf_difficulty,
             fd.first_valid_dnf_terrain, fd.first_valid_dnf_geocache_type,
             fd.first_valid_dnf_container_type,
             fel.first_engagement_date, fel.first_engagement_city,
             fel.first_engagement_cache_count, fel.first_engagement_cache,
             fel.first_engagement_difficulty, fel.first_engagement_terrain,
             fel.first_engagement_geocache_type, fel.first_engagement_container_type
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
        result[output] = rate(_sum_field(rows, numerator), _sum_field(rows, denominator))

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
        <h4>早期行为与后续活跃</h4>
        <p>{evidence}这些差异是筛选后的相关关系，既包含用户自身兴趣差异，也可能包含引导效果，不能直接证明因果。</p>
        <div class="table-wrap"><table><thead><tr><th>范围</th><th>早期行为</th><th>分组用户</th><th>D90 成熟用户</th><th>D90 活跃</th><th>M6 活跃</th><th>M12 活跃</th></tr></thead><tbody>{''.join(behavior_rows)}</tbody></table></div>
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
        <p class="caption">首找时机使用截至数据日观察到的首个 Found it；第 90 天后首找可能发生在 M3/M6/M12 观察窗口之后，因此只能用于描述晚激活，不能作为早期预测变量。</p>
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
        <p class="caption">漏斗只使用已经完整经过注册后30天的用户。Found、DNF或Attended构成“有行动”；后续1找、3找、5找、10找均只计算Found it，因此各阶段严格递减。</p>
        <h4>D30找寻深度与后续活跃</h4>
        <div class="table-wrap"><table><thead><tr><th>时期</th><th>D30找寻深度</th><th>用户数</th><th>占比</th><th>M3活跃</th><th>M6活跃</th><th>M12活跃</th></tr></thead><tbody>{''.join(depth_rows)}</tbody></table></div>
        <p class="caption">M3、M6、M12是独立观察窗口，不属于上述严格漏斗；括号内为已经完整经过相应窗口的成熟用户数。</p>
        <h4>首次DNF后的恢复路径</h4>
        <div class="insight-callout"><strong>恢复判断：</strong>2025视频窗口中，注册后30天内出现DNF且DNF早于或同日于首次Found的China用户有 {_fmt_int(event_recovery.get('newcomerFailureUsers'))} 人；其中30天内恢复成功 {_fmt_percent(event_recovery.get('recoveredWithin30Rate'))}，相邻月份为 {_fmt_percent(control_recovery.get('recoveredWithin30Rate'))}。已恢复且缓存代码可比较的用户中，{_fmt_percent(event_recovery.get('sameCacheRecoveryShare'))} 在首次DNF的同一缓存上留下Found，其余通过其他缓存恢复。</div>
        <div class="table-wrap"><table><thead><tr><th>时期</th><th>D30有DNF</th><th>DNF早于/同日首找</th><th>占DNF用户</th><th>先Found后DNF</th><th>30天恢复/成熟</th><th>30天恢复率</th><th>90天恢复率</th><th>同缓存恢复占比</th></tr></thead><tbody>{''.join(recovery_overview_rows)}</tbody></table></div>
        <div class="table-wrap"><table><thead><tr><th>时期</th><th>恢复时间</th><th>用户数</th><th>占新手失败用户</th><th>M3活跃</th><th>M6活跃</th><th>M12活跃</th></tr></thead><tbody>{''.join(recovery_bucket_rows)}</tbody></table></div>
        <p class="caption">“新手失败”限定为注册后30天内首次DNF早于首次Found，或与首次Found发生在同一天；由于日志只有日期，同日有Found（顺序未知），不能推断当天究竟先失败还是先成功。恢复指首次DNF日期当日或之后观察到Found it，仅覆盖大中华区日志。</p>
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
        <p class="caption">播放量为公开页面在 2026-09-10 至 2026-09-11 的快照，会继续变化。注册响应统计的是大中华区日志中可观察到、且注册日期有效的用户，不是 B 站点击到注册的直接转化。</p>
        <h4>注册规模与 China 构成</h4>
        <div class="table-wrap"><table><thead><tr><th>时期</th><th>全量用户</th><th>China 用户</th><th>China 占比</th></tr></thead><tbody>{volume_rows}</tbody></table></div>
        <h4>日级峰值</h4>
        <div class="table-wrap"><table><thead><tr><th>事件</th><th>注册日期</th><th>全量用户</th><th>China 用户</th><th>China 占比</th></tr></thead><tbody>{''.join(peak_rows)}</tbody></table></div>
        <h4>事件期间 China 用户行为</h4>
        <div class="table-wrap"><table><thead><tr><th>注册时期</th><th>China 用户</th><th>D2 首找</th><th>D7 首找</th><th>D30 首找</th><th>D30 达10找</th><th>D30 有DNF</th><th>找到+DNF</th><th>M3 活跃</th><th>M6 活跃</th><th>M12 活跃</th></tr></thead><tbody>{''.join(behavior_rows)}</tbody></table></div>
        <div class="insight-callout"><strong>行为特征：</strong>2025 两条高播放视频合并窗口内，China 用户的 D7、D30 首找率分别为 {_fmt_percent(wave1.get('firstFindD7Rate'))}、{_fmt_percent(wave1.get('firstFindD30Rate'))}，低于相邻六个月 China 用户的 {_fmt_percent(adjacent_behavior.get('firstFindD7Rate'))}、{_fmt_percent(adjacent_behavior.get('firstFindD30Rate'))}；D30 达10找仅 {_fmt_percent(wave1.get('finds10D30Rate'))}，同时 D30 有 DNF 达 {_fmt_percent(wave1.get('dnfD30Rate'))}，高于相邻月份的 {_fmt_percent(adjacent_behavior.get('dnfD30Rate'))}。更新 DNF-only 地域归属后，结论更清楚：视频显著扩大了入口，但吸引了大量尚未完成首次成功、深度转化较弱的新手。</div>
        {_video_conversion_and_dnf_html(periods)}
        <p>首次找到并未集中在视频中的少数目标：2023立即响应组覆盖 {_fmt_int(spread_2023.get('uniqueCaches'))} 个不同缓存和 {_fmt_int(spread_2023.get('uniqueCities'))} 个城市，单一缓存最多仅 {_fmt_int(spread_2023.get('maxSameCacheUsers'))} 人；2025两条高播放视频合并窗口覆盖 {_fmt_int(spread_wave1.get('uniqueCaches'))} 个缓存和 {_fmt_int(spread_wave1.get('uniqueCities'))} 个城市，8月26日低播放相关视频后的窗口覆盖 {_fmt_int(spread_wave2.get('uniqueCaches'))} 个缓存和 {_fmt_int(spread_wave2.get('uniqueCities'))} 个城市。这更支持视频激发了全国范围的玩法尝试，而不只是观众集中寻找视频展示的宝藏。</p>
        <p>留存判断以 reg_place=China 为主，降低境外用户仅在旅行期间留下大中华区日志造成的误判。2023 立即响应组的 China 样本为 {_fmt_int(wave_2023.get('chinaUsers'))} 人，其 M12 活跃率为 {_fmt_percent((wave_2023.get('chinaBehavior') or {}).get('activeM12Rate'))}；2025 第一轮为 {_fmt_percent(wave1.get('activeM12Rate'))}。这表明视频可以显著扩大入口，但长期价值取决于能否把首找用户继续推进到多次、跨日找寻。</p>
        <p class="caption">这是一项自然事件对照：时间响应、量级异常和 China 构成同时支持“视频是重要来源”的解释，但不能单凭时间重合证明因果，也无法排除同期传播、其他平台转发或数据覆盖变化。较低播放量的后续视频没有形成与8月6日同量级的独立注册峰值。</p>
      </section>
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

    available_years = sorted({_cohort_year(row) for row in cohorts})
    comparison_years = [year for year in (2017, 2024, 2025) if year in available_years]
    mature_rows = [
        row for row in cohorts
        if row.get("validUsers") and row.get("lifecycleEligible") == row.get("validUsers")
    ]
    latest_mature = mature_rows[-1] if mature_rows else None

    trend_rows = []
    trend_summaries: list[tuple[str, dict[str, Any]]] = []
    for year in comparison_years:
        trend_summaries.append((str(year), _year_summary(cohorts, year)))
    if latest_mature and str(latest_mature["cohortQuarter"]) not in {label for label, _ in trend_summaries}:
        trend_summaries.append(
            (str(latest_mature["cohortQuarter"]), _weighted_insight_summary([latest_mature]))
        )
    for label, summary in trend_summaries:
        trend_rows.append(
            f"<tr><td>{html.escape(label)}</td><td>{_fmt_int(summary['validUsers'])}</td>"
            f"{_metric_cells(summary)}</tr>"
        )

    narrative = []
    if len(trend_summaries) >= 2:
        first_label, first = trend_summaries[0]
        last_label, last = trend_summaries[-1]
        narrative.append(
            f"从 {html.escape(first_label)} 到 {html.escape(last_label)}，D7 首找由 "
            f"{_fmt_percent(first['firstFindD7Rate'])} 升至 {_fmt_percent(last['firstFindD7Rate'])}，"
            f"D90 首找由 {_fmt_percent(first['firstFindD90Rate'])} 升至 {_fmt_percent(last['firstFindD90Rate'])}，"
            f"D90 达 10 找由 {_fmt_percent(first['finds10D90Rate'])} 升至 {_fmt_percent(last['finds10D90Rate'])}。"
        )
        if last.get("deepConditionalD90") is not None and last.get("lightConditionalD90") is not None:
            ratio = (
                last["deepConditionalD90"] / last["lightConditionalD90"]
                if last["lightConditionalD90"] else None
            )
            ratio_text = f"，约为 1–9 找用户的 {ratio:.1f} 倍" if ratio is not None else ""
            narrative.append(
                f"{html.escape(last_label)} 的生命周期中，1–9 找占 {_fmt_percent(last['lightShare'])}，"
                f"10+ 找占 {_fmt_percent(last['deepShare'])}；10+ 找用户的条件 D90 活跃率为 "
                f"{_fmt_percent(last['deepConditionalD90'])}{ratio_text}。"
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
      <section class="insight-block"><h3>激活、找寻深度与留存趋势</h3>
        <div class="table-wrap"><table><thead><tr><th>时期</th><th>有效用户</th><th>D7 首找</th><th>D30 首找</th><th>D90 首找</th><th>D90 达10找</th><th>D30 留存</th><th>D90 留存</th></tr></thead><tbody>{''.join(trend_rows)}</tbody></table></div>
      </section>
      <section class="insight-block"><h3>生命周期结构与条件 D90 活跃</h3>
        <div class="table-wrap"><table><thead><tr><th>时期</th><th>成熟用户</th><th>0找占比</th><th>1–9找占比</th><th>10+找占比</th><th>0找后D90活跃</th><th>1–9找后D90活跃</th><th>10+找后D90活跃</th></tr></thead><tbody>{''.join(lifecycle_rows)}</tbody></table></div>
        <p>“0 找后 D90 活跃”可能表示晚激活、仅 DNF 或仅 Attended；它不等于从未参与。10+ 找与后续活跃高度相关，但不能据此认定达到 10 找会因果性地提高留存。</p>
      </section>
      <section class="insight-block"><h3>固定窗口留存洞察</h3>
        <p>{retention_narrative}</p>
        <div class="table-wrap"><table><thead><tr><th>群体</th><th>时期</th><th>M1</th><th>M2</th><th>M3</th><th>M6</th><th>M12</th></tr></thead><tbody>{''.join(retention_rows)}</tbody></table></div>
        <p class="caption">M1、M2、M3、M6、M12 是独立的 30 天窗口，不要求连续活跃。较晚窗口高于较早窗口可以反映回流，但各窗口只使用已完整成熟的用户，尤其近期 cohort 的 M6/M12 分母更小，不能把曲线直接解释为同一批用户逐月流失。</p>
      </section>
      <section class="insight-block"><h3>China 与非 China 对照</h3>
        <div class="table-wrap"><table><thead><tr><th>群体</th><th>有效用户</th><th>D7 首找</th><th>D30 首找</th><th>D90 首找</th><th>D90 达10找</th><th>D30 留存</th><th>D90 留存</th></tr></thead><tbody>{''.join(comparison_rows)}</tbody></table></div>
        <p class="caption">reg_place 是分析用地域归属：通常取用户首次 Found it 缓存所在国家；网站 Find 为 0 且数据库内 DNF 全部在大中华区同一地区时也归入该地区。它不代表注册地、居住地或国籍。</p>
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
      <h3>注册日起算敏感性对照</h3>
      <p>下表保留原有注册日起算口径，主要用于检查“从注册到首次活动的等待时间”是否改变城市排序。它不应单独用于判断城市造成了激活差异。</p>
      <div class="table-wrap"><table><thead><tr><th>首次活动城市</th><th>D30成熟</th><th>D30首找</th><th>D30达10找</th><th>M3成熟</th><th>M3活跃</th><th>M6活跃</th><th>M12活跃</th></tr></thead><tbody>{registration_rows}</tbody></table></div>
      <h3>注册年份稳健性</h3>
      <p>对全时期用户数最多的 6 个明确城市按注册年份拆分。若某个差异只在单一年份出现，或成熟人数很小，不应视为稳定城市特征。</p>
      <div class="table-wrap"><table><thead><tr><th>注册年份</th><th>首次活动城市</th><th>用户</th><th>E30成熟</th><th>E30有Found</th><th>E30达10找</th><th>EM3成熟</th><th>EM3活跃</th></tr></thead><tbody>{year_rows}</tbody></table></div>
      <h3>2025 视频窗口城市构成</h3>
      <p>比较 2025 年 8 月 6–13 日高播放视频合并窗口与 5–7 月、9–11 月对照期。该表用于判断全时期城市差异是否被特殊流量窗口改变，不能把组间差异解释为视频的独立因果效果。</p>
      <div class="table-wrap"><table><thead><tr><th>时期</th><th>首次活动城市</th><th>用户</th><th>时期内占比</th><th>首日仅DNF无Found</th><th>E30有Found</th><th>E30达10找</th><th>EM3活跃</th></tr></thead><tbody>{event_rows}</tbody></table></div>
      <p class="caption">表格展示用户数最多的 {int(city_analysis.get('topN') or CITY_COHORT_TOP_N)} 个明确城市，其余合并为“其他城市”。城市字段缺失单列为“未知城市”；注册后没有有效活动者单列；同一最早活动日涉及多个城市时列为“同日多城市”，不按任意日志顺序归属。所有比例都使用对应完整观察窗口的成熟用户作为分母。</p>
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
        for key, title in dimension_specs
    )
    recovery_tables = "".join(
        f"<h3>首次 DNF 后恢复：{title}</h3>"
        '<div class="table-wrap"><table><thead><tr><th>属性组</th><th>失败用户</th><th>30天成熟</th><th>30天内含同日Found</th><th>同日Found</th><th>1–30天后续恢复</th><th>后续恢复中位天数</th><th>缓存可比较恢复</th><th>原缓存恢复占比</th></tr></thead>'
        f"<tbody>{recovery_rows(recovery_dimensions.get(key) or [])}</tbody></table></div>"
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
      <p class="caption">D/T 的 0 值与 1–5 之外数值归为未知。缓存类型与容器类型中少于 30 人的类别合并为“其他”；容器类型因当前数据没有稳定的人类可读映射，保留数据库原始编码。所有差异均为观察性关联。</p>
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
  .table-wrap {{ overflow-x:auto; border:1px solid var(--line); }} table {{ width:100%; border-collapse:collapse; min-width:960px; font-size:13px; }} th,td {{ padding:9px 10px; border-bottom:1px solid var(--line); text-align:right; white-space:nowrap; }} th {{ background:var(--panel); color:var(--muted); font-weight:600; }} th:first-child,td:first-child {{ text-align:left; }}
  .insights {{ margin-top:38px; padding:24px; background:var(--panel); border:1px solid var(--line); }} .insights>h2 {{ margin-top:0; }} .insight-block {{ margin-top:26px; }} .insight-callout {{ padding:14px 16px; background:#fff; border-left:4px solid #0f766e; }} .insight-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; }} .compact-table {{ min-width:0; background:#fff; border:1px solid var(--line); }}
  .method {{ margin-top:40px; padding-top:18px; border-top:1px solid var(--line); }} ul {{ padding-left:20px; }}
  @media (max-width:760px) {{ main {{ padding:28px 16px 44px; }} h1 {{ font-size:25px; }} .kpis {{ grid-template-columns:repeat(2,1fr); }} .insights {{ padding:18px; }} .insight-grid {{ grid-template-columns:1fr; }} }}
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
    }
    (args.output_dir / "cohort-dashboard-data.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=serialize), encoding="utf-8"
    )
    (args.output_dir / "cohort-dashboard.html").write_text(
        build_html(
            cohorts, quality, args.as_of, china_cohorts, focus_analysis, growth_analysis,
            pathway_analysis, video_analysis, city_analysis, cache_attribute_analysis,
        ), encoding="utf-8"
    )
    LOGGER.info("Created %s", args.output_dir / "cohort-dashboard.html")


if __name__ == "__main__":
    main()
