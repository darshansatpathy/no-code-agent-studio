"""Compile a validated spec into a LangGraph state graph.

Each spec node becomes a graph node that does one thing (classify, extract, call a tool,
set values, respond, generate), records itself in the run's path, and picks the next node
by evaluating its outgoing edges in order. Tool nodes enforce the spec's argument limits,
pause for approval when the spec says so, and respect the run's tool budget.
"""

from __future__ import annotations

import json
import operator
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agentstudio.analyzer import analyze
from agentstudio.catalog import CATALOG, Systems, ToolError
from agentstudio.expr import ExprError, compile_expr, compile_template
from agentstudio.models_io import ModelProvider
from agentstudio.spec import (
    AgentSpec,
    ArgLimit,
    ClassifyNode,
    ExtractNode,
    GenerateNode,
    RespondNode,
    SetNode,
    SpecError,
    ToolNode,
)


def _merge(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any]:
    return {**(a or {}), **(b or {})}


class RunState(TypedDict, total=False):
    run_id: str
    input: dict[str, Any]
    values: Annotated[dict[str, Any], _merge]
    path: Annotated[list[str], operator.add]
    calls: Annotated[list[dict[str, Any]], operator.add]
    next: str
    status: str
    error: str


class StepError(Exception):
    """A node could not complete; the run fails unless the node routes errors elsewhere."""


@dataclass
class CompiledAgent:
    spec: AgentSpec
    graph: Any
    mermaid: str

    @property
    def content_hash(self) -> str:
        return self.spec.content_hash()


@dataclass
class Services:
    systems: Systems
    provider: ModelProvider | None = None
    approvals: Callable[[dict[str, Any]], dict[str, Any]] = field(default=interrupt)


def _check_limit(name: str, value: Any, limit: ArgLimit) -> str | None:
    if limit.one_of is not None and value not in limit.one_of:
        return f"{name} must be one of {limit.one_of}"
    if (limit.min is not None or limit.max is not None) and not isinstance(value, int | float):
        return f"{name} must be a number"
    if limit.min is not None and value < limit.min:
        return f"{name} {value} is below the limit {limit.min:g}"
    if limit.max is not None and value > limit.max:
        return f"{name} {value} is above the limit {limit.max:g}"
    if limit.max_length is not None and len(str(value)) > limit.max_length:
        return f"{name} is longer than {limit.max_length} characters"
    return None


def compile_agent(spec: AgentSpec, services: Services, checkpointer: BaseCheckpointSaver[Any]) -> CompiledAgent:
    analysis = analyze(spec)
    if not analysis.ok:
        raise SpecError(analysis.errors)
    s = spec.spec
    defaults = {k: f.default for k, f in s.state.items()}
    outgoing: dict[str, list[tuple[Any, str]]] = {}
    for e in s.edges:
        target = END if e.to == "END" else e.to  # the spec says END; LangGraph's marker is "__end__"
        outgoing.setdefault(e.from_, []).append((compile_expr(e.when) if e.when else None, target))

    def context(state: RunState, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        return {**defaults, **state.get("values", {}), "input": state.get("input", {}), **(extra or {})}

    def route(node_id: str, ctx: dict[str, Any]) -> str:
        for cond, target in outgoing.get(node_id, []):
            if cond is None or cond.evaluate(ctx):
                return target
        raise StepError(f"no edge from {node_id!r} matched")

    def make(node: Any) -> Any:
        handler = _handlers(node, spec, services)

        def run(state: RunState) -> dict[str, Any]:
            if len(state.get("path", [])) >= s.limits.max_steps:
                return {
                    "path": [node.id],
                    "status": "failed",
                    "next": END,
                    "error": f"step limit of {s.limits.max_steps} reached",
                }
            try:
                values, calls, jump = handler(state, context(state))
                ctx = {**context(state), **values}
                nxt = jump or route(node.id, ctx)
            except (StepError, ExprError) as exc:
                return {"path": [node.id], "status": "failed", "next": END, "error": f"{node.id}: {exc}"}
            update: dict[str, Any] = {"values": values, "path": [node.id], "calls": calls, "next": nxt}
            if nxt == END:
                update["status"] = "completed"
            return update

        return run

    g: StateGraph[RunState] = StateGraph(RunState)
    ids = [n.id for n in s.nodes]
    for n in s.nodes:
        g.add_node(n.id, make(n))
    g.add_edge(START, s.entry)
    for n in s.nodes:
        g.add_conditional_edges(n.id, lambda st: st["next"], [*ids, END])
    return CompiledAgent(spec, g.compile(checkpointer=checkpointer), to_mermaid(spec))


Handler = Callable[[RunState, dict[str, Any]], tuple[dict[str, Any], list[dict[str, Any]], str | None]]


def _handlers(node: Any, spec: AgentSpec, services: Services) -> Handler:
    if isinstance(node, ClassifyNode):
        source = compile_expr(node.input)

        def classify(state: RunState, ctx: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], None]:
            text = str(source.evaluate(ctx) or "")
            if node.method == "llm" and services.provider is not None:
                label = services.provider.classify(text, list(node.labels))
                label = label if label in node.labels else node.default
            else:
                low = text.lower()
                label = next(
                    (
                        lab
                        for lab, words in node.labels.items()
                        if any(re.search(rf"\b{re.escape(w.lower())}\b", low) for w in words)
                    ),
                    node.default,
                )
            return {node.output: label}, [], None

        return classify

    if isinstance(node, ExtractNode):
        source = compile_expr(node.input)
        patterns = {k: re.compile(v) for k, v in node.fields.items()}

        def extract(state: RunState, ctx: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], None]:
            text = str(source.evaluate(ctx) or "")[:10_000]
            out = {}
            for k, rx in patterns.items():
                m = rx.search(text)
                out[k] = m.group(1) if m else None
            return out, [], None

        return extract

    if isinstance(node, SetNode):
        exprs = {k: compile_expr(v) for k, v in node.values.items()}

        def assign(state: RunState, ctx: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], None]:
            out: dict[str, Any] = {}
            for k, e in exprs.items():
                out[k] = e.evaluate({**ctx, **out})
            return out, [], None

        return assign

    if isinstance(node, RespondNode | GenerateNode):
        template = compile_template(node.template if isinstance(node, RespondNode) else node.prompt)
        use_llm = isinstance(node, GenerateNode) and node.method == "llm"

        def respond(state: RunState, ctx: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], None]:
            text = template.render(ctx)
            if use_llm and services.provider is not None:
                text = services.provider.generate(text)
            return {node.output: text}, [], None

        return respond

    if isinstance(node, ToolNode):
        return _tool_handler(node, spec, services)
    raise TypeError(f"unsupported node {node!r}")  # pragma: no cover


def _tool_handler(node: ToolNode, spec: AgentSpec, services: Services) -> Handler:
    tool = CATALOG[node.tool]
    ref = spec.tool_ref(node.tool)
    assert ref is not None
    arg_exprs = {k: compile_expr(v) for k, v in node.args.items()}
    approval = compile_expr(ref.approval) if ref.approval else None
    budget = spec.spec.limits.max_tool_calls

    def fail(message: str, call: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
        if node.on_error:
            target = END if node.on_error == "END" else node.on_error
            return {"error": message} if "error" in spec.spec.state else {}, [call], target
        raise StepError(message)

    def run_tool(state: RunState, ctx: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
        args = {k: e.evaluate(ctx) for k, e in arg_exprs.items()}
        call: dict[str, Any] = {"node": node.id, "tool": node.tool, "kind": tool.kind, "args": args}
        for name, limit in ref.limits.items():
            if name in args:
                problem = _check_limit(name, args[name], limit)
                if problem:
                    return fail(f"limit: {problem}", {**call, "status": "blocked", "reason": problem})
        executed = sum(1 for c in state.get("calls", []) if c.get("status") in {"ok", "error"})
        if executed >= budget:
            raise StepError(f"tool-call budget of {budget} used up")
        if approval is not None and approval.evaluate({**ctx, "args": args}):
            request = {
                "run_id": state.get("run_id"),
                "node": node.id,
                "tool": node.tool,
                "args": args,
                "agent": spec.name,
                "version": spec.version,
                "summary": f"{node.tool} {json.dumps(args, sort_keys=True)}",
            }
            decision = services.approvals(request)
            call["approval"] = {"approved": bool(decision.get("approved")), "by": str(decision.get("approver", ""))}
            if not decision.get("approved"):
                return fail("approval denied", {**call, "status": "denied"})
        try:
            result = tool.call(services.systems, args)
        except ToolError as exc:
            return fail(str(exc), {**call, "status": "error", "error": str(exc)})
        out = {node.output: result} if node.output else {}
        return out, [{**call, "status": "ok"}], None

    return run_tool


_SHAPES = {
    "classify": ("{{", "}}"),
    "extract": ("[/", "/]"),
    "tool": ("[[", "]]"),
    "set": ("[", "]"),
    "respond": ("([", "])"),
    "generate": ("([", "])"),
}


def to_mermaid(spec: AgentSpec) -> str:
    lines = ["flowchart TD", f"  START((start)) --> {spec.spec.entry}"]
    for n in spec.spec.nodes:
        a, b = _SHAPES[n.type]
        label = f"{n.id}: {n.tool}" if isinstance(n, ToolNode) else f"{n.id} ({n.type})"
        lines.append(f'  {n.id}{a}"{label}"{b}')
    for e in spec.spec.edges:
        target = "END((end))" if e.to == "END" else e.to
        if e.when:
            text = e.when.replace('"', "'").replace("|", "/")[:60]
            lines.append(f'  {e.from_} -- "{text}" --> {target}')
        else:
            lines.append(f"  {e.from_} --> {target}")
    for n in spec.spec.nodes:
        if isinstance(n, ToolNode) and n.on_error:
            target = "END((end))" if n.on_error == "END" else n.on_error
            lines.append(f'  {n.id} -. "on error" .-> {target}')
    return "\n".join(lines)
