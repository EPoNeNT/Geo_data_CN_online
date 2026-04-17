#!/usr/bin/env python3
"""
在 Neon/Postgres 中按 createCity.py 同逻辑更新 caches.city。

说明：
1. 本脚本不读取任何本地城市边界文件，适合在 GitHub Actions 中长期运行。
2. 城市边界数据需提前一次性导入 map_cities（见 seed_map_cities_once.py）。
"""

import psycopg2

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from runtime_utils import require_env, setup_logging


logger = setup_logging("assign_cities.log")

TAIWAN_COUNTY_NAMES_SQL = ", ".join(
    [
        "'連江縣'",
        "'金門縣'",
        "'宜蘭縣'",
        "'彰化縣'",
        "'南投縣'",
        "'雲林縣'",
        "'屏東縣'",
        "'臺東縣'",
        "'花蓮縣'",
        "'澎湖縣'",
        "'基隆市'",
        "'新竹市'",
        "'臺北市'",
        "'新北市'",
        "'臺中市'",
        "'臺南市'",
        "'桃園市'",
        "'苗栗縣'",
        "'新竹縣'",
        "'嘉義市'",
        "'嘉義縣'",
        "'高雄市'",
    ]
)

CITY_UPDATE_SQL = f"""
UPDATE caches AS p
SET city = CASE
    WHEN p.country = 'Hong Kong' THEN '香港'
    WHEN p.country = 'Macao' THEN '澳门'
    ELSE (
        SELECT m.name AS display_city_name
        FROM map_cities AS m
        WHERE (
              p.country = 'Taiwan'
              AND m.name IN ({TAIWAN_COUNTY_NAMES_SQL})
          )
          OR (
              p.country IS DISTINCT FROM 'Taiwan'
              AND m.name NOT IN ({TAIWAN_COUNTY_NAMES_SQL})
          )
        ORDER BY
            m.geom <-> ST_SetSRID(ST_MakePoint(p.longitude, p.latitude), 4326),
            ST_Distance(m.geom, ST_SetSRID(ST_MakePoint(p.longitude, p.latitude), 4326))
        LIMIT 1
    )
END
WHERE (
      p.city IS NULL
      OR p.country = 'Taiwan'
  )
  AND (
      p.country IN ('Hong Kong', 'Macao', 'Taiwan')
      OR (p.longitude IS NOT NULL AND p.latitude IS NOT NULL)
  )
  AND p.longitude IS NOT NULL
  AND p.latitude IS NOT NULL;
"""


def connect_db(database_url: str):
    return psycopg2.connect(
        database_url,
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )


def ensure_schema(cursor) -> None:
    cursor.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
    cursor.execute("ALTER TABLE caches ADD COLUMN IF NOT EXISTS city TEXT;")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS map_cities (
            name TEXT NOT NULL,
            geom geometry(MultiPolygon, 4326) NOT NULL
        );
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_map_cities_geom ON map_cities USING GIST (geom);")


def ensure_boundaries_exist(cursor) -> None:
    cursor.execute("SELECT COUNT(*) FROM map_cities;")
    count = int(cursor.fetchone()[0])
    if count <= 0:
        raise RuntimeError(
            "map_cities 为空。请先一次性运行 seed_map_cities_once.py 将城市边界导入 Neon。"
        )


def update_cities(cursor) -> int:
    cursor.execute(CITY_UPDATE_SQL)
    return int(cursor.rowcount or 0)


def main() -> None:
    database_url = require_env("DATABASE_URL")
    conn = connect_db(database_url)

    try:
        with conn.cursor() as cursor:
            ensure_schema(cursor)
            ensure_boundaries_exist(cursor)
            updated = update_cities(cursor)
        conn.commit()
        logger.info("城市划分完成，更新了 %s 条 caches.city 记录", updated)
    except Exception:
        conn.rollback()
        logger.exception("城市划分失败")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
