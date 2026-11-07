from collections.abc import Callable

import pytest

from agentstudio.analyzer import analyze
from agentstudio.spec import AgentSpec, SpecError, load_spec


def codes(text: str) -> dict[str, int | None]:
    spec, lines = load_spec(text)
    return {i.code: i.line for i in analyze(spec, lines).issues}


def test_examples_are_clean(spec: Callable[[str], AgentSpec]) -> None:
    for rel in ("it-helpdesk/v1.0.0.yaml", "it-helpdesk/v1.1.0.yaml", "refund-desk/v1.0.0.yaml"):
        analysis = analyze(spec(rel))
        assert analysis.ok and not analysis.warnings, (rel, [str(i) for i in analysis.issues])


def test_hashes(spec: Callable[[str], AgentSpec], text: Callable[[str], str]) -> None:
    a = spec("it-helpdesk/v1.0.0.yaml")
    bumped = load_spec(text("it-helpdesk/v1.0.0.yaml").replace("version: 1.0.0", "version: 1.0.1"))[0]
    assert a.content_hash() != bumped.content_hash()
    assert a.body_hash() == bumped.body_hash()


@pytest.mark.parametrize(
    ("change", "fragment", "line"),
    [
        (lambda t: t.replace("kind: Agent", "kind: Robot"), "Input should be 'Agent'", 2),
        (lambda t: t.replace("version: 1.0.0", "version: one"), "String should match pattern", 5),
        (lambda t: t.replace("type: respond", "type: respnd", 1), "does not match any of the expected tags", 70),
        (
            lambda t: t.replace("      output: intent\n", "      output: intent\n      colour: blue\n"),
            "Extra inputs are not permitted",
            None,
        ),
        (lambda t: "just: [unclosed", "", 1),
        (lambda t: "- a list", "must be a YAML mapping", 1),
    ],
)
def test_schema_errors_point_at_lines(
    text: Callable[[str], str], change: Callable[[str], str], fragment: str, line: int | None
) -> None:
    with pytest.raises(SpecError) as err:
        load_spec(change(text("it-helpdesk/v1.0.0.yaml")))
    first = err.value.issues[0]
    assert fragment in first.message
    if line is not None:
        assert first.line == line


def test_too_large() -> None:
    with pytest.raises(SpecError, match="larger than"):
        load_spec("x: " + "a" * 300_000)


def test_semantic_errors(text: Callable[[str], str]) -> None:
    base = text("it-helpdesk/v1.0.0.yaml")
    broken = (
        base.replace("to: done_reset}", "to: done_rest}")
        .replace("when: \"intent == 'account_locked' and user.locked\"", 'when: "usr.locked"')
        .replace("args: {user_id: input.user_id}\n      output: user", "args: {user: input.user_id}\n      output: usr")
        .replace("entry: classify", "entry: start")
        .replace("summary: input.message", "summary: input.text")
    )
    found = codes(broken)
    for code in (
        "unknown_node",
        "unknown_name",
        "missing_arg",
        "unknown_arg",
        "undeclared_output",
        "unknown_entry",
        "unknown_input",
    ):
        assert code in found, (code, found)
    assert found["unknown_node"] is not None


def test_graph_shape_checks(minimal: str) -> None:
    dead = minimal.replace("  edges:\n    - {from: answer, to: END}\n", "  edges: []\n")
    assert "dead_end" in codes(dead)
    loop = minimal.replace(
        '    - {id: answer, type: respond, template: "You said: {{ input.message }}"}\n',
        '    - {id: answer, type: respond, template: "You said: {{ input.message }}"}\n'
        "    - {id: again, type: set, values: {reply: \"reply + '!'\"}}\n",
    ).replace(
        "    - {from: answer, to: END}\n",
        '    - {from: answer, to: again, when: "len(reply) < 20"}\n    - {from: answer, to: END}\n'
        "    - {from: again, to: answer}\n",
    )
    found = codes(loop)
    assert "loop" in found and "dead_end" not in found
    trap = minimal.replace(
        '    - {id: answer, type: respond, template: "You said: {{ input.message }}"}\n',
        '    - {id: answer, type: respond, template: "You said: {{ input.message }}"}\n'
        "    - {id: spin, type: set, values: {reply: \"'x'\"}}\n",
    ).replace("    - {from: answer, to: END}\n", "    - {from: answer, to: spin}\n    - {from: spin, to: spin}\n")
    assert "no_way_out" in codes(trap)
    extra = minimal.replace(
        "    - {from: answer, to: END}\n",
        '    - {from: answer, to: END}\n    - {from: answer, to: END, when: "true"}\n',
    )
    assert "unreachable_edge" in codes(extra)
    cond = minimal.replace("    - {from: answer, to: END}\n", "    - {from: answer, to: END, when: \"reply != ''\"}\n")
    assert "no_default_route" in codes(cond)
    orphan = minimal.replace(
        '    - {id: answer, type: respond, template: "You said: {{ input.message }}"}\n',
        '    - {id: answer, type: respond, template: "You said: {{ input.message }}"}\n'
        "    - {id: lonely, type: respond, template: x}\n",
    ).replace("    - {from: answer, to: END}\n", "    - {from: answer, to: END}\n    - {from: lonely, to: END}\n")
    assert "unreachable_node" in codes(orphan)


def test_tool_checks(minimal: str) -> None:
    with_tool = minimal.replace(
        "  entry: answer\n",
        "  tools:\n    - use: payments.issue_refund\n    - use: magic.wand\n"
        "    - use: kb.search\n      limits: {colour: {max: 1}}\n  entry: answer\n",
    )
    found = codes(with_tool)
    assert {"unguarded_write", "unknown_tool", "unknown_limit"} <= set(found)
    undeclared = minimal.replace(
        '    - {id: answer, type: respond, template: "You said: {{ input.message }}"}\n',
        "    - {id: answer, type: tool, tool: kb.search, args: {query: input.message}, on_error: nowhere}\n",
    )
    found = codes(undeclared)
    assert {"undeclared_tool", "unknown_node"} <= set(found)


def test_node_specific_checks(minimal: str) -> None:
    nodes = (
        "    - {id: answer, type: respond, template: \"{{ __import__('x') }}\"}\n"
        "    - {id: grab, type: extract, input: input.message, fields: {reply: '(a)(b)', missing: '[bad'}}\n"
        "    - {id: sort, type: classify, input: input.message, output: reply, labels: {x: []}}\n"
        '    - {id: make, type: generate, prompt: "{{ nope }}", output: reply}\n'
    )
    text = minimal.replace('    - {id: answer, type: respond, template: "You said: {{ input.message }}"}\n', nodes)
    found = codes(text)
    assert {"expression", "regex", "empty_label", "undeclared_output", "unknown_name"} <= set(found)


def test_test_checks(minimal: str) -> None:
    text = minimal.replace(
        '      input: {message: hi}\n      expect: {reply_contains: ["You said: hi"]}\n',
        "      input: {message: hi, extra: 1}\n      expect: {path: [nowhere], tools: [kb.search], "
        "state: {ghost: 1}}\n    - name: echoes\n",
    )
    found = codes(text)
    assert {"unknown_input", "unknown_node", "undeclared_tool", "duplicate_test"} <= set(found)
    assert "no_tests" in codes(minimal.replace(minimal[minimal.index("  tests:") :], ""))


def test_reserved_and_duplicate_names(minimal: str) -> None:
    text = (
        minimal.replace(
            '  state: {reply: {type: string, default: ""}}',
            '  state: {reply: {type: string, default: ""}, input: {type: object}}',
        )
        .replace("    - {from: answer, to: END}\n", "    - {from: answer, to: END}\n")
        .replace(
            '    - {id: answer, type: respond, template: "You said: {{ input.message }}"}\n',
            '    - {id: answer, type: respond, template: "You said: {{ input.message }}"}\n'
            '    - {id: answer, type: respond, template: "again"}\n',
        )
    )
    found = codes(text)
    assert {"reserved_name", "duplicate_node"} <= set(found)
