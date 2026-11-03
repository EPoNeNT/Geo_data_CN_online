"""Compare actual JavaScript ranking queries with the Python generator offline."""
import ast
from collections import defaultdict
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT.parent / "Geodataing"
SERVICE = Path(os.environ.get("RANK_SERVICE_PATH", str(FRONTEND / "server/player-all-ranks.js")))
AS_OF = "2026-11-03"
COUNTRIES = {"China": "china", "Hong Kong": "hongkong", "Taiwan": "taiwan", "Macao": "macao"}


def generator_queries():
    tree = ast.parse((ROOT / "generate_data.py").read_text(encoding="utf-8"))
    constants = {
        "EXCLUDED_CACHE_TYPES", "EVENT_CACHE_TYPES", "CACHE_NOT_DELETED_WHERE", "CACHE_NOT_DELETED_JOIN",
        "EXCLUDE_CACHE_WHERE", "EXCLUDE_CACHE_JOIN", "ACTIVE_CACHE_WHERE", "ACTIVE_CACHE_JOIN",
        "EVENT_CACHE_WHERE", "EVENT_CACHE_JOIN", "OWNER_USERNAME_FILTER", "RANKING_LOG_TYPE_CONDITION",
        "NEWBIE_REGISTRATION_FILTER", "PREVIOUS_NEWBIE_REGISTRATION_FILTER",
    }
    nodes = [node for node in tree.body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in constants for target in node.targets)]
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DataGenerator")
    methods = {"_player_grouped_sql", "_newbie_grouped_sql", "_event_grouped_sql", "generate_ranking_cache_condition", "generate_ranking_stats_date_condition"}
    cls = ast.ClassDef(name="QueryGenerator", bases=[], keywords=[], body=[node for node in original.body if isinstance(node, ast.FunctionDef) and node.name in methods], decorator_list=[])
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes + [cls], type_ignores=[])), "generator_contract", "exec"), namespace)
    return namespace["QueryGenerator"]()


def sqlite_sql(sql):
    sql = re.sub(r"CURRENT_DATE\s*-\s*INTERVAL\s*'([0-9]+)\s+(days?|years?)'", lambda match: f"date('{AS_OF}', '-{match[1]} {match[2]}')", sql, flags=re.I)
    sql = re.sub(r"date_trunc\('year',\s*CURRENT_DATE\)", f"date('{AS_OF}', 'start of year')", sql, flags=re.I)
    sql = sql.replace("CURRENT_DATE", f"'{AS_OF}'")
    sql = re.sub(r"::(?:int|integer|text|date|bigint)\b", "", sql)
    sql = sql.replace("COUNT(l.*)", "COUNT(*)")
    return sql.replace("$1", "?")


def fixture():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript('''
        CREATE TABLE caches(code TEXT, country TEXT, owner_username TEXT, geocache_type INTEGER, cache_status INTEGER, placed_date TEXT);
        CREATE TABLE logs(gc_code TEXT, user_name TEXT, visited TEXT, log_type TEXT, is_ftf INTEGER, favorite_point_used INTEGER);
        CREATE TABLE "user"(user_name TEXT, registration_date TEXT);
    ''')
    caches = [
        ("GC1", "China", "Alice", 2, 0, "2024-01-01"),
        ("GC2", "China", "Bob", 8, 2, "2026-11-01"),
        ("GC3", "Hong Kong", "Alice", 2, None, "2026-10-15"),
        ("GC4", "Taiwan", "Bob", 6, 0, "2026-11-01"),
        ("GC5", "China", "Alice", 2, 404, "2026-11-01"),
        ("GC6", "Macao", "Bob", 2, 1, "2026-11-01"),
        ("GC7", "China", "Alice", 13, 2, "2025-01-01"),
        ("GC8", "China", "Alice", 3653, 0, "2026-11-01"),
        ("GC9", "China", "[DELETED_USER]", 2, 0, "2026-11-01"),
        ("GC10", "Outside", "Alice", 2, 0, "2026-11-01"),
    ]
    db.executemany("INSERT INTO caches VALUES(?,?,?,?,?,?)", caches)
    logs = [
        ("GC1", "Alice", "2026-11-01", "Found it", 1, 1),
        ("GC1", "Alice", "2026-11-02", "Found it", 0, 0),
        ("GC1", "Bob", "2026-11-01", "Found it", 0, 1),
        ("GC1", "Charlie", "2026-11-01", "Didn't find it", 0, 0),
        ("GC1", "Charlie", "2026-11-01", "Write note", 0, 1),
        ("GC2", "Alice", "2026-11-01", "Found it", 1, 1),
        ("GC3", "Alice", "2026-02-01", "Found it", 1, 0),
        ("GC3", "Bob", "2025-12-31", "Found it", 0, 1),
        ("GC4", "Alice", "2026-11-01", "Attended", 0, 1),
        ("GC4", "Alice", "2026-11-02", "Attended", 0, 0),
        ("GC4", "Charlie", "2026-11-01", "Write note", 0, 0),
        ("GC5", "Alice", "2026-11-01", "Found it", 1, 1),
        ("GC6", "Alice", "2026-11-01", "Found it", 0, 1),
        ("GC6", "Bob", "2026-11-01", "Found it", 0, 0),
        ("GC7", "Alice", "2026-11-01", "Attended", 0, 0),
        ("GC8", "Bob", "2026-11-01", "Attended", 0, 0),
        ("GC9", "Alice", "2026-11-01", "Found it", 0, 0),
        ("GC10", "Alice", "2026-11-01", "Found it", 0, 0),
    ]
    db.executemany("INSERT INTO logs VALUES(?,?,?,?,?,?)", logs)
    db.executemany('INSERT INTO "user" VALUES(?,?)', [("Alice", "2026-05-01"), ("Bob", "2026-04-01"), ("Charlie", "2027-01-01")])
    return db


@unittest.skipUnless(SERVICE.is_file() and shutil.which("node"), "Both repositories and Node are needed for this offline cross-project check")
class RankingContractTests(unittest.TestCase):
    def test_dynamic_scores_and_tied_ranks_match_generator_in_all_scopes(self):
        script = r'''
const fs = require('node:fs'), vm = require('node:vm');
const sandbox = { module: { exports: {} }, require: () => ({ getPool() { throw Error('No database allowed'); } }) };
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
const queries=[];
for (const type of ['finds','ftf','hides','logs','favorites','events_hosted','events_attended','newbie_finds','newbie_hides']) {
  for (const range of (['finds','ftf','hides','logs','favorites'].includes(type) ? ['30d','ytd','active','all'] : ['all']))
    queries.push({type,range,sql:sandbox.module.exports.makeGroupedQuery(type,range).sql});
}
process.stdout.write(JSON.stringify(queries));
'''
        queries = json.loads(subprocess.run(["node", "-e", script, str(SERVICE)], check=True, capture_output=True, text=True).stdout)
        generator = generator_queries()
        with fixture() as db:
            for query in queries:
                kind, period = query["type"], query["range"]
                if kind.startswith("newbie_"):
                    reference = generator._newbie_grouped_sql(kind.removeprefix("newbie_"), False)
                elif kind.startswith("events_"):
                    reference = generator._event_grouped_sql("hosts" if kind == "events_hosted" else "participants", False)
                else:
                    reference = generator._player_grouped_sql(kind, period, False, False)
                reference_rows = [dict(row) for row in db.execute(sqlite_sql(reference))]
                scopes = defaultdict(lambda: defaultdict(int))
                for row in reference_rows:
                    scopes["all"][row["name"]] += row["score"]
                    if row["country"] in COUNTRIES:
                        scopes[COUNTRIES[row["country"]]][row["name"]] += row["score"]
                for username in ("Alice", "Bob", "Charlie", "alice"):
                    with self.subTest(type=kind, range=period, username=username):
                        actual = {row["region"]: (row["score"], row["rank"]) for row in db.execute(sqlite_sql(query["sql"]), (username,))}
                        expected = {}
                        for region, scores in scopes.items():
                            for name, score in scores.items():
                                if name.lower() == username.lower() and score > 0:
                                    expected[region] = (score, 1 + sum(other > score for other in scores.values()))
                        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
