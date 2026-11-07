"""Static analysis of a spec, before it can be published or run.

Catches the mistakes that otherwise surface at 2 a.m.: an edge to a node that doesn't
exist, a condition that reads a field nobody writes, a tool call missing an argument, a
node that can never reach the end, a write tool with no approval or limits, an expression
that tries to do more than compare values.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from agentstudio.catalog import CATALOG
from agentstudio.expr import ExprError, compile_expr, compile_template
from agentstudio.spec import (
    AgentSpec,
    ClassifyNode,
    ExtractNode,
    GenerateNode,
    Issue,
    LineMap,
    RespondNode,
    SetNode,
    ToolNode,
)

END = "END"


@dataclass
class Analysis:
    issues: list[Issue]

    @property
    def ok(self) -> bool:
        return not any(i.level == "error" for i in self.issues)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]


class _Checker:
    def __init__(self, spec: AgentSpec, lines: LineMap | None) -> None:
        self.spec = spec
        self.lines = lines or LineMap()
        self.issues: list[Issue] = []
        self.node_index = {n.id: i for i, n in enumerate(spec.spec.nodes)}
        self.fields = set(spec.spec.state)

    def add(self, level: str, code: str, message: str, path: tuple[Any, ...] = ()) -> None:
        from agentstudio.spec import path_str

        self.issues.append(Issue(level, code, message, path_str(path), self.lines.line(path)))  # type: ignore[arg-type]

    # -------------------------------------------------------------- expressions

    def expr(self, source: str, path: tuple[Any, ...], extra: set[str] | None = None) -> None:
        try:
            e = compile_expr(source)
        except ExprError as exc:
            self.add("error", "expression", str(exc), path)
            return
        self._names(e.names, e.tree, path, extra or set())

    def template(self, source: str, path: tuple[Any, ...]) -> None:
        try:
            t = compile_template(source)
        except ExprError as exc:
            self.add("error", "expression", str(exc), path)
            return
        for part in t.parts:
            if not isinstance(part, str):
                self._names(part.names, part.tree, path, set())

    def _names(self, names: frozenset[str], tree: ast.AST, path: tuple[Any, ...], extra: set[str]) -> None:
        known = self.fields | {"input"} | extra
        for name in sorted(names - known):
            self.add("error", "unknown_name", f"{name!r} is not a state field (declare it under spec.state)", path)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "input"
                and node.attr not in self.spec.spec.input
            ):
                self.add("error", "unknown_input", f"input.{node.attr} is not declared under spec.input", path)

    def writes(self, field_name: str, path: tuple[Any, ...]) -> None:
        if field_name not in self.fields:
            self.add(
                "error", "undeclared_output", f"writes {field_name!r}, which is not declared under spec.state", path
            )

    # -------------------------------------------------------------- checks

    def run(self) -> list[Issue]:
        s = self.spec.spec
        seen: set[str] = set()
        for i, n in enumerate(s.nodes):
            if n.id in seen:
                self.add("error", "duplicate_node", f"node id {n.id!r} is used twice", ("spec", "nodes", i, "id"))
            seen.add(n.id)
        if s.entry not in self.node_index:
            self.add("error", "unknown_entry", f"entry {s.entry!r} is not a node", ("spec", "entry"))
        for name in s.state:
            if name in {"input", "args"}:
                self.add("error", "reserved_name", f"{name!r} is reserved", ("spec", "state", name))
        declared_tools = {t.use for t in s.tools}
        for i, ref in enumerate(s.tools):
            self.tool_ref(i, ref)
        for i, n in enumerate(s.nodes):
            self.node(i, n, declared_tools)
        self.edges()
        self.tests()
        return self.issues

    def tool_ref(self, i: int, ref: Any) -> None:
        path = ("spec", "tools", i)
        tool = CATALOG.get(ref.use)
        if tool is None:
            self.add("error", "unknown_tool", f"{ref.use!r} is not in the tool catalog", (*path, "use"))
            return
        if ref.approval:
            self.expr(ref.approval, (*path, "approval"), extra={"args"})
        for arg in ref.limits:
            if arg not in tool.arg_names:
                self.add("error", "unknown_limit", f"{ref.use} has no argument {arg!r}", (*path, "limits", arg))
        if tool.kind == "write" and not ref.approval and not ref.limits:
            self.add(
                "warning",
                "unguarded_write",
                f"{ref.use} changes data and has neither an approval rule nor limits",
                path,
            )

    def node(self, i: int, n: Any, declared_tools: set[str]) -> None:
        path = ("spec", "nodes", i)
        if isinstance(n, ClassifyNode):
            self.expr(n.input, (*path, "input"))
            self.writes(n.output, (*path, "output"))
            for label, words in n.labels.items():
                if not words and n.method == "keywords":
                    self.add("error", "empty_label", f"label {label!r} has no keywords", (*path, "labels", label))
        elif isinstance(n, ExtractNode):
            self.expr(n.input, (*path, "input"))
            for fname, pattern in n.fields.items():
                self.writes(fname, (*path, "fields", fname))
                try:
                    rx = re.compile(pattern)
                    if rx.groups != 1 or len(pattern) > 200:
                        self.add(
                            "error",
                            "regex",
                            "pattern needs exactly one group and at most 200 characters",
                            (*path, "fields", fname),
                        )
                except re.error as exc:
                    self.add("error", "regex", f"invalid pattern: {exc}", (*path, "fields", fname))
        elif isinstance(n, ToolNode):
            tool = CATALOG.get(n.tool)
            if n.tool not in declared_tools:
                self.add(
                    "error", "undeclared_tool", f"{n.tool} is used but not listed under spec.tools", (*path, "tool")
                )
            if tool is not None:
                missing = tool.required_args - set(n.args)
                unknown = set(n.args) - tool.arg_names
                for a in sorted(missing):
                    self.add("error", "missing_arg", f"{n.tool} needs argument {a!r}", (*path, "args"))
                for a in sorted(unknown):
                    self.add("error", "unknown_arg", f"{n.tool} has no argument {a!r}", (*path, "args", a))
            for a, source in n.args.items():
                self.expr(source, (*path, "args", a))
            if n.output:
                self.writes(n.output, (*path, "output"))
            if n.on_error and n.on_error not in self.node_index and n.on_error != END:
                self.add("error", "unknown_node", f"on_error {n.on_error!r} is not a node", (*path, "on_error"))
        elif isinstance(n, SetNode):
            for fname, source in n.values.items():
                self.writes(fname, (*path, "values", fname))
                self.expr(source, (*path, "values", fname))
        elif isinstance(n, RespondNode):
            self.template(n.template, (*path, "template"))
            self.writes(n.output, (*path, "output"))
        elif isinstance(n, GenerateNode):
            self.template(n.prompt, (*path, "prompt"))
            self.writes(n.output, (*path, "output"))

    def edges(self) -> None:
        s = self.spec.spec
        outgoing: dict[str, list[tuple[int, Any]]] = defaultdict(list)
        for i, e in enumerate(s.edges):
            path = ("spec", "edges", i)
            if e.from_ not in self.node_index:
                self.add("error", "unknown_node", f"edge from unknown node {e.from_!r}", (*path, "from"))
            if e.to not in self.node_index and e.to != END:
                self.add("error", "unknown_node", f"edge to unknown node {e.to!r}", (*path, "to"))
            if e.when:
                self.expr(e.when, (*path, "when"))
            outgoing[e.from_].append((i, e))
        for n in s.nodes:
            edges = outgoing.get(n.id, [])
            idx = self.node_index[n.id]
            if not edges:
                self.add(
                    "error",
                    "dead_end",
                    f"node {n.id!r} has no outgoing edge (route it to END to finish)",
                    ("spec", "nodes", idx, "id"),
                )
                continue
            unconditional = [k for k, (_, e) in enumerate(edges) if not e.when]
            if unconditional and unconditional[0] < len(edges) - 1:
                i, _ = edges[unconditional[0] + 1]
                self.add(
                    "warning",
                    "unreachable_edge",
                    f"edges from {n.id!r} after an unconditional edge are never taken",
                    ("spec", "edges", i),
                )
            if not unconditional:
                i, _ = edges[-1]
                self.add(
                    "warning",
                    "no_default_route",
                    f"if no condition from {n.id!r} matches, the run fails; add an edge without 'when'",
                    ("spec", "edges", i),
                )
        # Reachability from the entry, and whether every reachable node can still reach END.
        graph: dict[str, set[str]] = defaultdict(set)
        for e in s.edges:
            graph[e.from_].add(e.to)
        for n in s.nodes:
            if isinstance(n, ToolNode) and n.on_error:
                graph[n.id].add(n.on_error)
        reach = self._reach(s.entry, graph)
        for n in s.nodes:
            if n.id not in reach:
                self.add(
                    "warning",
                    "unreachable_node",
                    f"node {n.id!r} cannot be reached from {s.entry!r}",
                    ("spec", "nodes", self.node_index[n.id], "id"),
                )
        reverse: dict[str, set[str]] = defaultdict(set)
        for a, targets in graph.items():
            for b in targets:
                reverse[b].add(a)
        can_finish = self._reach(END, reverse)
        for n in s.nodes:
            if n.id in reach and n.id not in can_finish and graph.get(n.id):
                self.add(
                    "error",
                    "no_way_out",
                    f"from {n.id!r} the run can never reach END",
                    ("spec", "nodes", self.node_index[n.id], "id"),
                )
        if self._has_cycle(graph):
            self.add(
                "warning",
                "loop",
                f"the graph has a loop; runs stop after {s.limits.max_steps} steps",
                ("spec", "limits"),
            )

    @staticmethod
    def _reach(start: str, graph: dict[str, set[str]]) -> set[str]:
        seen, stack = {start}, [start]
        while stack:
            for nxt in graph.get(stack.pop(), set()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    @staticmethod
    def _has_cycle(graph: dict[str, set[str]]) -> bool:
        state: dict[str, int] = {}

        def visit(n: str) -> bool:
            state[n] = 1
            for m in graph.get(n, set()):
                if state.get(m) == 1 or (m not in state and visit(m)):
                    return True
            state[n] = 2
            return False

        return any(n not in state and visit(n) for n in list(graph))

    def tests(self) -> None:
        s = self.spec.spec
        if not s.tests:
            self.add("warning", "no_tests", "no tests: this agent cannot be promoted past dev", ("spec",))
        names: set[str] = set()
        for i, t in enumerate(s.tests):
            path = ("spec", "tests", i)
            if t.name in names:
                self.add("error", "duplicate_test", f"test name {t.name!r} is used twice", (*path, "name"))
            names.add(t.name)
            for key in t.input:
                if key not in s.input:
                    self.add(
                        "error",
                        "unknown_input",
                        f"test input {key!r} is not declared under spec.input",
                        (*path, "input", key),
                    )
            for step in t.expect.path or []:
                if step not in self.node_index:
                    self.add(
                        "error",
                        "unknown_node",
                        f"expected path mentions unknown node {step!r}",
                        (*path, "expect", "path"),
                    )
            declared = {x.use for x in s.tools}
            for tool in [*(t.expect.tools or []), *t.expect.not_tools]:
                if tool not in declared:
                    self.add(
                        "error",
                        "undeclared_tool",
                        f"test expects {tool}, which the agent does not declare",
                        (*path, "expect"),
                    )
            for key in t.expect.state:
                if key not in self.fields:
                    self.add(
                        "error",
                        "unknown_name",
                        f"test checks undeclared state field {key!r}",
                        (*path, "expect", "state", key),
                    )


def analyze(spec: AgentSpec, lines: LineMap | None = None) -> Analysis:
    return Analysis(_Checker(spec, lines).run())
