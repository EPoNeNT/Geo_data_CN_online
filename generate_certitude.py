"""Generate and validate immutable Certitude batches before switching manifest."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import uuid

from certitude_data import AVAILABILITIES, REGIONS, build_cache_facts, build_scope, validate_dataset
from crawl_certitude import COUNTRIES, load_candidates, utc_now
from runtime_utils import connect_postgres, require_env, setup_logging


def build_dataset(candidates, checkers):
    generated = utc_now()
    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
    envelope = {"schemaVersion": 2, "datasetVersion": version, "generatedAt": generated}
    by_code = {str(c["code"]).strip().upper(): c for c in candidates}
    grouped = defaultdict(list)
    for checker in checkers.values():
        code = checker.get("cacheCode")
        if checker.get("mappingStatus") == "verified" and code in by_code:
            grouped[code].append(checker)
        elif checker.get("mappingStatus") == "conflict" and code in by_code and checker.get("mappingEvidence"):
            # The merge retains the last verified parent on a new conflict. Keep
            # that cache visible, without assigning the conflicting checker facts.
            grouped.setdefault(code, [])
    facts = {}
    for code, rows in sorted(grouped.items()):
        row = by_code[code]
        finals = [c for c in rows if c.get("role") == "final"]
        final = finals[0] if len(finals) == 1 else None
        owner = row.get("owner_username")
        cache = {
            "code": code, "name": row.get("name"), "owner": owner,
            "geocacheType": 8, "region": COUNTRIES.get(row.get("country")),
            "cacheStatus": row.get("cache_status"),
            "detailsUrl": row.get("details_url") or "https://coord.info/" + code,
            "completionRule": "single_final" if final else "unknown",
            "completionCheckerIds": [final["checkerId"]] if final else [],
            "finalCertitudeUrl": final["sourceUrl"] if final else None,
        }
        facts[code] = build_cache_facts(cache, sorted(rows, key=lambda c: c["checkerId"]))
    scopes = [{**envelope, **build_scope(list(facts.values()), region, availability)}
              for region in REGIONS for availability in AVAILABILITIES]
    manifest = {**envelope, "regions": list(REGIONS), "availabilities": list(AVAILABILITIES),
                "scopePathTemplate": f"/data/certitude/versions/{version}/scopes/{{region}}/{{availability}}.json"}
    return manifest, scopes, facts


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        handle.write("\n")


def publish_dataset(manifest, scopes, facts, *, output_dir="public/data/certitude"):
    validate_dataset(manifest, scopes, facts)
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
        # Read the files back, so serialization and every expected path are verified.
        disk_scopes = [json.loads((staging / "scopes" / s["scope"]["region"] / (s["scope"]["availability"] + ".json")).read_text(encoding="utf-8")) for s in scopes]
        validate_dataset(manifest, disk_scopes, facts)
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
        cursor.execute("SELECT checker_id, record FROM certitude_checkers")
        checkers = dict(cursor.fetchall())
    return candidates, checkers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="public/data/certitude")
    args = parser.parse_args()
    logger = setup_logging("generate_certitude.log")
    connection = connect_postgres(require_env("DATABASE_URL"), logger=logger)
    try:
        dataset = build_dataset(*load_inputs(connection))
        destination = publish_dataset(*dataset, output_dir=args.output_dir)
        logger.info("Certitude summaries validated: caches=%s scope_files=15 version=%s", len(dataset[2]), destination.name)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
