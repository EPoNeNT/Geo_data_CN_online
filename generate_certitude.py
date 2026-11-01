"""Generate and validate immutable Certitude batches before switching manifest."""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import shutil
import uuid

from certitude_data import AVAILABILITIES, REGIONS, build_cache_detail, build_scope, validate_dataset
from crawl_certitude import COUNTRIES, load_candidates, utc_now
from runtime_utils import connect_postgres, require_env, setup_logging


def _date_text(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10] if value else None


def _observation_stale(value, generated, freshness_hours):
    if not value or freshness_hours is None:
        return None
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            return None
        now = datetime.fromisoformat(generated.replace("Z", "+00:00"))
        return (now - observed).total_seconds() > freshness_hours * 3600
    except (TypeError, ValueError):
        return None


def build_dataset(candidates, descriptions, checkers, found_logs=None, *, freshness_hours=48):
    generated = utc_now()
    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
    envelope = {"schemaVersion": 1, "datasetVersion": version, "generatedAt": generated}
    by_code = {str(c["code"]).strip().upper(): c for c in candidates}
    grouped = defaultdict(list)
    for stored_checker in checkers.values():
        checker = deepcopy(stored_checker)
        crawl = checker.setdefault("crawl", {})
        states = [_observation_stale(checker.get(field), generated, freshness_hours)
                  for field in ("counterObservedAt", "solverListObservedAt")]
        crawl["counterStale"], crawl["solverListStale"] = states
        crawl["stale"] = True if True in states else None if None in states else False
        code = checker.get("cacheCode")
        if checker.get("mappingStatus") == "verified" and code in by_code:
            grouped[code].append(checker)
        elif checker.get("mappingStatus") == "conflict" and code in by_code and checker.get("mappingEvidence"):
            # The merge retains the last verified parent on a new conflict. Keep
            # that cache visible, without assigning the conflicting checker facts.
            grouped.setdefault(code, [])
    details = {}
    for code, rows in sorted(grouped.items()):
        row = by_code[code]
        description = descriptions.get(code, {})
        finals = [c for c in rows if c.get("role") == "final"]
        final = finals[0] if len(finals) == 1 else None
        owner = row.get("owner_username")
        cache = {
            "code": code, "name": row.get("name"), "owner": owner,
            "ownerKey": owner.strip().casefold() if owner else None,
            "geocacheType": 8, "region": COUNTRIES.get(row.get("country")), "city": row.get("city"),
            "cacheStatus": row.get("cache_status"),
            "difficulty": float(row["difficulty"]) if row.get("difficulty") is not None else None,
            "terrain": float(row["terrain"]) if row.get("terrain") is not None else None,
            "placedDate": _date_text(row.get("placed_date")),
            "detailsUrl": row.get("details_url") or "https://coord.info/" + code,
            "certitudeAvailable": "available",
            "descriptionLinks": description.get("descriptionLinks", []),
            "descriptionScannedAt": description.get("descriptionScannedAt"),
            "descriptionSource": description.get("descriptionSource"),
            "descriptionScanCoverage": description.get("descriptionScanCoverage", "unknown"),
            "descriptionScanStatus": description.get("descriptionScanStatus", "not_collected"),
            # Scanning a description does not prove the absence of hidden stages.
            "checkerSetCoverage": description.get("checkerSetCoverage", "unknown"),
            "completionRule": "single_final" if final else "unknown",
            "completionCheckerIds": [final["checkerId"]] if final else [],
            "completionRuleEvidence": ("verified_final: " + final["sourceUrl"]) if final else None,
            "finalCheckerId": final["checkerId"] if final else None,
            "finalCertitudeUrl": final["sourceUrl"] if final else None,
            "foundLogsAsOf": _date_text(row.get("logs_crawled_at")),
            "foundLogsLastAttemptAt": None,
        }
        details[code] = {**envelope, **build_cache_detail(cache, sorted(rows, key=lambda c: c["checkerId"]),
                                                       (found_logs or {}).get(code, []))}
    scopes = [{**envelope, **build_scope(list(details.values()), region, availability)}
              for region in REGIONS for availability in AVAILABILITIES]
    desc_complete = desc_failed = 0
    for code in by_code:
        state = descriptions.get(code, {})
        if state.get("descriptionScanStatus") == "error":
            desc_failed += 1
        elif state.get("descriptionScanStatus") == "ok" and state.get("descriptionScanCoverage") == "complete":
            desc_complete += 1
    verified = [checker for rows in grouped.values() for checker in rows]
    coverage = {
        "asOf": generated, "candidateCacheCount": len(by_code),
        "descriptionCompleteCount": desc_complete, "descriptionFailedCount": desc_failed,
        "descriptionPendingCount": len(by_code) - desc_complete - desc_failed,
        "mappedCacheCount": len(details), "verifiedCheckerCount": len(verified),
        "unresolvedCheckerCount": sum(c.get("mappingStatus") != "verified" for c in checkers.values()),
        "counterPairAvailableCount": sum(c.get("counterObservedAt") is not None and
                                          c.get("successCount") is not None and c.get("failureCount") is not None for c in verified),
        "completeSolverListCount": sum(c.get("solverListCoverage") == "complete" for c in verified),
        "latestCrawlErrorCount": sum(c.get("crawl", {}).get("status") == "error" for c in verified),
    }
    manifest = {**envelope, "regions": list(REGIONS), "availabilities": list(AVAILABILITIES),
                "scopePathTemplate": f"/data/certitude/versions/{version}/scopes/{{region}}/{{availability}}.json",
                "cachePathTemplate": f"/data/certitude/versions/{version}/caches/{{code}}.json",
                "cacheCount": len(details), "coverage": coverage}
    return manifest, scopes, details


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        handle.write("\n")


def publish_dataset(manifest, scopes, details, *, output_dir="public/data/certitude"):
    validate_dataset(manifest, scopes, details)
    version = manifest["datasetVersion"]
    if not version or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in version):
        raise ValueError("invalid dataset path segment")
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    staging = root / (".staging-" + uuid.uuid4().hex)
    destination = root / "versions" / version
    temporary_manifest = root / (".manifest-" + uuid.uuid4().hex + ".tmp")
    try:
        for scope in scopes:
            key = scope["scope"]
            _write_json(staging / "scopes" / key["region"] / (key["availability"] + ".json"), scope)
        for code, detail in details.items():
            _write_json(staging / "caches" / (code + ".json"), detail)
        # Read the files back, so serialization and every expected path are verified.
        disk_scopes = [json.loads((staging / "scopes" / s["scope"]["region"] / (s["scope"]["availability"] + ".json")).read_text(encoding="utf-8")) for s in scopes]
        disk_details = {code: json.loads((staging / "caches" / (code + ".json")).read_text(encoding="utf-8")) for code in details}
        validate_dataset(manifest, disk_scopes, disk_details)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise ValueError("immutable dataset version already exists")
        os.rename(staging, destination)
        _write_json(temporary_manifest, manifest)
        os.replace(temporary_manifest, root / "manifest.json")
    finally:
        # Only the uniquely created, verified staging path can be removed here.
        if staging.exists() and staging.resolve().is_relative_to(root) and staging.name.startswith(".staging-"):
            shutil.rmtree(staging)
        if temporary_manifest.exists() and temporary_manifest.resolve().parent == root:
            temporary_manifest.unlink()
    return destination


def load_inputs(connection):
    """Read all inputs in a single repeatable-read, read-only transaction."""
    connection.set_session(isolation_level="REPEATABLE READ", readonly=True)
    candidates = load_candidates(connection)
    with connection.cursor() as cursor:
        cursor.execute("SELECT cache_code, record FROM certitude_descriptions")
        descriptions = dict(cursor.fetchall())
        cursor.execute("SELECT checker_id, record FROM certitude_checkers")
        checkers = dict(cursor.fetchall())
        codes = sorted({c["cacheCode"] for c in checkers.values() if c.get("cacheCode") and c.get("mappingStatus") == "verified"})
        logs = defaultdict(list)
        if codes:
            cursor.execute("""
                SELECT gc_code, user_name, MIN(visited::date)
                FROM logs WHERE gc_code = ANY(%s) AND log_type = 'Found it'
                GROUP BY gc_code, user_name
            """, (codes,))
            for code, name, found_date in cursor.fetchall():
                # The upstream log stores a visit date, not an authenticated exact time.
                logs[code].append({"username": name, "foundDate": _date_text(found_date), "foundAt": None})
    return candidates, descriptions, checkers, logs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="public/data/certitude")
    args = parser.parse_args()
    logger = setup_logging("generate_certitude.log")
    connection = connect_postgres(require_env("DATABASE_URL"), logger=logger)
    try:
        dataset = build_dataset(*load_inputs(connection), freshness_hours=float(os.getenv("CERTITUDE_FRESHNESS_HOURS", "48")))
        destination = publish_dataset(*dataset, output_dir=args.output_dir)
        logger.info("Certitude dataset validated: caches=%s version=%s", dataset[0]["cacheCount"], destination.name)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
