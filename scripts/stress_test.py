#!/usr/bin/env python3
"""Drive GraphServe with repository questions and save request/Prometheus evidence."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any

import httpx

QUESTIONS = [
    "Trace a text request from the gateway through routing, worker selection, and completion.",
    "When does a 429 remain local, and when does a 503 overflow?",
    "Explain how prefill and decode KV transfer works with Mooncake.",
    "How does the planner decide whether to add prefill or decode replicas?",
    "Propose adding a circuit breaker to overflow calls. Name the files and tests to change.",
    "Which tests verify prefix-aware routing, and what behavior do they prove?",
    "Where is queue depth used when selecting a worker?",
    "Explain how the code prevents one tenant from owning all GPU capacity.",
]

METRIC_QUERIES = {
    "agent_requests": "agent_requests_total",
    "model_output_validation": "agent_model_outputs_total",
    "gateway_requests": "gateway_requests_total",
    "orch_guard": "orch_guard_decisions_total",
    "orch_admission": "orch_admission_decisions_total",
    "orch_placement": "orch_placement_total",
    "orch_queue_depth": "orch_replica_queue_depth",
    "orch_queue_wait": "orch_queue_wait_seconds_bucket",
    "orch_hops": "orch_hop_total",
    "orch_overflow": "orch_overflow_total",
    "orch_worker_health": "orch_worker_health",
    "orch_worker_kv": "orch_worker_kv_cache_usage",
    "ttft_buckets": "ray_vllm_time_to_first_token_seconds_bucket",
    "ttft_p95": "histogram_quantile(0.95, sum by (le) (rate(ray_vllm_time_to_first_token_seconds_bucket[1m])))",
    "e2e_buckets": "ray_vllm_e2e_request_latency_seconds_bucket",
    "e2e_p95": "histogram_quantile(0.95, sum by (le) (rate(ray_vllm_e2e_request_latency_seconds_bucket[1m])))",
    "tpot_buckets": "ray_vllm_time_per_output_token_seconds_bucket",
    "tpot_p95": "histogram_quantile(0.95, sum by (le) (rate(ray_vllm_time_per_output_token_seconds_bucket[1m])))",
    "engine_token_rate": "sum(rate(ray_vllm_prompt_tokens_total[1m])) + sum(rate(ray_vllm_generation_tokens_total[1m]))",
    "engine_request_rate": "sum(rate(ray_vllm_request_success_total[1m]))",
    "prompt_tokens": "ray_vllm_prompt_tokens_total",
    "generation_tokens": "ray_vllm_generation_tokens_total",
    "request_success": "ray_vllm_request_success_total",
    "running": "ray_vllm_num_requests_running",
    "waiting": "ray_vllm_num_requests_waiting",
    "kv_cache": "ray_vllm_kv_cache_usage_perc",
    "serve_queued": "ray_serve_deployment_queued_queries",
    "gpu_utilization": 'ray_node_gpus_utilization{RayNodeType="worker"}',
    "gpu_memory_mb": 'ray_node_gram_used{RayNodeType="worker"}',
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    rows = sorted(values)
    point = (len(rows) - 1) * quantile
    lower, upper = math.floor(point), math.ceil(point)
    if lower == upper:
        return rows[lower]
    return rows[lower] + (rows[upper] - rows[lower]) * (point - lower)


def read_key(path: Path) -> str:
    for line in path.read_text().splitlines():
        if line.startswith("AGENT_API_KEY="):
            key = line.split("=", 1)[1].strip()
            if key:
                return key
    raise ValueError(f"AGENT_API_KEY is missing from {path}")


def wait_http(url: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    last = "not attempted"
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=3)
            if response.status_code < 500:
                return
            last = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for {url}: {last}")


def start_tunnel(host: str, user: str, key: Path) -> tuple[subprocess.Popen, str, str]:
    app_port, prometheus_port = free_port(), free_port()
    command = [
        "ssh", "-i", str(key), "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new", "-o", "ExitOnForwardFailure=yes",
        "-N", "-L", f"{app_port}:127.0.0.1:8080",
        "-L", f"{prometheus_port}:127.0.0.1:9090", f"{user}@{host}",
    ]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    app_url = f"http://127.0.0.1:{app_port}"
    prometheus_url = f"http://127.0.0.1:{prometheus_port}"
    try:
        wait_http(app_url + "/healthz")
        wait_http(prometheus_url + "/-/ready")
    except Exception:
        process.terminate()
        error = process.stderr.read() if process.stderr else ""
        raise RuntimeError(f"SSH tunnel failed: {error.strip()}")
    return process, app_url, prometheus_url


def run_request(base_url: str, api_key: str, repository_id: str, scope: str | None,
                question: str, sequence: int, phase: str, timeout: float) -> dict[str, Any]:
    started_wall = time.time()
    started = time.perf_counter()
    record: dict[str, Any] = {
        "sequence": sequence,
        "phase": phase,
        "started_at": datetime.fromtimestamp(started_wall, timezone.utc).isoformat(),
        "question": question,
    }
    try:
        response = httpx.post(
            base_url.rstrip("/") + "/questions",
            headers={"Authorization": "Bearer " + api_key},
            json={"repository_id": repository_id, "question": question, "scope": scope},
            timeout=timeout,
        )
        record["status_code"] = response.status_code
        record["response_headers"] = {
            name: response.headers[name]
            for name in (
                "retry-after", "x-graphserve-request-id", "x-graphserve-worker",
                "x-graphserve-placement", "x-graphserve-hop", "x-graphserve-queue-wait-ms",
                "x-graphserve-rejection-reason", "x-graphserve-overflow-decision",
            )
            if name in response.headers
        }
        try:
            body = response.json()
        except ValueError:
            body = {"detail": response.text[:1000]}
        record["ok"] = response.is_success
        record["response"] = body
    except httpx.HTTPError as exc:
        record.update(status_code=None, ok=False, error=f"{type(exc).__name__}: {exc}")
    record["latency_s"] = time.perf_counter() - started
    record["finished_at"] = utc_now()
    return record


def prometheus_range(base_url: str, start: float, end: float, step: int) -> dict[str, Any]:
    output: dict[str, Any] = {}
    with httpx.Client(timeout=30) as client:
        for name, query in METRIC_QUERIES.items():
            response = client.get(
                base_url.rstrip("/") + "/api/v1/query_range",
                params={"query": query, "start": start, "end": end, "step": step},
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "success":
                raise RuntimeError(f"Prometheus query failed for {name}: {payload}")
            output[name] = {"query": query, "result": payload["data"]["result"]}
    return output


def summarize(records: list[dict[str, Any]], elapsed: float, concurrency: int) -> dict[str, Any]:
    latencies = [float(row["latency_s"]) for row in records if row.get("ok")]
    status_counts: dict[str, int] = {}
    prompt_tokens = completion_tokens = 0
    serving_counts: dict[str, dict[str, int]] = {
        "workers": {}, "placements": {}, "hops": {},
    }
    queue_wait_ms: list[float] = []
    retry_after_counts: dict[str, int] = {}
    for row in records:
        key = str(row.get("status_code") or "transport_error")
        status_counts[key] = status_counts.get(key, 0) + 1
        response = row.get("response", {})
        usage = response.get("inference_usage_total") or response.get("inference_usage") or {}
        prompt_tokens += int(usage.get("prompt_tokens") or 0)
        completion_tokens += int(usage.get("completion_tokens") or 0)
        serving = response.get("serving") or {}
        for source, field in (("workers", "worker"), ("placements", "placement"), ("hops", "hop")):
            value = serving.get(field)
            if value:
                serving_counts[source][str(value)] = serving_counts[source].get(str(value), 0) + 1
        if serving.get("queue_wait_ms") is not None:
            try:
                queue_wait_ms.append(float(serving["queue_wait_ms"]))
            except (TypeError, ValueError):
                pass
        retry_after = (row.get("response_headers") or {}).get("retry-after")
        if retry_after:
            retry_after_counts[retry_after] = retry_after_counts.get(retry_after, 0) + 1
    return {
        "concurrency": concurrency,
        "requests": len(records),
        "successful": len(latencies),
        "failed": len(records) - len(latencies),
        "status_counts": status_counts,
        "elapsed_s": elapsed,
        "request_throughput_per_s": len(latencies) / elapsed if elapsed else None,
        "latency_p50_s": percentile(latencies, 0.50),
        "latency_p95_s": percentile(latencies, 0.95),
        "latency_max_s": max(latencies) if latencies else None,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens_per_s": (prompt_tokens + completion_tokens) / elapsed if elapsed else None,
        "serving": {
            **serving_counts,
            "queue_wait_p50_ms": percentile(queue_wait_ms, 0.50),
            "queue_wait_p95_ms": percentile(queue_wait_ms, 0.95),
            "retry_after_counts": retry_after_counts,
        },
    }


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    connection = parser.add_mutually_exclusive_group(required=True)
    connection.add_argument("--host", help="Lambda VM IP; creates temporary app and Prometheus SSH tunnels")
    connection.add_argument("--base-url", help="Existing GraphServe URL, for example http://127.0.0.1:8080")
    parser.add_argument("--prometheus-url", help="Prometheus URL when using --base-url")
    parser.add_argument("--user", default="ubuntu")
    parser.add_argument("--ssh-key", type=Path, default=Path.home() / ".ssh/week-7-key.pem")
    parser.add_argument("--api-key-file", type=Path, default=root / "artifacts/lambda-deployment.env")
    parser.add_argument("--repository-id", required=True)
    parser.add_argument("--scope", default="class9")
    parser.add_argument("--concurrency", default="1,2,4", help="Comma-separated phase concurrencies")
    parser.add_argument("--requests-per-phase", type=int, default=12)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--metrics-step", type=int, default=5)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.requests_per_phase < 1 or args.warmup < 0:
        raise SystemExit("request counts must be positive")
    try:
        concurrencies = [int(value) for value in args.concurrency.split(",")]
    except ValueError as exc:
        raise SystemExit("--concurrency must be comma-separated integers") from exc
    if not concurrencies or any(value < 1 for value in concurrencies):
        raise SystemExit("concurrency values must be positive")

    api_key = read_key(args.api_key_file)
    tunnel = None
    if args.host:
        tunnel, base_url, prometheus_url = start_tunnel(args.host, args.user, args.ssh_key)
    else:
        base_url, prometheus_url = args.base_url, args.prometheus_url
        wait_http(base_url.rstrip("/") + "/healthz")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = Path(__file__).resolve().parents[1]
    output_dir = args.output_dir or root / "artifacts" / f"stress-{stamp}"
    output_dir.mkdir(parents=True, exist_ok=False)
    raw_path = output_dir / "requests.jsonl"
    summary: dict[str, Any] = {
        "started_at": utc_now(),
        "repository_id": args.repository_id,
        "scope": args.scope,
        "concurrencies": concurrencies,
        "requests_per_phase": args.requests_per_phase,
        "warmup_requests": args.warmup,
        "phases": [],
    }
    all_records: list[dict[str, Any]] = []
    sequence = 0
    try:
        for index in range(args.warmup):
            row = run_request(base_url, api_key, args.repository_id, args.scope,
                              QUESTIONS[index % len(QUESTIONS)], sequence, "warmup", args.timeout)
            sequence += 1
            all_records.append(row)
            with raw_path.open("a") as handle:
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")
            print(f"warmup {index + 1}/{args.warmup}: status={row.get('status_code')} latency={row['latency_s']:.2f}s")
            if not row.get("ok"):
                print("warning: warmup failed; recording it and continuing", file=sys.stderr)

        for concurrency in concurrencies:
            phase_name = f"c{concurrency}"
            phase_start = time.time()
            started = time.perf_counter()
            phase_records: list[dict[str, Any]] = []
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = []
                for request_index in range(args.requests_per_phase):
                    question = QUESTIONS[request_index % len(QUESTIONS)]
                    futures.append(pool.submit(run_request, base_url, api_key, args.repository_id,
                                               args.scope, question, sequence, phase_name, args.timeout))
                    sequence += 1
                for future in as_completed(futures):
                    phase_records.append(future.result())
            elapsed = time.perf_counter() - started
            phase_end = time.time()
            all_records.extend(phase_records)
            with raw_path.open("a") as handle:
                for row in sorted(phase_records, key=lambda item: item["sequence"]):
                    handle.write(json.dumps(row, separators=(",", ":")) + "\n")
            phase_summary = summarize(phase_records, elapsed, concurrency)
            if prometheus_url:
                phase_summary["metrics_file"] = f"metrics-{phase_name}.json"
                metrics = prometheus_range(prometheus_url, phase_start - 5, phase_end + 5, args.metrics_step)
                (output_dir / phase_summary["metrics_file"]).write_text(json.dumps(metrics, indent=2))
            summary["phases"].append(phase_summary)
            print(json.dumps(phase_summary, indent=2))

        summary["finished_at"] = utc_now()
        (output_dir / "summary.json").write_text(json.dumps(summary, indent=2))
        print(f"Raw requests: {raw_path}")
        print(f"Summary: {output_dir / 'summary.json'}")
        return 0
    finally:
        if tunnel is not None:
            tunnel.terminate()
            try:
                tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel.kill()


if __name__ == "__main__":
    sys.exit(main())
