from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from agentstudio.spec import AgentSpec, load_spec

AGENTS = Path(__file__).resolve().parents[1] / "agents"


@pytest.fixture(scope="session")
def text() -> Callable[[str], str]:
    def _read(rel: str) -> str:
        return (AGENTS / rel).read_text(encoding="utf-8")

    return _read


@pytest.fixture
def spec(text: Callable[[str], str]) -> Callable[[str], AgentSpec]:
    def _load(rel: str) -> AgentSpec:
        return load_spec(text(rel))[0]

    return _load


MINIMAL = """apiVersion: studio/v1
kind: Agent
metadata: {name: echo, version: 0.1.0}
spec:
  input: {message: string}
  state: {reply: {type: string, default: ""}}
  entry: answer
  nodes:
    - {id: answer, type: respond, template: "You said: {{ input.message }}"}
  edges:
    - {from: answer, to: END}
  tests:
    - name: echoes
      input: {message: hi}
      expect: {reply_contains: ["You said: hi"]}
"""


@pytest.fixture
def minimal() -> str:
    return MINIMAL


@pytest.fixture
def postgres_url() -> Iterator[str]:
    url = os.environ.get("STUDIO_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set STUDIO_TEST_DATABASE_URL to run PostgreSQL tests")
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        for table in (
            "agent_versions",
            "agent_environments",
            "agent_audit",
            "agent_runs",
            "checkpoints",
            "checkpoint_blobs",
            "checkpoint_writes",
            "checkpoint_migrations",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")  # type: ignore[arg-type]
    yield url
