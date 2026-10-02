import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from repo_agent.gateway import Orchestrator, Rejected


def payload(content="Explain the repository", max_tokens=128):
    return json.dumps({
        "model": "qwen-coder",
        "messages": [
            {"role": "system", "content": "Return cited JSON."},
            {"role": "user", "content": content},
        ],
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }).encode()


def request(prefix="repo:root", tenant="tenant-a", priority="interactive", timeout_ms="30000"):
    return SimpleNamespace(headers={
        "x-graphserve-prefix-key": prefix,
        "x-graphserve-tenant": tenant,
        "x-graphserve-priority": priority,
        "x-graphserve-timeout-ms": timeout_ms,
    })


def test_named_guard_rejects_before_admission(monkeypatch):
    orchestrator = Orchestrator([("a", "http://worker-a")])
    assert orchestrator.inspect(payload()).estimated_total_tokens > 128
    for body, reason in [
        (b"not-json", "invalid_json"),
        (json.dumps({"model": "wrong", "messages": [{"role": "user", "content": "x"}]}).encode(), "model"),
        (payload("x" * 40_000, 1024), "context_tokens"),
    ]:
        with pytest.raises(Rejected) as exc:
            orchestrator.inspect(body)
        assert exc.value.reason == reason


def test_admission_affinity_and_bounded_queues(monkeypatch):
    monkeypatch.setenv("ORCH_QUEUE_CAPACITY", "1")
    monkeypatch.setenv("ORCH_DISPATCH_CONCURRENCY", "1")
    orchestrator = Orchestrator([("a", "http://worker-a"), ("b", "http://worker-b")])
    for worker in orchestrator.workers.values():
        worker.healthy = True

    async def exercise():
        guard = orchestrator.inspect(payload())
        first = await orchestrator.admit(guard, request(prefix="repo-one"))
        second = await orchestrator.admit(guard, request(prefix="repo-one"))
        assert first.worker != second.worker  # the affinity target queue is full
        assert second.placement_reason == "queue_load"
        with pytest.raises(Rejected) as exc:
            await orchestrator.admit(guard, request(prefix="repo-two"))
        assert (exc.value.status_code, exc.value.reason) == (429, "queue_full")

    asyncio.run(exercise())


def test_tenant_token_window(monkeypatch):
    monkeypatch.setenv("ORCH_TENANT_TOKENS_PER_MIN", "200")
    orchestrator = Orchestrator([("a", "http://worker-a")])
    orchestrator.workers["a"].healthy = True

    async def exercise():
        guard = orchestrator.inspect(payload(max_tokens=128))
        await orchestrator.admit(guard, request(prefix="one"))
        with pytest.raises(Rejected) as exc:
            await orchestrator.admit(guard, request(prefix="two"))
        assert (exc.value.status_code, exc.value.reason) == (429, "tenant_tokens")
        assert exc.value.retry_after

    asyncio.run(exercise())


def test_dispatch_records_worker_and_preserves_status(monkeypatch):
    monkeypatch.setenv("ORCH_DISPATCH_CONCURRENCY", "1")
    orchestrator = Orchestrator([("a", "http://worker-a"), ("b", "http://worker-b")])
    for worker in orchestrator.workers.values():
        worker.healthy = True

    def handler(req: httpx.Request):
        assert req.url.path == "/v1/chat/completions"
        return httpx.Response(200, request=req, json={
            "choices": [{"message": {"content": '{"claims":[]}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        })

    async def exercise():
        orchestrator.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        for worker in orchestrator.workers.values():
            worker.consumers.append(asyncio.create_task(orchestrator._consume(worker)))
        item = await orchestrator.admit(orchestrator.inspect(payload()), request())
        reply = await asyncio.wait_for(item.future, 2)
        assert reply.status_code == 200
        assert reply.headers["X-GraphServe-Worker"] == item.worker
        assert reply.headers["X-GraphServe-Placement"] in {"least_loaded", "prefix_affinity", "queue_load"}
        assert reply.headers["X-GraphServe-Hop"] == "cold_start"
        await orchestrator.stop()

    asyncio.run(exercise())


def test_dispatch_preserves_engine_overflow_metadata(monkeypatch):
    monkeypatch.setenv("ORCH_DISPATCH_CONCURRENCY", "1")
    orchestrator = Orchestrator([("a", "http://worker-a")])
    orchestrator.workers["a"].healthy = True

    def handler(req: httpx.Request):
        return httpx.Response(
            503,
            request=req,
            headers={"Retry-After": "7"},
            json={"detail": "engine overloaded"},
        )

    async def exercise():
        orchestrator.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        worker = orchestrator.workers["a"]
        worker.consumers.append(asyncio.create_task(orchestrator._consume(worker)))
        item = await orchestrator.admit(orchestrator.inspect(payload()), request())
        reply = await asyncio.wait_for(item.future, 2)
        assert reply.status_code == 503
        assert reply.headers["Retry-After"] == "7"
        assert reply.headers["X-GraphServe-Overflow-Decision"] == "stay"
        await orchestrator.stop()

    asyncio.run(exercise())
