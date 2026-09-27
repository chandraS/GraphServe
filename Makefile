.PHONY: test local render

test:
	.venv/bin/pytest -q

local:
	docker compose up --build

render:
	kubectl kustomize deploy/k8s
