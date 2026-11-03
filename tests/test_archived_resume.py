"""Offline regression tests for archived crawler resume and failure handling."""
import ast
from pathlib import Path
import unittest
from typing import Any, Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "crawl_archived_logs.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
GLOBALS = {
    "Any": Any,
    "Dict": Dict,
    "List": List,
    "Optional": Optional,
    "Tuple": Tuple,
    "CacheRow": Tuple[str, Optional[float], Optional[float], bool],
    "CrawlCache": Tuple[str, Optional[float], Optional[float]],
    "DatabaseManager": object,
}


def load_functions(*names):
    nodes = [node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = dict(GLOBALS)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


class ArchivedResumeTests(unittest.TestCase):
    def test_limit_advances_across_resume_and_account_group_filters(self):
        filter_group = load_functions("filter_group")["filter_group"]
        rows = [
            ("GC1", None, None, False),  # already completed
            ("GC2", None, None, True),   # not eligible in nonpremium mode
            ("GC3", None, None, False),
            ("GC4", None, None, False),
        ]
        premium, ordinary = filter_group(rows, "nonpremium", {"GC1"}, True, limit=1)
        self.assertEqual(premium, [])
        self.assertEqual(ordinary, [("GC3", None, None)])

    def test_limit_applies_after_eligibility_for_all_groups(self):
        filter_group = load_functions("filter_group")["filter_group"]
        rows = [
            ("GC1", None, None, False),
            ("GC2", None, None, True),
            ("GC3", None, None, False),
        ]
        premium, ordinary = filter_group(rows, "all", set(), True, limit=2)
        self.assertEqual(premium, [("GC2", None, None)])
        self.assertEqual(ordinary, [("GC1", None, None)])

    def test_archived_query_does_not_apply_limit_before_resume(self):
        get_archived_caches = load_functions("get_archived_caches")["get_archived_caches"]

        class Cursor:
            query = ""
            params = None

            def execute(self, query, params):
                self.query, self.params = query, params

            def fetchall(self):
                return [("GC1", 1, 2, False)]

        class DB:
            cursor = Cursor()

        db = DB()
        get_archived_caches(db, skip_db_crawled=True, start_after="GC0")
        self.assertNotIn("LIMIT", db.cursor.query.upper())
        self.assertEqual(db.cursor.params, ["GC0"])

    def test_failed_items_produce_nonzero_exit(self):
        raise_for_failed_items = load_functions("raise_for_failed_items")["raise_for_failed_items"]
        raise_for_failed_items(0)
        with self.assertRaises(SystemExit) as raised:
            raise_for_failed_items(1)
        self.assertEqual(raised.exception.code, 1)

    def test_failure_exit_occurs_after_final_state_save(self):
        main = next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        calls = [
            node.func.id
            for node in ast.walk(main)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        self.assertIn("save_state", calls)
        self.assertIn("raise_for_failed_items", calls)
        self.assertLess(calls.index("save_state"), calls.index("raise_for_failed_items"))


if __name__ == "__main__":
    unittest.main()
