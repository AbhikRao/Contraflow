"""Public boundary around the existing Contraflow application.

One process, dedicated Redis/FalkorDB instance, per-visitor road/evidence/memory
graphs, signed cookies, bounded admission, and a worker slot held until the
actual model job ends (including when its HTTP client disconnects).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse

from contraflow import api
from contraflow.agent import llm
from contraflow.agent.runtime import run_agent
from contraflow.engine import Engine
from contraflow.graphdb import DB, Graph, connect_db
from contraflow.memory import Memory
from contraflow.model import RULES, build

LOG = logging.getLogger("contraflow.public")
ORIGIN = os.environ.get("PUBLIC_ORIGIN", "http://127.0.0.1:8008").rstrip("/")
HOST = urlsplit(ORIGIN).hostname
SECURE = ORIGIN.startswith("https://")
STATE = Path(os.environ.get("PUBLIC_STATE_DIR", "/state"))
COOKIE = "cf_public_session"
BODY_LIMIT = 32768
SESSION_TTL = 6 * 3600
MAX_SESSIONS = 24
CURRENT = contextvars.ContextVar("public_state")
registry = None
GRAPH_GATE = asyncio.Semaphore(2)
GRAPH_WAITING = 0
AGENT_GATE = threading.BoundedSemaphore(1)
AGENTS = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="public-agent")


def error(status, code, message, retry=None):
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status,
                        headers={"Retry-After": str(retry)} if retry else None)


class TenantDB(DB):
    """Map the app's base-graph references to this visitor's private copy."""
    def __init__(self, seed, sid):
        self.client = seed.client
        self._lock = threading.Lock()
        self.base = f"pub_{sid}_road"

    def graph(self, name="contraflow"):
        return Graph(self, self.base if name == "contraflow" else name)

    @contextmanager
    def scenario_clone(self, ledger=None):
        name = "pub_sc_" + secrets.token_hex(10)
        t = time.perf_counter()
        self.client.connection.execute_command("GRAPH.COPY", self.base, name)
        if ledger is not None:
            ledger.add(name, "graph_copy", f"GRAPH.COPY {self.base} {name}", None,
                       (time.perf_counter() - t) * 1000, 0)
        try:
            yield self.graph(name)
        finally:
            t = time.perf_counter()
            self.delete(name)
            if ledger is not None:
                ledger.add(name, "graph_delete", f"GRAPH.DELETE {name}", None,
                           (time.perf_counter() - t) * 1000, 0)


class Registry:
    def __init__(self):
        # Never silently connect this deployment to the developer's localhost DB.
        if os.environ.get("FALKORDB_HOST") != "db":
            raise RuntimeError("Public deployment requires its dedicated Compose service FALKORDB_HOST=db")
        STATE.mkdir(parents=True, exist_ok=True)
        key = STATE / "session.key"
        if not key.exists():
            fd = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(secrets.token_bytes(32))
        self.secret = key.read_bytes()
        self.lock = threading.RLock()
        self.sql = sqlite3.connect(STATE / "registry.sqlite3", check_same_thread=False)
        self.sql.execute("CREATE TABLE IF NOT EXISTS sessions (sid TEXT PRIMARY KEY, expires REAL)")
        self.sql.execute("CREATE TABLE IF NOT EXISTS quotas (key TEXT PRIMARY KEY, window INTEGER, used INTEGER)")
        # used: the visitor made an API call (has graph copies); seen: last request. Both drive eviction order.
        for col in ("used INTEGER DEFAULT 0", "seen REAL DEFAULT 0"):
            try:
                self.sql.execute(f"ALTER TABLE sessions ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        self.sql.commit()
        self.net = build()
        self.db = connect_db()
        self.load = self.db.load_base(self.net)
        # Only this dedicated public DB is addressed; no developer graph is loaded.
        for name in self.db.list_graphs():
            if name.startswith("pub_sc_"):
                self.db.delete(name)
        self.states = {}
        self.active = {}
        self.cleanup()

    def quota(self, key, period, limit, cost=1):
        with self.lock:
            window = int(time.time()) // period
            row = self.sql.execute("SELECT window, used FROM quotas WHERE key=?", (key,)).fetchone()
            used = row[1] if row and row[0] == window else 0
            if used + cost > limit:
                return False
            self.sql.execute("INSERT OR REPLACE INTO quotas VALUES (?, ?, ?)", (key, window, used + cost))
            self.sql.commit()
            return True

    def cleanup(self):
        with self.lock:
            expired = self.sql.execute("SELECT sid FROM sessions WHERE expires < ?", (time.time(),)).fetchall()
            for (sid,) in expired:
                if self.active.get(sid, 0):
                    continue
                self.drop(sid)
            self.sql.commit()

    def drop(self, sid):
        """Remove one visitor completely: graphs, in-memory state, quotas, disposable memory log."""
        with self.lock:
            for suffix in ("road", "memory"):
                self.db.delete(f"pub_{sid}_{suffix}")
            self.states.pop(sid, None)
            self.sql.execute("DELETE FROM sessions WHERE sid=?", (sid,))
            self.sql.execute("DELETE FROM quotas WHERE key LIKE ?", (sid + ":%",))
            # Visitor logs are disposable; the developer's log is never mounted.
            log = STATE / "visitors" / sid / "memory.jsonl"
            if log.exists():
                log.unlink()

    def sign(self, sid):
        return sid + "." + hmac.new(self.secret, sid.encode(), hashlib.sha256).hexdigest()

    def session(self, token, create=False):
        with self.lock:
            sid = (token or "").split(".", 1)[0]
            if re.fullmatch(r"[a-f0-9]{32}", sid) and hmac.compare_digest(token, self.sign(sid)):
                row = self.sql.execute("SELECT expires FROM sessions WHERE sid=?", (sid,)).fetchone()
                if row and row[0] > time.time():
                    now = time.time()        # sliding expiry: an active visitor is never timed out mid-demo
                    self.sql.execute("UPDATE sessions SET seen=?, expires=? WHERE sid=?", (now, now + SESSION_TTL, sid))
                    self.sql.commit()
                    return sid
            if not create:
                return None
            self.cleanup()
            if self.sql.execute("SELECT count(*) FROM sessions").fetchone()[0] >= MAX_SESSIONS:
                # Full: evict instead of locking everyone out (public URLs are crawled; every cookieless GET / is a
                # session). Sessions that never called the API go first, then the least recently seen idle one.
                victim = next((v for (v,) in self.sql.execute(
                    "SELECT sid FROM sessions ORDER BY used ASC, seen ASC").fetchall() if not self.active.get(v, 0)), None)
                if victim is None:
                    return None
                self.drop(victim)
            if not self.quota("new_sessions", 3600, 1200):
                return None
            sid = secrets.token_hex(16)
            now = time.time()
            self.sql.execute("INSERT INTO sessions (sid, expires, used, seen) VALUES (?, ?, 0, ?)", (sid, now + SESSION_TTL, now))
            self.sql.commit()
            return sid

    def state(self, sid):
        with self.lock:
            if sid not in self.states:
                db = TenantDB(self.db, sid)
                if db.base not in self.db.list_graphs():
                    self.db.client.connection.execute_command("GRAPH.COPY", "contraflow", db.base)
                memory = Memory(db, STATE / "visitors" / sid / "memory.jsonl", f"pub_{sid}_memory")
                status = memory.ensure()
                eng = Engine(self.net, db)
                for rule in RULES:
                    eng.baseline(rule)
                self.states[sid] = {"net": self.net, "db": db, "engine": eng, "load": self.load,
                                    "memory": memory, "memory_status": status, "swept": 0}
                self.sql.execute("UPDATE sessions SET used=1 WHERE sid=?", (sid,))
                self.sql.commit()
            return self.states[sid]

    def pin(self, sid, change):
        with self.lock:
            self.active[sid] = self.active.get(sid, 0) + change


def bounded_chat(messages, tools, timeout=45):
    # Fixed local model endpoint, no paid API and no caller-selected upstream.
    r = httpx.post(llm.BASE + "/chat/completions", json={
        "model": "qwen2.5:7b", "messages": messages, "temperature": 0, "seed": 7,
        "max_tokens": 768, "tools": [{"type": "function", "function": x} for x in tools], "tool_choice": "auto",
    }, headers={"Authorization": "Bearer ollama"}, timeout=min(timeout, 45))
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


@asynccontextmanager
async def lifespan(_app):
    global registry
    registry = await asyncio.to_thread(Registry)
    async def collect_expired():
        while True:
            await asyncio.sleep(300)
            await asyncio.to_thread(registry.cleanup)
    collector = asyncio.create_task(collect_expired())
    yield
    collector.cancel()
    try:
        await collector
    except asyncio.CancelledError:
        pass
    # Uvicorn drains normal requests. Do not delete visitors or permanent state at shutdown.
    AGENTS.shutdown(wait=True)


api.state = lambda: CURRENT.get()
api.app.router.lifespan_context = lifespan
llm.chat = bounded_chat

GETS = {"/api/meta", "/api/layers", "/api/evidence", "/api/memory", "/api/agent/status", "/api/health"}
POSTS = {"/api/evaluate", "/api/interventions", "/api/vulnerability", "/api/apply_candidate",
         "/api/agent", "/api/agent/stream", "/api/memory/review", "/api/memory/decision"}
CASE = re.compile(r"/api/case/inv_[a-zA-Z0-9_-]{1,32}(?:/robustness)?$")


def validate_payload(obj):
    if not isinstance(obj, dict):
        raise ValueError("Expected a JSON object")
    for k in ("scenario", "current"):
        sc = obj.get(k)
        if sc is not None and not isinstance(sc, dict):
            raise ValueError("Invalid scenario")
    scenarios = [obj] + [obj[k] for k in ("scenario", "current") if isinstance(obj.get(k), dict)]
    for sc in scenarios:
        for key, maximum in (("closed_carriageways", 7), ("closed_segments", 128),
                             ("reversed_segments", 24), ("constraints", 21)):
            if key in sc and (not isinstance(sc[key], list) or len(sc[key]) > maximum):
                raise ValueError(f"Public demo limit: {key} permits at most {maximum} entries")
    if "history" in obj:
        if not isinstance(obj["history"], list) or len(obj["history"]) > 24:
            raise ValueError("Conversation limit reached; reload for a new conversation")
        for item in obj["history"]:
            if not isinstance(item, dict) or len(str(item.get("content", ""))) > 8000:
                raise ValueError("History entry too large")
    if obj.get("mode") not in (None, "auto", "llm", "deterministic"):
        raise ValueError("Invalid agent mode")


async def agent_response(obj, st, sid, stream):
    try:
        body = api.AgentIn.model_validate(obj)
        st["engine"].validate(body.scenario.to_engine())
    except Exception:
        return error(422, "invalid_agent_request", "Invalid investigation input")
    if st["memory"].stats()["investigations"] >= 30:
        return error(429, "memory_limit", "This visitor's investigation limit has been reached.", 3600)
    if not AGENT_GATE.acquire(blocking=False):
        return error(429, "agent_busy", "Another investigation is running. Please retry shortly.", 15)
    if not registry.quota(sid + ":agent", 600, 6) or not registry.quota("agent_global", 3600, 40):
        AGENT_GATE.release()
        return error(429, "agent_limit", "The public demo investigation allowance has been reached. Try later.", 600)
    q = queue.Queue(maxsize=100)
    registry.pin(sid, 1)

    def work():
        try:
            def step(s):
                q.put_nowait({"type": "step", "step": {k: s.get(k) for k in
                    ("kind", "tool", "args", "result_summary", "ms", "queries", "text")}})
            result = run_agent(st, body.message, body.scenario.to_engine(), body.history,
                               body.mode, body.confirm, on_step=step)
            q.put_nowait({"type": "reply", "reply": result})
            return result
        except Exception:
            LOG.exception("Public investigation failed")
            q.put_nowait({"type": "error", "detail": {"code": "agent_failed", "message": "Investigation could not complete."}})
            return None
        finally:
            q.put_nowait(None)
            registry.pin(sid, -1)
            AGENT_GATE.release()

    future = AGENTS.submit(work)
    if not stream:
        result = await asyncio.shield(asyncio.wrap_future(future))
        return JSONResponse(result) if result else error(503, "agent_failed", "Investigation could not complete")

    async def events():
        yield ": connected\n\n"
        last = time.monotonic()
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                if time.monotonic() - last >= 10:
                    yield ": waiting for model or graph\n\n"
                    last = time.monotonic()
                await asyncio.sleep(0.05)
                continue
            if item is None:
                break
            yield "data: " + json.dumps(item, default=str) + "\n\n"
            last = time.monotonic()
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


class Gateway:
    async def __call__(self, scope, receive, send):
        global GRAPH_WAITING
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            return await api.app(scope, receive, send)
        request = Request(scope, receive)
        path, method = scope["path"], scope["method"]
        headers = request.headers
        host = headers.get("host", "").split(":")[0]
        started = False
        sid = None
        cookie = None
        token = None
        pinned = False
        acquired = False

        async def secure_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                hs = list(message.get("headers", []))
                hs.extend([(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
                           (b"referrer-policy", b"same-origin"), (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                           (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")])
                if SECURE:
                    hs.append((b"strict-transport-security", b"max-age=86400"))
                if cookie:
                    value = f"{COOKIE}={cookie}; Path=/; Max-Age={SESSION_TTL}; HttpOnly; SameSite=Lax" + ("; Secure" if SECURE else "")
                    hs.append((b"set-cookie", value.encode()))
                if path.startswith("/api/") or path == "/":
                    hs.append((b"cache-control", b"no-store"))
                message["headers"] = hs
            await send(message)

        async def reply(response):
            await response(scope, receive, secure_send)

        try:
            if host not in {HOST, "127.0.0.1", "localhost"}:
                return await reply(error(400, "invalid_host", "Invalid host"))
            if len(path) > 256 or len(scope.get("query_string", b"")) > 1024:
                return await reply(error(414, "request_too_long", "Request URL too long"))
            if path == "/api/health" and method == "GET":
                ok = registry is not None and api._frontend_built()
                return await reply(JSONResponse({"ok": ok, "frontend_built": api._frontend_built(),
                                                  "deployment": "isolated-public-demo"}, status_code=200 if ok else 503))
            static = method in ("GET", "HEAD") and (path == "/" or path.startswith("/assets/"))
            allowed = static or (method == "GET" and path in GETS) or (method == "POST" and (path in POSTS or CASE.fullmatch(path)))
            if not allowed:
                return await reply(error(404, "not_found", "Not found"))
            if registry is None:
                return await reply(error(503, "starting", "Demo is starting", 10))
            if path.startswith("/assets/"):
                return await api.app(scope, receive, secure_send)
            if not registry.quota("http_global", 60, 600):
                return await reply(error(429, "busy", "Demo traffic limit reached", 30))
            sid = registry.session(request.cookies.get(COOKIE), create=path == "/")
            if not sid:
                return await reply(error(429 if path == "/" else 401, "session_required", "Open or reload the homepage to start an available demo session.", 60))
            if path == "/":
                cookie = registry.sign(sid)
                return await api.app(scope, receive, secure_send)
            if method == "POST" and headers.get("origin") != ORIGIN:
                return await reply(error(403, "origin_rejected", "Cross-origin requests are not permitted"))
            body = b""
            if method == "POST":
                if headers.get("content-type", "").split(";", 1)[0] != "application/json":
                    return await reply(error(415, "json_required", "Use application/json"))
                try:
                    async def read():
                        nonlocal body
                        while True:
                            item = await receive()
                            if item["type"] == "http.disconnect":
                                raise ValueError("Disconnected")
                            body += item.get("body", b"")
                            if len(body) > BODY_LIMIT:
                                raise OverflowError()
                            if not item.get("more_body"):
                                break
                    await asyncio.wait_for(read(), 10)
                    obj = json.loads(body)
                    validate_payload(obj)
                except OverflowError:
                    return await reply(error(413, "body_limit", "Request body exceeds 32 KiB"))
                except (ValueError, TypeError, asyncio.TimeoutError):
                    return await reply(error(422, "invalid_input", "Invalid or oversized public-demo input"))
            cost = 15 if path.endswith("/robustness") else 10 if path == "/api/vulnerability" else 5 if path in {"/api/interventions", "/api/memory/review"} or path.startswith("/api/case/") else 1
            if not registry.quota(sid + ":api", 60, 120, cost) or not registry.quota("graph_global", 60, 360, cost):
                return await reply(error(429, "rate_limit", "Please wait before running more analyses", 30))
            if GRAPH_WAITING >= 8:
                return await reply(error(429, "graph_busy", "The graph is busy; retry shortly", 3))
            GRAPH_WAITING += 1
            try:
                await asyncio.wait_for(GRAPH_GATE.acquire(), 5)
                acquired = True
            except asyncio.TimeoutError:
                return await reply(error(429, "graph_busy", "The graph is busy; retry shortly", 3))
            finally:
                GRAPH_WAITING -= 1
            registry.pin(sid, 1)
            pinned = True
            st = await asyncio.to_thread(registry.state, sid)
            token = CURRENT.set(st)
            if path in {"/api/agent", "/api/agent/stream"}:
                response = await agent_response(obj, st, sid, path.endswith("/stream"))
                GRAPH_GATE.release()
                acquired = False
                return await reply(response)
            if path == "/api/agent/status":
                status = await asyncio.to_thread(llm.llm_status)
                return await reply(JSONResponse({"llm_available": status["llm_available"], "model": "qwen2.5:7b"}))
            sent = False
            async def replay_receive():
                nonlocal sent
                if not sent and method == "POST":
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()
            return await api.app(scope, replay_receive, secure_send)
        except Exception:
            LOG.exception("Public request failed")
            if not started:
                await reply(error(503, "unavailable", "The public demo could not complete this request. Retry shortly.", 10))
        finally:
            if token is not None:
                CURRENT.reset(token)
            if pinned:
                registry.pin(sid, -1)
            if acquired:
                GRAPH_GATE.release()


app = Gateway()
