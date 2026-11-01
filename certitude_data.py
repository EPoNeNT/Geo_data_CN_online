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


def build_cache_facts(cache, checkers):
    """Build internal-only cache facts used to derive public summary scopes."""
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
    owner_name = raw_cache.get("owner")
    owner_key = _norm(owner_name)
    solver_names = {}
    checker_solver_keys = {}
    for c in checker_rows:
        cid = _checker_id(c)
        checker_solver_keys[cid] = {s["normalizedUsername"] for s in c["publicSolvers"]}
        for solver in c["publicSolvers"]:
            solver_names.setdefault(solver["normalizedUsername"], solver["username"])
    certified_solvers = []
    if known:
        keys = set().union(*(checker_solver_keys.get(cid, set()) for cid in final_ids))
        for key in sorted(keys):
            present = {cid for cid in final_ids if key in checker_solver_keys.get(cid, set())}
            if not _completion(rule, final_ids, present) or (owner_key and key == owner_key):
                continue
            required = [cid for cid in final_ids if cid in present] if rule == "any_final" else final_ids
            times = [solvers_by_checker[cid][key].get("certifiedAt") for cid in required]
            certified_at = None
            if times and all(_time_sort(value) is not None for value in times):
                certified_at = (min(times, key=_time_sort) if rule == "any_final" else
                                max(times, key=_time_sort) if rule == "all_final" else times[0])
            certified_solvers.append({"key": key, "username": solver_names[key], "certifiedAt": certified_at})
    stats = _cache_stats(raw_cache, checker_rows, certified_solvers)
    raw_cache["completionRule"] = rule
    raw_cache["completionCheckerIds"] = final_ids
    raw_cache["checkerSetCoverage"] = raw_cache.get("checkerSetCoverage", "unknown")
    return {"cache": raw_cache, "checkers": checker_rows, "stats": stats, "certifiedSolvers": certified_solvers}


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


def build_scope(facts, region, availability):
    selected = []
    for detail in facts:
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
        owner_key = _norm(c.get("owner")) or None
        owner_name = c.get("owner")
        public_stats = {field: stats.get(field) for field in
                        ("publicCorrectCount", "successCount", "failureCount", "attemptCount", "submissionRate")}
        cache_rows.append({"code": code, "name": c.get("name"), "owner": owner_name,
                           "geocacheType": c.get("geocacheType", 8), "region": c.get("region"),
                           "cacheStatus": c.get("cacheStatus"), "detailsUrl": c.get("detailsUrl"),
                           "finalCertitudeUrl": c.get("finalCertitudeUrl"), "stats": public_stats})
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
                           "cacheCodes": sorted(codes), "_weight": cert_count})
    owner_rows = _rank_rows(owner_rows, "puzzleCount")
    for row in owner_rows:
        row.pop("_weight", None)
    edge_rows = [{"ownerKey": ok, "ownerName": owner_names[ok], "solverKey": sk,
                  "solverName": solver_names[sk], "count": len(codes)}
                 for (ok, sk), codes in sorted(edges.items())]
    owner_weights, solver_weights = defaultdict(int), defaultdict(int)
    for edge in edge_rows:
        owner_weights[edge["ownerKey"]] += edge["count"]
        solver_weights[edge["solverKey"]] += edge["count"]
    players = {}
    for key, name in {**owner_names, **solver_names}.items():
        players[key] = {"key": key, "username": name}
    scope_obj = {"region": region, "availability": availability}
    return {"scope": scope_obj,
            "summary": {"cacheCount": len(cache_rows), "solverCount": len(solver_rows), "ownerCount": len(owner_rows),
                        "certificationCount": sum(len(v) for v in solvers.values())},
            "caches": cache_rows, "rankings": {"solvers": solver_rows, "owners": owner_rows},
            "puzzleRankings": _puzzle_rankings(cache_rows),
            "graph": {"players": [players[k] for k in sorted(players)], "edges": edge_rows,
                      "order": {"owners": sorted(owner_weights, key=lambda k: (-owner_weights[k], k)),
                                "solvers": sorted(solver_weights, key=lambda k: (-solver_weights[k], k))}}}


def _fail(message):
    raise ValueError(message)


def _valid_timestamp(value):
    if not isinstance(value, str) or _time_sort(value) is None:
        return False
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.tzinfo is not None and dt.utcoffset().total_seconds() == 0
    except (TypeError, ValueError, AttributeError):
        return False


def _canonical_scope_projection(scope):
    """Return the required public fields, ignoring retained legacy extras."""
    return {
        "scope": {k: scope["scope"][k] for k in ("region", "availability")},
        "summary": {k: scope["summary"][k] for k in ("cacheCount", "solverCount", "ownerCount", "certificationCount")},
        "caches": [{"code": row["code"], "name": row.get("name"), "owner": row.get("owner"),
                    "geocacheType": row["geocacheType"], "region": row.get("region"),
                    "cacheStatus": row.get("cacheStatus"), "detailsUrl": row.get("detailsUrl"),
                    "finalCertitudeUrl": row.get("finalCertitudeUrl"),
                    "stats": {k: row["stats"].get(k) for k in
                              ("publicCorrectCount", "successCount", "failureCount", "attemptCount", "submissionRate")}}
                   for row in scope["caches"]],
        "rankings": {kind: [{k: row[k] for k in fields} for row in scope["rankings"][kind]]
                     for kind, fields in (("solvers", ("key", "username", "rank", "solvedCount", "ownerCount", "cacheCodes")),
                                          ("owners", ("key", "username", "rank", "puzzleCount", "solverCount", "cacheCodes")))},
        "puzzleRankings": {metric: {direction: [{"code": row["code"], "rank": row["rank"]}
                                                  for row in scope["puzzleRankings"][metric][direction]]
                                     for direction in ("asc", "desc")}
                           for metric in ("rate", "correct", "attempts")},
        "graph": {"players": [{"key": row["key"], "username": row["username"]} for row in scope["graph"]["players"]],
                  "edges": [{k: row[k] for k in ("ownerKey", "ownerName", "solverKey", "solverName", "count")}
                            for row in scope["graph"]["edges"]],
                  "order": {side: list(scope["graph"]["order"][side]) for side in ("owners", "solvers")}}
    }


def _cross_scope_projection(scope):
    """Compare scope identity and counts while allowing display-name casing to vary by filter."""
    projection = _canonical_scope_projection(scope)
    for kind in ("solvers", "owners"):
        for row in projection["rankings"][kind]:
            row["username"] = _norm(row["username"])
    for player in projection["graph"]["players"]:
        player["username"] = _norm(player["username"])
    for edge in projection["graph"]["edges"]:
        edge["ownerName"] = _norm(edge["ownerName"])
        edge["solverName"] = _norm(edge["solverName"])
    return projection


def _count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_rank_rows(rows, field, cache_codes):
    if not isinstance(rows, list):
        _fail("player rankings must be lists")
    keys = []
    prior, rank = object(), 0
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or not isinstance(row.get("key"), str) or not row["key"]:
            _fail("invalid player ranking row")
        keys.append(row["key"])
        value = row.get(field)
        codes = row.get("cacheCodes")
        if (not isinstance(row.get("username"), str) or not _count(value) or not isinstance(codes, list) or
                any(not isinstance(c, str) for c in codes) or len(codes) != len(set(codes)) or any(c not in cache_codes for c in codes)):
            _fail("invalid player ranking counts or cache references")
        if codes != sorted(codes) or value != len(codes):
            _fail("player ranking count mismatch")
        if value != prior:
            rank, prior = index, value
        if type(row.get("rank")) is not int or row.get("rank") != rank:
            _fail("player ranking rank mismatch")
    if keys != sorted(keys, key=lambda key: (-next(r[field] for r in rows if r["key"] == key), key)) or len(keys) != len(set(keys)):
        _fail("player ranking order or keys mismatch")
    return {row["key"]: row for row in rows}


def _validate_dataset(manifest, scopes, facts=None):
    """Validate schema 2 required public fields, tolerating legacy extras."""
    if not isinstance(manifest, dict):
        _fail("invalid manifest")
    version, generated = manifest.get("datasetVersion"), manifest.get("generatedAt")
    if manifest.get("schemaVersion") != 2 or not isinstance(version, str) or not version:
        _fail("invalid manifest version")
    if not _valid_timestamp(generated):
        _fail("invalid manifest generatedAt")
    if manifest.get("regions") != list(REGIONS) or manifest.get("availabilities") != list(AVAILABILITIES):
        _fail("invalid manifest scope enums")
    path = f"/data/certitude/versions/{version}/scopes/{{region}}/{{availability}}.json"
    if manifest.get("scopePathTemplate") != path:
        _fail("manifest paths do not match version")
    if not isinstance(scopes, list) or len(scopes) != 15:
        _fail("expected all 15 scopes")
    scope_map = {}
    for scope in scopes:
        if not isinstance(scope, dict) or scope.get("schemaVersion") != 2 or scope.get("datasetVersion") != version or scope.get("generatedAt") != generated:
            _fail("scope envelope mismatch")
        key = (scope.get("scope", {}).get("region"), scope.get("scope", {}).get("availability"))
        if key not in {(r, a) for r in REGIONS for a in AVAILABILITIES} or key in scope_map:
            _fail("invalid or duplicate scope")
        scope_map[key] = scope
        summary, caches = scope.get("summary"), scope.get("caches")
        rankings, graph = scope.get("rankings"), scope.get("graph")
        if not isinstance(summary, dict) or not isinstance(caches, list) or not isinstance(rankings, dict) or not isinstance(graph, dict):
            _fail("scope required objects are missing")
        codes = [row.get("code") for row in caches if isinstance(row, dict)]
        if len(codes) != len(caches) or len(codes) != len(set(codes)) or any(not isinstance(code, str) or not _GC_RE.match(code) for code in codes):
            _fail("invalid or duplicate cache code")
        if any(not _count(summary.get(f)) for f in ("cacheCount", "solverCount", "ownerCount", "certificationCount")) or summary["cacheCount"] != len(caches):
            _fail("scope summary count mismatch")
        for row in caches:
            required = ("code", "name", "owner", "geocacheType", "region", "cacheStatus", "detailsUrl", "finalCertitudeUrl", "stats")
            if any(field not in row for field in required) or type(row["geocacheType"]) is not int or row["geocacheType"] != 8 or not isinstance(row["stats"], dict):
                _fail("cache required fields missing or invalid")
            if (row["name"] is not None and not isinstance(row["name"], str) or
                    row["owner"] is not None and not isinstance(row["owner"], str) or
                    row["region"] is not None and row["region"] not in REGIONS[1:] or
                    row["cacheStatus"] is not None and type(row["cacheStatus"]) is not int or
                    row["detailsUrl"] is not None and not isinstance(row["detailsUrl"], str) or
                    row["finalCertitudeUrl"] is not None and not isinstance(row["finalCertitudeUrl"], str)):
                _fail("invalid cache nullable field")
            st = row["stats"]
            stats_fields = ("publicCorrectCount", "successCount", "failureCount", "attemptCount", "submissionRate")
            if any(field not in st for field in stats_fields):
                _fail("cache required stats missing")
            for field in ("publicCorrectCount", "successCount", "failureCount", "attemptCount"):
                if st.get(field) is not None and not _count(st[field]):
                    _fail("invalid cache stat count")
            success, failure, attempts = (st.get(k) for k in ("successCount", "failureCount", "attemptCount"))
            if (success is None) != (failure is None) or (success is None and attempts is not None) or (success is not None and attempts != success + failure):
                _fail("cache attempt count mismatch")
            rate = st.get("submissionRate")
            expected_rate = success / attempts if success is not None and attempts else None
            if rate is not None and (not isinstance(rate, (int, float)) or isinstance(rate, bool) or not 0 <= rate <= 1):
                _fail("invalid submission rate")
            if rate != expected_rate:
                _fail("cache submission rate mismatch")
        cache_codes = set(codes)
        solver_rows = _valid_rank_rows(rankings.get("solvers"), "solvedCount", cache_codes)
        owner_rows = _valid_rank_rows(rankings.get("owners"), "puzzleCount", cache_codes)
        if summary["solverCount"] != len(solver_rows) or summary["ownerCount"] != len(owner_rows):
            _fail("scope player count mismatch")
        if summary["certificationCount"] != sum(row["solvedCount"] for row in solver_rows.values()):
            _fail("scope certification count mismatch")
        for row in solver_rows.values():
            if not _count(row.get("ownerCount")) or row["key"] != _norm(row["username"]):
                _fail("invalid solver key or owner count")
        for row in owner_rows.values():
            if not _count(row.get("solverCount")) or row["key"] != _norm(row["username"]):
                _fail("invalid owner key or solver count")
        cache_map = {row["code"]: row for row in caches}
        expected_owner_codes = defaultdict(list)
        for cache_row in caches:
            owner_key = _norm(cache_row.get("owner"))
            if owner_key:
                expected_owner_codes[owner_key].append(cache_row["code"])
            count = cache_row["stats"].get("publicCorrectCount")
            if count is not None:
                actual_count = sum(cache_row["code"] in row["cacheCodes"] for row in solver_rows.values())
                if count != actual_count:
                    _fail("public correct count does not match solver GC facts")
        for key, row in owner_rows.items():
            expected_codes = sorted(expected_owner_codes.get(key, []))
            if row["cacheCodes"] != expected_codes:
                _fail("owner cache list does not match cache owners")
        for key, row in solver_rows.items():
            if any(_norm(cache_map[code].get("owner")) == key for code in row["cacheCodes"]):
                _fail("solver cache list includes own cache")
        player_rows = graph.get("players")
        edges = graph.get("edges")
        order = graph.get("order")
        if not isinstance(player_rows, list) or not isinstance(edges, list) or not isinstance(order, dict):
            _fail("graph required fields missing")
        player_keys = [p.get("key") for p in player_rows if isinstance(p, dict)]
        ranking_keys = set(solver_rows) | set(owner_rows)
        if len(player_keys) != len(player_rows) or player_keys != sorted(ranking_keys) or len(player_keys) != len(set(player_keys)):
            _fail("graph player directory mismatch")
        players = {p["key"]: p for p in player_rows}
        for p in player_rows:
            if not isinstance(p.get("username"), str) or p["key"] != _norm(p["username"]):
                _fail("invalid graph player")
        for key, player in players.items():
            canonical_name = solver_rows[key]["username"] if key in solver_rows else owner_rows[key]["username"]
            if player["username"] != canonical_name:
                _fail("graph player name mismatch")
        cache_owners = {row["code"]: _norm(row.get("owner")) or None for row in caches}
        expected_edges = defaultdict(set)
        for skey, row in solver_rows.items():
            for code in row["cacheCodes"]:
                okey = cache_owners.get(code)
                if okey and okey != skey:
                    expected_edges[(okey, skey)].add(code)
        ow, sw = defaultdict(int), defaultdict(int)
        owner_solver, solver_owner = defaultdict(set), defaultdict(set)
        actual_pairs = []
        for edge in edges:
            if not isinstance(edge, dict) or any(k not in edge for k in ("ownerKey", "ownerName", "solverKey", "solverName", "count")):
                _fail("invalid graph edge")
            pair = (edge["ownerKey"], edge["solverKey"])
            if pair[0] == pair[1] or pair[0] not in owner_rows or pair[1] not in solver_rows or not _count(edge["count"]) or edge["count"] < 1:
                _fail("invalid graph edge reference or count")
            if edge["ownerName"] != owner_rows[pair[0]]["username"] or edge["solverName"] != solver_rows[pair[1]]["username"]:
                _fail("graph edge player names mismatch")
            actual_pairs.append(pair)
            ow[pair[0]] += edge["count"]
            sw[pair[1]] += edge["count"]
            owner_solver[pair[0]].add(pair[1]); solver_owner[pair[1]].add(pair[0])
        if actual_pairs != sorted(set(actual_pairs)):
            _fail("graph edges duplicated or unsorted")
        actual_counts = {(e["ownerKey"], e["solverKey"]): e["count"] for e in edges}
        if actual_counts != {pair: len(gcset) for pair, gcset in expected_edges.items()}:
            _fail("graph edge counts do not match distinct GC facts")
        if order.get("owners") != sorted(ow, key=lambda k: (-ow[k], k)) or order.get("solvers") != sorted(sw, key=lambda k: (-sw[k], k)):
            _fail("graph order mismatch")
        for key, row in solver_rows.items():
            if row["ownerCount"] != len(solver_owner[key]):
                _fail("solver owner count mismatch")
        for key, row in owner_rows.items():
            if row["solverCount"] != len(owner_solver[key]):
                _fail("owner solver count mismatch")
        for metric in ("rate", "correct", "attempts"):
            field = {"rate": "submissionRate", "correct": "publicCorrectCount", "attempts": "attemptCount"}[metric]
            if metric not in scope.get("puzzleRankings", {}):
                _fail("puzzle rankings missing")
            values = {row["code"]: row["stats"].get(field) for row in caches}
            for direction in ("asc", "desc"):
                rows = scope["puzzleRankings"][metric].get(direction)
                if not isinstance(rows, list):
                    _fail("puzzle rank list missing")
                ordered = sorted(values, key=lambda c: (values[c] is None, (values[c] or 0) * (1 if direction == "asc" else -1), c))
                if [r.get("code") for r in rows] != ordered:
                    _fail("puzzle rank coverage or order mismatch")
                prior, rank = object(), 0
                for index, row in enumerate(rows, 1):
                    value = values[row["code"]]
                    if (not isinstance(row, dict) or not isinstance(row.get("code"), str) or
                            row.get("rank") is not None and type(row.get("rank")) is not int):
                        _fail("invalid puzzle rank row")
                    if value is not None and value != prior:
                        rank, prior = index, value
                    if row.get("rank") != (rank if value is not None else None):
                        _fail("puzzle rank value mismatch")
    if set(scope_map) != {(r, a) for r in REGIONS for a in AVAILABILITIES}:
        _fail("scope combination missing")
    all_codes = {row["code"] for row in scope_map[("all", "all")]["caches"]}
    base = scope_map[("all", "all")]
    base_caches = {row["code"]: row for row in base["caches"]}
    certified_by_code = defaultdict(list)
    for solver in base["rankings"]["solvers"]:
        for code in solver["cacheCodes"]:
            certified_by_code[code].append({"key": solver["key"], "username": solver["username"], "certifiedAt": None})
    for (region, availability), scope in scope_map.items():
        selected = {row["code"] for row in scope["caches"]}
        if not selected.issubset(all_codes):
            _fail("filtered scope contains cache outside all scope")
        expected = {row["code"] for row in scope_map[("all", "all")]["caches"]
                    if (region == "all" or row.get("region") == region) and
                    (availability == "all" or (availability == "active" and row.get("cacheStatus") is not None and row.get("cacheStatus") != 2) or
                     (availability == "archived" and row.get("cacheStatus") == 2))}
        if selected != expected:
            _fail("filtered scope cache set mismatch")
        filtered_cache_rows = [base_caches[code] for code in sorted(expected)]
        synthetic_facts = [{"cache": cache_row, "stats": cache_row["stats"],
                            "certifiedSolvers": certified_by_code.get(cache_row["code"], [])}
                           for cache_row in filtered_cache_rows]
        expected_scope = build_scope(synthetic_facts, region, availability)
        if _cross_scope_projection(scope) != _cross_scope_projection(expected_scope):
            _fail("filtered scope contents do not match all scope")
    if facts is not None:
        if isinstance(facts, list):
            facts = {row.get("cache", {}).get("code"): row for row in facts}
        if not isinstance(facts, dict) or set(facts) != all_codes:
            _fail("internal facts do not match all scope")
        for code, fact in facts.items():
            if fact.get("cache", {}).get("code") != code or not isinstance(fact.get("checkers"), list):
                _fail("invalid internal facts")
            rebuilt = build_cache_facts(fact["cache"], fact["checkers"])
            if rebuilt["stats"] != fact.get("stats") or rebuilt["certifiedSolvers"] != fact.get("certifiedSolvers"):
                _fail("internal facts do not match source checker records")
        for key, scope in scope_map.items():
            expected = build_scope(list(facts.values()), *key)
            if _canonical_scope_projection(scope) != _canonical_scope_projection(expected):
                _fail(f"scope {key} does not match internal facts")
    return True


def validate_dataset(manifest, scopes, facts=None):
    """Validate schema 2 required fields, tolerating legacy extras."""
    try:
        return _validate_dataset(manifest, scopes, facts)
    except (AttributeError, KeyError, TypeError, IndexError) as exc:
        raise ValueError("malformed required dataset fields") from exc
