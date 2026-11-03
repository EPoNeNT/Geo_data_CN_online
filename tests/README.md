# Offline regression tests

From the repository root, run:

```powershell
python -B -m unittest discover -s tests -p 'test_*.py'
```

The suite uses fixtures, fake database connections and HTTP responses. It does not run collectors or connect to production. New tests cover owner identity preservation, archived crawl limits and failure handling, static publication rollback, and the policy that existing database observations remain usable when collection fails.

`test_rank_contract.py` additionally compares the JavaScript ranking SQL in the sibling `Geodataing` project with the Python generator using an in-memory SQLite compatibility layer. It checks fixture behavior rather than PostgreSQL performance or deployment readiness. It skips explicitly when the sibling repository or Node is unavailable. The optional YAML parser check skips when PyYAML is not installed.

Only this `tests/` directory is automatically discovered. Local diagnostic and maintenance scripts in `test/` remain separate because some of them connect to or modify a real database.
