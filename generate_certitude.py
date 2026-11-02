"""Generate and validate immutable Certitude batches before switching manifest."""
from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from certitude_data import AVAILABILITIES, REGIONS, build_cache_facts, build_scope, validate_dataset
from crawl_certitude import COUNTRIES, load_candidates, utc_now
from runtime_utils import connect_postgres, require_env, setup_logging


EXCLUSIONS_PATH = Path(__file__).resolve().with_name("certitude_exclusions.json")


def _unique_exclusion_entries(pairs):
    entries = {}
    for key, value in pairs:
        if key in entries:
            raise ValueError(f"duplicate Certitude exclusion: {key}")
        entries[key] = value
    return entries


def load_exclusions(path=None):
    """Read explicit parent-cache exclusions; malformed rules block publication."""
    content = Path(path if path is not None else EXCLUSIONS_PATH).read_text(encoding="utf-8")
    entries = json.loads(content, object_pairs_hook=_unique_exclusion_entries)
    if not isinstance(entries, dict):
        raise ValueError("Certitude exclusions must be an object mapping GC codes to reasons")
    return _validate_exclusion_map(entries, "Certitude exclusion")


def _validate_exclusion_map(entries, label):
    if not isinstance(entries, dict):
        raise ValueError(f"{label}s must be an object mapping GC codes to reasons")
    exclusions = {}
    for code, reason in entries.items():
        if not isinstance(code, str):
            raise ValueError(f"invalid {label} GC code: {code}")
        normalized = code.strip().upper()
        if not re.fullmatch(r"GC[A-Z0-9]+", normalized):
            raise ValueError(f"invalid {label} GC code: {code}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"{label} requires a reason: {code}")
        if normalized in exclusions:
            raise ValueError(f"duplicate normalized {label}: {normalized}")
        exclusions[normalized] = reason.strip()
    return exclusions


def _audit_exclusion(record):
    """Return a validated exclusion only for an applied, currently valid audit result."""
    if not isinstance(record, dict):
        raise ValueError("Certitude applied usage audit record must be an object")
    audit = record.get("usageAudit")
    if not isinstance(audit, dict) or audit.get("autoExcluded") is not True:
        raise ValueError("Certitude applied usage audit record has invalid usageAudit metadata")
    timestamp = audit.get("checkedAt")
    if not isinstance(timestamp, str):
        raise ValueError("Certitude applied usage audit requires a UTC checkedAt timestamp")
    try:
        checked_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Certitude applied usage audit requires a UTC checkedAt timestamp") from error
    if checked_at.tzinfo is None or checked_at.utcoffset() is None or checked_at.utcoffset().total_seconds() != 0:
        raise ValueError("Certitude applied usage audit requires a UTC checkedAt timestamp")
    if audit.get("lastValidStatus") != "not_linked":
        raise ValueError("Certitude applied usage audit must have lastValidStatus not_linked")
    reason = audit.get("autoExclusionReason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Certitude applied usage audit requires an auto-exclusion reason")
    return reason.strip()


def _user_index(users):
    """Only a unique normalized user name can supply a Geocaching identity."""
    grouped = defaultdict(list)
    for row in users or []:
        name = row.get("user_name")
        if isinstance(name, str) and name.strip():
            grouped[name.strip().casefold()].append(row)
    result = {}
    for key, rows in grouped.items():
        if len(rows) != 1:
            continue
        guid = None
        try:
            guid = str(uuid.UUID(str(rows[0].get("guid") or "").strip()))
        except ValueError:
            pass
        result[key] = (rows[0]["user_name"].strip(), guid)
    return result


def _linked_user(name, users):
    key = name.strip().casefold() if isinstance(name, str) else ""
    return users.get(key, (name, None))


def _linked_checker(record, users):
    """Attach local identity metadata without altering the saved source records."""
    linked = deepcopy(record)
    for solver in linked.get("publicSolvers") or []:
        if not isinstance(solver, dict):
            continue
        name, guid = _linked_user(solver.get("username"), users)
        solver["username"], solver["guid"] = name, guid
        if isinstance(name, str) and name.strip().casefold() in users:
            solver["normalizedUsername"] = name.strip().casefold()
    return linked


def build_dataset(candidates, checkers, users=None, audit_exclusions=None):
    exclusions = load_exclusions()
    automatic = _validate_exclusion_map({} if audit_exclusions is None else audit_exclusions,
                                        "Certitude audit exclusion")
    # Explicit exclusions remain authoritative if the same parent appears in both.
    exclusions.update({code: reason for code, reason in automatic.items() if code not in exclusions})
    generated = utc_now()
    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
    envelope = {"schemaVersion": 2, "datasetVersion": version, "generatedAt": generated}
    by_code = {str(c["code"]).strip().upper(): c for c in candidates
               if str(c["code"]).strip().upper() not in exclusions}
    user_index = _user_index(users)
    grouped = defaultdict(list)
    for checker in checkers.values():
        code = checker.get("cacheCode")
        if checker.get("mappingStatus") == "verified" and code in by_code:
            grouped[code].append(_linked_checker(checker, user_index))
        elif checker.get("mappingStatus") == "conflict" and code in by_code and checker.get("mappingEvidence"):
            # The merge retains the last verified parent on a new conflict. Keep
            # that cache visible, without assigning the conflicting checker facts.
            grouped.setdefault(code, [])
    facts = {}
    for code, rows in sorted(grouped.items()):
        row = by_code[code]
        finals = [c for c in rows if c.get("role") == "final"]
        final = finals[0] if len(finals) == 1 else None
        owner, owner_guid = _linked_user(row.get("owner_username"), user_index)
        cache = {
            "code": code, "name": row.get("name"), "owner": owner,
            "ownerGuid": owner_guid,
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
        cursor.execute('SELECT user_name, guid FROM "user"')
        users = [{"user_name": name, "guid": guid} for name, guid in cursor.fetchall()]
        cursor.execute("SELECT cache_code, record->'usageAudit' FROM certitude_descriptions "
                       "WHERE record->'usageAudit'->'autoExcluded' = 'true'::jsonb")
        audit_exclusions = {}
        for code, audit in cursor.fetchall():
            if not isinstance(audit, dict):
                raise ValueError("Certitude applied usage audit record must be an object")
            reason = _audit_exclusion({"usageAudit": audit})
            normalized = _validate_exclusion_map({code: reason}, "Certitude audit exclusion")
            normalized_code, normalized_reason = next(iter(normalized.items()))
            if normalized_code in audit_exclusions:
                raise ValueError(f"duplicate normalized Certitude audit exclusion: {normalized_code}")
            audit_exclusions[normalized_code] = normalized_reason
    return candidates, checkers, users, audit_exclusions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="public/data/certitude")
    args = parser.parse_args()
    logger = setup_logging("generate_certitude.log")
    logger.info("Certitude configured exclusions: %s", ", ".join(sorted(load_exclusions())) or "none")
    connection = connect_postgres(require_env("DATABASE_URL"), logger=logger)
    try:
        dataset = build_dataset(*load_inputs(connection))
        destination = publish_dataset(*dataset, output_dir=args.output_dir)
        logger.info("Certitude summaries validated: caches=%s scope_files=15 version=%s", len(dataset[2]), destination.name)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
