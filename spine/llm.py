"""Nemotron on Nebius Token Factory: one async client, live prices, and a US$ cost on every call.

The API key comes from the environment (or a local .env) and is never logged.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

import httpx
from openai import AsyncOpenAI

BASE_URL = "https://api.tokenfactory.nebius.com/v1"
ROOT = Path(__file__).resolve().parent.parent

MODELS = {
    "lightning": {"name": "Nemotron 3.5 Lightning", "model": "nvidia/Nemotron-3_5-Lightning"},
    "nano": {"name": "Nemotron 3 Nano 30B", "model": "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"},
    "super": {"name": "Nemotron 3 Super 120B", "model": "nvidia/nemotron-3-super-120b-a12b"},
    "ultra": {"name": "Nemotron 3 Ultra 550B", "model": "nvidia/Nemotron-3-Ultra-550b-a55b"},
}

# US$ per token, used only if the live /models call fails (checked 2026-10-27).
FALLBACK_PRICES = {
    "nvidia/Nemotron-3_5-Lightning": (6e-8, 2.4e-7),
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": (6e-8, 2.4e-7),
    "nvidia/nemotron-3-super-120b-a12b": (3e-7, 9e-7),
    "nvidia/Nemotron-3-Ultra-550b-a55b": (1e-6, 3e-6),
}


def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
_prices: dict | None = None
_client: AsyncOpenAI | None = None


def has_key() -> bool:
    return bool(os.environ.get("NEBIUS_API_KEY"))


async def prices() -> dict:
    """Live per-token prices from Token Factory's /models (verbose), falling back to the table above."""
    global _prices
    if _prices is None:
        _prices = dict(FALLBACK_PRICES)
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"{BASE_URL}/models", params={"verbose": "true"},
                                headers={"Authorization": f"Bearer {os.environ.get('NEBIUS_API_KEY', '')}"})
            for m in r.json().get("data", []):
                p = m.get("pricing") or {}
                if m.get("id") in _prices and p.get("prompt") is not None:
                    _prices[m["id"]] = (float(p["prompt"]), float(p["completion"]))
        except Exception:
            pass
    return _prices


def client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(base_url=BASE_URL, api_key=os.environ["NEBIUS_API_KEY"], max_retries=2, timeout=120)
    return _client


class Budget:
    """Running spend for one check; refuses calls past its cap."""

    def __init__(self, cap_usd: float):
        self.cap, self.spent, self.calls = cap_usd, 0.0, []

    def add(self, rec: dict) -> None:
        self.spent += rec["cost_usd"]
        self.calls.append(rec)


async def chat(model_key: str, messages: list[dict], *, budget: Budget, role: str, thinking: bool = False,
               max_tokens: int = 900, temperature: float = 0.0) -> dict:
    """One chat call. Returns {"text", "cost_usd", "seconds", "tokens_in", "tokens_out", ...}."""
    if budget.spent >= budget.cap:
        raise RuntimeError(f"spend cap of US${budget.cap:.2f} reached for this check")
    m = MODELS[model_key]["model"]
    t0 = time.perf_counter()
    r = await client().chat.completions.create(
        model=m, messages=messages, temperature=temperature,
        max_tokens=max_tokens + (4096 if thinking else 0),
        extra_body={"chat_template_kwargs": {"enable_thinking": thinking}})
    secs = time.perf_counter() - t0
    pin, pout = (await prices()).get(m, FALLBACK_PRICES[m])
    u = r.usage
    text = r.choices[0].message.content or ""
    text = re.sub(r"(?s)^.*?</think>\s*", "", text) if "</think>" in text else text
    rec = {"role": role, "model": model_key, "thinking": thinking, "text": text.strip(),
           "tokens_in": u.prompt_tokens, "tokens_out": u.completion_tokens,
           "cost_usd": u.prompt_tokens * pin + u.completion_tokens * pout, "seconds": round(secs, 2)}
    budget.add(rec)
    return rec


def parse_json(text: str) -> dict:
    """The first JSON object in a model reply (tolerates ```json fences and chatter around it)."""
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    i = s.find("{")
    if i < 0:
        raise ValueError("no JSON object in reply")
    obj, _ = json.JSONDecoder().raw_decode(s[i:])
    return obj


async def chat_json(model_key: str, system: str, user: str, *, budget: Budget, role: str, retries: int = 1,
                    **kw) -> tuple[dict, dict]:
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for attempt in range(retries + 1):
        rec = await chat(model_key, msgs, budget=budget, role=role, **kw)
        try:
            return parse_json(rec["text"]), rec
        except (ValueError, json.JSONDecodeError):
            if attempt == retries:
                raise
            msgs = msgs + [{"role": "assistant", "content": rec["text"]},
                           {"role": "user", "content": "Reply with only the JSON object."}]
    raise AssertionError


async def gather(*aws):
    return await asyncio.gather(*aws)
