.PHONY: test local render orchestration-smoke

test:
	.venv/bin/pytest -q

local:
	docker compose up --build

render:
	kubectl kustomize deploy/k8s


orchestration-smoke:
	@test -n "$(CONTROL_HOST)" || (echo "Set CONTROL_HOST to the Lambda control-node IP" >&2; exit 2)
	.venv/bin/python scripts/orchestration_smoke.py --host "$(CONTROL_HOST)"
