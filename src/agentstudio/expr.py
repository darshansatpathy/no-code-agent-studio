"""A small, safe expression language for conditions, arguments and templates.

Spec authors write `intent == 'refund' and order.total > 100`. Expressions are parsed
with Python's `ast` and then checked against a whitelist: literals, names, dotted access
into mappings, constant subscripts, comparisons, boolean logic, arithmetic on numbers,
conditional expressions and a handful of pure functions. Anything else (calls to
arbitrary functions, attribute access on objects, comprehensions, lambdas, dunder names)
is rejected when the spec is validated, long before anything runs. Nothing is ever passed
to `eval`.
"""

from __future__ import annotations

import ast
import operator
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

MAX_LENGTH = 500
MAX_NODES = 120
CONSTANT_NAMES = {"true": True, "false": False, "null": None, "none": None, "True": True, "False": False, "None": None}


class ExprError(ValueError):
    """An expression that is not allowed, or that failed while evaluating."""


def _num(*values: Any) -> bool:
    return all(isinstance(v, int | float) and not isinstance(v, bool) for v in values)


def _len(x: Any) -> int:
    if not isinstance(x, str | list | tuple | dict):
        raise ExprError("len() needs a string, list or mapping")
    return len(x)


def _text(fn: Callable[[str], str]) -> Callable[[Any], str]:
    def wrapped(x: Any) -> str:
        return fn(str(x)) if x is not None else ""

    return wrapped


def _contains(haystack: Any, needle: Any) -> bool:
    if haystack is None:
        return False
    if isinstance(haystack, str):
        return str(needle).lower() in haystack.lower()
    if isinstance(haystack, list | tuple | dict):
        return needle in haystack
    raise ExprError("contains() needs a string, list or mapping")


def _round(x: Any, digits: Any = 0) -> float:
    if not _num(x) or not isinstance(digits, int):
        raise ExprError("round() needs a number")
    return round(float(x), digits)


def _number(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError) as exc:
        raise ExprError(f"cannot convert {x!r} to a number") from exc


FUNCTIONS: dict[str, Callable[..., Any]] = {
    "len": _len,
    "lower": _text(str.lower),
    "upper": _text(str.upper),
    "strip": _text(str.strip),
    "contains": _contains,
    "round": _round,
    "number": _number,
    "min": lambda *a: min(a),
    "max": lambda *a: max(a),
    "str": lambda x: "" if x is None else str(x),
    "coalesce": lambda *a: next((x for x in a if x not in (None, "")), None),
}

_COMPARE: dict[type[ast.cmpop], Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b if b is not None else False,
    ast.NotIn: lambda a, b: a not in b if b is not None else True,
}
_ARITH: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
}
_ALLOWED = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.BinOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    ast.Name,
    ast.Load,
    ast.Attribute,
    ast.Subscript,
    ast.Constant,
    ast.List,
    ast.Tuple,
    *_COMPARE,
    *_ARITH,
)


@dataclass(frozen=True)
class Expression:
    source: str
    tree: ast.Expression
    names: frozenset[str]  # root names it reads, for the analyzer

    def evaluate(self, context: Mapping[str, Any]) -> Any:
        return _eval(self.tree.body, context)


def compile_expr(source: str) -> Expression:
    if not isinstance(source, str) or not source.strip():
        raise ExprError("expression is empty")
    if len(source) > MAX_LENGTH:
        raise ExprError(f"expression is longer than {MAX_LENGTH} characters")
    try:
        tree = ast.parse(source.strip(), mode="eval")
    except SyntaxError as exc:
        raise ExprError(f"cannot parse {source!r}: {exc.msg}") from exc
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES:
        raise ExprError("expression is too complex")
    names: set[str] = set()
    for node in nodes:
        if not isinstance(node, _ALLOWED):
            raise ExprError(f"{type(node).__name__} is not allowed in expressions")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ExprError("names starting with '_' are not allowed")
        if isinstance(node, ast.Call) and (
            not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS or node.keywords
        ):
            raise ExprError(f"only these functions are allowed: {', '.join(sorted(FUNCTIONS))}")
        if isinstance(node, ast.Subscript) and not isinstance(node.slice, ast.Constant):
            raise ExprError("subscripts must be constants, like items[0] or data['key']")
        if isinstance(node, ast.Name):
            if node.id.startswith("_"):
                raise ExprError("names starting with '_' are not allowed")
            if node.id not in CONSTANT_NAMES and node.id not in FUNCTIONS:
                names.add(node.id)
    # Function names used as values (not called) would slip through; reject them.
    called = {id(n.func) for n in nodes if isinstance(n, ast.Call)}
    for node in nodes:
        if isinstance(node, ast.Name) and node.id in FUNCTIONS and id(node) not in called:
            raise ExprError(f"{node.id} can only be called")
    return Expression(source, tree, frozenset(names))


def _get(container: Any, key: Any) -> Any:
    if isinstance(container, Mapping):
        return container.get(key)
    if isinstance(container, list | tuple) and isinstance(key, int):
        return container[key] if -len(container) <= key < len(container) else None
    if container is None:
        return None
    raise ExprError(f"cannot read {key!r} from a {type(container).__name__}")


def _eval(node: ast.AST, ctx: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in CONSTANT_NAMES:
            return CONSTANT_NAMES[node.id]
        if node.id not in ctx:
            raise ExprError(f"unknown name {node.id!r}")
        return ctx[node.id]
    if isinstance(node, ast.Attribute):
        return _get(_eval(node.value, ctx), node.attr)
    if isinstance(node, ast.Subscript):
        return _get(_eval(node.value, ctx), node.slice.value)  # type: ignore[attr-defined]
    if isinstance(node, ast.List | ast.Tuple):
        return [_eval(e, ctx) for e in node.elts]
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for v in node.values:
                result = _eval(v, ctx)
                if not result:
                    return result
            return result
        result = False
        for v in node.values:
            result = _eval(v, ctx)
            if result:
                return result
        return result
    if isinstance(node, ast.UnaryOp):
        value = _eval(node.operand, ctx)
        if isinstance(node.op, ast.Not):
            return not value
        if not _num(value):
            raise ExprError("unary minus needs a number")
        return -value
    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left, ctx), _eval(node.right, ctx)
        if isinstance(node.op, ast.Add) and isinstance(left, str) and isinstance(right, str):
            if len(left) + len(right) > 10_000:
                raise ExprError("string too long")
            return left + right
        if not _num(left, right):
            raise ExprError(f"arithmetic needs numbers, got {type(left).__name__} and {type(right).__name__}")
        if isinstance(node.op, ast.Div | ast.Mod) and right == 0:
            raise ExprError("division by zero")
        return _ARITH[type(node.op)](left, right)
    if isinstance(node, ast.Compare):
        left = _eval(node.left, ctx)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval(comparator, ctx)
            try:
                ok = _COMPARE[type(op)](left, right)
            except TypeError as exc:
                if isinstance(op, ast.Eq | ast.NotEq):
                    raise
                raise ExprError(f"cannot compare {type(left).__name__} with {type(right).__name__}") from exc
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _eval(node.body, ctx) if _eval(node.test, ctx) else _eval(node.orelse, ctx)
    if isinstance(node, ast.Call):
        fn = FUNCTIONS[node.func.id]  # type: ignore[attr-defined]
        try:
            return fn(*[_eval(a, ctx) for a in node.args])
        except ExprError:
            raise
        except (TypeError, ValueError) as exc:
            raise ExprError(f"{node.func.id}(): {exc}") from exc  # type: ignore[attr-defined]
    raise ExprError(f"{type(node).__name__} is not allowed")  # pragma: no cover - blocked at compile time


_TEMPLATE = re.compile(r"\{\{\s*(.+?)\s*\}\}")


@dataclass(frozen=True)
class Template:
    source: str
    parts: tuple[str | Expression, ...]

    @property
    def names(self) -> frozenset[str]:
        out: set[str] = set()
        for p in self.parts:
            if isinstance(p, Expression):
                out |= p.names
        return frozenset(out)

    def render(self, context: Mapping[str, Any]) -> str:
        out = []
        for p in self.parts:
            if isinstance(p, Expression):
                value = p.evaluate(context)
                out.append("" if value is None else _fmt(value))
            else:
                out.append(p)
        return "".join(out)


def _fmt(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return f"{value:.2f}" if abs(value) < 1e12 else str(value)
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def compile_template(source: str) -> Template:
    if len(source) > 5000:
        raise ExprError("template is longer than 5000 characters")
    parts: list[str | Expression] = []
    last = 0
    for m in _TEMPLATE.finditer(source):
        if m.start() > last:
            parts.append(source[last : m.start()])
        parts.append(compile_expr(m.group(1)))
        last = m.end()
    if last < len(source):
        parts.append(source[last:])
    return Template(source, tuple(parts))
