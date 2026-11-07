"""The agent spec: a YAML document, parsed into typed models with line-numbered errors."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

FieldType = Literal["string", "number", "integer", "boolean", "object", "list"]
NAME = r"^[a-z][a-z0-9-]{1,48}$"
IDENT = r"^[a-z_][a-z0-9_]{0,40}$"
SEMVER = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class StateField(_Model):
    type: FieldType
    default: Any = None
    description: str = ""


class ArgLimit(_Model):
    min: float | None = None
    max: float | None = None
    max_length: int | None = Field(default=None, ge=1)
    one_of: list[Any] | None = None


class ToolRef(_Model):
    use: str = Field(pattern=r"^[a-z]+\.[a-z_]+$")
    approval: str | None = None  # expression over state, input and args: when true, a person must approve
    limits: dict[str, ArgLimit] = Field(default_factory=dict)


class Limits(_Model):
    max_steps: int = Field(default=25, ge=1, le=200)
    max_tool_calls: int = Field(default=10, ge=1, le=100)


class _Node(_Model):
    id: str = Field(pattern=IDENT)
    description: str = ""


class ClassifyNode(_Node):
    type: Literal["classify"]
    input: str
    output: str = Field(pattern=IDENT)
    labels: dict[str, list[str]] = Field(min_length=1)
    default: str = "other"
    method: Literal["keywords", "llm"] = "keywords"


class ExtractNode(_Node):
    type: Literal["extract"]
    input: str
    fields: dict[str, str] = Field(min_length=1)  # state field -> regex with one group


class ToolNode(_Node):
    type: Literal["tool"]
    tool: str = Field(pattern=r"^[a-z]+\.[a-z_]+$")
    args: dict[str, str] = Field(default_factory=dict)  # argument -> expression
    output: str | None = Field(default=None, pattern=IDENT)
    on_error: str | None = None  # node to go to when the call fails or approval is denied


class SetNode(_Node):
    type: Literal["set"]
    values: dict[str, str] = Field(min_length=1)


class RespondNode(_Node):
    type: Literal["respond"]
    template: str = Field(min_length=1)
    output: str = Field(default="reply", pattern=IDENT)


class GenerateNode(_Node):
    type: Literal["generate"]
    prompt: str = Field(min_length=1)
    output: str = Field(pattern=IDENT)
    method: Literal["template", "llm"] = "template"


Node = Annotated[
    ClassifyNode | ExtractNode | ToolNode | SetNode | RespondNode | GenerateNode, Field(discriminator="type")
]


class Edge(_Model):
    from_: str = Field(alias="from")
    to: str
    when: str | None = None


class TestExpect(_Model):
    status: Literal["completed", "failed", "awaiting_approval"] = "completed"
    path: list[str] | None = None
    tools: list[str] | None = None
    not_tools: list[str] = Field(default_factory=list)
    state: dict[str, Any] = Field(default_factory=dict)
    reply_contains: list[str] = Field(default_factory=list)
    reply_not_contains: list[str] = Field(default_factory=list)


class TestCase(_Model):
    name: str = Field(min_length=1, max_length=120)
    input: dict[str, Any] = Field(default_factory=dict)
    approvals: list[bool] = Field(default_factory=list)  # decisions given, in order, when the run pauses
    expect: TestExpect = Field(default_factory=TestExpect)


class Metadata(_Model):
    name: str = Field(pattern=NAME)
    version: str = Field(pattern=SEMVER)
    description: str = Field(default="", max_length=500)
    owner: str = Field(default="", max_length=80)
    labels: dict[str, str] = Field(default_factory=dict)


class Body(_Model):
    input: dict[str, FieldType] = Field(default_factory=dict)
    state: dict[str, StateField] = Field(default_factory=dict)
    tools: list[ToolRef] = Field(default_factory=list)
    limits: Limits = Field(default_factory=Limits)
    entry: str
    nodes: list[Node] = Field(min_length=1)
    edges: list[Edge] = Field(default_factory=list)
    tests: list[TestCase] = Field(default_factory=list)


class AgentSpec(_Model):
    api_version: Literal["studio/v1"] = Field(alias="apiVersion")
    kind: Literal["Agent"]
    metadata: Metadata
    spec: Body

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def version(self) -> str:
        return self.metadata.version

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(by_alias=True, mode="json")

    def content_hash(self) -> str:
        return _hash(self.canonical())

    def body_hash(self) -> str:
        """Hash of everything but the version number: same behaviour, same hash."""
        data = self.canonical()
        data["metadata"] = {k: v for k, v in data["metadata"].items() if k != "version"}
        return _hash(data)

    def tool_ref(self, name: str) -> ToolRef | None:
        return next((t for t in self.spec.tools if t.use == name), None)

    def node(self, node_id: str) -> Any:
        return next((n for n in self.spec.nodes if n.id == node_id), None)


def _hash(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Issue:
    level: Literal["error", "warning"]
    code: str
    message: str
    path: str = ""
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"level": self.level, "code": self.code, "message": self.message, "path": self.path, "line": self.line}

    def __str__(self) -> str:
        where = f"line {self.line}: " if self.line else ""
        at = f" ({self.path})" if self.path else ""
        return f"{self.level}: {where}{self.message}{at} [{self.code}]"


class SpecError(ValueError):
    def __init__(self, issues: list[Issue]) -> None:
        super().__init__("; ".join(str(i) for i in issues))
        self.issues = issues


@dataclass
class LineMap:
    """Path in the document -> 1-based line, so errors point at the YAML."""

    lines: dict[tuple[Any, ...], int] = field(default_factory=dict)

    def line(self, path: tuple[Any, ...]) -> int | None:
        while path:
            if path in self.lines:
                return self.lines[path]
            path = path[:-1]
        return self.lines.get((), None)


def _walk(node: yaml.Node, path: tuple[Any, ...], out: dict[tuple[Any, ...], int]) -> None:
    out.setdefault(path, node.start_mark.line + 1)
    if isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            key = k.value
            out[(*path, key)] = k.start_mark.line + 1
            _walk(v, (*path, key), out)
    elif isinstance(node, yaml.SequenceNode):
        for i, item in enumerate(node.value):
            _walk(item, (*path, i), out)


def path_str(path: tuple[Any, ...]) -> str:
    out = ""
    for p in path:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else str(p))
    return out


MAX_SPEC_BYTES = 256_000


def load_spec(text: str) -> tuple[AgentSpec, LineMap]:
    if len(text.encode()) > MAX_SPEC_BYTES:
        raise SpecError([Issue("error", "too_large", f"spec is larger than {MAX_SPEC_BYTES // 1000} KB")])
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        raise SpecError(
            [Issue("error", "yaml", str(getattr(exc, "problem", exc)), line=mark.line + 1 if mark else None)]
        ) from exc
    lines = LineMap()
    if root is not None:
        _walk(root, (), lines.lines)
    if not isinstance(data, dict):
        raise SpecError([Issue("error", "shape", "spec must be a YAML mapping", line=1)])
    try:
        spec = AgentSpec.model_validate(data)
    except ValidationError as exc:
        issues = []
        for err in exc.errors()[:50]:
            loc = _strip_tags(tuple(err["loc"]))
            issues.append(Issue("error", "schema", err["msg"], path_str(loc), lines.line(loc)))
        raise SpecError(issues) from exc
    return spec, lines


_UNION_TAGS = {"classify", "extract", "tool", "set", "respond", "generate"}


def _strip_tags(loc: tuple[Any, ...]) -> tuple[Any, ...]:
    """Pydantic puts the union tag after the list index (nodes.0.tool.args); drop it."""
    out: list[Any] = []
    for i, part in enumerate(loc):
        if i >= 2 and loc[i - 2] == "nodes" and isinstance(loc[i - 1], int) and part in _UNION_TAGS:
            continue
        out.append(part)
    return tuple(out)
