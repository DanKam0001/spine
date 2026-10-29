"""Spine web app: POST a message, stream the check as server-sent events, replay saved runs."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from spine import llm
from spine.check import run_check

ROOT = Path(__file__).resolve().parent
RUNS = Path(os.environ.get("SPINE_RUNS_DIR", ROOT / "runs"))
DEMO = ROOT / "demo_runs"
RUNS.mkdir(parents=True, exist_ok=True)
SERVER_CAP = float(os.environ.get("SPINE_SERVER_CAP_USD", "5"))   # total live spend this instance may make
CHECK_CAP = float(os.environ.get("SPINE_CHECK_CAP_USD", "0.10"))  # per check
MAX_ACTIVE = int(os.environ.get("SPINE_MAX_ACTIVE", "3"))
MAX_CHARS = 6000

app = FastAPI(title="Spine")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
state = {"spent": 0.0, "active": 0}


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "live": llm.has_key() and state["spent"] < SERVER_CAP,
            "spent_usd": round(state["spent"], 4), "cap_usd": SERVER_CAP,
            "models": {k: v["model"] for k, v in llm.MODELS.items()}}


@app.get("/api/runs")
def list_runs():
    out = []
    for p in DEMO.glob("*.json"):
        d = json.loads(p.read_text(encoding="utf-8"))
        out.append({"id": p.stem, "title": d.get("title", ""), "verdict": d.get("verdict"), "order": d.get("order", 99)})
    return sorted(out, key=lambda r: r["order"])


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    if not run_id.isalnum():
        raise HTTPException(404)
    for d in (DEMO, RUNS):
        p = d / f"{run_id}.json"
        if p.exists():
            return JSONResponse(json.loads(p.read_text(encoding="utf-8")))
    raise HTTPException(404)


@app.post("/api/check")
async def check(req: Request):
    body = await req.json()
    message = str(body.get("message", "")).strip()
    reply = str(body.get("reply") or "").strip() or None
    target = body.get("target", "nano")
    if not message or len(message) > MAX_CHARS or (reply and len(reply) > MAX_CHARS):
        raise HTTPException(400, f"message is required, up to {MAX_CHARS} characters")
    if target not in llm.MODELS:
        raise HTTPException(400, "unknown model")
    if not llm.has_key() or state["spent"] >= SERVER_CAP:
        raise HTTPException(503, "Live checks are paused (this server's spend cap is used up). "
                                 "The recorded checks below still replay.")
    if state["active"] >= MAX_ACTIVE:
        raise HTTPException(429, "A few checks are already running. Try again in a moment.")

    run_id = secrets.token_hex(6)
    events: list[dict] = []

    async def stream():
        state["active"] += 1
        try:
            yield f"data: {json.dumps({'type': 'run', 'id': run_id})}\n\n"
            async for ev in run_check(message, target=target, target_thinking=bool(body.get("thinking")),
                                      reply=reply, pushback=bool(body.get("pushback")), cap_usd=CHECK_CAP):
                events.append(ev)
                if ev["type"] == "done":
                    state["spent"] += ev["cost_usd"]
                yield f"data: {json.dumps(ev)}\n\n"
        finally:
            state["active"] -= 1
            if events:
                done = events[-1]
                (RUNS / f"{run_id}.json").write_text(json.dumps(
                    {"id": run_id, "verdict": done.get("verdict"), "events": events}, ensure_ascii=False),
                    encoding="utf-8")

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
