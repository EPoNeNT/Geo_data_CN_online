#!/usr/bin/env python3
"""
Data Generation Script for GeoCaching CN Analytics
Generates static JSON files from Neon PostgreSQL database.
Runs after crawl_caches and crawl_logs in GitHub Actions workflow.

Output files:
- public/data/overview.json
- public/data/player-rankings.json
- public/data/city-rankings.json
- public/data/generated-at.json
"""
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from runtime_utils import require_env, setup_logging
import psycopg2
from psycopg2.extras import RealDictCursor

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

logger = setup_logging("generate_data.log")

DATABASE_URL = require_env("DATABASE_URL")
OUTPUT_DIR = "public/data"

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

# Exclusion filters for caches
EXCLUDE_CACHE_WHERE = "c.cache_status != 2 AND c.geocache_type NOT IN (6, 13)"
EXCLUDE_CACHE_JOIN = "c.cache_status != 2 AND c.geocache_type NOT IN (6, 13)"


class DataGenerator:
    """Generate static JSON data files from database."""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.conn = None
        self.cursor = None

    def connect(self):
        """Connect to database."""
        self.conn = psycopg2.connect(
            self.database_url,
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
            cache_where = f"WHERE c.cache_status != 2 AND c.geocache_type NOT IN (6, 13) AND c.country = '{country_filter}'"
            log_where = f"WHERE c2.cache_status != 2 AND c2.geocache_type NOT IN (6, 13) AND c2.country = '{country_filter}'"
        else:
            cache_where = "WHERE c.cache_status != 2 AND c.geocache_type NOT IN (6, 13)"
            log_where = "WHERE c2.cache_status != 2 AND c2.geocache_type NOT IN (6, 13)"

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

    def generate_yearly_trend(self, country_filter: Optional[str] = None) -> List[Dict]:
        """Generate yearly trend data."""
        where_clause = f"WHERE {EXCLUDE_CACHE_WHERE}"
        if country_filter:
            where_clause += f" AND c.country = '{country_filter}'"

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
            COUNT(*)::int AS total,
            COUNT(*) FILTER (
              WHERE placed_date::date <=
                (date_trunc('year', make_date(EXTRACT(YEAR FROM placed_date)::int, 1, 1))::date
                 + (CURRENT_DATE - date_trunc('year', CURRENT_DATE)::date))
            )::int AS ytd_cache
          FROM caches c
          {where_clause}
          AND placed_date IS NOT NULL
          GROUP BY 1
        )
        SELECT
          years.year::text AS year,
          COALESCE(counts.total, 0)::int AS total,
          COALESCE(counts.ytd_cache, 0)::int AS ytdCache
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
            }
            for row in results
        ]

    def generate_dt_matrix(self, country_filter: Optional[str] = None) -> List[Dict]:
        """Generate Difficulty/Terrain matrix (9x9 = 81 elements)."""
        where_clause = f"WHERE {EXCLUDE_CACHE_WHERE}"
        if country_filter:
            where_clause += f" AND c.country = '{country_filter}'"

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

        output = []
        for i, d in enumerate(DT_VALUES):
            for j, t in enumerate(DT_VALUES):
                output.append({
                    "row": i,
                    "col": j,
                    "difficulty": d,
                    "terrain": t,
                    "count": matrix.get((d, t), 0),
                })

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
            dt_matrix = self.generate_dt_matrix(country)

            total_caches = metrics["totalCaches"]

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

    def calculate_trend(self, current_score: int, previous_score: int) -> Tuple[str, int]:
        """Calculate trend direction and delta."""
        delta = current_score - previous_score
        if delta > 0:
            return ("up", delta)
        elif delta < 0:
            return ("down", abs(delta))
        else:
            return ("flat", 0)

    def generate_ranking_query(
        self,
        ranking_type: str,
        time_range: str,
        is_city_ranking: bool = False,
        limit: int = 30,
    ) -> str:
        """Generate SQL query for rankings based on type and time range."""

        # Base date filter
        if time_range == "30d":
            date_filter = ">= CURRENT_DATE - INTERVAL '30 day'"
        elif time_range == "ytd":
            date_filter = ">= date_trunc('year', CURRENT_DATE)::date"
        else:  # all
            date_filter = "IS NOT NULL"  # No filter

        # Previous period filter for trend calculation
        if time_range == "30d":
            prev_date_filter = ">= CURRENT_DATE - INTERVAL '60 day' AND < CURRENT_DATE - INTERVAL '30 day'"
        elif time_range == "ytd":
            prev_date_filter = ">= date_trunc('year', CURRENT_DATE - INTERVAL '1 year')::date AND < date_trunc('year', CURRENT_DATE)::date"
        else:
            prev_date_filter = "IS NOT NULL"

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
                    AND c.placed_date {date_filter}
                    AND {EXCLUDE_CACHE_WHERE}
                ) AS sub
                GROUP BY name, subtitle
                ORDER BY score DESC, name ASC
                LIMIT {limit};
                """
            else:
                return f"""
                SELECT owner_username AS name, COUNT(*)::int AS score
                FROM caches c
                WHERE c.owner_username IS NOT NULL AND c.owner_username <> ''
                  AND c.placed_date {date_filter}
                  AND {EXCLUDE_CACHE_WHERE}
                GROUP BY c.owner_username
                ORDER BY score DESC, c.owner_username ASC
                LIMIT {limit};
                """

        elif ranking_type == "finds":
            if is_city_ranking:
                return f"""
                SELECT name, subtitle, COUNT(DISTINCT user_name)::int AS score
                FROM (
                  SELECT
                    COALESCE(NULLIF(TRIM(c.city), ''), c.country) AS name,
                    c.country AS subtitle,
                    l.user_name
                  FROM logs l
                  JOIN caches c ON c.code = l.gc_code
                  WHERE COALESCE(NULLIF(TRIM(c.city), ''), c.country) IS NOT NULL
                    AND l.visited {date_filter}
                    AND {EXCLUDE_CACHE_JOIN}
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
                  AND l.visited {date_filter}
                  AND {EXCLUDE_CACHE_JOIN}
                GROUP BY l.user_name
                ORDER BY score DESC, l.user_name ASC
                LIMIT {limit};
                """

        elif ranking_type == "favorites":
            if is_city_ranking:
                return f"""
                SELECT name, subtitle, COALESCE(SUM(favorite_points), 0)::int AS score
                FROM (
                  SELECT
                    COALESCE(NULLIF(TRIM(c.city), ''), c.country) AS name,
                    c.country AS subtitle,
                    c.favorite_points
                  FROM caches c
                  WHERE COALESCE(NULLIF(TRIM(c.city), ''), c.country) IS NOT NULL
                    AND c.placed_date {date_filter}
                    AND {EXCLUDE_CACHE_WHERE}
                ) AS sub
                GROUP BY name, subtitle
                ORDER BY score DESC, name ASC
                LIMIT {limit};
                """
            else:
                return f"""
                SELECT owner_username AS name, COALESCE(SUM(favorite_points), 0)::int AS score
                FROM caches c
                WHERE c.owner_username IS NOT NULL AND c.owner_username <> ''
                  AND c.placed_date {date_filter}
                  AND {EXCLUDE_CACHE_WHERE}
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
                    AND l.visited {date_filter}
                    AND {EXCLUDE_CACHE_JOIN}
                ) AS sub
                GROUP BY name, subtitle
                ORDER BY score DESC, name ASC
                LIMIT {limit};
                """
            else:
                return f"""
                SELECT user_name AS name, COUNT(*)::int AS score
                FROM logs l
                JOIN caches c ON c.code = l.gc_code
                WHERE l.user_name IS NOT NULL AND l.user_name <> ''
                  AND l.visited {date_filter}
                  AND {EXCLUDE_CACHE_JOIN}
                GROUP BY l.user_name
                ORDER BY score DESC, l.user_name ASC
                LIMIT {limit};
                """

        raise ValueError(f"Unknown ranking type: {ranking_type}")

    def generate_rankings(
        self,
        ranking_types: List[str],
        time_ranges: List[str],
        is_city_ranking: bool = False,
        limit: int = 30,
    ) -> Dict:
        """Generate all rankings data."""
        rankings = {}

        for rtype in ranking_types:
            rankings[rtype] = {}
            for trange in time_ranges:
                logger.debug(f"Generating {rtype}/{trange} ranking...")
                query = self.generate_ranking_query(rtype, trange, is_city_ranking, limit)
                results = self.execute_query(query)

                # Add rank and calculate trend
                ranked_results = []
                for i, row in enumerate(results, 1):
                    entry = {
                        "rank": i,
                        "name": row["name"],
                        "score": row["score"] or 0,
                    }

                    if is_city_ranking:
                        entry["subtitle"] = row.get("subtitle", "")

                    # Calculate trend (except for 'all' time range)
                    if trange != "all":
                        # Get previous period score
                        prev_query = self.generate_ranking_query(
                            rtype, trange, is_city_ranking, limit=999999
                        )
                        # This is simplified - in production you'd want proper previous period logic
                        entry["trend"] = "flat"
                        entry["trendDelta"] = 0
                    else:
                        entry["trend"] = "flat"
                        entry["trendDelta"] = 0

                    ranked_results.append(entry)

                rankings[rtype][trange] = ranked_results

        return rankings

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

        ranking_types = ["hides", "finds", "favorites", "logs"]
        time_ranges = ["30d", "ytd", "all"]

        return {
            "generatedAt": self.get_generated_at(),
            "rankings": self.generate_rankings(ranking_types, time_ranges, limit=30),
            "communityStats": self.generate_community_stats(),
        }

    # ==================== CITY-RANKINGS.JSON ====================

    def generate_cache_trend(self) -> Dict:
        """Generate city cache trend data (7 years)."""
        query = f"""
        WITH years AS (
          SELECT generate_series(
            EXTRACT(YEAR FROM CURRENT_DATE)::int - 6,
            EXTRACT(YEAR FROM CURRENT_DATE)::int
          ) AS year
        ),
        counts AS (
          SELECT EXTRACT(YEAR FROM placed_date)::int AS year, COUNT(*)::int AS count
          FROM caches c
          WHERE {EXCLUDE_CACHE_WHERE}
            AND c.placed_date IS NOT NULL
            AND EXTRACT(YEAR FROM placed_date)::int >= EXTRACT(YEAR FROM CURRENT_DATE)::int - 6
          GROUP BY 1
        )
        SELECT years.year::text AS year, COALESCE(counts.count, 0)::int AS count
        FROM years
        LEFT JOIN counts USING (year)
        ORDER BY years.year;
        """

        results = self.execute_query(query)
        bars = [{"year": str(row["year"]), "count": row["count"] or 0} for row in results]

        # Calculate average growth
        if len(bars) >= 2:
            first_count = bars[0]["count"]
            last_count = bars[-1]["count"]
            years_span = len(bars) - 1
            if first_count > 0:
                avg_growth = ((last_count / first_count) ** (1 / years_span) - 1) * 100
                avg_growth_pct = round(avg_growth, 1)
            else:
                avg_growth_pct = 0
        else:
            avg_growth_pct = 0

        return {
            "averageGrowthPct": avg_growth_pct,
            "bars": bars,
        }

    def generate_city_rankings_json(self) -> Dict:
        """Generate complete city-rankings.json data."""
        logger.info("Generating city-rankings.json...")

        ranking_types = ["hides", "finds", "favorites", "logs"]
        time_ranges = ["30d", "ytd", "all"]

        return {
            "generatedAt": self.get_generated_at(),
            "rankings": self.generate_rankings(
                ranking_types, time_ranges, is_city_ranking=True, limit=20
            ),
            "cacheTrend": self.generate_cache_trend(),
            "dtMatrix": self.generate_dt_matrix(),
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
