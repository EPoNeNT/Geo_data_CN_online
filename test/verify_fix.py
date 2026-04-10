#!/usr/bin/env python3
"""
Verification script to test the fix for crawl-logs filtering logic.
This simulates the FIXED get_all_caches_to_crawl() logic.
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
    print("VERIFICATION SCRIPT: Testing FIXED crawl-logs logic")
    print(f"Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    conn = psycopg2.connect(database_url, connect_timeout=10)
    cursor = conn.cursor()

    try:
        # Simulate the FIXED logic (using caches.last_found_date instead of logs.MAX(visited))
        cursor.execute(
            """
            SELECT c.code, c.latitude, c.longitude, c.premium_only,
                   c.logs_crawled_at, c.last_found_date
            FROM caches c
            WHERE c.cache_status != 2
            ORDER BY c.code
            """
        )

        rows = cursor.fetchall()
        would_process_new_logic = []
        skipped_reasons = {
            'no_new_activity': 0,
            'last_found_older_than_crawled': 0,
            'no_last_found_date': 0
        }

        for row in rows:
            code, lat, lng, premium_only, logs_crawled_at, last_found_date = row

            # Convert to date for comparison (handle both datetime and date types)
            logs_crawled_date = logs_crawled_at.date() if hasattr(logs_crawled_at, 'date') else logs_crawled_at
            last_found_date_cmp = last_found_date.date() if hasattr(last_found_date, 'date') else last_found_date

            if logs_crawled_at is None:
                # Never crawled before - should process
                would_process_new_logic.append(code)
            elif last_found_date is not None and last_found_date_cmp >= logs_crawled_date:
                # Has new activity since last crawl - should process
                would_process_new_logic.append(code)
            else:
                # No new activity - skip
                if last_found_date is None:
                    skipped_reasons['no_last_found_date'] += 1
                else:
                    skipped_reasons['last_found_older_than_crawled'] += 1

        total_non_archived = len(rows)

        print(f"\n[RESULTS] Fixed Logic Simulation:")
        print(f"{'=' * 50}")
        print(f"Total non-archived caches: {total_non_archived}")
        print(f"Caches to PROCESS (new logic): {len(would_process_new_logic)}")
        print(f"  - Never crawled before: ???")
        print(f"  - Have NEW activity: ???")
        print(f"Caches to SKIP:")
        print(f"  - No activity change: {skipped_reasons['last_found_older_than_crawled']}")
        print(f"  - No last_found_date: {skipped_reasons['no_last_found_date']}")

        # Count breakdown of processed caches
        never_crawled_count = sum(1 for row in rows if row[4] is None)
        has_new_activity_count = len(would_process_new_logic) - never_crawled_count

        print(f"\n[DETAILED BREAKDOWN]")
        print(f"{'=' * 50}")
        print(f"To process - Never crawled: {never_crawled_count}")
        print(f"To process - Has new activity: {has_new_activity_count}")
        print(f"Total to process: {len(would_process_new_logic)}")

        # Comparison with OLD logic (from previous diagnostic)
        print(f"\n[COMPARISON]")
        print(f"{'=' * 50}")
        print(f"OLD logic (buggy) would process: 3 caches")
        print(f"NEW logic (fixed) will process: {len(would_process_new_logic)} caches")
        print(f"IMPROVEMENT: +{len(would_process_new_logic) - 3} caches now correctly included!")

        # Show sample of caches that will NOW be processed
        if len(would_process_new_logic) > 3:
            print(f"\n[SAMPLE] Caches that will NOW be processed (first 10):")
            print(f"{'Code':<12} {'Last Found':<22} {'Last Crawled'}")
            print("-" * 50)

            count = 0
            for row in rows:
                code, lat, lng, premium_only, logs_crawled_at, last_found_date = row

                # Convert for comparison
                logs_crawled_cmp = logs_crawled_at.date() if hasattr(logs_crawled_at, 'date') else logs_crawled_at
                last_found_cmp = last_found_date.date() if hasattr(last_found_date, 'date') else last_found_date

                if logs_crawled_at is None or (
                    last_found_date is not None and last_found_cmp >= logs_crawled_cmp
                ):
                    if count < 10:
                        last_found_str = str(last_found_date)[:19] if last_found_date else 'NULL'
                        crawled_str = str(logs_crawled_at)[:10] if logs_crawled_at else 'NULL'
                        print(f"{code:<12} {last_found_str:<22} {crawled_str}")
                    count += 1

            if count > 10:
                print(f"... and {count - 10} more")

        # Final verdict
        print("\n" + "=" * 70)
        print("VERDICT")
        print("=" * 70)

        if len(would_process_new_logic) > 100:
            print("✅ FIX SUCCESSFUL!")
            print(f"The fix will now correctly process {len(would_process_new_logic)} caches")
            print(f"instead of incorrectly skipping them.")
            print()
            print("Ready to deploy to GitHub Actions.")
        else:
            print("⚠️ Unexpected result - please review the data manually.")

        print("=" * 70)

    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    main()
