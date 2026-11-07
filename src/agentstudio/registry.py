"""The agent registry: immutable versions, environments, promotion rules, audit trail.

- A version is immutable. Publishing the same name and version with different content is
  refused; publishing identical content again is a no-op.
- Versions only go up (semantic versioning).
- Environments `dev`, `staging` and `prod` each point at one version per agent.
- Promotion rules: dev takes any valid version; staging needs every spec test passing;
  prod needs the version to have been in staging, tests passing, the release role, and an
  explicit acknowledgement when the diff against the current prod version is high risk.
- Every publish and promotion is written to the audit log with the actor.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

from agentstudio.analyzer import analyze
from agentstudio.diffing import Diff, diff
from agentstudio.spec import AgentSpec, SpecError, load_spec
from agentstudio.testing import TestReport, run_tests

ENVIRONMENTS = ("dev", "staging", "prod")
ROLE_FOR = {"publish": "author", "dev": "author", "staging": "reviewer", "prod": "release-manager"}


class RegistryError(ValueError):
    status = 400


class NotFound(RegistryError):
    status = 404


class Conflict(RegistryError):
    status = 409


class Forbidden(RegistryError):
    status = 403


class GateFailed(RegistryError):
    status = 422


def _semver(v: str) -> tuple[int, int, int]:
    a, b, c = (int(x) for x in v.split("."))
    return a, b, c


@dataclass(frozen=True)
class VersionRecord:
    name: str
    version: str
    content_hash: str
    body_hash: str
    created_at: float
    created_by: str
    note: str
    tests_passed: int
    tests_total: int
    warnings: int
    spec_yaml: str

    def to_dict(self, with_spec: bool = False) -> dict[str, Any]:
        data = {k: v for k, v in self.__dict__.items() if k != "spec_yaml"}
        if with_spec:
            data["spec_yaml"] = self.spec_yaml
        return data

    def spec(self) -> AgentSpec:
        return load_spec(self.spec_yaml)[0]


_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS agent_versions (
        name TEXT NOT NULL, version TEXT NOT NULL, content_hash TEXT NOT NULL, body_hash TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL, created_by TEXT NOT NULL, note TEXT NOT NULL,
        tests_passed INTEGER NOT NULL, tests_total INTEGER NOT NULL, warnings INTEGER NOT NULL,
        spec_yaml TEXT NOT NULL, PRIMARY KEY (name, version))""",
    """CREATE TABLE IF NOT EXISTS agent_environments (
        name TEXT NOT NULL, env TEXT NOT NULL, version TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL,
        updated_by TEXT NOT NULL, PRIMARY KEY (name, env))""",
    """CREATE TABLE IF NOT EXISTS agent_runs (
        run_id TEXT PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL, env TEXT NOT NULL,
        started_by TEXT NOT NULL, started_at DOUBLE PRECISION NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS agent_audit (
        seq {serial}, at DOUBLE PRECISION NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
        name TEXT NOT NULL, version TEXT NOT NULL, env TEXT NOT NULL, detail TEXT NOT NULL)""",
]


class Registry:
    def __init__(self, conn: Any, dialect: str = "sqlite") -> None:
        self._conn = conn
        self._dialect = dialect
        self._lock = threading.RLock()
        serial = "INTEGER PRIMARY KEY AUTOINCREMENT" if dialect == "sqlite" else "BIGSERIAL PRIMARY KEY"
        with self._lock:
            for stmt in _SCHEMA:
                self._conn.execute(stmt.format(serial=serial))
            self._conn.commit()

    @classmethod
    def sqlite(cls, path: str = ":memory:") -> Registry:
        return cls(sqlite3.connect(path, check_same_thread=False), "sqlite")

    @classmethod
    def postgres(cls, dsn: str) -> Registry:
        import psycopg

        return cls(psycopg.connect(dsn), "postgres")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ SQL plumbing

    def _q(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        if self._dialect != "sqlite":
            sql = sql.replace("?", "%s")
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
            self._conn.commit()
            return list(rows)

    def _audit(self, actor: str, action: str, name: str, version: str, env: str, detail: dict[str, Any]) -> None:
        self._q(
            "INSERT INTO agent_audit (at, actor, action, name, version, env, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), actor, action, name, version, env, json.dumps(detail, sort_keys=True)),
        )

    # ------------------------------------------------------------------ reads

    def versions(self, name: str) -> list[VersionRecord]:
        rows = self._q(
            "SELECT name, version, content_hash, body_hash, created_at, created_by, note, tests_passed, "
            "tests_total, warnings, spec_yaml FROM agent_versions WHERE name = ?",
            (name,),
        )
        records = [VersionRecord(*r) for r in rows]
        return sorted(records, key=lambda r: _semver(r.version))

    def version(self, name: str, version: str) -> VersionRecord:
        rows = self._q(
            "SELECT name, version, content_hash, body_hash, created_at, created_by, note, tests_passed, "
            "tests_total, warnings, spec_yaml FROM agent_versions WHERE name = ? AND version = ?",
            (name, version),
        )
        if not rows:
            raise NotFound(f"{name} {version} is not published")
        return VersionRecord(*rows[0])

    def agents(self) -> list[dict[str, Any]]:
        names = sorted({r[0] for r in self._q("SELECT name FROM agent_versions")})
        out = []
        for name in names:
            versions = self.versions(name)
            out.append(
                {
                    "name": name,
                    "latest": versions[-1].version,
                    "versions": len(versions),
                    "environments": self.environments(name),
                    "description": versions[-1].spec().metadata.description,
                }
            )
        return out

    def environments(self, name: str) -> dict[str, str]:
        return {env: ver for env, ver in self._q("SELECT env, version FROM agent_environments WHERE name = ?", (name,))}

    def resolve(self, name: str, *, env: str | None = None, version: str | None = None) -> VersionRecord:
        if version:
            return self.version(name, version)
        env = env or "prod"
        pinned = self.environments(name).get(env)
        if pinned is None:
            raise NotFound(f"{name} has no version in {env}")
        return self.version(name, pinned)

    def audit(self, name: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT seq, at, actor, action, name, version, env, detail FROM agent_audit"
        rows = self._q(sql + (" WHERE name = ?" if name else "") + " ORDER BY seq DESC", (name,) if name else ())
        return [
            {
                "seq": r[0],
                "at": r[1],
                "actor": r[2],
                "action": r[3],
                "name": r[4],
                "version": r[5],
                "env": r[6],
                "detail": json.loads(r[7]),
            }
            for r in rows[:limit]
        ]

    def record_run(self, run_id: str, name: str, version: str, env: str, actor: str) -> None:
        self._q(
            "INSERT INTO agent_runs (run_id, name, version, env, started_by, started_at) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, name, version, env, actor, time.time()),
        )

    def run_record(self, run_id: str) -> dict[str, Any]:
        rows = self._q(
            "SELECT run_id, name, version, env, started_by, started_at FROM agent_runs WHERE run_id = ?", (run_id,)
        )
        if not rows:
            raise NotFound(f"run {run_id} not found")
        r = rows[0]
        return {"run_id": r[0], "name": r[1], "version": r[2], "env": r[3], "started_by": r[4], "started_at": r[5]}

    def diff(self, name: str, old: str, new: str) -> Diff:
        return diff(self.version(name, old).spec(), self.version(name, new).spec())

    # ------------------------------------------------------------------ writes

    def publish(
        self, spec_yaml: str, *, actor: str, roles: set[str] | frozenset[str] = frozenset({"author"}), note: str = ""
    ) -> tuple[VersionRecord, TestReport, bool]:
        """Validate, test and store a new version. Returns (record, test report, created)."""
        if "author" not in roles:
            raise Forbidden("publishing needs the author role")
        spec, lines = load_spec(spec_yaml)
        analysis = analyze(spec, lines)
        if not analysis.ok:
            raise SpecError(analysis.errors)
        existing = [r for r in self.versions(spec.name)]
        same = next((r for r in existing if r.version == spec.version), None)
        report = run_tests(spec)
        if same is not None:
            if same.content_hash == spec.content_hash():
                return same, report, False
            raise Conflict(f"{spec.name} {spec.version} is already published with different content; bump the version")
        if existing and _semver(spec.version) <= _semver(existing[-1].version):
            raise Conflict(f"version {spec.version} is not newer than {existing[-1].version}")
        record = VersionRecord(
            spec.name,
            spec.version,
            spec.content_hash(),
            spec.body_hash(),
            time.time(),
            actor,
            note[:500],
            report.passed,
            report.total,
            len(analysis.warnings),
            spec_yaml,
        )
        self._q(
            "INSERT INTO agent_versions (name, version, content_hash, body_hash, created_at, created_by, note, "
            "tests_passed, tests_total, warnings, spec_yaml) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.name,
                record.version,
                record.content_hash,
                record.body_hash,
                record.created_at,
                record.created_by,
                record.note,
                record.tests_passed,
                record.tests_total,
                record.warnings,
                record.spec_yaml,
            ),
        )
        self._audit(
            actor,
            "publish",
            spec.name,
            spec.version,
            "",
            {
                "hash": record.content_hash,
                "tests": f"{report.passed}/{report.total}",
                "warnings": record.warnings,
                "same_body_as_previous": bool(existing) and existing[-1].body_hash == record.body_hash,
            },
        )
        return record, report, True

    def promote(
        self,
        name: str,
        version: str,
        env: str,
        *,
        actor: str,
        roles: set[str] | frozenset[str],
        acknowledge_risk: bool = False,
    ) -> dict[str, Any]:
        if env not in ENVIRONMENTS:
            raise RegistryError(f"unknown environment {env!r}")
        if ROLE_FOR[env] not in roles:
            raise Forbidden(f"promoting to {env} needs the {ROLE_FOR[env]} role")
        record = self.version(name, version)
        spec = record.spec()
        report: TestReport | None = None
        if env in {"staging", "prod"}:
            report = run_tests(spec)
            if not report.ok:
                failing = [r.name for r in report.results if not r.passed]
                raise GateFailed(
                    f"tests must pass for {env}: {report.passed}/{report.total}"
                    + (f"; failing: {', '.join(failing)}" if failing else "; the spec has no tests")
                )
        risk: Diff | None = None
        if env == "prod":
            was_staged = any(
                a["action"] == "promote" and a["env"] == "staging" and a["version"] == version
                for a in self.audit(name, limit=10_000)
            )
            if not was_staged:
                raise GateFailed(f"{name} {version} must be promoted to staging before prod")
            current = self.environments(name).get("prod")
            if current and current != version:
                risk = diff(self.version(name, current).spec(), spec)
                if risk.high_risk and not acknowledge_risk:
                    findings = "; ".join(r.message for r in risk.risks if r.level == "high")
                    raise GateFailed(
                        f"high-risk changes since prod {current}: {findings}. "
                        "Review them and promote again with acknowledge_risk"
                    )
        previous = self.environments(name).get(env)
        self._q("DELETE FROM agent_environments WHERE name = ? AND env = ?", (name, env))
        self._q(
            "INSERT INTO agent_environments (name, env, version, updated_at, updated_by) VALUES (?, ?, ?, ?, ?)",
            (name, env, version, time.time(), actor),
        )
        detail = {
            "from": previous,
            "tests": f"{report.passed}/{report.total}" if report else None,
            "rollback": previous is not None and _semver(version) < _semver(previous),
            "acknowledged_risks": [r.message for r in risk.risks if r.level == "high"] if risk else [],
        }
        self._audit(actor, "promote", name, version, env, detail)
        return {"name": name, "env": env, "version": version, "previous": previous, **detail}
