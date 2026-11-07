"""Structural diff between two versions of an agent, with a risk review.

A reviewer approving a promotion wants two answers: what changed, and does any of it widen
what the agent is allowed to do. The risk rules flag changes that add or loosen side
effects (a new write tool, a removed approval, a raised limit, a bigger tool budget) and
changes that reduce the evidence (tests removed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from agentstudio.catalog import CATALOG
from agentstudio.spec import AgentSpec

Level = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class Change:
    kind: str  # node | edge | tool | limits | input | state | test | metadata
    action: str  # added | removed | changed
    item: str
    detail: str = ""


@dataclass(frozen=True)
class Risk:
    level: Level
    message: str


@dataclass
class Diff:
    old_version: str
    new_version: str
    changes: list[Change] = field(default_factory=list)
    risks: list[Risk] = field(default_factory=list)

    @property
    def high_risk(self) -> bool:
        return any(r.level == "high" for r in self.risks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": self.old_version,
            "to": self.new_version,
            "high_risk": self.high_risk,
            "changes": [c.__dict__ for c in self.changes],
            "risks": [r.__dict__ for r in self.risks],
        }

    def markdown(self) -> str:
        lines = [f"### {self.old_version} → {self.new_version}", ""]
        if self.risks:
            lines += ["| Risk | Finding |", "|---|---|", *[f"| {r.level} | {r.message} |" for r in self.risks], ""]
        lines += ["| Change | Item | Detail |", "|---|---|---|"]
        lines += [f"| {c.kind} {c.action} | {c.item} | {c.detail} |" for c in self.changes] or ["| none | | |"]
        return "\n".join(lines)


def _keyed(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {str(i[key]): i for i in items}


def _write(tool: str) -> bool:
    t = CATALOG.get(tool)
    return t is not None and t.kind == "write"


def diff(old: AgentSpec, new: AgentSpec) -> Diff:
    a, b = old.canonical()["spec"], new.canonical()["spec"]
    d = Diff(old.version, new.version)
    add, risk = d.changes.append, d.risks.append

    # Nodes
    na, nb = _keyed(a["nodes"], "id"), _keyed(b["nodes"], "id")
    for nid in sorted(nb.keys() - na.keys()):
        add(Change("node", "added", nid, nb[nid]["type"]))
        if nb[nid]["type"] == "tool" and _write(nb[nid]["tool"]):
            risk(Risk("medium", f"new node {nid!r} calls write tool {nb[nid]['tool']}"))
    for nid in sorted(na.keys() - nb.keys()):
        add(Change("node", "removed", nid, na[nid]["type"]))
    for nid in sorted(na.keys() & nb.keys()):
        if na[nid] != nb[nid]:
            fields = sorted(k for k in set(na[nid]) | set(nb[nid]) if na[nid].get(k) != nb[nid].get(k))
            add(Change("node", "changed", nid, ", ".join(fields)))
            if nb[nid]["type"] == "tool" and "args" in fields and _write(nb[nid]["tool"]):
                risk(Risk("medium", f"arguments of write tool call {nid!r} changed"))

    # Edges
    ea = {(e["from"], e["to"], e.get("when")) for e in a["edges"]}
    eb = {(e["from"], e["to"], e.get("when")) for e in b["edges"]}
    for e in sorted(eb - ea, key=str):
        add(Change("edge", "added", f"{e[0]} → {e[1]}", e[2] or ""))
    for e in sorted(ea - eb, key=str):
        add(Change("edge", "removed", f"{e[0]} → {e[1]}", e[2] or ""))
    if [(e["from"], e["to"], e.get("when")) for e in a["edges"]] != [
        (e["from"], e["to"], e.get("when")) for e in b["edges"]
    ] and ea == eb:
        add(Change("edge", "changed", "order", "edge evaluation order changed"))

    # Tools
    ta, tb = _keyed(a["tools"], "use"), _keyed(b["tools"], "use")
    for name in sorted(tb.keys() - ta.keys()):
        add(Change("tool", "added", name))
        if _write(name):
            level: Level = "high" if not tb[name].get("approval") and not tb[name].get("limits") else "medium"
            risk(Risk(level, f"write tool {name} added" + (" with no approval or limits" if level == "high" else "")))
    for name in sorted(ta.keys() - tb.keys()):
        add(Change("tool", "removed", name))
    for name in sorted(ta.keys() & tb.keys()):
        old_t, new_t = ta[name], tb[name]
        if old_t == new_t:
            continue
        add(Change("tool", "changed", name))
        if old_t.get("approval") and not new_t.get("approval"):
            risk(Risk("high", f"approval removed from {name}"))
        elif old_t.get("approval") != new_t.get("approval") and new_t.get("approval"):
            risk(Risk("medium", f"approval rule for {name} changed: {old_t.get('approval')!r} → {new_t['approval']!r}"))
        for arg, old_l in (old_t.get("limits") or {}).items():
            new_l = (new_t.get("limits") or {}).get(arg)
            if new_l is None:
                risk(Risk("high", f"limit on {name}.{arg} removed"))
                continue
            if old_l.get("max") is not None and (new_l.get("max") is None or new_l["max"] > old_l["max"]):
                risk(Risk("high", f"{name}.{arg} max raised from {old_l['max']:g} to {new_l.get('max')}"))
            if old_l.get("one_of") is not None and (
                new_l.get("one_of") is None or not set(new_l["one_of"]) <= set(old_l["one_of"])
            ):
                risk(Risk("high", f"{name}.{arg} allowed values widened"))

    # Limits, interface, tests
    for key in sorted(set(a["limits"]) | set(b["limits"])):
        if a["limits"].get(key) != b["limits"].get(key):
            add(Change("limits", "changed", key, f"{a['limits'].get(key)} → {b['limits'].get(key)}"))
            if key == "max_tool_calls" and b["limits"][key] > a["limits"][key]:
                risk(Risk("medium", f"tool-call budget raised to {b['limits'][key]}"))
    for section in ("input", "state"):
        for k in sorted(set(b[section]) - set(a[section])):
            add(Change(section, "added", k))
        for k in sorted(set(a[section]) - set(b[section])):
            add(Change(section, "removed", k))
            if section == "input":
                risk(Risk("medium", f"input {k!r} removed: callers that send it will be rejected"))
    tests_a, tests_b = {t["name"] for t in a["tests"]}, {t["name"] for t in b["tests"]}
    for t in sorted(tests_b - tests_a):
        add(Change("test", "added", t))
    for t in sorted(tests_a - tests_b):
        add(Change("test", "removed", t))
    if len(tests_b) < len(tests_a):
        risk(Risk("medium", f"{len(tests_a) - len(tests_b)} test(s) removed"))
    if old.metadata.description != new.metadata.description:
        add(Change("metadata", "changed", "description"))
    return d
