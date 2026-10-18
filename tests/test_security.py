"""F1 regression: the static/SPA route must never serve files outside web/dist."""
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

import contraflow.api as api
from contraflow.api import app

client = TestClient(app)
TRAVERSALS = [
    "/%2e%2e/%2e%2e/README.md", "/%2e%2e/%2e%2e/pyproject.toml", "/..%2f..%2fREADME.md", "/%2e%2e%2f%2e%2e%2fREADME.md",
    "/assets/..%2f..%2f..%2fREADME.md", "/assets/%2e%2e/%2e%2e/%2e%2e/README.md", "/%252e%252e/%252e%252e/README.md",
    "/....//....//README.md", "/%2e%2e%5c%2e%2e%5cREADME.md", "/%00/../README.md", "/..%00/README.md",
    "/%2e%2e/%2e%2e/contraflow/api.py", "/%2e%2e/%2e%2e/%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/hosts",
]


def leaks(body: bytes) -> bool:
    t = body.decode("utf-8", "ignore")
    return any(m in t for m in ("[project]", "Graph Hacks", "FastAPI", "localhost", "def spa"))


@pytest.mark.parametrize("path", TRAVERSALS)
def test_testclient_traversal_rejected(path):
    r = client.get(path)
    assert not (r.status_code == 200 and leaks(r.content)), (path, r.status_code)


@pytest.fixture(scope="module")
def live_server():
    p = subprocess.Popen([".venv/bin/uvicorn", "contraflow.api:app", "--host", "127.0.0.1", "--port", "8765"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ok = False
    for _ in range(120):
        r = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "http://127.0.0.1:8765/api/health"],
                           capture_output=True, text=True)
        if r.stdout == "200":
            ok = True
            break
        time.sleep(0.25)
    assert ok, "test server did not start"
    yield "http://127.0.0.1:8765"
    p.terminate()
    p.wait(timeout=10)


@pytest.mark.parametrize("path", TRAVERSALS)
def test_raw_http_traversal_rejected(live_server, path):
    """curl --path-as-is sends '..' and encoded variants unnormalised, exactly like the audit reproduction."""
    r = subprocess.run(["curl", "-s", "--path-as-is", "-w", "\n%{http_code}", f"{live_server}{path}"], capture_output=True)
    assert r.returncode == 0, "server unreachable"
    body, _, code = r.stdout.rpartition(b"\n")
    assert code in (b"400", b"404"), (path, code)
    assert not leaks(body), path


def test_spa_routes_and_api_still_work():
    if not api._frontend_built():
        pytest.skip("web/dist not built")
    assert client.get("/").status_code == 200 and b'<div id="root">' in client.get("/").content
    assert client.get("/some/spa/route").status_code == 200
    assert client.get("/api/meta").status_code == 200
    assert client.get("/api/does-not-exist").status_code == 404
    assert client.get("/missing.js").status_code == 404


def test_missing_bundle_returns_503(monkeypatch, tmp_path):
    monkeypatch.setattr(api, "WEB", tmp_path / "dist")
    r = client.get("/")
    assert r.status_code == 503 and "not built" in r.json()["detail"]
    assert client.get("/api/health").json()["frontend_built"] is False
