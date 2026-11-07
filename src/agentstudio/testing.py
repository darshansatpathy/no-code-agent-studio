"""Run a spec's own tests. Each test gets fresh systems, so tests cannot affect each other."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentstudio.catalog import Systems
from agentstudio.models_io import ModelProvider
from agentstudio.runtime import InputError, RunResult, Runtime
from agentstudio.spec import AgentSpec, TestCase


@dataclass
class TestResult:
    name: str
    passed: bool
    failures: list[str]
    run: RunResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "failures": self.failures,
            "status": self.run.status if self.run else None,
            "path": self.run.path if self.run else None,
        }


@dataclass
class TestReport:
    agent: str
    version: str
    results: list[TestResult] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(r.passed for r in self.results)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def ok(self) -> bool:
        return self.total > 0 and self.passed == self.total

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "version": self.version,
            "passed": self.passed,
            "total": self.total,
            "ok": self.ok,
            "results": [r.to_dict() for r in self.results],
        }


def run_case(spec: AgentSpec, case: TestCase, provider: ModelProvider | None = None) -> TestResult:
    runtime = Runtime(systems=Systems(), provider=provider)
    try:
        run = runtime.start(spec, case.input, run_id=f"test-{abs(hash(case.name)) % 10**8}")
    except InputError as exc:
        return TestResult(case.name, False, [f"input rejected: {exc}"])
    decisions = list(case.approvals)
    while run.status == "awaiting_approval" and decisions:
        run = runtime.resume(spec, run.run_id, approved=decisions.pop(0), approver="spec-test")
    exp = case.expect
    failures: list[str] = []
    if run.status != exp.status:
        failures.append(f"status {run.status} != {exp.status}" + (f" ({run.error})" if run.error else ""))
    if exp.path is not None and run.path != exp.path:
        failures.append(f"path {run.path} != {exp.path}")
    ok_tools = [c["tool"] for c in run.calls if c.get("status") == "ok"]
    if exp.tools is not None and ok_tools != exp.tools:
        failures.append(f"tools {ok_tools} != {exp.tools}")
    for tool in exp.not_tools:
        if tool in ok_tools:
            failures.append(f"{tool} was called")
    for key, want in exp.state.items():
        if run.state.get(key) != want:
            failures.append(f"state.{key} = {run.state.get(key)!r}, expected {want!r}")
    reply = run.reply
    for text in exp.reply_contains:
        if text not in reply:
            failures.append(f"reply lacks {text!r}")
    for text in exp.reply_not_contains:
        if text in reply:
            failures.append(f"reply contains {text!r}")
    return TestResult(case.name, not failures, failures, run)


def run_tests(spec: AgentSpec, provider: ModelProvider | None = None) -> TestReport:
    report = TestReport(spec.name, spec.version)
    for case in spec.spec.tests:
        report.results.append(run_case(spec, case, provider))
    return report
