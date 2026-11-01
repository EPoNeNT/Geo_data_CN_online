"""Neon persistence and observation-preserving merges for Certitude."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path


def _valid_counter_pair(record):
    return (
        isinstance(record.get("successCount"), int)
        and not isinstance(record.get("successCount"), bool)
        and record.get("successCount") >= 0
        and isinstance(record.get("failureCount"), int)
        and not isinstance(record.get("failureCount"), bool)
        and record.get("failureCount") >= 0
        and _parse_time(record.get("counterObservedAt")) is not None
    )


def _valid_solver_list(record):
    return (record.get("solverListCoverage") in ("complete", "partial")
            and isinstance(record.get("publicSolvers"), list)
            and _parse_time(record.get("solverListObservedAt")) is not None)


def _retained_solver_list(record):
    rows = deepcopy(record["publicSolvers"])
    coverage = record["solverListCoverage"]
    return {
        "publicSolvers": rows,
        "solverRowsRetrieved": record.get("solverRowsRetrieved")
        if isinstance(record.get("solverRowsRetrieved"), int) else len(rows),
        "solverReportedTotal": record.get("solverReportedTotal"),
        "publicSolverCount": record.get("publicSolverCount")
        if record.get("publicSolverCount") is not None else (len(rows) if coverage == "complete" else None),
        "solverListCoverage": coverage,
        "solverListObservedAt": record["solverListObservedAt"],
    }


def _parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except ValueError:
        return None


def _stale(observation_times, now, freshness_hours):
    parsed = [_parse_time(value) for value in observation_times]
    parsed = [value for value in parsed if value is not None]
    if not parsed or freshness_hours is None:
        return None
    current = _parse_time(now) or datetime.now(timezone.utc)
    limit = current - timedelta(hours=freshness_hours)
    return any(value < limit for value in parsed)


def merge_checker(previous, incoming, last_attempt_at=None, error_code=None, freshness_hours=48):
    """Merge a crawl without replacing valid observations with failed/partial data."""
    previous = deepcopy(previous or {})
    incoming = deepcopy(incoming) if isinstance(incoming, dict) else None
    merged = deepcopy(previous)
    if incoming is not None:
        merged.update(incoming)

    old_id = previous.get("checkerId")
    new_id = incoming.get("checkerId") if incoming else None
    if old_id is not None and new_id is not None and old_id != new_id:
        raise ValueError("checkerId is an opaque case-sensitive identifier")
    merged["checkerId"] = old_id or new_id

    if incoming is None:
        if _valid_counter_pair(previous):
            for field in ("successCount", "failureCount", "counterObservedAt"):
                merged[field] = deepcopy(previous[field])
        else:
            merged.update(successCount=None, failureCount=None, counterObservedAt=None)
        if _valid_solver_list(previous):
            merged.update(_retained_solver_list(previous))
        else:
            merged.update(publicSolvers=[], solverRowsRetrieved=0, solverReportedTotal=None,
                          publicSolverCount=None, solverListCoverage="unknown", solverListObservedAt=None)
    else:
        if not _valid_counter_pair(incoming):
            if _valid_counter_pair(previous):
                for field in ("successCount", "failureCount", "counterObservedAt"):
                    merged[field] = deepcopy(previous[field])
            else:
                merged.update(successCount=None, failureCount=None, counterObservedAt=None)
        previous_list_valid = _valid_solver_list(previous)
        incoming_list_valid = _valid_solver_list(incoming)
        previous_complete = previous.get("solverListCoverage") == "complete" and previous_list_valid
        incoming_list_is_new = (
            incoming_list_valid
            and (not previous_list_valid or
                 (_parse_time(incoming.get("solverListObservedAt")) >
                  _parse_time(previous.get("solverListObservedAt"))))
        )
        accept_list = incoming_list_is_new and not (
            previous_complete and incoming.get("solverListCoverage") != "complete"
        )
        if not accept_list:
            if previous_list_valid:
                merged.update(_retained_solver_list(previous))
            else:
                merged.update(publicSolvers=[], solverRowsRetrieved=0, solverReportedTotal=None,
                              publicSolverCount=None, solverListCoverage="unknown", solverListObservedAt=None)

        # Preserve known source facts when a later parse omits them or only returns
        # the parser's unknown sentinel. Mapping conflicts are handled below.
        for field in ("publishedAt", "latestActivityAt", "sourceTimezone", "answerType", "matchingMode", "role"):
            old_value = previous.get(field)
            new_value = incoming.get(field)
            if old_value is not None and (new_value is None or new_value == "unknown"):
                merged[field] = deepcopy(old_value)
        if previous.get("mappingStatus") == "conflict":
            old_parent = previous.get("cacheCode")
            new_parent = incoming.get("cacheCode")
            conflict_evidence = deepcopy(previous.get("mappingConflictEvidence") or {})
            if not isinstance(conflict_evidence, dict):
                conflict_evidence = {}
            conflict_evidence.setdefault("previousCacheCode", old_parent)
            additional = list(conflict_evidence.get("additionalConflicts") or [])
            if new_parent and new_parent != old_parent:
                already_recorded = (
                    new_parent == conflict_evidence.get("conflictingCacheCode")
                    and incoming.get("mappingEvidence") == conflict_evidence.get("conflictingEvidence")
                )
                observation = {"cacheCode": new_parent, "evidence": incoming.get("mappingEvidence")}
                if not already_recorded and observation not in additional:
                    additional.append(observation)
            conflict_evidence["additionalConflicts"] = additional
            merged["cacheCode"] = old_parent
            merged["mappingStatus"] = "conflict"
            merged["mappingConflictEvidence"] = conflict_evidence
            merged["mappingEvidence"] = previous.get("mappingEvidence")
            merged["role"] = "unknown"
        elif previous.get("mappingStatus") == "verified" and previous.get("cacheCode"):
            old_parent = previous["cacheCode"]
            new_parent = incoming.get("cacheCode")
            if new_parent and old_parent.upper() != new_parent.upper():
                merged["cacheCode"] = old_parent
                merged["mappingStatus"] = "conflict"
                old_evidence = previous.get("mappingEvidence")
                new_evidence = incoming.get("mappingEvidence")
                merged["mappingConflictEvidence"] = {
                    "previousCacheCode": old_parent,
                    "previousEvidence": old_evidence,
                    "conflictingCacheCode": new_parent,
                    "conflictingEvidence": new_evidence,
                }
                merged["mappingEvidence"] = "; ".join(
                    value for value in (old_evidence, new_evidence) if isinstance(value, str) and value
                ) or None
                merged["role"] = "unknown"
            else:
                for field in ("cacheCode", "mappingStatus", "mappingEvidence", "role"):
                    if field in previous:
                        merged[field] = deepcopy(previous[field])

    old_crawl = previous.get("crawl") if isinstance(previous.get("crawl"), dict) else {}
    new_crawl = incoming.get("crawl") if incoming and isinstance(incoming.get("crawl"), dict) else {}
    crawl = deepcopy(old_crawl)
    crawl.update(new_crawl)
    attempt = last_attempt_at or new_crawl.get("lastAttemptAt")
    if attempt:
        crawl["lastAttemptAt"] = attempt
    successful_data = bool(incoming and (_valid_counter_pair(incoming) or _valid_solver_list(incoming)))
    last_success = new_crawl.get("lastSuccessAt") if successful_data else None
    if last_success:
        crawl["lastSuccessAt"] = last_success
    elif old_crawl.get("lastSuccessAt"):
        crawl["lastSuccessAt"] = old_crawl["lastSuccessAt"]
    if incoming is None:
        crawl["status"] = "error"
        crawl["errorCode"] = error_code
    elif new_crawl.get("status") == "error":
        crawl["status"] = "error"
        crawl["errorCode"] = error_code or new_crawl.get("errorCode")
    else:
        crawl["status"] = new_crawl.get("status", "ok")
        crawl["errorCode"] = None
    # Coverage is specific to this parse attempt; never inherit it on failure.
    crawl["fieldCoverage"] = new_crawl.get("fieldCoverage", "unknown")
    crawl["stale"] = _stale((merged.get("counterObservedAt"), merged.get("solverListObservedAt")),
                            attempt, freshness_hours)
    merged["crawl"] = crawl
    if _valid_counter_pair(merged):
        total = merged["successCount"] + merged["failureCount"]
        merged["successRate"] = merged["successCount"] / total if total else None
    else:
        merged["successRate"] = None
    return merged


class CertitudeStore:
    def __init__(self, connection):
        self.connection = connection

    def _run(self, query, params=None, fetch=False):
        cursor = self.connection.cursor()
        try:
            cursor.execute(query, params)
            rows = cursor.fetchall() if fetch else None
            self.connection.commit()
            return rows
        except Exception:
            self.connection.rollback()
            raise
        finally:
            close = getattr(cursor, "close", None)
            if close:
                close()

    @staticmethod
    def _decode(value):
        return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value

    def ensure_schema(self):
        sql = (Path(__file__).resolve().parent / "sql" / "certitude_setup.sql").read_text(encoding="utf-8")
        self._run(sql)

    def load_descriptions(self):
        rows = self._run("SELECT cache_code, record FROM certitude_descriptions", fetch=True)
        return {code: self._decode(record) for code, record in rows}

    def load_checkers(self):
        rows = self._run("SELECT checker_id, record FROM certitude_checkers", fetch=True)
        return {checker_id: self._decode(record) for checker_id, record in rows}

    def load_activity_scans(self):
        rows = self._run("SELECT scope, record FROM certitude_activity_scans", fetch=True)
        return {scope: self._decode(record) for scope, record in rows}

    def save_activity_scan(self, scope, record):
        if not isinstance(record, dict):
            raise ValueError("activity scan record must be a JSON object")
        updated_at = record.get("lastAttemptAt") or datetime.now(timezone.utc).isoformat()
        self._run(
            "INSERT INTO certitude_activity_scans (scope, record, updated_at) VALUES (%s, %s::jsonb, %s) "
            "ON CONFLICT (scope) DO UPDATE SET record = EXCLUDED.record, updated_at = EXCLUDED.updated_at",
            (scope, json.dumps(record, ensure_ascii=False), updated_at),
        )

    def save_description(self, code, record):
        existing = self._run("SELECT record FROM certitude_descriptions WHERE cache_code = %s", (code,), fetch=True)
        previous = self._decode(existing[0][0]) if existing else {}
        merged = deepcopy(record)
        links = list(merged.get("descriptionLinks") or [])
        known = {json.dumps(row, ensure_ascii=False, sort_keys=True): row
                 for row in links if isinstance(row, dict)}
        for row in previous.get("descriptionLinks", []):
            if isinstance(row, dict) and row.get("mappingStatus") == "verified":
                known.setdefault(json.dumps(row, ensure_ascii=False, sort_keys=True), row)
        merged["descriptionLinks"] = list(known.values())
        self._run(
            "INSERT INTO certitude_descriptions (cache_code, record, last_attempt_at, scanned_at) "
            "VALUES (%s, %s::jsonb, %s, %s) ON CONFLICT (cache_code) DO UPDATE SET "
            "record = EXCLUDED.record, last_attempt_at = EXCLUDED.last_attempt_at, scanned_at = EXCLUDED.scanned_at",
            (code, json.dumps(merged, ensure_ascii=False), merged.get("lastAttemptAt"),
             merged.get("descriptionScannedAt")),
        )

    def save_checker(self, record, snapshot=None):
        checker_id = record["checkerId"]
        statements = [
            ("INSERT INTO certitude_checkers (checker_id, record, updated_at) VALUES (%s, %s::jsonb, %s) "
             "ON CONFLICT (checker_id) DO UPDATE SET record = EXCLUDED.record, updated_at = EXCLUDED.updated_at",
             (checker_id, json.dumps(record, ensure_ascii=False), record.get("crawl", {}).get("lastAttemptAt") or
              record.get("crawl", {}).get("lastSuccessAt") or datetime.now(timezone.utc).isoformat())),
        ]
        if snapshot is not None:
            observed_at = snapshot.get("observedAt")
            crawl_status = record.get("crawl", {}).get("status")
            if (not observed_at or _parse_time(observed_at) is None or
                (snapshot.get("checkerId") is not None and snapshot.get("checkerId") != checker_id) or
                not _valid_counter_pair({
                "successCount": snapshot.get("successCount"), "failureCount": snapshot.get("failureCount"),
                "counterObservedAt": observed_at,
                }) or crawl_status not in ("ok", "error")):
                raise ValueError("snapshot requires matching ID, valid pair/time, and an ok/error crawl status")
            snapshot_record = dict(snapshot)
            snapshot_record["checkerId"] = checker_id
            statements.append((
                "INSERT INTO certitude_snapshots (checker_id, observed_at, record) VALUES (%s, %s, %s::jsonb) "
                "ON CONFLICT (checker_id, observed_at) DO NOTHING",
                (checker_id, observed_at, json.dumps(snapshot_record, ensure_ascii=False)),
            ))
        cursor = self.connection.cursor()
        try:
            for query, params in statements:
                cursor.execute(query, params)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            close = getattr(cursor, "close", None)
            if close:
                close()
