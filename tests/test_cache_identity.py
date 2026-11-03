import ast
import unittest
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "crawl_caches.py"


def load_owner_guid_resolver():
    """Compile the production helper without importing collector side effects."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name in {"_resolve_owner_guid", "run_crawler"}]
    namespace = {
        "Optional": Optional,
        "_extract_owner_guid_from_detail_page": lambda *_args: None,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


class OwnerGuidRegression(unittest.TestCase):
    def setUp(self):
        self.namespace = load_owner_guid_resolver()

    def test_metadata_change_preserves_guid_for_same_owner(self):
        existing = {"owner_username": "Alice", "owner_guid": "guid-old"}
        latest = {"owner_username": "Alice", "owner_guid": None, "premium_only": False}
        self.assertEqual(self.namespace["_resolve_owner_guid"](existing, latest, "GC1"), "guid-old")

    def test_transfer_uses_new_guid_when_lookup_succeeds(self):
        self.namespace["_extract_owner_guid_from_detail_page"] = lambda code, premium: (
            "guid-new" if (code, premium) == ("GC1", True) else None
        )
        self.assertEqual(self.namespace["_resolve_owner_guid"](
            {"owner_username": "Alice", "owner_guid": "guid-old"},
            {"owner_username": "Bob", "premium_only": True}, "GC1"), "guid-new")

    def test_transfer_clears_old_guid_when_lookup_fails(self):
        self.assertIsNone(self.namespace["_resolve_owner_guid"](
            {"owner_username": "Alice", "owner_guid": "guid-old"},
            {"owner_username": "Bob", "premium_only": False}, "GC1"))

    def test_missing_guid_stays_missing_for_same_owner(self):
        looked_up = []
        self.namespace["_extract_owner_guid_from_detail_page"] = lambda *args: looked_up.append(args)
        self.assertIsNone(self.namespace["_resolve_owner_guid"](
            {"owner_username": "Alice", "owner_guid": None},
            {"owner_username": "Alice", "premium_only": False}, "GC1"))
        self.assertEqual(looked_up, [])

    def test_run_crawler_uses_resolver_for_existing_caches(self):
        crawler = next(node for node in ast.parse(SOURCE.read_text(encoding="utf-8")).body
                       if isinstance(node, ast.FunctionDef) and node.name == "run_crawler")
        calls = [node for node in ast.walk(crawler) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "_resolve_owner_guid"]
        self.assertEqual(len(calls), 1)

if __name__ == "__main__":
    unittest.main()
