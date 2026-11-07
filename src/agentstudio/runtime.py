"""Run compiled agents: start, pause for approval, resume, inspect.

A run is pinned to one exact spec (by content hash), so an agent changed or promoted while
a run waits for approval does not change what that run does when it resumes.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agentstudio.catalog import Systems
from agentstudio.compiler import CompiledAgent, Services, compile_agent
from agentstudio.models_io import ModelProvider
from agentstudio.spec import AgentSpec

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "object": (dict,),
    "list": (list,),
}


class InputError(ValueError):
    pass


class RunNotPaused(ValueError):
    pass


@dataclass
class RunResult:
    run_id: str
    agent: str
    version: str
    content_hash: str
    status: str  # completed | failed | awaiting_approval
    path: list[str]
    state: dict[str, Any]
    calls: list[dict[str, Any]]
    error: str = ""
    pending: dict[str, Any] | None = None

    @property
    def reply(self) -> str:
        return str(self.state.get("reply") or "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent": self.agent,
            "version": self.version,
            "content_hash": self.content_hash,
            "status": self.status,
            "path": self.path,
            "state": self.state,
            "calls": self.calls,
            "error": self.error,
            "pending": self.pending,
        }


def validate_input(spec: AgentSpec, data: dict[str, Any]) -> dict[str, Any]:
    declared = spec.spec.input
    unknown = set(data) - set(declared)
    if unknown:
        raise InputError(f"unknown input field(s): {', '.join(sorted(unknown))}")
    for name, kind in declared.items():
        if name not in data:
            raise InputError(f"missing input field {name!r}")
        value = data[name]
        ok = isinstance(value, _TYPES[kind]) and not (kind in {"number", "integer"} and isinstance(value, bool))
        if not ok:
            raise InputError(f"input {name!r} must be a {kind}")
        if isinstance(value, str) and len(value) > 10_000:
            raise InputError(f"input {name!r} is too long")
    return dict(data)


@dataclass
class Runtime:
    systems: Systems = field(default_factory=Systems)
    checkpointer: BaseCheckpointSaver[Any] = field(default_factory=InMemorySaver)
    provider: ModelProvider | None = None

    def __post_init__(self) -> None:
        self._compiled: dict[str, CompiledAgent] = {}
        self._lock = threading.Lock()

    def compiled(self, spec: AgentSpec) -> CompiledAgent:
        key = spec.content_hash()
        with self._lock:
            if key not in self._compiled:
                self._compiled[key] = compile_agent(spec, Services(self.systems, self.provider), self.checkpointer)
            return self._compiled[key]

    @staticmethod
    def _config(run_id: str) -> Any:
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 250}

    def start(self, spec: AgentSpec, data: dict[str, Any], run_id: str | None = None) -> RunResult:
        agent = self.compiled(spec)
        rid = run_id or f"run-{uuid.uuid4().hex[:16]}"
        initial = {
            "run_id": rid,
            "input": validate_input(spec, data),
            "values": {},
            "path": [],
            "calls": [],
            "status": "running",
            "error": "",
        }
        agent.graph.invoke(initial, self._config(rid))
        return self.get(spec, rid)

    def resume(self, spec: AgentSpec, run_id: str, *, approved: bool, approver: str) -> RunResult:
        current = self.get(spec, run_id)
        if current.pending is None:
            raise RunNotPaused(f"run {run_id} is not waiting for approval")
        if not approver:
            raise RunNotPaused("an approver is required")
        self.compiled(spec).graph.invoke(
            Command(resume={"approved": approved, "approver": approver}), self._config(run_id)
        )
        return self.get(spec, run_id)

    def get(self, spec: AgentSpec, run_id: str) -> RunResult:
        snapshot = self.compiled(spec).graph.get_state(self._config(run_id))
        values: dict[str, Any] = dict(snapshot.values or {})
        if not values:
            raise KeyError(run_id)
        pending = None
        for task in snapshot.tasks:
            for intr in task.interrupts:
                pending = dict(intr.value)
        defaults = {k: f.default for k, f in spec.spec.state.items()}
        status = "awaiting_approval" if pending else str(values.get("status", "running"))
        return RunResult(
            run_id,
            spec.name,
            spec.version,
            spec.content_hash(),
            status,
            list(values.get("path", [])),
            {**defaults, **values.get("values", {})},
            list(values.get("calls", [])),
            str(values.get("error", "")),
            pending,
        )
