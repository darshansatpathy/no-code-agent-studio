from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentstudio.api import create_app
from agentstudio.cli import main
from agentstudio.config import ConfigError, Settings, bootstrap, build
from agentstudio.registry import Registry

AGENTS = Path(__file__).resolve().parents[1] / "agents"
EVERYONE = {"X-User-Id": "ana", "X-User-Roles": "author,reviewer,release-manager,operator,approver"}
AUTHOR = {"X-User-Id": "ana", "X-User-Roles": "author"}
Text = Callable[[str], str]


def _client(token: str | None = None, boot: bool = False) -> TestClient:
    settings = Settings(
        service_token=token,
        bootstrap_dir=str(AGENTS) if boot else None,
        bootstrap_promote=("dev", "staging", "prod") if boot else (),
    )
    return TestClient(create_app(build(settings), settings))


@pytest.fixture
def client() -> Iterator[TestClient]:
    c = _client(boot=True)
    yield c
    c.app.state.wiring.close()  # type: ignore[attr-defined]


def test_health_page_catalog(client: TestClient) -> None:
    assert client.get("/healthz").json()["agents"] == 2
    page = client.get("/studio")
    assert page.status_code == 200 and "Agent Studio" in page.text
    assert "script-src" in page.headers["content-security-policy"]
    assert client.get("/", follow_redirects=False).status_code == 307
    tools = {t["name"]: t for t in client.get("/v1/catalog").json()["tools"]}
    assert tools["payments.issue_refund"]["kind"] == "write"


def test_bootstrap_promoted_latest_versions(client: TestClient) -> None:
    agents = {a["name"]: a for a in client.get("/v1/agents").json()["agents"]}
    assert agents["it-helpdesk"]["environments"] == {"dev": "1.1.0", "staging": "1.1.0", "prod": "1.1.0"}
    detail = client.get("/v1/agents/it-helpdesk").json()
    assert [v["version"] for v in detail["versions"]] == ["1.0.0", "1.1.0"]
    assert client.get("/v1/agents/nope").status_code == 404


def test_validate_and_test_endpoints(client: TestClient, text: Text) -> None:
    good = client.post("/v1/specs/validate", json={"yaml": text("refund-desk/v1.0.0.yaml")}).json()
    assert good["ok"] and good["mermaid"].startswith("flowchart TD") and good["name"] == "refund-desk"
    bad = client.post(
        "/v1/specs/validate", json={"yaml": text("refund-desk/v1.0.0.yaml").replace("to: done_refund}", "to: gone}")}
    ).json()
    assert not bad["ok"] and bad["issues"][0]["line"] is not None
    broken = client.post("/v1/specs/validate", json={"yaml": "kind: Agent"}).json()
    assert broken["ok"] is False and broken["issues"][0]["code"] == "schema"
    report = client.post("/v1/specs/test", json={"yaml": text("refund-desk/v1.0.0.yaml")}).json()
    assert report["ok"] and report["total"] == 9
    refused = client.post(
        "/v1/specs/test", json={"yaml": text("refund-desk/v1.0.0.yaml").replace("to: done_refund}", "to: gone}")}
    )
    assert refused.status_code == 422 and refused.json()["issues"]


def test_publish_promote_diff_audit(client: TestClient, text: Text) -> None:
    risky = (
        text("it-helpdesk/v1.1.0.yaml")
        .replace("version: 1.1.0", "version: 1.2.0")
        .replace(
            '    - use: directory.reset_password\n      approval: "user.privileged == true"\n',
            "    - use: directory.reset_password\n",
        )
    )
    assert client.post("/v1/agents", json={"yaml": risky}, headers={"X-User-Roles": "reviewer"}).status_code == 403
    r = client.post("/v1/agents", json={"yaml": risky, "note": "drop approval"}, headers=AUTHOR)
    assert r.status_code == 201 and r.json()["created"] and r.json()["tests"]["ok"]
    again = client.post("/v1/agents", json={"yaml": risky}, headers=AUTHOR)
    assert again.json()["created"] is False
    conflict = client.post("/v1/agents", json={"yaml": risky.replace("Hi {{", "Hello {{")}, headers=AUTHOR)
    assert conflict.status_code == 409
    d = client.get("/v1/agents/it-helpdesk/diff", params={"old": "1.1.0", "new": "1.2.0"}).json()
    assert d["high_risk"] and "approval removed" in d["markdown"]
    promote = {"version": "1.2.0", "env": "staging"}
    assert client.post("/v1/agents/it-helpdesk/promote", json=promote, headers=AUTHOR).status_code == 403
    assert client.post("/v1/agents/it-helpdesk/promote", json=promote, headers=EVERYONE).status_code == 200
    prod = {"version": "1.2.0", "env": "prod"}
    refused = client.post("/v1/agents/it-helpdesk/promote", json=prod, headers=EVERYONE)
    assert refused.status_code == 422 and "acknowledge_risk" in refused.json()["detail"]
    ok = client.post("/v1/agents/it-helpdesk/promote", json={**prod, "acknowledge_risk": True}, headers=EVERYONE)
    assert ok.status_code == 200 and ok.json()["previous"] == "1.1.0"
    audit = client.get("/v1/agents/it-helpdesk/audit").json()["audit"]
    assert audit[0]["actor"] == "ana" and audit[0]["env"] == "prod"
    version = client.get("/v1/agents/it-helpdesk/versions/1.2.0").json()
    assert version["note"] == "drop approval" and "flowchart" in version["mermaid"]
    assert client.get("/v1/agents/it-helpdesk/versions/9.9.9").status_code == 404


def test_runs_and_approvals_are_pinned_to_a_version(client: TestClient) -> None:
    body = {"input": {"message": "O-5002 arrived broken"}}
    assert client.post("/v1/agents/refund-desk/runs", json=body, headers=AUTHOR).status_code == 403
    run = client.post("/v1/agents/refund-desk/runs", json=body, headers=EVERYONE).json()
    assert run["status"] == "awaiting_approval" and run["version"] == "1.0.0"
    rid = run["run_id"]
    assert client.get(f"/v1/runs/{rid}", headers=AUTHOR).status_code == 403
    assert client.get(f"/v1/runs/{rid}", headers=EVERYONE).json()["pending"]["args"]["amount"] == 189.0
    ops_only = {"X-User-Id": "op", "X-User-Roles": "operator"}
    assert client.post(f"/v1/runs/{rid}/approval", json={"approved": True}, headers=ops_only).status_code == 403
    done = client.post(
        f"/v1/runs/{rid}/approval", json={"approved": True}, headers={"X-User-Id": "boss", "X-User-Roles": "approver"}
    ).json()
    assert done["status"] == "completed" and done["calls"][-1]["approval"]["by"] == "boss"
    assert client.post(f"/v1/runs/{rid}/approval", json={"approved": True}, headers=EVERYONE).status_code == 409
    assert client.get("/v1/runs/nope", headers=EVERYONE).status_code == 404
    bad = client.post("/v1/agents/refund-desk/runs", json={"input": {"msg": "x"}}, headers=EVERYONE)
    assert bad.status_code == 422
    assert client.post("/v1/agents/refund-desk/runs", json={**body, "env": "qa"}, headers=EVERYONE).status_code == 400
    pinned = client.post(
        "/v1/agents/it-helpdesk/runs",
        json={"input": {"user_id": "U-103", "message": "forgot password"}, "version": "1.0.0"},
        headers=EVERYONE,
    ).json()
    assert pinned["version"] == "1.0.0" and pinned["status"] == "completed"
    text = client.get("/metrics").text
    assert 'studio_runs_total{agent="refund-desk",status="awaiting_approval"} 1' in text
    assert 'studio_approvals_total{decision="approved"} 1' in text


def test_identity_checks() -> None:
    c = _client(token="s3cret")
    try:
        assert c.get("/v1/agents").status_code == 401
        assert c.get("/v1/agents", headers={"Authorization": "Bearer s3cret"}).status_code == 200
        assert (
            c.get("/v1/agents", headers={"Authorization": "Bearer s3cret", "X-User-Roles": "root"}).status_code == 400
        )
        assert c.get("/healthz", headers={"X-Request-ID": "bad id"}).headers["X-Request-ID"] != "bad id"
    finally:
        c.app.state.wiring.close()  # type: ignore[attr-defined]


def test_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STORE", "redis")
    with pytest.raises(ConfigError):
        Settings.from_env()
    monkeypatch.setenv("STUDIO_STORE", "postgres")
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        Settings.from_env()
    monkeypatch.setenv("STUDIO_STORE", "memory")
    monkeypatch.setenv("STUDIO_BOOTSTRAP_PROMOTE", "dev,qa")
    with pytest.raises(ConfigError, match="qa"):
        Settings.from_env()
    monkeypatch.setenv("STUDIO_BOOTSTRAP_PROMOTE", "dev, staging")
    assert Settings.from_env().bootstrap_promote == ("dev", "staging")


def test_bootstrap_skips_bad_files_and_is_idempotent(tmp_path: Path, text: Text) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "good.yaml").write_text(text("refund-desk/v1.0.0.yaml"))
    (tmp_path / "a" / "broken.yaml").write_text("kind: [")
    (tmp_path / "a" / "invalid.yaml").write_text(
        text("refund-desk/v1.0.0.yaml")
        .replace("version: 1.0.0", "version: 1.0.1")
        .replace("to: done_refund}", "to: gone}")
    )
    reg = Registry.sqlite()
    assert bootstrap(reg, tmp_path, ("dev", "prod")) == ["refund-desk@1.0.0"]
    assert reg.environments("refund-desk") == {"dev": "1.0.0"}  # prod refused: never staged
    assert bootstrap(reg, tmp_path, ("dev",)) == []
    reg.close()


def test_sqlite_store(tmp_path: Path) -> None:
    w = build(
        Settings(
            store="sqlite", sqlite_path=str(tmp_path / "s.db"), bootstrap_dir=str(AGENTS), bootstrap_promote=("dev",)
        )
    )
    try:
        assert w.registry.environments("it-helpdesk") == {"dev": "1.1.0"}
    finally:
        w.close()


def test_postgres_store(postgres_url: str) -> None:
    w = build(
        Settings(
            store="postgres",
            database_url=postgres_url,
            bootstrap_dir=str(AGENTS),
            bootstrap_promote=("dev", "staging", "prod"),
        )
    )
    try:
        spec = w.registry.resolve("refund-desk").spec()
        run = w.runtime.start(spec, {"message": "O-5002 arrived broken"}, run_id="pg-1")
        assert run.status == "awaiting_approval"
        assert w.runtime.resume(spec, "pg-1", approved=True, approver="u").status == "completed"
    finally:
        w.close()


def test_cli_validate_test_check(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, text: Text, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = str(AGENTS / "refund-desk" / "v1.0.0.yaml")
    assert main(["validate", f]) == 0
    assert "refund-desk 1.0.0 ok" in capsys.readouterr().out
    bad = tmp_path / "bad.yaml"
    bad.write_text(text("refund-desk/v1.0.0.yaml").replace("to: done_refund}", "to: gone}"))
    assert main(["validate", str(bad)]) == 1
    assert "line" in capsys.readouterr().out
    broken = tmp_path / "broken.yaml"
    broken.write_text("kind: Agent")
    assert main(["validate", str(broken)]) == 1
    assert main(["test", f]) == 0
    assert "9/9 tests passed" in capsys.readouterr().out
    assert main(["test", str(bad)]) == 1
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert main(["check", str(AGENTS)]) == 0
    assert "| agents" in summary.read_text() or "refund-desk 1.0.0" in summary.read_text()
    assert main(["check", str(tmp_path)]) == 1
    assert main(["check", str(tmp_path / "empty")]) == 1


def test_cli_graph_diff_run_demo(capsys: pytest.CaptureFixture[str], tmp_path: Path, text: Text) -> None:
    v10, v11 = str(AGENTS / "it-helpdesk" / "v1.0.0.yaml"), str(AGENTS / "it-helpdesk" / "v1.1.0.yaml")
    assert main(["graph", v10]) == 0 and "flowchart TD" in capsys.readouterr().out
    assert main(["diff", v10, v11]) == 0 and "kb.search" in capsys.readouterr().out
    risky = tmp_path / "risky.yaml"
    risky.write_text(
        text("it-helpdesk/v1.1.0.yaml").replace(
            '    - use: directory.reset_password\n      approval: "user.privileged == true"\n',
            "    - use: directory.reset_password\n",
        )
    )
    assert main(["diff", v11, str(risky), "--fail-on-high", "--json"]) == 1
    assert '"high_risk": true' in capsys.readouterr().out
    refund = str(AGENTS / "refund-desk" / "v1.0.0.yaml")
    assert main(["run", refund, "--input", '{"message": "O-5002 arrived broken"}', "--approve"]) == 0
    out = capsys.readouterr().out
    assert "approval requested" in out and "189.00 EUR" in out
    assert main(["run", refund, "--input", '{"message": "O-5002 broken"}', "--json"]) == 0
    assert '"awaiting_approval"' in capsys.readouterr().out
    assert main(["run", refund, "--input", "not json"]) == 2
    assert main(["run", str(tmp_path / "missing.yaml"), "--input", "{}"]) == 2
    assert main(["demo", str(AGENTS)]) == 0
    demo = capsys.readouterr().out
    assert "must be promoted to staging before prod" in demo
    assert "approval removed from directory.reset_password" in demo
    assert "your account is unlocked" in demo
