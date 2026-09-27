"""Bounded inference proxy. No retrieval, tools or repository data access."""
import asyncio
import os
from fastapi import FastAPI, HTTPException, Request, Response
import httpx
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

app = FastAPI(title="Inference gateway")
slots = asyncio.Semaphore(4)
CALLS = Counter("gateway_requests_total", "Inference calls", ["status"])
DURATION = Histogram("gateway_inference_seconds", "Upstream inference duration")

@app.get("/healthz")
def health():
    return {"status": "ok"}

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

@app.post("/v1/chat/completions")
async def chat(request: Request):
    raw = await request.body()
    if len(raw) > 200_000:
        raise HTTPException(413, "Request too large")
    body = await request.json()
    if body.get("model") != "qwen-coder" or body.get("stream") or not 1 <= body.get("max_tokens", 1024) <= 1024:
        raise HTTPException(422, "Unsupported model, stream or output budget")
    try:
        await asyncio.wait_for(slots.acquire(), timeout=1)
    except TimeoutError:
        CALLS.labels("busy").inc()
        raise HTTPException(429, "Inference capacity busy")
    try:
        with DURATION.time():
            async with httpx.AsyncClient(timeout=120) as client:
                upstream = await client.post(os.getenv("RAY_BASE_URL", "http://repo-llm-serve-svc:8000") + "/v1/chat/completions", json=body)
        CALLS.labels(str(upstream.status_code)).inc()
        return Response(upstream.content, status_code=upstream.status_code, media_type="application/json")
    except httpx.HTTPError:
        CALLS.labels("unavailable").inc()
        raise HTTPException(503, "Inference unavailable")
    finally:
        slots.release()
