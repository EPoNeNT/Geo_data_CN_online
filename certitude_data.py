"""Pure derivation and validation for the Certitude static dataset."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import re


REGIONS = ("all", "china", "taiwan", "hong-kong", "macao")
AVAILABILITIES = ("all", "active", "archived")
RULES = {"single_final", "any_final", "all_final"}
STATUSES = {"complete", "partial", "unknown"}
_GC_RE = re.compile(r"^GC[A-Z0-9]+$")


def _norm(value):
    return value.strip().casefold() if isinstance(value, str) else ""


def _time_sort(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def _checker_id(row):
    return row.get("checkerId") if isinstance(row.get("checkerId"), str) else ""


def _solver_rows(checker):
    rows = checker.get("publicSolvers") or []
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("username"), str):
            continue
        name = row["username"].strip()
        key = _norm(row.get("normalizedUsername") or name)
        if not name or not key:
            continue
        row = {
            "username": name,
            "normalizedUsername": key,
            "sourceUserId": row.get("sourceUserId"),
            "certifiedAt": row.get("certifiedAt"),
            "sourceRank": row.get("sourceRank"),
        }
        identity = ("id", row["sourceUserId"]) if row.get("sourceUserId") else ("name", key)
        prev = result.get(identity)
        if prev is None:
            result[identity] = dict(row, username=name, normalizedUsername=key)
        elif _time_sort(row.get("certifiedAt")) is not None and (
            _time_sort(prev.get("certifiedAt")) is None or
            _time_sort(row.get("certifiedAt")) < _time_sort(prev.get("certifiedAt"))
        ):
            result[identity] = dict(row, username=name, normalizedUsername=key)
    return list(result.values())


def _rule_state(cache, by_id):
    rule = cache.get("completionRule")
    ids = cache.get("completionCheckerIds")
    if rule not in RULES or not isinstance(ids, list) or not ids:
        return False, [], rule
    ids = list(dict.fromkeys(i for i in ids if isinstance(i, str) and i))
    if cache.get("completionRuleConflict") is True or cache.get("ruleConflict") is True:
        return False, [], rule
    if not ids or any(
        i not in by_id or by_id[i].get("role") != "final" or
        ("mappingStatus" in by_id[i] and by_id[i].get("mappingStatus") != "verified")
        for i in ids
    ):
        return False, [], rule
    if rule == "single_final" and len(ids) != 1:
        return False, [], rule
    return True, ids, rule


def _completion(rule, final_ids, present):
    if rule == "single_final":
        return bool(final_ids and final_ids[0] in present)
    if rule == "any_final":
        return any(i in present for i in final_ids)
    return bool(final_ids) and all(i in present for i in final_ids)


def _cache_stats(cache, checkers, certified):
    known, final_ids, rule = _rule_state(cache, { _checker_id(c): c for c in checkers })
    observed = len(certified) if known else 0
    def valid_pair(c):
        return (isinstance(c.get("successCount"), int) and not isinstance(c.get("successCount"), bool) and c["successCount"] >= 0 and
                isinstance(c.get("failureCount"), int) and not isinstance(c.get("failureCount"), bool) and c["failureCount"] >= 0 and
                _time_sort(c.get("counterObservedAt")) is not None)
    valid_pairs = sum(valid_pair(c) for c in checkers)
    counter_unknown = valid_pairs != len(checkers) or not checkers
    if counter_unknown:
        success = failure = attempts = rate = None
    else:
        success = sum(c["successCount"] for c in checkers)
        failure = sum(c["failureCount"] for c in checkers)
        attempts = success + failure
        rate = success / attempts if attempts else None
    if checkers and valid_pairs == len(checkers):
        counter_coverage = "complete"
    elif valid_pairs:
        counter_coverage = "partial"
    else:
        counter_coverage = "unknown"
    required_lists = [c for c in checkers if _checker_id(c) in final_ids] if known else []
    lists_complete = bool(known and required_lists and all(c.get("solverListCoverage") == "complete" for c in required_lists))
    return {
        "knownRule": known,
        "complete": lists_complete,
        "observedCount": observed,
        "correctCount": observed if lists_complete else None,
        "publicCorrectCount": observed if known else None,
        "successCount": success,
        "failureCount": failure,
        "attemptCount": attempts,
        "submissionRate": rate,
        "counterCoverage": counter_coverage,
    }


def build_cache_detail(cache, checkers, found_logs):
    """Derive one cache's detail document body; caller adds common envelope."""
    raw_cache = dict(cache)
    dedup = {}
    for checker_row in checkers:
        cid = _checker_id(checker_row)
        if cid and cid not in dedup:
            row = dict(checker_row)
            row["publicSolvers"] = _solver_rows(checker_row)
            row["solverRowsRetrieved"] = checker_row.get("solverRowsRetrieved", len(checker_row.get("publicSolvers") or []))
            row["publicSolverCount"] = len(row["publicSolvers"]) if checker_row.get("solverListCoverage") == "complete" else None
            dedup[cid] = row
    checker_rows = list(dedup.values())
    by_id = {_checker_id(c): c for c in checker_rows}
    solvers_by_checker = {
        _checker_id(c): {solver["normalizedUsername"]: solver for solver in c["publicSolvers"]}
        for c in checker_rows
    }
    known, final_ids, rule = _rule_state(raw_cache, by_id)
    candidates = defaultdict(dict)
    stage_checkers = [c for c in checker_rows if c.get("role") == "stage"]
    for c in checker_rows:
        cid = _checker_id(c)
        found = {_norm(s.get("normalizedUsername")): s for s in c["publicSolvers"]}
        for solver in c["publicSolvers"]:
            key = solver["normalizedUsername"]
            candidates[key].setdefault("username", solver["username"])
    owner_name = raw_cache.get("owner")
    owner_key = _norm(raw_cache.get("ownerKey") or owner_name)
    if owner_key:
        candidates[owner_key].setdefault("username", owner_name.strip() if isinstance(owner_name, str) else owner_key)
    logs_by_user = {}
    for log in found_logs:
        name = log.get("username")
        key = _norm(name)
        if not key:
            continue
        candidates[key].setdefault("username", name.strip())
        current = logs_by_user.get(key)
        if current is None or (log.get("foundDate") or "9999") < (current.get("foundDate") or "9999"):
            logs_by_user[key] = log
    player_records = {}
    for key, user in candidates.items():
        checks = []
        for checker_row in checker_rows:
            cid = _checker_id(checker_row)
            solver = solvers_by_checker[cid].get(key)
            checks.append({
                "checkerId": cid,
                "status": "certified" if solver else "no_public_record" if checker_row.get("solverListCoverage") == "complete" else "unknown",
                "certifiedAt": solver.get("certifiedAt") if solver else None,
                "sourceRank": solver.get("sourceRank") if solver else None,
                "solverListCoverage": checker_row.get("solverListCoverage", "unknown"),
                "solverListObservedAt": checker_row.get("solverListObservedAt"),
            })
        present = {r["checkerId"] for r in checks if r["status"] == "certified"}
        stages = {r["checkerId"] for r in checks if r["status"] == "certified" and by_id[r["checkerId"]].get("role") == "stage"}
        complete = known and _completion(rule, final_ids, present)
        if complete:
            solve_status = "certified"
        elif not known:
            solve_status = "unknown"
        elif stages:
            solve_status = "partial"
        elif known and all(by_id[i].get("solverListCoverage") == "complete" for i in final_ids):
            solve_status = "no_public_record"
        else:
            solve_status = "unknown"
        time_values = []
        if complete:
            if rule in ("single_final", "any_final"):
                satisfied = [i for i in final_ids if i in present] if rule == "any_final" else final_ids
                times = [next((r.get("certifiedAt") for r in checks if r["checkerId"] == i), None) for i in satisfied]
                if all(_time_sort(t) is not None for t in times):
                    time_values = [min(times, key=_time_sort)] if rule == "any_final" else times[:1]
            else:
                times = [next((r.get("certifiedAt") for r in checks if r["checkerId"] == i), None) for i in final_ids]
                if all(_time_sort(t) is not None for t in times):
                    time_values = [max(times, key=_time_sort)]
        log = logs_by_user.get(key)
        stage_coverage = ("complete" if raw_cache.get("checkerSetCoverage") == "complete" else
                          "partial" if raw_cache.get("checkerSetCoverage") == "partial" else "unknown")
        relevant = [by_id[i] for i in final_ids] if known else checker_rows
        list_coverage = _aggregate_list_coverage(relevant)
        observed_times = [c.get("solverListObservedAt") for c in relevant if _time_sort(c.get("solverListObservedAt")) is not None]
        attempt_times = [c.get("crawl", {}).get("lastAttemptAt") for c in relevant if _time_sort(c.get("crawl", {}).get("lastAttemptAt")) is not None]
        player_records[key] = {
            "username": user["username"], "normalizedUsername": key, "cacheCode": raw_cache["code"],
            "solveStatus": solve_status,
            "solveReason": None,
            "checkerResults": checks,
            "stageProgress": {"completed": len(stages), "denominator": len(stage_checkers) if stage_coverage == "complete" else None,
                              "coverage": stage_coverage},
            "certifiedAt": time_values[0] if time_values else None,
            "foundStatus": "found" if log else "unknown",
            "foundAt": log.get("foundAt") if log else None,
            "foundDate": log.get("foundDate") if log else None,
            "isOwner": key == owner_key if owner_key else None,
            "sourceCoverage": {
                "publicSolverLists": list_coverage,
                "foundItLogs": raw_cache.get("foundLogsCoverage", "unknown"),
                "certitudeObservedAt": min(observed_times, key=_time_sort) if observed_times else None,
                "certitudeLastAttemptAt": max(attempt_times, key=_time_sort) if attempt_times else None,
                "foundLogsAsOf": raw_cache.get("foundLogsAsOf"),
                "foundLogsLastAttemptAt": raw_cache.get("foundLogsLastAttemptAt"),
            },
        }
    certified_solvers = []
    for key, record in player_records.items():
        if record["solveStatus"] == "certified" and record.get("isOwner") is not True:
            certified_solvers.append({"key": key, "username": record["username"], "certifiedAt": record["certifiedAt"]})
    certified_solvers.sort(key=lambda r: r["key"])
    stats = _cache_stats(raw_cache, checker_rows, certified_solvers)
    raw_cache["checkers"] = checker_rows
    raw_cache["playerRecords"] = player_records
    raw_cache["completionRule"] = rule
    raw_cache["completionCheckerIds"] = final_ids
    raw_cache["checkerSetCoverage"] = raw_cache.get("checkerSetCoverage", "unknown")
    relevant_final = [by_id[i] for i in final_ids] if known else checker_rows
    cache_found_coverage = raw_cache.get("foundLogsCoverage", "unknown")
    detail = {
        "cache": raw_cache,
        "stats": stats,
        "certifiedSolvers": certified_solvers,
        "playerOptions": [{"key": k, "username": v["username"]} for k, v in sorted(candidates.items())],
        "lookupCoverage": {"publicSolverLists": _aggregate_list_coverage(relevant_final), "foundItLogs": cache_found_coverage,
                           "certitudeObservedAt": min((c.get("solverListObservedAt") for c in relevant_final if _time_sort(c.get("solverListObservedAt")) is not None), key=_time_sort, default=None),
                           "foundLogsAsOf": raw_cache.get("foundLogsAsOf"), "foundUsers": {}},
    }
    return detail


def _aggregate_list_coverage(rows):
    if rows and all(c.get("solverListCoverage") == "complete" for c in rows):
        return "complete"
    if any(c.get("solverListCoverage") == "partial" for c in rows):
        return "partial"
    return "unknown"


def _rank_rows(rows, score_field):
    ordered = sorted(rows, key=lambda r: (-r[score_field], r["key"]))
    rank = 0
    previous = None
    for index, row in enumerate(ordered, 1):
        if row[score_field] != previous:
            rank = index
            previous = row[score_field]
        row["rank"] = rank
    return ordered


def _puzzle_rankings(caches):
    result = {}
    metrics = {"rate": "submissionRate", "correct": "publicCorrectCount", "attempts": "attemptCount"}
    for output, field in metrics.items():
        result[output] = {}
        for direction in ("asc", "desc"):
            sorted_rows = sorted(caches, key=lambda c: (c["stats"].get(field) is None,
                                                        (c["stats"].get(field) or 0) * (1 if direction == "asc" else -1),
                                                        c["code"]))
            rows, last, rank = [], object(), 0
            for i, cache_row in enumerate(sorted_rows, 1):
                value = cache_row["stats"].get(field)
                if value is not None and value != last:
                    rank = i
                    last = value
                rows.append({"code": cache_row["code"], "rank": rank if value is not None else None})
            result[output][direction] = rows
    return result


def build_scope(details, region, availability):
    selected = []
    for detail in details:
        c = detail.get("cache", detail)
        if region != "all" and c.get("region") != region:
            continue
        status = c.get("cacheStatus")
        if availability == "active" and (status is None or status == 2):
            continue
        if availability == "archived" and status != 2:
            continue
        selected.append(detail)
    cache_rows = []
    solvers, owners = defaultdict(set), defaultdict(set)
    solver_owners = defaultdict(set)
    solver_names, owner_names = {}, {}
    edges = defaultdict(set)
    for detail in selected:
        c = detail.get("cache", detail)
        stats = detail.get("stats", {})
        code = c["code"].upper()
        owner_key = _norm(c.get("ownerKey") or c.get("owner")) or None
        owner_name = c.get("owner")
        cache_rows.append({"code": code, "name": c.get("name"), "owner": owner_name, "ownerKey": owner_key,
                           "geocacheType": c.get("geocacheType", 8), "region": c.get("region"), "city": c.get("city"),
                           "cacheStatus": c.get("cacheStatus"), "detailsUrl": c.get("detailsUrl"),
                           "finalCheckerId": c.get("finalCheckerId"), "finalCertitudeUrl": c.get("finalCertitudeUrl"), "stats": stats})
        if owner_key:
            owners[owner_key].add(code)
            owner_names[owner_key] = owner_name or owner_key
        for solver in detail.get("certifiedSolvers", []):
            skey = _norm(solver.get("key") or solver.get("username"))
            if not skey or (owner_key and skey == owner_key):
                continue
            solvers[skey].add(code)
            solver_names[skey] = solver.get("username") or skey
            if owner_key:
                solver_owners[skey].add(owner_key)
                edges[(owner_key, skey)].add(code)
    cache_rows.sort(key=lambda r: r["code"])
    solver_rows = _rank_rows([{"key": k, "username": solver_names[k], "solvedCount": len(v), "ownerCount": len(solver_owners[k]),
                                "cacheCodes": sorted(v)} for k, v in solvers.items()], "solvedCount")
    owner_rows = []
    for key, codes in owners.items():
        solver_codes = set().union(*(edges[(key, sk)] for sk in solvers if (key, sk) in edges)) if solvers else set()
        cert_count = sum(len(v) for (ok, _), v in edges.items() if ok == key)
        owner_rows.append({"key": key, "username": owner_names[key], "puzzleCount": len(codes),
                           "solverCount": len({sk for ok, sk in edges if ok == key}),
                           "certificationCount": cert_count, "cacheCodes": sorted(codes),
                           "_weight": cert_count})
    owner_rows = _rank_rows(owner_rows, "puzzleCount")
    for row in owner_rows:
        row.pop("_weight", None)
    edge_rows = [{"ownerKey": ok, "ownerName": owner_names[ok], "solverKey": sk,
                  "solverName": solver_names[sk], "count": len(codes), "cacheCodes": sorted(codes)}
                 for (ok, sk), codes in sorted(edges.items())]
    owner_weights, solver_weights = defaultdict(int), defaultdict(int)
    for edge in edge_rows:
        owner_weights[edge["ownerKey"]] += edge["count"]
        solver_weights[edge["solverKey"]] += edge["count"]
    players = {}
    for key, name in {**owner_names, **solver_names}.items():
        players[key] = {"key": key, "username": name}
    scope_obj = {"region": region, "availability": availability}
    coverage = {"ruleUnknownCacheCount": sum(not c["stats"].get("knownRule") for c in cache_rows),
                "incompleteSolverCacheCount": sum(not c["stats"].get("complete") for c in cache_rows),
                "noOwnerCacheCount": sum(not c.get("ownerKey") for c in cache_rows)}
    return {"scope": scope_obj,
            "summary": {"cacheCount": len(cache_rows), "solverCount": len(solver_rows), "ownerCount": len(owner_rows),
                        "certificationCount": sum(len(v) for v in solvers.values())},
            "caches": cache_rows, "rankings": {"solvers": solver_rows, "owners": owner_rows},
            "puzzleRankings": _puzzle_rankings(cache_rows),
            "graph": {"players": [players[k] for k in sorted(players)], "edges": edge_rows,
                      "order": {"owners": sorted(owner_weights, key=lambda k: (-owner_weights[k], k)),
                                "solvers": sorted(solver_weights, key=lambda k: (-solver_weights[k], k))}},
            "coverage": coverage}


def _fail(message):
    raise ValueError(message)


def _valid_timestamp(value):
    return isinstance(value, str) and _time_sort(value) is not None


def validate_dataset(manifest, scopes, details):
    """Raise ValueError on inconsistent dataset; return True when valid."""
    version = manifest.get("datasetVersion")
    generated = manifest.get("generatedAt")
    if manifest.get("schemaVersion") != 1 or not isinstance(version, str) or not version:
        _fail("invalid manifest version")
    if not _valid_timestamp(generated):
        _fail("invalid manifest generatedAt")
    if manifest.get("regions") != list(REGIONS) or manifest.get("availabilities") != list(AVAILABILITIES):
        _fail("invalid manifest scope enums")
    if f"/{version}/scopes/{{region}}/{{availability}}.json" not in manifest.get("scopePathTemplate", "") or \
       f"/{version}/caches/{{code}}.json" not in manifest.get("cachePathTemplate", ""):
        _fail("manifest paths do not match version")
    if len(scopes) != 15:
        _fail("expected all 15 scopes")
    scope_map = {}
    for scope in scopes:
        if scope.get("schemaVersion") != 1 or scope.get("datasetVersion") != version or scope.get("generatedAt") != generated:
            _fail("scope envelope mismatch")
        key = (scope.get("scope", {}).get("region"), scope.get("scope", {}).get("availability"))
        if key[0] not in REGIONS or key[1] not in AVAILABILITIES or key in scope_map:
            _fail("invalid or duplicate scope")
        scope_map[key] = scope
        caches = scope.get("caches")
        if not isinstance(caches, list):
            _fail("scope caches must be list")
        codes = [c.get("code") for c in caches]
        if len(codes) != len(set(codes)) or any(not isinstance(code, str) or not _GC_RE.match(code) for code in codes):
            _fail("invalid or duplicate cache code")
        if scope.get("summary", {}).get("cacheCount") != len(caches):
            _fail("scope cache count mismatch")
        if any(not isinstance(c.get("stats"), dict) for c in caches):
            _fail("cache stats missing")
        for cache_row in caches:
            st = cache_row["stats"]
            for field in ("observedCount", "publicCorrectCount", "correctCount", "successCount", "failureCount", "attemptCount"):
                value = st.get(field)
                if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                    _fail("invalid cache stat count")
            rate = st.get("submissionRate")
            if rate is not None and (not isinstance(rate, (int, float)) or isinstance(rate, bool) or not 0 <= rate <= 1):
                _fail("invalid submission rate")
            if not st.get("knownRule") and st.get("publicCorrectCount") is not None:
                _fail("unknown completion rule has a public correct count")
            if st.get("attemptCount") is not None and st.get("successCount") is not None and st.get("failureCount") is not None and st["attemptCount"] != st["successCount"] + st["failureCount"]:
                _fail("attempt count does not match counter pair")
        for code in codes:
            detail = details.get(code)
            if not detail:
                _fail("scope cache missing detail")
            if detail.get("schemaVersion") != 1 or detail.get("datasetVersion") != version or detail.get("generatedAt") != generated:
                _fail("detail envelope mismatch")
            if detail.get("cache", {}).get("code") != code:
                _fail("detail code mismatch")
            if detail.get("stats") != next(c["stats"] for c in caches if c["code"] == code):
                _fail("detail stats mismatch")
        if any(code not in details for code in codes):
            _fail("detail reference missing")
        for edge in scope.get("graph", {}).get("edges", []):
            if edge.get("count") != len(edge.get("cacheCodes", [])) or len(set(edge.get("cacheCodes", []))) != len(edge.get("cacheCodes", [])):
                _fail("graph edge count mismatch")
            if edge.get("ownerKey") == edge.get("solverKey"):
                _fail("self solve graph edge")
            if not set(edge.get("cacheCodes", [])).issubset(set(codes)):
                _fail("graph edge cache reference missing")
        for metric in ("rate", "correct", "attempts"):
            for direction in ("asc", "desc"):
                rows = scope.get("puzzleRankings", {}).get(metric, {}).get(direction, [])
                if {r.get("code") for r in rows} != set(codes) or len(rows) != len(codes):
                    _fail("puzzle rank coverage mismatch")
                values = {c["code"]: c["stats"].get({"rate": "submissionRate", "correct": "publicCorrectCount", "attempts": "attemptCount"}[metric]) for c in caches}
                sorted_values = sorted(values, key=lambda code: (values[code] is None,
                    (values[code] or 0) * (1 if direction == "asc" else -1), code))
                if [r["code"] for r in rows] != sorted_values:
                    _fail("puzzle rank order mismatch")
                prior, rank = object(), 0
                for index, row in enumerate(rows, 1):
                    value = values[row["code"]]
                    if value is not None and value != prior:
                        rank, prior = index, value
                    expected = rank if value is not None else None
                    if row.get("rank") != expected:
                        _fail("puzzle rank value mismatch")
        rankings = scope.get("rankings", {})
        for kind, field in (("solvers", "solvedCount"), ("owners", "puzzleCount")):
            rows = rankings.get(kind, [])
            if any(not isinstance(row.get(field), int) or row[field] < 0 for row in rows):
                _fail("invalid player ranking count")
            expected_order = sorted(rows, key=lambda row: (-row[field], row["key"]))
            if [row.get("key") for row in rows] != [row.get("key") for row in expected_order]:
                _fail("player ranking order mismatch")
            prior, rank = object(), 0
            for index, row in enumerate(rows, 1):
                if row[field] != prior:
                    rank, prior = index, row[field]
                if row.get("rank") != rank or row.get("cacheCodes") != sorted(set(row.get("cacheCodes", []))) or row[field] != len(row["cacheCodes"]):
                    _fail("player ranking count or rank mismatch")
        summary = scope["summary"]
        if summary.get("solverCount") != len(rankings.get("solvers", [])) or summary.get("ownerCount") != len(rankings.get("owners", [])):
            _fail("scope player count mismatch")
        if summary.get("certificationCount") != sum(row["solvedCount"] for row in rankings.get("solvers", [])):
            _fail("scope certification count mismatch")
        graph = scope.get("graph", {})
        player_keys = [p.get("key") for p in graph.get("players", [])]
        expected_player_keys = sorted({r["key"] for r in rankings.get("solvers", [])} | {r["key"] for r in rankings.get("owners", [])})
        if player_keys != expected_player_keys:
            _fail("graph player directory mismatch")
        owner_weights, solver_weights = defaultdict(int), defaultdict(int)
        for edge in graph.get("edges", []):
            owner_weights[edge["ownerKey"]] += edge["count"]
            solver_weights[edge["solverKey"]] += edge["count"]
        expected_owner_order = sorted(owner_weights, key=lambda k: (-owner_weights[k], k))
        expected_solver_order = sorted(solver_weights, key=lambda k: (-solver_weights[k], k))
        if graph.get("order", {}).get("owners") != expected_owner_order or graph.get("order", {}).get("solvers") != expected_solver_order:
            _fail("graph order mismatch")
        expected_scope = build_scope(list(details.values()), key[0], key[1])
        for field in ("scope", "summary", "caches", "rankings", "puzzleRankings", "graph", "coverage"):
            if scope.get(field) != expected_scope.get(field):
                _fail(f"scope {field} does not match cache detail facts")
    if len(scope_map) != 15:
        _fail("scope combination missing")
    if manifest.get("cacheCount") != len(details):
        _fail("manifest cache count mismatch")
    if len({c for scope in scope_map.values() if scope["scope"] == {"region": "all", "availability": "all"} for c in [r["code"] for r in scope["caches"]]}) != len(details):
        _fail("all scope does not represent the dataset detail set")
    for code, detail in details.items():
        if not isinstance(code, str) or code != code.upper() or not _GC_RE.match(code):
            _fail("invalid detail code key")
        if detail.get("schemaVersion") != 1 or detail.get("datasetVersion") != version or detail.get("generatedAt") != generated:
            _fail("detail envelope mismatch")
        if detail.get("cache", {}).get("code") != code:
            _fail("detail cache code mismatch")
        cache_data = detail.get("cache", {})
        checker_rows = cache_data.get("checkers", [])
        if not isinstance(checker_rows, list):
            _fail("detail checkers must be a list")
        for checker_row in checker_rows:
            for field in ("successCount", "failureCount", "solverRowsRetrieved", "publicSolverCount"):
                value = checker_row.get(field)
                if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                    _fail("invalid checker count")
        found_logs = [{"username": row.get("username"), "foundDate": row.get("foundDate"), "foundAt": row.get("foundAt")}
                      for row in cache_data.get("playerRecords", {}).values() if row.get("foundStatus") == "found"]
        rebuilt = build_cache_detail(cache_data, checker_rows, found_logs)
        for field in ("stats", "certifiedSolvers", "playerOptions"):
            if detail.get(field) != rebuilt.get(field):
                _fail(f"detail {field} does not match checker facts")
        player_records = cache_data.get("playerRecords", {})
        if player_records != rebuilt["cache"].get("playerRecords"):
            _fail("player records do not match checker and log facts")
        if detail.get("stats", {}).get("knownRule"):
            ids = cache_data.get("completionCheckerIds", [])
            if cache_data.get("completionRule") == "single_final" and len(ids) == 1:
                final = next((c for c in checker_rows if _checker_id(c) == ids[0]), None)
                if cache_data.get("finalCheckerId") != ids[0] or not final or cache_data.get("finalCertitudeUrl") != final.get("sourceUrl"):
                    _fail("final checker link does not match verified final")
    return True
