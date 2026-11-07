import pytest

from agentstudio.expr import ExprError, compile_expr, compile_template

CTX = {
    "intent": "refund",
    "order": {"total": 120.0, "items": [1, 2], "status": "delivered"},
    "input": {"message": "Hello There"},
    "nothing": None,
    "n": 3,
}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("intent == 'refund' and order.total > 100", True),
        ("order.total - 20", 100.0),
        ("len(order.items)", 2),
        ("order.items[0]", 1),
        ("order.items[9]", None),
        ("order['status']", "delivered"),
        ("order.missing == null", True),
        ("contains(input.message, 'there')", True),
        ("lower(input.message)", "hello there"),
        ("'a' if n > 2 else 'b'", "a"),
        ("intent in ['refund', 'return']", True),
        ("intent not in ['other']", True),
        ("not nothing", True),
        ("coalesce(nothing, '', 'x')", "x"),
        ("round(10 / 3, 2)", 3.33),
        ("number('4.5') + 1", 5.5),
        ("max(1, n, 2)", 3),
        ("1 < n < 5", True),
        ("-n", -3),
        ("nothing.deep.path", None),
        ("str(nothing)", ""),
        ("'ab' + 'cd'", "abcd"),
        ("n % 2", 1),
        ("true or false", True),
    ],
)
def test_evaluates(source: str, expected: object) -> None:
    assert compile_expr(source).evaluate(CTX) == expected


def test_reports_root_names() -> None:
    assert compile_expr("intent == 'x' and order.total > input.limit").names == {"intent", "order", "input"}
    assert compile_expr("true and len('x') > 0").names == frozenset()


@pytest.mark.parametrize(
    "source",
    [
        "__import__('os').system('id')",
        "order.__class__",
        "_secret",
        "open('/etc/passwd')",
        "[x for x in order.items]",
        "lambda: 1",
        "order.items[n]",
        "len",
        "len(x=1)",
        "2 ** 100",
        "a := 1",
        "f'{intent}'",
        "",
        "x" * 600,
        "+".join(["1"] * 80),
        "intent ==",
        "{'a': 1}",
    ],
)
def test_rejects_unsafe_or_invalid(source: str) -> None:
    with pytest.raises(ExprError):
        compile_expr(source)


@pytest.mark.parametrize(
    "source",
    [
        "'a' * 1000",
        "intent - 1",
        "-intent",
        "1 / 0",
        "missing_name",
        "len(5)",
        "round('x')",
        "number('abc')",
        "intent < 3",
        "contains(5, 1)",
        "order.items.total",
        "('x' * 1)",
    ],
)
def test_runtime_errors_are_expr_errors(source: str) -> None:
    with pytest.raises(ExprError):
        compile_expr(source).evaluate({**CTX, "order": {"items": [1]}})


def test_long_string_concatenation_is_bounded() -> None:
    with pytest.raises(ExprError, match="too long"):
        compile_expr("s + s").evaluate({"s": "x" * 6000})


def test_templates() -> None:
    t = compile_template("Total {{ order.total }} for {{ lower(input.message) }}{{ nothing }}!")
    assert t.render(CTX) == "Total 120.00 for hello there!"
    assert t.names == {"order", "input", "nothing"}
    assert compile_template("no placeholders").render({}) == "no placeholders"
    assert compile_template("{{ 1.25 }} {{ 7 }}").render({}) == "1.25 7"
    with pytest.raises(ExprError):
        compile_template("{{ __import__('os') }}")
    with pytest.raises(ExprError):
        compile_template("x" * 5001)
