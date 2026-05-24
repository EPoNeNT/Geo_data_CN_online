#!/usr/bin/env python3
"""Resumable crawler for logs of archived caches.

This maintenance tool reuses crawl_logs.py for token fetching, log parsing,
FTF detection, coordinate updates, and smart log upserts.
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import psycopg2


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_STATE_FILE = Path(__file__).with_name("crawl_archived_logs_state.json")
DEFAULT_FAILED_FILE = Path(__file__).with_name("crawl_archived_logs_failed.jsonl")


def load_local_env() -> None:
    """Load repo .env before importing modules that require env vars."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]

        os.environ.setdefault(key, value)

    legacy_cookie = os.environ.get("GEOCACHING_COOKIE")
    if legacy_cookie and not os.environ.get("GEOCOOKIE_NONPREMIUM"):
        os.environ["GEOCOOKIE_NONPREMIUM"] = legacy_cookie
    if legacy_cookie and not os.environ.get("GEOCOOKIE_PREMIUM"):
        os.environ["GEOCOOKIE_PREMIUM"] = legacy_cookie


load_local_env()

from crawl_logs import (  # noqa: E402
    DATABASE_URL,
    FOUND_LOG_TYPE,
    PROFILES,
    AuthenticationError,
    DatabaseManager,
    fetch_logs_for_cache,
    get_coordinates,
    get_logbook_token,
    logger,
    normalize_log_date_for_compare,
)


CacheRow = Tuple[str, Optional[float], Optional[float], bool]
CrawlCache = Tuple[str, Optional[float], Optional[float]]
ChangedCacheSummary = Dict[str, Dict[str, Any]]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl logs for archived caches with retries, batching, and resume state."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional maximum number of archived caches to crawl after filtering.",
    )
    parser.add_argument(
        "--only",
        choices=["all", "premium", "nonpremium"],
        default="all",
        help="Restrict the run to one cache group.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Flush logs, logs_crawled_at, and checkpoint every N successfully crawled caches.",
    )
    parser.add_argument(
        "--cache-retries",
        type=int,
        default=3,
        help="Outer retry count for each cache. crawl_logs.py still performs its own request retries.",
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=5.0,
        help="Seconds to sleep between per-cache retry attempts.",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help="Checkpoint JSON file used for resume.",
    )
    parser.add_argument(
        "--failed-file",
        type=Path,
        default=DEFAULT_FAILED_FILE,
        help="JSONL file for caches that fail after all retries.",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Ignore and overwrite the existing checkpoint state.",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Do not skip completed codes from the checkpoint.",
    )
    parser.add_argument(
        "--skip-db-crawled",
        action="store_true",
        help="Skip archived caches whose logs_crawled_at is already set in Neon.",
    )
    parser.add_argument(
        "--start-after",
        default=None,
        help="Skip ordered cache codes through this code, useful for manual resume.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print cache counts and resume information without crawling.",
    )
    return parser.parse_args()


def load_state(path: Path, reset_state: bool) -> Dict[str, Any]:
    if reset_state or not path.exists():
        return {
            "version": 1,
            "createdAt": utc_now_iso(),
            "updatedAt": utc_now_iso(),
            "completed": {},
            "failed": {},
            "stats": {
                "success": 0,
                "failed": 0,
                "logsChanged": 0,
            },
        }

    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        backup = path.with_suffix(path.suffix + f".corrupt-{int(time.time())}")
        path.replace(backup)
        logger.warning("Checkpoint state was corrupt and was moved to %s", backup)
        return load_state(path, reset_state=True)

    state.setdefault("version", 1)
    state.setdefault("createdAt", utc_now_iso())
    state.setdefault("updatedAt", utc_now_iso())
    state.setdefault("completed", {})
    state.setdefault("failed", {})
    state.setdefault("stats", {})
    state["stats"].setdefault("success", len(state["completed"]))
    state["stats"].setdefault("failed", len(state["failed"]))
    state["stats"].setdefault("logsChanged", 0)
    return state


def save_state(path: Path, state: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updatedAt"] = utc_now_iso()
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def append_failed(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def summarize_changed_logs_by_cache(
    db: DatabaseManager,
    new_logs: List[dict],
) -> ChangedCacheSummary:
    if not new_logs:
        return {}

    gc_codes = list({log["GCCode"] for log in new_logs})
    existing_logs = db.get_existing_logs_for_caches(gc_codes)
    summary: ChangedCacheSummary = defaultdict(
        lambda: {"raw": 0, "inserted": 0, "updated": 0, "details": []}
    )

    for log in new_logs:
        gc_code = log["GCCode"]
        user_name = log["UserName"]
        key = (gc_code, user_name)

        if key not in existing_logs:
            summary[gc_code]["raw"] += 1
            summary[gc_code]["inserted"] += 1
            summary[gc_code]["details"].append(
                {
                    "user": user_name,
                    "action": "insert",
                    "reasons": ["missing"],
                    "api": {
                        "visited": normalize_log_date_for_compare(log["Visited"]),
                        "favorite_point_used": bool(log.get("FavoritePointUsed", False)),
                        "is_ftf": bool(log.get("IsFTF", False)),
                        "log_type": log.get("LogType", FOUND_LOG_TYPE),
                    },
                }
            )
            continue

        old_record = existing_logs[key]
        db_visited_str = normalize_log_date_for_compare(old_record["visited"])
        api_visited_str = normalize_log_date_for_compare(log["Visited"])
        api_record = {
            "visited": api_visited_str,
            "favorite_point_used": bool(log.get("FavoritePointUsed", False)),
            "is_ftf": bool(log.get("IsFTF", False)),
            "log_type": log.get("LogType", FOUND_LOG_TYPE),
        }
        db_record = {
            "visited": db_visited_str,
            "favorite_point_used": bool(old_record["favorite_point_used"]),
            "is_ftf": bool(old_record["is_ftf"]),
            "log_type": old_record.get("log_type") or FOUND_LOG_TYPE,
            "duplicate_count": int(old_record.get("duplicate_count") or 1),
        }
        fields_match = (
            db_record["visited"] == api_record["visited"]
            and db_record["favorite_point_used"] == api_record["favorite_point_used"]
            and db_record["is_ftf"] == api_record["is_ftf"]
            and db_record["log_type"] == api_record["log_type"]
        )

        if not fields_match or db_record["duplicate_count"] > 1:
            reasons = [
                field
                for field in ("visited", "favorite_point_used", "is_ftf", "log_type")
                if db_record[field] != api_record[field]
            ]
            if db_record["duplicate_count"] > 1:
                reasons.append("duplicate_count")
            summary[gc_code]["raw"] += 1
            summary[gc_code]["updated"] += 1
            summary[gc_code]["details"].append(
                {
                    "user": user_name,
                    "action": "update",
                    "reasons": reasons,
                    "db": db_record,
                    "api": api_record,
                }
            )

    return dict(summary)


def merge_changed_cache_summary(
    target: ChangedCacheSummary,
    source: ChangedCacheSummary,
) -> None:
    for code, counts in source.items():
        existing = target.setdefault(code, {"raw": 0, "inserted": 0, "updated": 0, "details": []})
        existing["raw"] += counts.get("raw", 0)
        existing["inserted"] += counts.get("inserted", 0)
        existing["updated"] += counts.get("updated", 0)
        existing["details"].extend(counts.get("details", []))


def log_changed_cache_summary(
    prefix: str,
    summary: ChangedCacheSummary,
    detail_limit_per_cache: int = 5,
) -> None:
    if not summary:
        logger.info("%s changed caches: 0", prefix)
        return

    logger.info("%s changed caches: %s", prefix, len(summary))
    for code in sorted(summary):
        counts = summary[code]
        logger.info(
            "%s changed cache: %s raw %s, inserted %s, updated %s",
            prefix,
            code,
            counts.get("raw", 0),
            counts.get("inserted", 0),
            counts.get("updated", 0),
        )
        for detail in counts.get("details", [])[:detail_limit_per_cache]:
            logger.info(
                "%s changed log: %s user=%s action=%s reasons=%s db=%s api=%s",
                prefix,
                code,
                detail.get("user"),
                detail.get("action"),
                ",".join(detail.get("reasons", [])),
                detail.get("db"),
                detail.get("api"),
            )


def count_summary_changes(summary: ChangedCacheSummary) -> int:
    return sum(
        int(counts.get("inserted", 0)) + int(counts.get("updated", 0))
        for counts in summary.values()
    )


def get_archived_caches(
    db: DatabaseManager,
    limit: Optional[int] = None,
    skip_db_crawled: bool = False,
    start_after: Optional[str] = None,
) -> List[CacheRow]:
    query = """
        SELECT code, latitude, longitude, premium_only
        FROM caches
        WHERE cache_status = 2
    """
    params: List[Any] = []

    if skip_db_crawled:
        query += " AND logs_crawled_at IS NULL"
    if start_after:
        query += " AND code > %s"
        params.append(start_after.upper())

    query += " ORDER BY code"

    if limit is not None:
        query += " LIMIT %s"
        params.append(limit)

    db.cursor.execute(query, params)
    return [
        (code, lat, lng, bool(premium_only))
        for code, lat, lng, premium_only in db.cursor.fetchall()
    ]


def filter_group(
    rows: List[CacheRow],
    only: str,
    completed_codes: set[str],
    resume: bool,
) -> Tuple[List[CrawlCache], List[CrawlCache]]:
    premium_caches: List[CrawlCache] = []
    nonpremium_caches: List[CrawlCache] = []

    for code, lat, lng, premium_only in rows:
        if resume and code in completed_codes:
            continue
        if premium_only and only in {"all", "premium"}:
            premium_caches.append((code, lat, lng))
        if not premium_only and only in {"all", "nonpremium"}:
            nonpremium_caches.append((code, lat, lng))

    return premium_caches, nonpremium_caches


def flush_batch(
    db: DatabaseManager,
    state: Dict[str, Any],
    state_file: Path,
    pending_logs: List[dict],
    pending_codes: List[str],
    pending_completed: Dict[str, Dict[str, Any]],
    today_str: str,
) -> Tuple[int, ChangedCacheSummary]:
    if not pending_logs and not pending_codes:
        return 0, {}

    changed_logs = 0
    changed_cache_summary: ChangedCacheSummary = {}
    if pending_logs:
        changed_cache_summary = summarize_changed_logs_by_cache(db, pending_logs)
        inserted, updated = db.smart_upsert_logs(pending_logs)
        changed_logs = inserted + updated
        logger.info(
            "Batch logs upserted: raw %s, inserted %s, updated %s, changed %s",
            len(pending_logs),
            inserted,
            updated,
            changed_logs,
        )
        candidate_changed_logs = count_summary_changes(changed_cache_summary)
        if changed_logs > 0:
            if candidate_changed_logs != changed_logs:
                logger.warning(
                    "Batch changed-log candidate count %s differs from actual changed count %s",
                    candidate_changed_logs,
                    changed_logs,
                )
                log_changed_cache_summary("Batch candidate", changed_cache_summary)
            else:
                log_changed_cache_summary("Batch actual", changed_cache_summary)
        elif candidate_changed_logs > 0:
            logger.info(
                "Batch changed-log candidates %s produced no actual DB changes",
                candidate_changed_logs,
            )

    if pending_codes:
        db.batch_update_logs_crawled_at(pending_codes, today_str)

    db.commit()
    state["completed"].update(pending_completed)
    state["stats"]["success"] = len(state["completed"])
    state["stats"]["logsChanged"] = state["stats"].get("logsChanged", 0) + changed_logs
    save_state(state_file, state)
    if changed_logs <= 0:
        return changed_logs, {}
    return changed_logs, changed_cache_summary


def crawl_one_cache(
    db: DatabaseManager,
    group_name: str,
    code: str,
    old_lat: Optional[float],
    old_lng: Optional[float],
) -> List[dict]:
    cfg = PROFILES[group_name]
    cookie = cfg["COOKIES"]
    page_sleep = cfg["page_sleep"]
    update_coordinates = cfg["update_coordinates"]

    if update_coordinates:
        new_lat, new_lng = get_coordinates(code, cookie)
        if new_lat and new_lng:
            lat_changed = abs(float(new_lat or 0) - float(old_lat or 0)) > 1e-6
            lng_changed = abs(float(new_lng or 0) - float(old_lng or 0)) > 1e-6
            if lat_changed or lng_changed:
                db.update_cache_coordinates(code, new_lat, new_lng)
                logger.info("  Coordinates updated: %s (%s, %s)", code, new_lat, new_lng)
        time.sleep(1)

    token = get_logbook_token(code, cookie)
    if not token:
        raise RuntimeError("missing_logbook_token")

    return fetch_logs_for_cache(code, token, page_sleep, cookie)


def crawl_group_resumable(
    db: DatabaseManager,
    group_name: str,
    caches: List[CrawlCache],
    args: argparse.Namespace,
    state: Dict[str, Any],
    today_str: str,
) -> Dict[str, int]:
    logger.info("开始处理 %s 组，共 %s 个 cache", group_name, len(caches))

    pending_logs: List[dict] = []
    pending_codes: List[str] = []
    pending_completed: Dict[str, Dict[str, Any]] = {}
    success_count = 0
    failed_count = 0
    changed_logs = 0
    group_changed_caches: ChangedCacheSummary = {}

    max_attempts = max(1, args.cache_retries)
    batch_size = max(1, args.batch_size)

    for index, (code, old_lat, old_lng) in enumerate(caches, start=1):
        logger.info("[%s %s/%s] 处理: %s", group_name, index, len(caches), code)

        last_error = ""
        for attempt in range(1, max_attempts + 1):
            try:
                cache_logs = crawl_one_cache(db, group_name, code, old_lat, old_lng)
                if cache_logs:
                    pending_logs.extend(cache_logs)
                    logger.info("  获取 %s 条 logs", len(cache_logs))
                else:
                    logger.info("  无 logs 数据")

                pending_codes.append(code)
                pending_completed[code] = {
                    "group": group_name,
                    "completedAt": utc_now_iso(),
                    "logsFetched": len(cache_logs),
                }
                state["failed"].pop(code, None)
                success_count += 1
                break

            except AuthenticationError:
                logger.exception("Authentication failed, stopping run")
                batch_changed_logs, batch_changed_caches = flush_batch(
                    db,
                    state,
                    args.state_file,
                    pending_logs,
                    pending_codes,
                    pending_completed,
                    today_str,
                )
                changed_logs += batch_changed_logs
                merge_changed_cache_summary(group_changed_caches, batch_changed_caches)
                raise
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
                last_error = repr(exc)
                logger.warning(
                    "  数据库连接出错: %s，attempt %s/%s",
                    exc,
                    attempt,
                    max_attempts,
                )
                try:
                    db.reconnect()
                except Exception:
                    logger.exception("  数据库重连失败")
                if attempt < max_attempts:
                    time.sleep(max(0.0, args.retry_delay))
            except Exception as exc:
                last_error = repr(exc)
                logger.warning(
                    "  出错: %s，attempt %s/%s",
                    exc,
                    attempt,
                    max_attempts,
                )
                if attempt < max_attempts:
                    time.sleep(max(0.0, args.retry_delay))

        else:
            failed_count += 1
            failed_row = {
                "code": code,
                "group": group_name,
                "failedAt": utc_now_iso(),
                "error": last_error,
                "attempts": max_attempts,
            }
            state["failed"][code] = failed_row
            state["stats"]["failed"] = len(state["failed"])
            append_failed(args.failed_file, failed_row)
            save_state(args.state_file, state)
            logger.error("  失败并记录: %s", code)

        if len(pending_codes) >= batch_size:
            batch_changed_logs, batch_changed_caches = flush_batch(
                db,
                state,
                args.state_file,
                pending_logs,
                pending_codes,
                pending_completed,
                today_str,
            )
            changed_logs += batch_changed_logs
            merge_changed_cache_summary(group_changed_caches, batch_changed_caches)
            pending_logs = []
            pending_codes = []
            pending_completed = {}

    batch_changed_logs, batch_changed_caches = flush_batch(
        db,
        state,
        args.state_file,
        pending_logs,
        pending_codes,
        pending_completed,
        today_str,
    )
    changed_logs += batch_changed_logs
    merge_changed_cache_summary(group_changed_caches, batch_changed_caches)

    logger.info(
        "%s 组处理完成: 成功 %s, 失败 %s, changed logs %s",
        group_name,
        success_count,
        failed_count,
        changed_logs,
    )
    log_changed_cache_summary(f"{group_name} group", group_changed_caches)
    return {
        "success_count": success_count,
        "failed_count": failed_count,
        "logs_count": changed_logs,
        "changed_caches": group_changed_caches,
    }


def main() -> None:
    args = parse_args()
    state = load_state(args.state_file, reset_state=args.reset_state)
    resume = not args.no_resume
    completed_codes = set(state.get("completed", {}).keys())

    db = DatabaseManager(DATABASE_URL)
    db.connect()

    try:
        archived_caches = get_archived_caches(
            db,
            limit=args.limit,
            skip_db_crawled=args.skip_db_crawled,
            start_after=args.start_after,
        )
        premium_caches, nonpremium_caches = filter_group(
            archived_caches,
            args.only,
            completed_codes,
            resume=resume,
        )

        logger.info(
            "Archived logs run: selected %s, completed in state %s, premium todo %s, nonpremium todo %s",
            len(archived_caches),
            len(completed_codes),
            len(premium_caches),
            len(nonpremium_caches),
        )
        logger.info("State file: %s", args.state_file)
        logger.info("Failed file: %s", args.failed_file)

        if args.dry_run:
            save_state(args.state_file, state)
            return

        today_str = datetime.now().strftime("%Y-%m-%d")
        total_success = 0
        total_failed = 0
        total_changed_logs = 0
        total_changed_caches: ChangedCacheSummary = {}

        if args.only in {"all", "premium"}:
            premium_stats = crawl_group_resumable(
                db,
                "premium",
                premium_caches,
                args,
                state,
                today_str,
            )
            total_success += premium_stats["success_count"]
            total_failed += premium_stats["failed_count"]
            total_changed_logs += premium_stats["logs_count"]
            merge_changed_cache_summary(total_changed_caches, premium_stats.get("changed_caches", {}))

        if args.only in {"all", "nonpremium"}:
            nonpremium_stats = crawl_group_resumable(
                db,
                "nonpremium",
                nonpremium_caches,
                args,
                state,
                today_str,
            )
            total_success += nonpremium_stats["success_count"]
            total_failed += nonpremium_stats["failed_count"]
            total_changed_logs += nonpremium_stats["logs_count"]
            merge_changed_cache_summary(total_changed_caches, nonpremium_stats.get("changed_caches", {}))

        save_state(args.state_file, state)
        logger.info(
            "Archived logs run completed: success %s, failed %s, changed logs %s",
            total_success,
            total_failed,
            total_changed_logs,
        )
        log_changed_cache_summary("Archived logs run total", total_changed_caches)
    finally:
        db.close()


if __name__ == "__main__":
    main()
