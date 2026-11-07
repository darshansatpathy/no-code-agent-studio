from collections.abc import Callable, Iterator

import pytest

from agentstudio.diffing import diff
from agentstudio.registry import Conflict, Forbidden, GateFailed, NotFound, Registry, RegistryError
from agentstudio.spec import SpecError, load_spec

ALL = frozenset({"author", "reviewer", "release-manager"})
Text = Callable[[str], str]


def remove_approval(text: str) -> str:
    return text.replace(
        '    - use: directory.reset_password\n      approval: "user.privileged == true"\n',
        "    - use: directory.reset_password\n",
    )


@pytest.fixture(params=["sqlite", "postgres"])
def reg(request: pytest.FixtureRequest) -> Iterator[Registry]:
    r = Registry.sqlite() if request.param == "sqlite" else Registry.postgres(request.getfixturevalue("postgres_url"))
    yield r
    r.close()


def test_publish_is_immutable_and_ordered(reg: Registry, text: Text) -> None:
    v1 = text("it-helpdesk/v1.0.0.yaml")
    record, report, created = reg.publish(v1, actor="ana")
    assert created and report.ok and record.tests_total == 8
    assert reg.publish(v1, actor="ana")[2] is False  # identical content: no-op
    with pytest.raises(Conflict, match="different content"):
        reg.publish(v1.replace("your account is unlocked", "you are unlocked"), actor="ana")
    reg.publish(text("it-helpdesk/v1.1.0.yaml"), actor="ana")
    with pytest.raises(Conflict, match="not newer"):
        reg.publish(v1.replace("version: 1.0.0", "version: 1.0.5"), actor="ana")
    assert [v.version for v in reg.versions("it-helpdesk")] == ["1.0.0", "1.1.0"]
    with pytest.raises(Forbidden):
        reg.publish(v1, actor="ana", roles=frozenset({"reviewer"}))
    with pytest.raises(SpecError):
        reg.publish(v1.replace("to: done_reset}", "to: nowhere}"), actor="ana")


def test_promotion_rules(reg: Registry, text: Text) -> None:
    reg.publish(text("it-helpdesk/v1.0.0.yaml"), actor="ana")
    reg.publish(text("it-helpdesk/v1.1.0.yaml"), actor="ana")
    with pytest.raises(Forbidden, match="reviewer"):
        reg.promote("it-helpdesk", "1.0.0", "staging", actor="ana", roles=frozenset({"author"}))
    with pytest.raises(GateFailed, match="staging before prod"):
        reg.promote("it-helpdesk", "1.0.0", "prod", actor="rm", roles=ALL)
    for env in ("dev", "staging", "prod"):
        reg.promote("it-helpdesk", "1.0.0", env, actor="rm", roles=ALL)
    assert reg.environments("it-helpdesk") == {"dev": "1.0.0", "staging": "1.0.0", "prod": "1.0.0"}
    with pytest.raises(RegistryError, match="unknown environment"):
        reg.promote("it-helpdesk", "1.0.0", "qa", actor="rm", roles=ALL)
    with pytest.raises(NotFound):
        reg.promote("it-helpdesk", "9.9.9", "dev", actor="rm", roles=ALL)
    reg.promote("it-helpdesk", "1.1.0", "staging", actor="rv", roles=ALL)
    result = reg.promote("it-helpdesk", "1.1.0", "prod", actor="rm", roles=ALL)
    assert result["previous"] == "1.0.0" and not result["rollback"]
    back = reg.promote("it-helpdesk", "1.0.0", "prod", actor="rm", roles=ALL)
    assert back["rollback"] is True
    assert reg.resolve("it-helpdesk").version == "1.0.0"
    assert reg.resolve("it-helpdesk", version="1.1.0").version == "1.1.0"
    with pytest.raises(NotFound):
        reg.resolve("refund-desk")
    audit = reg.audit("it-helpdesk")
    assert audit[0]["action"] == "promote" and audit[0]["detail"]["rollback"] is True
    assert {a["actor"] for a in audit} == {"ana", "rm", "rv"}


def test_staging_needs_passing_tests(reg: Registry, text: Text) -> None:
    failing = text("it-helpdesk/v1.0.0.yaml").replace(
        'reply_contains: ["Asha Rao", "unlocked"]', 'reply_contains: ["Asha Rao", "nope"]'
    )
    record, report, _ = reg.publish(failing, actor="ana")
    assert record.tests_passed == 7 and not report.ok
    reg.promote("it-helpdesk", "1.0.0", "dev", actor="ana", roles=ALL)  # dev accepts it
    with pytest.raises(GateFailed, match=r"7/8.*locked account is unlocked"):
        reg.promote("it-helpdesk", "1.0.0", "staging", actor="rv", roles=ALL)


def test_untested_agents_stop_at_dev(reg: Registry, minimal: str) -> None:
    reg.publish(minimal[: minimal.index("  tests:")], actor="ana")
    reg.promote("echo", "0.1.0", "dev", actor="ana", roles=ALL)
    with pytest.raises(GateFailed, match="no tests"):
        reg.promote("echo", "0.1.0", "staging", actor="rv", roles=ALL)


def test_high_risk_changes_need_acknowledgement(reg: Registry, text: Text) -> None:
    v11 = text("it-helpdesk/v1.1.0.yaml")
    reg.publish(v11, actor="ana")
    for env in ("staging", "prod"):
        reg.promote("it-helpdesk", "1.1.0", env, actor="rm", roles=ALL)
    reg.publish(remove_approval(v11.replace("version: 1.1.0", "version: 1.2.0")), actor="ana")
    reg.promote("it-helpdesk", "1.2.0", "staging", actor="rm", roles=ALL)
    with pytest.raises(GateFailed, match=r"approval removed from directory\.reset_password"):
        reg.promote("it-helpdesk", "1.2.0", "prod", actor="rm", roles=ALL)
    done = reg.promote("it-helpdesk", "1.2.0", "prod", actor="rm", roles=ALL, acknowledge_risk=True)
    assert done["acknowledged_risks"] == ["approval removed from directory.reset_password"]


def test_agents_listing_and_runs(reg: Registry, text: Text) -> None:
    reg.publish(text("refund-desk/v1.0.0.yaml"), actor="ana")
    listing = reg.agents()
    assert listing[0]["name"] == "refund-desk" and listing[0]["latest"] == "1.0.0"
    reg.record_run("run-1", "refund-desk", "1.0.0", "prod", "op")
    assert reg.run_record("run-1")["version"] == "1.0.0"
    with pytest.raises(NotFound):
        reg.run_record("run-2")
    assert reg.version("refund-desk", "1.0.0").to_dict(with_spec=True)["spec_yaml"].startswith("apiVersion")


def test_diff_finds_changes_and_risks(text: Text) -> None:
    v10 = load_spec(text("it-helpdesk/v1.0.0.yaml"))[0]
    v11 = load_spec(text("it-helpdesk/v1.1.0.yaml"))[0]
    d = diff(v10, v11)
    kinds = {(c.kind, c.action, c.item) for c in d.changes}
    assert ("node", "added", "kb") in kinds and ("tool", "added", "kb.search") in kinds
    assert ("test", "added", "how-to question answered from the knowledge base") in kinds
    assert not d.high_risk and d.risks == []
    assert "| tool added | kb.search |" in d.markdown()


def test_diff_risk_rules(text: Text) -> None:
    base_text = text("refund-desk/v1.0.0.yaml")
    base = load_spec(base_text)[0]

    def risks(new_text: str) -> set[str]:
        return {f"{r.level}:{r.message}" for r in diff(base, load_spec(new_text)[0]).risks}

    raised = base_text.replace("amount: {max: 500}", "amount: {max: 5000}")
    assert any(r.startswith("high:payments.issue_refund.amount max raised") for r in risks(raised))
    no_limit = base_text.replace("      limits:\n        amount: {max: 500}\n", "")
    assert "high:limit on payments.issue_refund.amount removed" in risks(no_limit)
    loosened = base_text.replace('approval: "args.amount > 100"', 'approval: "args.amount > 400"')
    assert any(r.startswith("medium:approval rule for payments.issue_refund changed") for r in risks(loosened))
    budget = base_text.replace("max_tool_calls: 3", "max_tool_calls: 9")
    assert "medium:tool-call budget raised to 9" in risks(budget)
    fewer = base_text[: base_text.index("    - name: no order number")]
    assert "medium:2 test(s) removed" in risks(fewer)
    new_write = base_text.replace(
        "    - use: orders.lookup_order\n", "    - use: orders.lookup_order\n    - use: tickets.create_ticket\n"
    )
    assert "high:write tool tickets.create_ticket added with no approval or limits" in risks(new_write)
    no_input = base_text.replace("  input:\n    message: string\n", "  input: {}\n")
    changed = {(c.kind, c.action) for c in diff(base, load_spec(no_input.replace("input.message", "'x'"))[0]).changes}
    assert ("input", "removed") in changed


def test_diff_one_of_widening(text: Text) -> None:
    v = text("it-helpdesk/v1.0.0.yaml")
    wider = v.replace("priority: {one_of: [low, normal]}", "priority: {one_of: [low, normal, high]}")
    d = diff(load_spec(v)[0], load_spec(wider)[0])
    assert d.high_risk and "allowed values widened" in d.risks[0].message
