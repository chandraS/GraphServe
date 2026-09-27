import asyncio
from contextlib import asynccontextmanager
import json
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
from .core import Context, ingest, retrieve, verify, retrieval_scope
from .sie import rerank

REQUESTS = Counter("agent_requests_total", "Agent requests", ["operation", "status"])
LATENCY = Histogram("agent_request_seconds", "Question latency")
TOKENS = Histogram("agent_prompt_tokens", "Assembled prompt size", buckets=(512, 1024, 2048, 4096, 8192))
ROOT = Path(os.getenv("DATA_DIR", ".data")).resolve()
MOCK = os.getenv("INFERENCE_MODE", "mock") == "mock"
lock = asyncio.Lock()
context = None


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
        if not evidence:
            answer = {"claims": [], "proposal": [], "limitations": ["No usable source evidence found."]}
        elif MOCK:
            answer = {"claims": [{"text": "Mock response: this source excerpt was retrieved and verified.",
                                  "citations": [evidence[0]["id"]]}], "proposal": [],
                      "limitations": ["Mock mode does not perform model reasoning."]}
        else:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(os.getenv("INFERENCE_BASE_URL", "http://gateway:8080") + "/v1/chat/completions",
                    json={"model": "qwen-coder", "messages": messages, "temperature": 0,
                          "max_tokens": 1024, "response_format": {"type": "json_object"}})
                response.raise_for_status()
                answer = json.loads(response.json()["choices"][0]["message"]["content"])
        answer = await asyncio.to_thread(verify, answer, evidence, snapshot)
        REQUESTS.labels("question", "ok").inc()
        return {"answer": answer, "sources": evidence, "prompt_tokens": count, "scope": scope,
                "mode": "mock" if MOCK else "real",
                "verification": "Source IDs, blob contents and line ranges verified; semantic entailment is not guaranteed."}
    except (ValueError, KeyError, TypeError, subprocess.SubprocessError, httpx.HTTPError) as exc:
        REQUESTS.labels("question", "error").inc()
        raise HTTPException(502, "Retrieval, inference or source verification failed") from exc
    finally:
        LATENCY.observe(time.monotonic() - start)
