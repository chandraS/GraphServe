import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import re
import secrets
import subprocess
import time

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import httpx
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel, Field
from .core import Context, ingest, retrieve, verify, verified_subset, retrieval_scope
from .sie import rerank

REQUESTS = Counter("agent_requests_total", "Agent requests", ["operation", "status"])
LATENCY = Histogram("agent_request_seconds", "Question latency")
TOKENS = Histogram("agent_prompt_tokens", "Assembled prompt size", buckets=(512, 1024, 2048, 4096, 8192))
MODEL_OUTPUTS = Counter("agent_model_outputs_total", "Model output verification attempts", ["result"])
ROOT = Path(os.getenv("DATA_DIR", ".data")).resolve()
MOCK = os.getenv("INFERENCE_MODE", "mock") == "mock"
lock = asyncio.Lock()
context = None
logger = logging.getLogger("repo_agent")


@asynccontextmanager
async def lifespan(app):
    global context
    context = await asyncio.to_thread(Context, MOCK)
    yield


app = FastAPI(title="Repository understanding agent", lifespan=lifespan)


bearer = HTTPBearer(auto_error=False, description="Enter your API key only. For default local Docker development: local-development-only")


def auth(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    token = os.getenv("AGENT_API_KEY", "")
    if token and (credentials is None or not secrets.compare_digest(credentials.credentials.encode(), token.encode())):
        raise HTTPException(401, "Invalid API key", headers={"WWW-Authenticate": "Bearer"})
    if not token and not MOCK:
        raise HTTPException(503, "AGENT_API_KEY must be configured for real inference")


class Repository(BaseModel):
    url: str = Field(max_length=300)
    commit: str = Field(min_length=40, max_length=40)


class Question(BaseModel):
    repository_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    question: str = Field(min_length=1, max_length=12000)
    scope: str | None = Field(default=None, max_length=300)


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/healthz")
def health():
    return {"status": "ok", "mode": "mock" if MOCK else "real"}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/repositories", dependencies=[Depends(auth)])
async def repositories(body: Repository):
    try:
        async with lock:
            key = await asyncio.to_thread(ingest, ROOT, body.url, body.commit)
        REQUESTS.labels("ingest", "ok").inc()
        return {"repository_id": key, "commit": body.commit.lower()}
    except (ValueError, subprocess.SubprocessError, OSError):
        REQUESTS.labels("ingest", "error").inc()
        raise HTTPException(422, "Repository ingestion failed; check URL, SHA, reachability and Graphify installation")


@app.post("/questions", dependencies=[Depends(auth)])
async def questions(body: Question):
    start = time.monotonic()
    snapshot = ROOT / body.repository_id
    if not (snapshot / "snapshot.json").exists():
        raise HTTPException(404, "Repository snapshot not found")
    try:
        try:
            scope = await asyncio.to_thread(retrieval_scope, snapshot, body.question, body.scope)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        evidence = await asyncio.to_thread(retrieve, snapshot, body.question, scope=scope)
        if os.getenv("SIE_BASE_URL") and evidence:
            evidence = await rerank(body.question, evidence)
        messages, evidence, count = context.assemble(body.question, evidence)
        TOKENS.observe(count)
        inference_usage = None
        inference_usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        inference_attempts = 0
        answer_verified = False
        if not evidence:
            answer = {"claims": [], "proposal": [], "limitations": ["No usable source evidence found."]}
        elif MOCK:
            answer = {"claims": [{"text": "Mock response: this source excerpt was retrieved and verified.",
                                  "citations": [evidence[0]["id"]]}], "proposal": [],
                      "limitations": ["Mock mode does not perform model reasoning."]}
        else:
            async with httpx.AsyncClient(timeout=120) as client:
                request_messages = messages
                for attempt in range(2):
                    response = await client.post(
                        os.getenv("INFERENCE_BASE_URL", "http://gateway:8080") + "/v1/chat/completions",
                        json={"model": "qwen-coder", "messages": request_messages, "temperature": 0,
                              "max_tokens": 1024, "response_format": {"type": "json_object"}},
                    )
                    response.raise_for_status()
                    inference_response = response.json()
                    inference_attempts += 1
                    inference_usage = inference_response.get("usage") or {}
                    for name in inference_usage_total:
                        inference_usage_total[name] += int(inference_usage.get(name) or 0)
                    answer = json.loads(inference_response["choices"][0]["message"]["content"])
                    try:
                        answer = await asyncio.to_thread(verify, answer, evidence, snapshot)
                        MODEL_OUTPUTS.labels("valid" if attempt == 0 else "repaired").inc()
                        answer_verified = True
                        break
                    except ValueError as exc:
                        MODEL_OUTPUTS.labels("invalid").inc()
                        if attempt:
                            answer = await asyncio.to_thread(verified_subset, answer, evidence, snapshot)
                            MODEL_OUTPUTS.labels("filtered").inc()
                            answer_verified = True
                            break
                        logger.warning("Retrying model output after verification failure: %s", exc)
                        source_ids = ", ".join(item["id"] for item in evidence)
                        request_messages = [
                            {"role": "system", "content": messages[0]["content"] +
                             "\nVALIDATION RETRY: Your prior answer was rejected (" + str(exc) +
                             "). Return a fresh complete JSON object. Every claims/proposal item must "
                             "have a non-empty citations array using only these IDs: " + source_ids +
                             ". Omit any item you cannot cite; use an empty array when there are no items."},
                            messages[1],
                        ]
        if not answer_verified:
            answer = await asyncio.to_thread(verify, answer, evidence, snapshot)
        REQUESTS.labels("question", "ok").inc()
        return {"answer": answer, "sources": evidence, "prompt_tokens": count,
                "inference_usage": inference_usage,
                "inference_usage_total": inference_usage_total if inference_attempts else None,
                "inference_attempts": inference_attempts, "scope": scope,
                "mode": "mock" if MOCK else "real",
                "verification": "Source IDs, blob contents and line ranges verified; semantic entailment is not guaranteed."}
    except (ValueError, KeyError, TypeError, subprocess.SubprocessError, httpx.HTTPError) as exc:
        REQUESTS.labels("question", "error").inc()
        logger.exception("Question processing failed: %s", exc)
        raise HTTPException(502, "Retrieval, inference or source verification failed") from exc
    finally:
        LATENCY.observe(time.monotonic() - start)
