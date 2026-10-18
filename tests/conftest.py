"""Isolate investigation memory during tests (never pollute the demo's memory graph or log).
The test memory graph and its log are both per-session: the graph is wiped at session start and deleted at the end."""
import os
import tempfile

os.environ.setdefault("CONTRAFLOW_MEMORY_GRAPH", "contraflow_memory_test")
os.environ.setdefault("CONTRAFLOW_MEMORY_LOG", os.path.join(tempfile.mkdtemp(prefix="cf_mem_"), "memory.jsonl"))


def _drop_test_memory():
    name = os.environ["CONTRAFLOW_MEMORY_GRAPH"]
    if name == "contraflow_memory":            # never touch the demo memory graph
        return
    try:
        from contraflow.graphdb import DB
        db = DB()
        if name in db.list_graphs():
            db.delete(name)
    except Exception:                          # database not running: the DB-backed tests will report it
        pass


def pytest_sessionstart(session):
    _drop_test_memory()


def pytest_sessionfinish(session, exitstatus):
    _drop_test_memory()
