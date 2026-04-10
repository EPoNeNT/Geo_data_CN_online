#!/usr/bin/env python3
"""
Diagnostic script to analyze why crawl-logs processes fewer caches than expected.
This script queries the database to understand the data distribution and filtering logic.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime_utils import require_env
import psycopg2


def main():
    database_url = require_env("DATABASE_URL")
    
    print("=" * 70)
    print("DIAGNOSTIC SCRIPT: Analyzing crawl-logs data discrepancy")
    print(f"Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    
    conn = psycopg2.connect(database_url, connect_timeout=10)
    cursor = conn.cursor()
    
    try:
        # 1. Total caches (non-archived)
        cursor.execute("""
            SELECT COUNT(*) 
            FROM caches 
            WHERE cache_status != 2
        """)
        total_non_archived = cursor.fetchone()[0]
        print(f"\n[1] Total non-archived caches: {total_non_archived}")
        
        # 2. Caches with logs_crawled_at IS NULL (never crawled)
        cursor.execute("""
            SELECT COUNT(*) 
            FROM caches 
            WHERE cache_status != 2 AND logs_crawled_at IS NULL
        """)
        never_crawled = cursor.fetchone()[0]
        print(f"[2] Caches never crawled for logs (logs_crawled_at IS NULL): {never_crawled}")
        
        # 3. Caches with logs_crawled_at IS NOT NULL (previously crawled)
        cursor.execute("""
            SELECT COUNT(*) 
            FROM caches 
            WHERE cache_status != 2 AND logs_crawled_at IS NOT NULL
        """)
        previously_crawled = cursor.fetchone()[0]
        print(f"[3] Caches previously crawled (logs_crawled_at IS NOT NULL): {previously_crawled}")
        
        # 4. Among previously crawled, how many have NEW logs?
        cursor.execute("""
            SELECT COUNT(*)
            FROM caches c
            LEFT JOIN (
                SELECT gc_code, MAX(visited) AS max_visited
                FROM logs
                GROUP BY gc_code
            ) l ON c.code = l.gc_code
            WHERE c.cache_status != 2 
              AND c.logs_crawled_at IS NOT NULL 
              AND l.max_visited >= c.logs_crawled_at
        """)
        has_new_logs = cursor.fetchone()[0]
        print(f"[4] Previously crawled but have NEW logs: {has_new_logs}")
        
        # 5. Simulate the exact logic from get_all_caches_to_crawl()
        cursor.execute("""
            SELECT c.code, c.latitude, c.longitude, c.premium_only, c.logs_crawled_at,
                   COALESCE(l.max_visited, '1970-01-01') AS max_visited
            FROM caches c
            LEFT JOIN (
                SELECT gc_code, MAX(visited) AS max_visited
                FROM logs
                GROUP BY gc_code
            ) l ON c.code = l.gc_code
            WHERE c.cache_status != 2
            ORDER BY c.code
        """)
        
        rows = cursor.fetchall()
        would_process = []
        skipped_reasons = {
            'already_up_to_date': 0,
            'no_new_logs': 0
        }
        
        for row in rows:
            code, lat, lng, premium_only, logs_crawled_at, max_visited = row
            if logs_crawled_at is None or max_visited >= logs_crawled_at:
                would_process.append(code)
            else:
                if logs_crawled_at is not None:
                    skipped_reasons['already_up_to_date'] += 1
        
        print(f"\n[5] SIMULATION of get_all_caches_to_crawl() logic:")
        print(f"    - Would process: {len(would_process)} caches")
        print(f"    - Would skip (already up to date): {skipped_reasons['already_up_to_date']}")
        
        # 6. Show sample of caches that would be processed
        if would_process:
            print(f"\n[6] Sample of caches that WOULD be processed (first 10):")
            for code in would_process[:10]:
                print(f"    - {code}")
            if len(would_process) > 10:
                print(f"    ... and {len(would_process) - 10} more")
        
        # 7. Check logs_crawled_at distribution
        cursor.execute("""
            SELECT
                CASE
                    WHEN logs_crawled_at IS NULL THEN 'Never'
                    WHEN logs_crawled_at < CURRENT_DATE - INTERVAL '30 days' THEN '>30 days ago'
                    WHEN logs_crawled_at < CURRENT_DATE - INTERVAL '7 days' THEN '7-30 days ago'
                    WHEN logs_crawled_at < CURRENT_DATE THEN '1-6 days ago'
                    ELSE 'Today'
                END as age_group,
                COUNT(*)
            FROM caches
            WHERE cache_status != 2
            GROUP BY age_group
            ORDER BY age_group
        """)

        print(f"\n[7] Distribution of logs_crawled_at timestamps:")
        print(f"{'Age Group':<20} {'Count':>10}")
        print("-" * 32)
        for age_group, count in cursor.fetchall():
            print(f"{age_group:<20} {count:>10}")

        # 8. CRITICAL CHECK: Compare last_found_date vs logs_crawled_at
        print("\n" + "=" * 70)
        print("[8] CRITICAL ANALYSIS: last_found_date vs logs_crawled_at")
        print("=" * 70)

        cursor.execute("""
            SELECT
                COUNT(*) as total_checked,
                COUNT(CASE WHEN c.last_found_date IS NOT NULL
                           AND c.logs_crawled_at IS NOT NULL
                           AND c.last_found_date > c.logs_crawled_at
                      THEN 1 END) as should_crawl_but_skipped,
                COUNT(CASE WHEN c.last_found_date IS NOT NULL
                           AND c.logs_crawled_at IS NOT NULL
                           AND c.last_found_date <= c.logs_crawled_at
                      THEN 1 END) as correctly_skipped,
                COUNT(CASE WHEN c.logs_crawled_at IS NULL THEN 1 END) as never_crawled_check
            FROM caches c
            WHERE c.cache_status != 2
              AND c.last_found_date IS NOT NULL
        """)

        row = cursor.fetchone()
        total_checked, should_crawl_but_skipped, correctly_skipped, never_crawled_check = row

        print(f"\nCaches with valid last_found_date: {total_checked}")
        print(f"  -> Should be crawled (last_found_date > logs_crawled_at): {should_crawl_but_skipped}")
        print(f"  -> Correctly skipped (last_found_date <= logs_crawled_at): {correctly_skipped}")
        print(f"  -> Never crawled for logs: {never_crawled_check}")

        # Show specific examples of missed caches
        if should_crawl_but_skipped > 0:
            print(f"\n{'!' * 70}")
            print(f"PROBLEM CONFIRMED: {should_crawl_but_skipped} caches have new activity")
            print(f"but are being SKIPPED by crawl-logs!")
            print(f"{'!' * 70}")

            cursor.execute("""
                SELECT c.code, c.name, c.last_found_date, c.logs_crawled_at,
                       COALESCE(l.max_visited::text, 'No logs') as max_log_visited
                FROM caches c
                LEFT JOIN (
                    SELECT gc_code, MAX(visited) AS max_visited
                    FROM logs
                    GROUP BY gc_code
                ) l ON c.code = l.gc_code
                WHERE c.cache_status != 2
                  AND c.last_found_date IS NOT NULL
                  AND c.logs_crawled_at IS NOT NULL
                  AND c.last_found_date > c.logs_crawled_at
                ORDER BY c.last_found_date DESC
                LIMIT 15
            """)

            print(f"\nExamples of MISSED caches (should be crawled but weren't):")
            print(f"{'Code':<12} {'Name':<30} {'Last Found':<12} {'Last Crawled':<12} {'Max Log Visit'}")
            print("-" * 80)
            for row in cursor.fetchall():
                code, name, last_found, last_crawled, max_log_visit = row
                name = (name[:28] + '..') if len(name) > 30 else name
                print(f"{code:<12} {name:<30} {str(last_found):<12} {str(last_crawled):<12} {str(max_log_visit)}")

        # 9. Summary and diagnosis
        print("\n" + "=" * 70)
        print("DIAGNOSIS SUMMARY")
        print("=" * 70)
        print(f"Expected by user: ~1254 caches (41 new + 1213 updated)")
        print(f"Actual processing: {len(would_process)} caches")
        print()

        if should_crawl_but_skipped > 0:
            print("ROOT CAUSE IDENTIFIED:")
            print("-" * 70)
            print("BUG IN FILTERING LOGIC!")
            print()
            print("Current logic uses:")
            print("  - logs table's MAX(visited) date to check for new logs")
            print()
            print("But it SHOULD use:")
            print("  - caches table's last_found_date field")
            print()
            print("The problem:")
            print("  1. crawl_caches updates caches.last_found_date from API")
            print("  2. crawl_logs checks logs table, NOT caches.last_found_date")
            print("  3. If logs table is empty or outdated, caches are wrongly skipped")
            print()
            print(f"Result: {should_crawl_but_skipped} caches with new activity were missed!")
        elif len(would_process) == never_crawled:
            print("Normal incremental behavior - no obvious bug detected")
        else:
            print("Complex case - need further investigation")

        print("=" * 70)
        
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    main()
