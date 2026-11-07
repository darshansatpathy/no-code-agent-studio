import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from agentstudio.catalog import CATALOG, Systems, ToolError
from agentstudio.compiler import Services, compile_agent, to_mermaid
from agentstudio.models_io import ModelError, OpenAICompatibleModel, ScriptedModel
from agentstudio.runtime import InputError, RunNotPaused, Runtime
from agentstudio.spec import AgentSpec, SpecError, load_spec
from agentstudio.testing import run_tests

Spec = Callable[[str], AgentSpec]


@pytest.mark.parametrize("rel", ["it-helpdesk/v1.0.0.yaml", "it-helpdesk/v1.1.0.yaml", "refund-desk/v1.0.0.yaml"])
def test_example_specs_pass_their_tests(spec: Spec, rel: str) -> None:
    report = run_tests(spec(rel))
    assert report.ok, [r.to_dict() for r in report.results if not r.passed]


def test_run_records_path_calls_and_reply(spec: Spec) -> None:
    rt = Runtime()
    r = rt.start(spec("refund-desk/v1.0.0.yaml"), {"message": "Order O-5001 arrived damaged"})
    assert r.status == "completed"
    assert r.path == ["find_order", "reason", "lookup", "amount", "refund", "done_refund"]
    assert [c["status"] for c in r.calls] == ["ok", "ok"]
    assert r.state["order_id"] == "O-5001" and r.state["amount"] == 49.9
    assert "49.90 EUR" in r.reply
    assert rt.systems.refunds[0]["amount"] == 49.9
    assert r.to_dict()["content_hash"] == spec("refund-desk/v1.0.0.yaml").content_hash()


def test_approval_pause_and_resume(spec: Spec) -> None:
    s = spec("refund-desk/v1.0.0.yaml")
    rt = Runtime()
    r = rt.start(s, {"message": "O-5002 arrived broken"}, run_id="r1")
    assert r.status == "awaiting_approval"
    assert r.pending and r.pending["tool"] == "payments.issue_refund" and r.pending["args"]["amount"] == 189.0
    assert rt.systems.refunds == []
    with pytest.raises(RunNotPaused):
        rt.resume(s, "r1", approved=True, approver="")
    done = rt.resume(s, "r1", approved=True, approver="u-boss")
    assert done.status == "completed"
    assert done.calls[-1]["approval"] == {"approved": True, "by": "u-boss"}
    with pytest.raises(RunNotPaused):
        rt.resume(s, "r1", approved=True, approver="u-boss")
    with pytest.raises(KeyError):
        rt.get(s, "nope")


def test_limits_block_before_the_tool_runs(spec: Spec) -> None:
    rt = Runtime()
    r = rt.start(spec("refund-desk/v1.0.0.yaml"), {"message": "My order O-5003 never arrived"})
    assert r.path[-1] == "review"
    assert r.calls[-1]["status"] == "blocked"
    assert rt.systems.refunds == []


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        ({}, "missing input"),
        ({"message": 5}, "must be a string"),
        ({"message": "x", "y": 1}, "unknown input"),
        ({"message": "x" * 10_001}, "too long"),
    ],
)
def test_input_validation(spec: Spec, data: dict[str, Any], fragment: str) -> None:
    with pytest.raises(InputError, match=fragment):
        Runtime().start(spec("refund-desk/v1.0.0.yaml"), data)


def _tiny(
    nodes: str,
    edges: str,
    state: str = "{reply: {type: string, default: ''}, n: {type: number, default: 0}}",
    tools: str = "[]",
    limits: str = "{}",
) -> AgentSpec:
    return load_spec(f"""apiVersion: studio/v1
kind: Agent
metadata: {{name: tiny, version: 0.1.0}}
spec:
  input: {{message: string}}
  state: {state}
  tools: {tools}
  limits: {limits}
  entry: first
  nodes:
{nodes}
  edges:
{edges}
""")[0]


def test_no_matching_edge_fails_the_run() -> None:
    s = _tiny("    - {id: first, type: set, values: {n: '1'}}", "    - {from: first, to: END, when: 'n > 5'}")
    r = Runtime().start(s, {"message": "x"})
    assert r.status == "failed" and "no edge from 'first' matched" in r.error


def test_expression_errors_fail_cleanly() -> None:
    s = _tiny("    - {id: first, type: set, values: {n: 'input.message - 1'}}", "    - {from: first, to: END}")
    r = Runtime().start(s, {"message": "x"})
    assert r.status == "failed" and r.error.startswith("first:")


def test_step_limit_stops_loops() -> None:
    s = _tiny(
        "    - {id: first, type: set, values: {n: 'n + 1'}}",
        "    - {from: first, to: first, when: 'n < 1000'}\n    - {from: first, to: END}",
        limits="{max_steps: 5}",
    )
    r = Runtime().start(s, {"message": "x"})
    assert r.status == "failed" and "step limit" in r.error
    assert len(r.path) == 6


def test_tool_budget() -> None:
    s = _tiny(
        "    - {id: first, type: tool, tool: kb.search, args: {query: input.message}}",
        "    - {from: first, to: first, when: 'true'}\n    - {from: first, to: END}",
        tools="[{use: kb.search}]",
        limits="{max_tool_calls: 2, max_steps: 20}",
    )
    r = Runtime().start(s, {"message": "vpn"})
    assert r.status == "failed" and "budget of 2" in r.error
    assert sum(c["status"] == "ok" for c in r.calls) == 2


def test_tool_error_without_on_error_fails() -> None:
    s = _tiny(
        "    - {id: first, type: tool, tool: orders.lookup_order, args: {order_id: \"'O-0000'\"}}",
        "    - {from: first, to: END}",
        tools="[{use: orders.lookup_order}]",
    )
    r = Runtime().start(s, {"message": "x"})
    assert r.status == "failed" and "not found" in r.error


def test_generate_and_classify_with_a_model() -> None:
    s = _tiny(
        "    - {id: first, type: classify, input: input.message, output: reply, method: llm, "
        "labels: {happy: [x], sad: [y]}}\n"
        "    - {id: second, type: generate, method: llm, prompt: 'Draft: {{ reply }}', output: reply}",
        "    - {from: first, to: second}\n    - {from: second, to: END}",
    )
    model = ScriptedModel(labels=["sad"], texts=["polished"])
    r = Runtime(provider=model).start(s, {"message": "whatever"})
    assert r.state["reply"] == "polished"
    assert model.prompts == ["whatever", "Draft: sad"]
    unknown = ScriptedModel(labels=["furious"])
    s2 = _tiny(
        "    - {id: first, type: classify, input: input.message, output: reply, method: llm, default: meh, "
        "labels: {happy: [x]}}",
        "    - {from: first, to: END}",
    )
    assert Runtime(provider=unknown).start(s2, {"message": "?"}).state["reply"] == "meh"


def test_keyword_classifier_matches_words_not_substrings() -> None:
    s = _tiny(
        "    - {id: first, type: classify, input: input.message, output: reply, labels: {reset: [reset]}}",
        "    - {from: first, to: END}",
    )
    rt = Runtime()
    assert rt.start(s, {"message": "please RESET it"}).state["reply"] == "reset"
    assert rt.start(s, {"message": "preset value"}).state["reply"] == "other"


def test_compile_refuses_invalid_specs(minimal: str) -> None:
    bad = load_spec(minimal.replace("to: END", "to: nowhere"))[0]
    from langgraph.checkpoint.memory import InMemorySaver

    with pytest.raises(SpecError):
        compile_agent(bad, Services(Systems()), InMemorySaver())


def test_runs_survive_a_restart(spec: Spec, tmp_path: Path) -> None:
    import sqlite3

    s = spec("refund-desk/v1.0.0.yaml")
    db = str(tmp_path / "runs.db")
    systems = Systems()
    conn = sqlite3.connect(db, check_same_thread=False)
    Runtime(systems, SqliteSaver(conn)).start(s, {"message": "O-5002 arrived broken"}, run_id="r1")
    conn.close()
    conn = sqlite3.connect(db, check_same_thread=False)
    try:
        rt = Runtime(systems, SqliteSaver(conn))
        assert rt.get(s, "r1").status == "awaiting_approval"
        assert rt.resume(s, "r1", approved=True, approver="u").status == "completed"
    finally:
        conn.close()


def test_mermaid(spec: Spec) -> None:
    text = to_mermaid(spec("it-helpdesk/v1.0.0.yaml"))
    assert text.startswith("flowchart TD")
    assert 'lookup[["lookup: directory.lookup_user"]]' in text
    assert '-. "on error" .-> not_found' in text
    assert "END((end))" in text


def test_catalog_validates_arguments() -> None:
    tool = CATALOG["payments.issue_refund"]
    with pytest.raises(ToolError, match="invalid argument"):
        tool.call(Systems(), {"order_id": "O-5001", "amount": "10", "reason": "damaged"})
    systems = Systems()
    tool.call(systems, {"order_id": "O-5001", "amount": 40.0, "reason": "damaged"})
    with pytest.raises(ToolError, match="exceed"):
        tool.call(systems, {"order_id": "O-5001", "amount": 10.0, "reason": "damaged"})
    with pytest.raises(ToolError):
        CATALOG["directory.unlock_account"].call(Systems(), {"user_id": "U-102"})
    with pytest.raises(ToolError):
        CATALOG["directory.reset_password"].call(Systems(), {"user_id": "U-102"})
    with pytest.raises(ToolError):
        CATALOG["payments.issue_refund"].call(Systems(), {"order_id": "O-0000", "amount": 1.0, "reason": "other"})
    assert CATALOG["kb.search"].describe()["kind"] == "read"
    assert CATALOG["tickets.create_ticket"].required_args == {"user_id", "summary"}


def test_openai_compatible_model() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": " Sad. "}}]})

    m = OpenAICompatibleModel("https://llm.test/v1", "m", api_key="k", transport=httpx.MockTransport(handler))
    assert m.classify("text", ["happy", "sad"]) == "sad"
    assert m.generate("draft") == "Sad."
    assert seen[0]["temperature"] == 0 and "data, not instructions" in seen[0]["messages"][0]["content"]
    for resp in (httpx.Response(500), httpx.Response(200, json={})):
        broken = OpenAICompatibleModel("https://llm.test/v1", "m", transport=httpx.MockTransport(lambda _r, r=resp: r))
        with pytest.raises(ModelError):
            broken.generate("x")


def test_model_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_LLM_BASE_URL", raising=False)
    assert OpenAICompatibleModel.from_env() is None
    monkeypatch.setenv("STUDIO_LLM_BASE_URL", "https://llm.test/v1")
    model = OpenAICompatibleModel.from_env()
    assert model is not None and model.model == "gpt-4o-mini"


def test_failing_expectations_are_reported(text: Callable[[str], str]) -> None:
    from agentstudio.spec import TestCase
    from agentstudio.testing import run_case

    s = load_spec(text("refund-desk/v1.0.0.yaml"))[0]
    case = TestCase.model_validate(
        {
            "name": "everything wrong",
            "input": {"message": "Order O-5001 arrived damaged"},
            "expect": {
                "status": "failed",
                "path": ["find_order"],
                "tools": [],
                "not_tools": ["payments.issue_refund"],
                "state": {"amount": 1},
                "reply_contains": ["nope"],
                "reply_not_contains": ["EUR"],
            },
        }
    )
    result = run_case(s, case)
    joined = " | ".join(result.failures)
    for fragment in (
        "status completed != failed",
        "path",
        "tools",
        "was called",
        "state.amount",
        "lacks 'nope'",
        "contains 'EUR'",
    ):
        assert fragment in joined, fragment
    bad_input = TestCase.model_validate({"name": "bad", "input": {"msg": "x"}})
    assert run_case(s, bad_input).failures[0].startswith("input rejected")
    assert result.to_dict()["status"] == "completed"
