"""OpenAI-compatible chat client (default: local Ollama). No paid service is required.

Configure with environment variables:
  CONTRAFLOW_LLM_BASE_URL  default http://127.0.0.1:11434/v1  (Ollama)
  CONTRAFLOW_LLM_MODEL     default qwen2.5:7b
  CONTRAFLOW_LLM_API_KEY   optional (e.g. a free-tier key for another OpenAI-compatible provider)
"""

from __future__ import annotations

import os
import time

import httpx

BASE = os.environ.get("CONTRAFLOW_LLM_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
MODEL = os.environ.get("CONTRAFLOW_LLM_MODEL", "qwen2.5:7b")
KEY = os.environ.get("CONTRAFLOW_LLM_API_KEY", "ollama")
_status_cache: dict = {"t": 0.0, "v": None}


def llm_status() -> dict:
    if time.time() - _status_cache["t"] < 15 and _status_cache["v"]:
        return _status_cache["v"]
    try:
        r = httpx.get(f"{BASE}/models", headers={"Authorization": f"Bearer {KEY}"}, timeout=2.0)
        r.raise_for_status()
        ids = [m.get("id") for m in r.json().get("data", [])]
        ok = MODEL in ids
        v = {"llm_available": ok, "model": MODEL, "base_url": BASE,
             "reason": None if ok else f"model {MODEL} not served at {BASE} (available: {', '.join(ids) or 'none'})"}
    except Exception as e:  # noqa: BLE001
        v = {"llm_available": False, "model": MODEL, "base_url": BASE, "reason": f"LLM endpoint unreachable: {e}"}
    _status_cache.update(t=time.time(), v=v)
    return v


def chat(messages: list[dict], tools: list[dict], timeout: float = 45.0) -> dict:
    body = {"model": MODEL, "messages": messages, "temperature": 0, "seed": 7,
            "tools": [{"type": "function", "function": t} for t in tools], "tool_choice": "auto"}
    r = httpx.post(f"{BASE}/chat/completions", json=body, headers={"Authorization": f"Bearer {KEY}"}, timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]
