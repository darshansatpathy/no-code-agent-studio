.PHONY: install check test specs demo serve docker kind-deploy audit

install:
	python -m venv .venv && .venv/bin/pip install -e ".[dev]"

check:
	ruff check . && ruff format --check . && mypy && pytest --cov --cov-fail-under=90 && studio check agents

test:
	pytest

specs:
	studio check agents

demo:
	studio demo

serve:
	STUDIO_BOOTSTRAP_DIR=agents STUDIO_BOOTSTRAP_PROMOTE=dev,staging,prod studio serve --port 8080

docker:
	docker build -t agent-studio:dev .

kind-deploy: docker
	kind load docker-image agent-studio:dev
	kubectl apply -k deploy/k8s/overlays/dev
	kubectl -n agent-studio rollout status deploy/agent-studio

audit:
	pip-audit --skip-editable
