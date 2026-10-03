#!/usr/bin/env python3
"""Safely exercise GraphServe gateway policy through a temporary SSH tunnel."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

import httpx


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_http(url: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for {url}")


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="K3s control-node public IP or hostname")
    parser.add_argument("--user", default="ubuntu")
    parser.add_argument("--ssh-key", type=Path, default=Path.home() / ".ssh/week-7-key.pem")
    parser.add_argument("--distribution-requests", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--output", type=Path, default=root / "artifacts" / "orchestration-smoke.json")
    return parser.parse_args()


def completion_payload(content: str = "Reply with exactly OK.") -> dict:
    return {
        "model": "qwen-coder",
        "messages": [{"role": "user", "content": content}],
        "temperature": 0,
        "max_tokens": 8,
    }


def serving_headers(response: httpx.Response) -> dict[str, str | None]:
    return {
        name: response.headers.get(name)
        for name in (
            "x-graphserve-request-id",
            "x-graphserve-worker",
            "x-graphserve-placement",
            "x-graphserve-hop",
            "x-graphserve-queue-wait-ms",
            "x-graphserve-rejection-reason",
            "x-graphserve-overflow-decision",
            "retry-after",
        )
        if response.headers.get(name) is not None
    }


def main() -> int:
    args = parse_args()
    if not args.ssh_key.is_file():
        raise SystemExit(f"SSH key not found: {args.ssh_key}")
    if args.distribution_requests < 2:
        raise SystemExit("--distribution-requests must be at least 2")

    ssh = [
        "ssh", "-i", str(args.ssh_key), "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
    ]
    target = f"{args.user}@{args.host}"
    lookup = subprocess.run(
        ssh + [target, "sudo k3s kubectl -n repo-agent get svc gateway -o jsonpath='{.spec.clusterIP}'"],
        check=True, capture_output=True, text=True,
    )
    gateway_ip = lookup.stdout.strip()
    ipaddress.ip_address(gateway_ip)

    port = free_port()
    tunnel = subprocess.Popen(
        ssh + ["-o", "ExitOnForwardFailure=yes", "-N", "-L", f"{port}:{gateway_ip}:8080", target],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    report: dict = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "control_host": args.host,
        "checks": [],
        "affinity": {},
        "distribution": {},
    }
    failures: list[str] = []

    def check(name: str, condition: bool, observed) -> None:
        report["checks"].append({"name": name, "passed": bool(condition), "observed": observed})
        if not condition:
            failures.append(name)

    try:
        wait_http(base + "/healthz")
        with httpx.Client(base_url=base, timeout=args.timeout) as client:
            health = client.get("/healthz").json()
            workers = health.get("workers", {})
            check("two healthy stable workers", set(workers) == {"a", "b"} and all(
                worker.get("healthy") for worker in workers.values()
            ), workers)

            guard_cases = [
                ("invalid JSON", b"{", {}, 400, "invalid_json"),
                ("wrong model", json.dumps({"model": "wrong", "messages": [{"role": "user", "content": "x"}]}).encode(), {}, 422, "model"),
                ("streaming disabled", json.dumps({**completion_payload(), "stream": True}).encode(), {}, 422, "stream"),
                ("context budget", json.dumps(completion_payload("x" * 33_000)).encode(), {}, 422, "context_tokens"),
                ("invalid priority", json.dumps(completion_payload()).encode(), {"x-graphserve-priority": "urgent"}, 422, "priority"),
            ]
            for name, body, headers, expected_status, reason in guard_cases:
                response = client.post("/v1/chat/completions", content=body, headers={"content-type": "application/json", **headers})
                observed = {"status": response.status_code, "headers": serving_headers(response)}
                check(name, response.status_code == expected_status and response.headers.get(
                    "x-graphserve-rejection-reason") == reason, observed)

            prefix = "smoke-" + uuid.uuid4().hex
            headers = {
                "x-graphserve-tenant": "smoke-affinity",
                "x-graphserve-prefix-key": prefix,
                "x-graphserve-priority": "interactive",
                "x-graphserve-timeout-ms": "115000",
            }
            first = client.post("/v1/chat/completions", headers=headers, json=completion_payload())
            second = client.post("/v1/chat/completions", headers=headers, json=completion_payload())
            first_headers, second_headers = serving_headers(first), serving_headers(second)
            report["affinity"] = {"first": first_headers, "second": second_headers}
            check("affinity requests succeed", first.status_code == second.status_code == 200,
                  [first.status_code, second.status_code])
            check("first request is cold", first_headers.get("x-graphserve-hop") == "cold_start", first_headers)
            check("repeat stays on worker", first_headers.get("x-graphserve-worker") == second_headers.get(
                "x-graphserve-worker") and second_headers.get("x-graphserve-placement") == "prefix_affinity" and
                second_headers.get("x-graphserve-hop") == "same_worker", second_headers)

            placements = []
            for index in range(args.distribution_requests):
                key = f"distribution-{uuid.uuid4().hex}-{index}"
                response = client.post(
                    "/v1/chat/completions",
                    headers={
                        "x-graphserve-tenant": "smoke-distribution",
                        "x-graphserve-prefix-key": key,
                        "x-graphserve-priority": "interactive",
                        "x-graphserve-timeout-ms": "115000",
                    },
                    json=completion_payload(),
                )
                placements.append({"status": response.status_code, **serving_headers(response)})
            observed_workers = sorted({row.get("x-graphserve-worker") for row in placements if row.get("x-graphserve-worker")})
            report["distribution"] = {"workers": observed_workers, "requests": placements}
            check("unique prefixes reach both workers", observed_workers == ["a", "b"], observed_workers)

            batch = client.post(
                "/v1/chat/completions",
                headers={
                    "x-graphserve-tenant": "smoke-priority",
                    "x-graphserve-prefix-key": "batch-" + uuid.uuid4().hex,
                    "x-graphserve-priority": "batch",
                    "x-graphserve-timeout-ms": "115000",
                },
                json=completion_payload(),
            )
            check("batch priority accepted", batch.status_code == 200, {
                "status": batch.status_code, "headers": serving_headers(batch)
            })

            metrics = client.get("/metrics").text
            metric_names = [
                "orch_guard_decisions_total", "orch_admission_decisions_total",
                "orch_placement_total", "orch_replica_queue_depth",
                "orch_queue_wait_seconds", "orch_hop_total", "orch_overflow_total",
                "orch_worker_health",
            ]
            missing = [name for name in metric_names if name not in metrics]
            check("orchestration metrics exposed", not missing, missing)

        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["passed"] = not failures
        report["failures"] = failures
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        print(f"Saved: {args.output}")
        return 0 if not failures else 1
    finally:
        tunnel.terminate()
        try:
            tunnel.wait(timeout=5)
        except subprocess.TimeoutExpired:
            tunnel.kill()


if __name__ == "__main__":
    sys.exit(main())
