"""Explicit guard, admission, placement and bounded queues in front of vLLM.

The gateway owns decisions made before an engine request. vLLM still owns token
scheduling, continuous batching, KV block allocation, waiting and preemption.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
import itertools
import json
import math
import os
import re
import time
import uuid

from fastapi import FastAPI, HTTPException, Request, Response
import httpx
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST


GUARD = Counter("orch_guard_decisions_total", "Gateway guard decisions", ["decision", "reason"])
ADMISSION = Counter("orch_admission_decisions_total", "Gateway admission decisions", ["decision", "reason"])
PLACEMENT = Counter("orch_placement_total", "Gateway worker placements", ["worker", "reason"])
HOPS = Counter("orch_hop_total", "Prefix placement outcomes", ["source", "target", "outcome"])
OVERFLOW = Counter("orch_overflow_total", "Post-engine overflow decisions", ["decision", "reason"])
CANCELLATIONS = Counter("orch_cancellations_total", "Cancelled requests", ["stage", "reason"])
CALLS = Counter("gateway_requests_total", "Inference calls", ["status"])
DURATION = Histogram("gateway_inference_seconds", "Upstream inference duration", ["worker"])
QUEUE_WAIT = Histogram("orch_queue_wait_seconds", "Time in the gateway queue", ["worker", "priority"])
QUEUE_DEPTH = Gauge("orch_replica_queue_depth", "Requests in the gateway queue", ["worker", "priority"])
INFLIGHT = Gauge("orch_replica_inflight", "Requests dispatched to an engine", ["worker"])
WORKER_HEALTH = Gauge("orch_worker_health", "Worker health probe result", ["worker"])
WORKER_KV = Gauge("orch_worker_kv_cache_usage", "Last observed vLLM KV cache usage", ["worker"])
ENGINE_REQUESTS = Gauge("orch_engine_requests", "Last observed vLLM requests", ["worker", "state"])
AFFINITY_EVICTIONS = Counter("orch_affinity_evictions_total", "Prefix affinity evictions", ["reason"])


def _integer(name: str, default: int, minimum: int = 1) -> int:
    value = int(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _floating(name: str, default: float, minimum: float = 0.0) -> float:
    value = float(os.getenv(name, str(default)))
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def parse_workers() -> list[tuple[str, str]]:
    configured = os.getenv("WORKER_ENDPOINTS", "").strip()
    if not configured:
        return [("default", os.getenv("RAY_BASE_URL", "http://repo-llm-serve-svc:8000").rstrip("/"))]
    workers = []
    for entry in configured.split(","):
        name, separator, url = entry.partition("=")
        name, url = name.strip(), url.strip().rstrip("/")
        if not separator or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name):
            raise ValueError("WORKER_ENDPOINTS entries must use name=http(s)://host")
        if not re.fullmatch(r"https?://[^\s]+", url):
            raise ValueError(f"Invalid worker URL for {name}")
        workers.append((name, url))
    if len({name for name, _ in workers}) != len(workers):
        raise ValueError("Worker names must be unique")
    return workers


@dataclass(frozen=True)
class GuardResult:
    body: dict
    estimated_prompt_tokens: int
    estimated_total_tokens: int


@dataclass
class GatewayReply:
    content: bytes
    status_code: int
    media_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class WorkItem:
    request_id: str
    body: dict
    tenant: str
    prefix_key: str
    priority: str
    priority_value: int
    estimated_tokens: int
    deadline: float
    worker: str
    placement_reason: str
    hop_outcome: str
    enqueued_at: float
    future: asyncio.Future
    cancelled: bool = False
    upstream_task: asyncio.Task | None = None


@dataclass
class WorkerState:
    name: str
    url: str
    queue: asyncio.PriorityQueue
    healthy: bool = False
    inflight: int = 0
    latency_ema: float = 8.0
    kv_usage: float | None = None
    engine_running: float = 0.0
    engine_waiting: float = 0.0
    telemetry_at: float = 0.0
    consumers: list[asyncio.Task] = field(default_factory=list)

    def load(self) -> float:
        kv_penalty = 4.0 * self.kv_usage if self.kv_usage is not None else 0.0
        return self.queue.qsize() + self.inflight + self.engine_waiting + kv_penalty


class Rejected(Exception):
    def __init__(self, status_code: int, reason: str, retry_after: int | None = None):
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason
        self.retry_after = retry_after


class Orchestrator:
    def __init__(self, workers: list[tuple[str, str]] | None = None):
        self.queue_capacity = _integer("ORCH_QUEUE_CAPACITY", 8)
        self.dispatch_concurrency = _integer("ORCH_DISPATCH_CONCURRENCY", 4)
        self.tenant_tokens_per_minute = _integer("ORCH_TENANT_TOKENS_PER_MIN", 50_000)
        self.affinity_ttl = _floating("ORCH_AFFINITY_TTL_SECONDS", 900)
        self.affinity_limit = _integer("ORCH_AFFINITY_MAX_ENTRIES", 10_000)
        self.affinity_imbalance = _floating("ORCH_AFFINITY_IMBALANCE", 2.0)
        self.kv_shed_threshold = _floating("ORCH_KV_SHED_THRESHOLD", 0.95)
        self.health_interval = _floating("ORCH_HEALTH_INTERVAL_SECONDS", 5.0, 0.1)
        self.telemetry_interval = _floating("ORCH_TELEMETRY_INTERVAL_SECONDS", 5.0, 0.1)
        self.default_timeout = _floating("ORCH_REQUEST_TIMEOUT_SECONDS", 120.0, 1.0)
        self.prometheus_url = os.getenv(
            "PROMETHEUS_URL", "http://monitoring-kube-prometheus-prometheus.monitoring.svc:9090"
        ).rstrip("/")
        rows = workers or parse_workers()
        self.workers = {
            name: WorkerState(name, url, asyncio.PriorityQueue(self.queue_capacity))
            for name, url in rows
        }
        self.affinity: dict[str, tuple[str, float]] = {}
        self.tenant_usage: dict[str, deque[tuple[float, int]]] = defaultdict(deque)
        self.lock = asyncio.Lock()
        self.sequence = itertools.count()
        self.background: list[asyncio.Task] = []
        self.client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self.client = httpx.AsyncClient(timeout=None)
        await self.refresh_health()
        for worker in self.workers.values():
            for _ in range(self.dispatch_concurrency):
                worker.consumers.append(asyncio.create_task(self._consume(worker)))
        self.background = [
            asyncio.create_task(self._health_loop()),
            asyncio.create_task(self._telemetry_loop()),
            asyncio.create_task(self._affinity_loop()),
        ]

    async def stop(self) -> None:
        tasks = self.background + [task for worker in self.workers.values() for task in worker.consumers]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.client:
            await self.client.aclose()

    async def refresh_health(self) -> None:
        async def probe(worker: WorkerState) -> None:
            try:
                assert self.client
                response = await self.client.get(worker.url + "/v1/models", timeout=3)
                worker.healthy = response.status_code < 500
            except httpx.HTTPError:
                worker.healthy = False
            WORKER_HEALTH.labels(worker.name).set(1 if worker.healthy else 0)
        await asyncio.gather(*(probe(worker) for worker in self.workers.values()))

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(self.health_interval)
            await self.refresh_health()

    async def _prometheus_value(self, query: str) -> float | None:
        try:
            assert self.client
            response = await self.client.get(
                self.prometheus_url + "/api/v1/query", params={"query": query}, timeout=3
            )
            response.raise_for_status()
            rows = response.json().get("data", {}).get("result", [])
            return max((float(row["value"][1]) for row in rows), default=None)
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return None

    async def refresh_telemetry(self) -> None:
        for worker in self.workers.values():
            pod_pattern = f"repo-llm-{worker.name}-.*gpu-worker.*" if worker.name != "default" else ".*gpu-worker.*"
            selector = f'pod=~"{pod_pattern}"'
            kv, running, waiting = await asyncio.gather(
                self._prometheus_value(f"ray_vllm_kv_cache_usage_perc{{{selector}}}"),
                self._prometheus_value(f"ray_vllm_num_requests_running{{{selector}}}"),
                self._prometheus_value(f"ray_vllm_num_requests_waiting{{{selector}}}"),
            )
            if kv is not None:
                worker.kv_usage = kv
                WORKER_KV.labels(worker.name).set(kv)
            if running is not None:
                worker.engine_running = running
                ENGINE_REQUESTS.labels(worker.name, "running").set(running)
            if waiting is not None:
                worker.engine_waiting = waiting
                ENGINE_REQUESTS.labels(worker.name, "waiting").set(waiting)
            if any(value is not None for value in (kv, running, waiting)):
                worker.telemetry_at = time.monotonic()

    async def _telemetry_loop(self) -> None:
        while True:
            await self.refresh_telemetry()
            await asyncio.sleep(self.telemetry_interval)

    async def _affinity_loop(self) -> None:
        while True:
            await asyncio.sleep(min(self.affinity_ttl, 60))
            now = time.monotonic()
            async with self.lock:
                expired = [key for key, (_, expiry) in self.affinity.items() if expiry <= now]
                for key in expired:
                    self.affinity.pop(key, None)
                    AFFINITY_EVICTIONS.labels("ttl").inc()

    def inspect(self, raw: bytes) -> GuardResult:
        if len(raw) > 200_000:
            GUARD.labels("reject", "body_bytes").inc()
            raise Rejected(413, "body_bytes")
        try:
            body = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            GUARD.labels("reject", "invalid_json").inc()
            raise Rejected(400, "invalid_json")
        if not isinstance(body, dict):
            GUARD.labels("reject", "invalid_shape").inc()
            raise Rejected(422, "invalid_shape")
        if body.get("model") != "qwen-coder":
            GUARD.labels("reject", "model").inc()
            raise Rejected(422, "model")
        if body.get("stream") not in (None, False):
            GUARD.labels("reject", "stream").inc()
            raise Rejected(422, "stream")
        max_tokens = body.get("max_tokens", 1024)
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= 1024:
            GUARD.labels("reject", "output_tokens").inc()
            raise Rejected(422, "output_tokens")
        messages = body.get("messages")
        if not isinstance(messages, list) or not 1 <= len(messages) <= 64:
            GUARD.labels("reject", "messages").inc()
            raise Rejected(422, "messages")
        chars = 0
        for message in messages:
            if (not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant", "tool"}
                    or not isinstance(message.get("content"), str)):
                GUARD.labels("reject", "message_shape").inc()
                raise Rejected(422, "message_shape")
            chars += len(message["content"])
        if body.get("tools"):
            GUARD.labels("reject", "tools").inc()
            raise Rejected(422, "tools")
        prompt_tokens = math.ceil(chars / 4) + 16 * len(messages)
        total_tokens = prompt_tokens + max_tokens
        if total_tokens > 8192:
            GUARD.labels("reject", "context_tokens").inc()
            raise Rejected(422, "context_tokens")
        GUARD.labels("allow", "valid").inc()
        return GuardResult(body, prompt_tokens, total_tokens)

    def request_metadata(self, request: Request, body: dict) -> tuple[str, str, str, int, float]:
        tenant = request.headers.get("x-graphserve-tenant", "anonymous")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", tenant):
            raise Rejected(422, "tenant")
        priority = request.headers.get("x-graphserve-priority", "interactive")
        priorities = {"interactive": 0, "batch": 10}
        if priority not in priorities:
            raise Rejected(422, "priority")
        prefix_key = request.headers.get("x-graphserve-prefix-key", "")
        if not prefix_key:
            system = next((m["content"] for m in body["messages"] if m["role"] == "system"), "")
            prefix_key = hashlib.sha256((tenant + "\0" + system).encode()).hexdigest()[:32]
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", prefix_key):
            prefix_key = hashlib.sha256(prefix_key.encode()).hexdigest()[:32]
        timeout_ms = request.headers.get("x-graphserve-timeout-ms", "")
        try:
            timeout = float(timeout_ms) / 1000 if timeout_ms else self.default_timeout
        except ValueError:
            raise Rejected(422, "deadline")
        if not 1 <= timeout <= 600:
            raise Rejected(422, "deadline")
        return tenant, prefix_key, priority, priorities[priority], timeout

    def _cleanup_tenant(self, tenant: str, now: float) -> int:
        window = self.tenant_usage[tenant]
        while window and window[0][0] <= now - 60:
            window.popleft()
        return sum(tokens for _, tokens in window)

    def pick(self, prefix_key: str, candidates: list[WorkerState], now: float) -> tuple[WorkerState, str, str, str]:
        previous = self.affinity.get(prefix_key)
        previous_name = previous[0] if previous else "none"
        if previous and previous[1] <= now:
            self.affinity.pop(prefix_key, None)
            AFFINITY_EVICTIONS.labels("ttl").inc()
            previous = None
            previous_name = "none"
        minimum = min(worker.load() for worker in candidates)
        preferred = next((worker for worker in candidates if previous and worker.name == previous[0]), None)
        if preferred and preferred.load() <= minimum + self.affinity_imbalance:
            chosen, reason = preferred, "prefix_affinity"
        else:
            best = [worker for worker in candidates if worker.load() == minimum]
            chosen = best[int(hashlib.sha256(prefix_key.encode()).hexdigest(), 16) % len(best)]
            reason = "least_loaded"
        outcome = "same_worker" if previous_name == chosen.name else (
            "cold_start" if previous_name == "none" else "cold_recompute"
        )
        return chosen, reason, previous_name, outcome

    def should_shed(self, guard: GuardResult, tenant: str, worker: WorkerState,
                    timeout: float, now: float) -> Rejected | None:
        used = self._cleanup_tenant(tenant, now)
        if used + guard.estimated_total_tokens > self.tenant_tokens_per_minute:
            oldest = self.tenant_usage[tenant][0][0] if self.tenant_usage[tenant] else now
            return Rejected(429, "tenant_tokens", max(1, math.ceil(oldest + 60 - now)))
        batches_ahead = worker.queue.qsize() // self.dispatch_concurrency
        estimated_wait = batches_ahead * worker.latency_ema
        if estimated_wait + min(worker.latency_ema, 5.0) >= timeout:
            return Rejected(429, "timeout_queue", max(1, math.ceil(worker.latency_ema)))
        return None

    async def admit(self, guard: GuardResult, request: Request) -> WorkItem:
        try:
            tenant, prefix_key, priority, priority_value, timeout = self.request_metadata(request, guard.body)
        except Rejected as exc:
            ADMISSION.labels("reject", exc.reason).inc()
            raise
        now = time.monotonic()
        async with self.lock:
            candidates = [worker for worker in self.workers.values() if worker.healthy]
            if not candidates:
                ADMISSION.labels("reject", "no_healthy_worker").inc()
                raise Rejected(503, "no_healthy_worker", 5)
            kv_candidates = [
                worker for worker in candidates
                if worker.kv_usage is None or worker.kv_usage < self.kv_shed_threshold
            ]
            if not kv_candidates:
                ADMISSION.labels("reject", "kv_free").inc()
                raise Rejected(429, "kv_free", 2)
            chosen, reason, source, outcome = self.pick(prefix_key, kv_candidates, now)
            if chosen.queue.full():
                alternatives = [worker for worker in kv_candidates if not worker.queue.full()]
                if alternatives:
                    original_source = source
                    minimum = min(worker.load() for worker in alternatives)
                    best = [worker for worker in alternatives if worker.load() == minimum]
                    chosen = best[int(hashlib.sha256(prefix_key.encode()).hexdigest(), 16) % len(best)]
                    reason = "queue_load"
                    source = original_source
                    outcome = "cold_start" if source == "none" else (
                        "same_worker" if source == chosen.name else "cold_recompute"
                    )
                else:
                    ADMISSION.labels("reject", "queue_full").inc()
                    raise Rejected(429, "queue_full", max(1, math.ceil(chosen.latency_ema)))
            shed = self.should_shed(guard, tenant, chosen, timeout, now)
            if shed:
                ADMISSION.labels("reject", shed.reason).inc()
                raise shed
            future = asyncio.get_running_loop().create_future()
            item = WorkItem(
                request_id=uuid.uuid4().hex,
                body=guard.body,
                tenant=tenant,
                prefix_key=prefix_key,
                priority=priority,
                priority_value=priority_value,
                estimated_tokens=guard.estimated_total_tokens,
                deadline=now + timeout,
                worker=chosen.name,
                placement_reason=reason,
                hop_outcome=outcome,
                enqueued_at=now,
                future=future,
            )
            chosen.queue.put_nowait((priority_value, next(self.sequence), item))
            self.tenant_usage[tenant].append((now, guard.estimated_total_tokens))
            if len(self.affinity) >= self.affinity_limit and prefix_key not in self.affinity:
                oldest_key = min(self.affinity, key=lambda key: self.affinity[key][1])
                self.affinity.pop(oldest_key, None)
                AFFINITY_EVICTIONS.labels("capacity").inc()
            self.affinity[prefix_key] = (chosen.name, now + self.affinity_ttl)
            QUEUE_DEPTH.labels(chosen.name, priority).inc()
            ADMISSION.labels("allow", "capacity").inc()
            PLACEMENT.labels(chosen.name, reason).inc()
            HOPS.labels(source, chosen.name, outcome).inc()
            return item

    async def cancel(self, item: WorkItem, reason: str) -> None:
        item.cancelled = True
        if item.upstream_task and not item.upstream_task.done():
            item.upstream_task.cancel()
            CANCELLATIONS.labels("engine", reason).inc()
        else:
            CANCELLATIONS.labels("queue", reason).inc()

    async def _consume(self, worker: WorkerState) -> None:
        assert self.client
        while True:
            _, _, item = await worker.queue.get()
            QUEUE_DEPTH.labels(worker.name, item.priority).dec()
            dispatched = False
            try:
                if item.cancelled or item.future.cancelled():
                    continue
                remaining = item.deadline - time.monotonic()
                if remaining <= 0:
                    if not item.future.done():
                        item.future.set_result(GatewayReply(
                            json.dumps({"detail": "timeout_queue", "reason": "timeout_queue"}).encode(),
                            429, headers={"Retry-After": "1"},
                        ))
                    ADMISSION.labels("reject", "timeout_queue").inc()
                    CALLS.labels("429").inc()
                    continue
                queue_wait = time.monotonic() - item.enqueued_at
                QUEUE_WAIT.labels(worker.name, item.priority).observe(queue_wait)
                worker.inflight += 1
                dispatched = True
                INFLIGHT.labels(worker.name).set(worker.inflight)
                started = time.monotonic()
                item.upstream_task = asyncio.create_task(self.client.post(
                    worker.url + "/v1/chat/completions", json=item.body, timeout=remaining
                ))
                try:
                    upstream = await item.upstream_task
                    status = upstream.status_code
                    reply_headers = {
                        "X-GraphServe-Request-ID": item.request_id,
                        "X-GraphServe-Worker": worker.name,
                        "X-GraphServe-Placement": item.placement_reason,
                        "X-GraphServe-Hop": item.hop_outcome,
                        "X-GraphServe-Queue-Wait-Ms": str(round(queue_wait * 1000, 2)),
                    }
                    if upstream.headers.get("retry-after"):
                        reply_headers["Retry-After"] = upstream.headers["retry-after"]
                    if status in {503, 529}:
                        OVERFLOW.labels("stay", str(status)).inc()
                        reply_headers["X-GraphServe-Overflow-Decision"] = "stay"
                        reply_headers.setdefault("Retry-After", "1")
                    CALLS.labels(str(status)).inc()
                    reply = GatewayReply(
                        upstream.content,
                        status,
                        upstream.headers.get("content-type", "application/json").split(";", 1)[0],
                        reply_headers,
                    )
                except asyncio.CancelledError:
                    if not item.cancelled:
                        raise
                    continue
                except httpx.HTTPError:
                    worker.healthy = False
                    WORKER_HEALTH.labels(worker.name).set(0)
                    OVERFLOW.labels("stay", "unavailable").inc()
                    CALLS.labels("unavailable").inc()
                    reply = GatewayReply(
                        json.dumps({"detail": "worker_unavailable", "reason": "worker_unavailable"}).encode(),
                        503,
                        headers={"Retry-After": "5", "X-GraphServe-Request-ID": item.request_id,
                                 "X-GraphServe-Worker": worker.name},
                    )
                elapsed = time.monotonic() - started
                worker.latency_ema = 0.8 * worker.latency_ema + 0.2 * elapsed
                DURATION.labels(worker.name).observe(elapsed)
                if not item.future.done():
                    item.future.set_result(reply)
            finally:
                if dispatched:
                    worker.inflight = max(0, worker.inflight - 1)
                    INFLIGHT.labels(worker.name).set(worker.inflight)
                worker.queue.task_done()

    def health(self) -> dict:
        return {
            "status": "ok",
            "workers": {
                worker.name: {
                    "healthy": worker.healthy,
                    "queue_depth": worker.queue.qsize(),
                    "inflight": worker.inflight,
                    "kv_cache_usage": worker.kv_usage,
                    "engine_running": worker.engine_running,
                    "engine_waiting": worker.engine_waiting,
                }
                for worker in self.workers.values()
            },
        }


orchestrator: Orchestrator | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global orchestrator
    orchestrator = Orchestrator()
    await orchestrator.start()
    yield
    await orchestrator.stop()


app = FastAPI(title="Inference gateway", lifespan=lifespan)


@app.get("/healthz")
def health():
    return orchestrator.health() if orchestrator else {"status": "starting", "workers": {}}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def rejection(exc: Rejected) -> HTTPException:
    headers = {"X-GraphServe-Rejection-Reason": exc.reason}
    if exc.retry_after is not None:
        headers["Retry-After"] = str(exc.retry_after)
    CALLS.labels(str(exc.status_code)).inc()
    return HTTPException(exc.status_code, exc.reason, headers=headers)


@app.post("/v1/chat/completions")
async def chat(request: Request):
    assert orchestrator
    try:
        guard = orchestrator.inspect(await request.body())
        item = await orchestrator.admit(guard, request)
    except Rejected as exc:
        raise rejection(exc)
    try:
        while True:
            remaining = item.deadline - time.monotonic()
            if remaining <= 0:
                await orchestrator.cancel(item, "deadline")
                raise rejection(Rejected(429, "timeout_queue", 1))
            done, _ = await asyncio.wait({item.future}, timeout=min(0.25, remaining))
            if done:
                reply = item.future.result()
                return Response(reply.content, status_code=reply.status_code,
                                media_type=reply.media_type, headers=reply.headers)
            if await request.is_disconnected():
                await orchestrator.cancel(item, "client_gone")
                raise HTTPException(499, "client_gone")
    except asyncio.CancelledError:
        await orchestrator.cancel(item, "client_gone")
        raise
