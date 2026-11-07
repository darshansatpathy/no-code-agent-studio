"""HTTP API and the studio page.

Identity is asserted by the gateway in front of the service: `X-User-Id` and
`X-User-Roles` (author, reviewer, release-manager, approver, operator). With
`STUDIO_SERVICE_TOKEN` set, calls must also carry `Authorization: Bearer <token>`.
"""

from __future__ import annotations

import hmac
import re
import threading
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from agentstudio import __version__
from agentstudio.analyzer import analyze
from agentstudio.catalog import CATALOG
from agentstudio.compiler import to_mermaid
from agentstudio.config import Settings, Wiring, build
from agentstudio.observability import configure_logging, event, log
from agentstudio.registry import ENVIRONMENTS, RegistryError
from agentstudio.runtime import InputError, RunNotPaused
from agentstudio.spec import SpecError, load_spec
from agentstudio.testing import run_tests

ROLES = {"author", "reviewer", "release-manager", "approver", "operator"}
_PAGE = Path(__file__).resolve().parent / "web" / "studio.html"


class SpecIn(BaseModel):
    yaml: str = Field(min_length=1, max_length=256_000)
    note: str = Field(default="", max_length=500)


class PromoteIn(BaseModel):
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    env: str
    acknowledge_risk: bool = False


class RunIn(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)
    env: str = "prod"
    version: str | None = Field(default=None, pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")


class ApprovalIn(BaseModel):
    approved: bool


class Caller(BaseModel):
    user: str
    roles: frozenset[str]


def create_app(wiring: Wiring | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    wired = wiring or build(settings)
    registry, runtime = wired.registry, wired.runtime
    lock = threading.Lock()
    counters: Counter[str] = Counter()
    if not settings.service_token:
        log.warning("STUDIO_SERVICE_TOKEN is not set: identity headers are trusted (development mode)")
    app = FastAPI(title="no-code-agent-studio", version=__version__)

    @app.exception_handler(RegistryError)
    async def registry_error(_: Request, exc: RegistryError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(SpecError)
    async def spec_error(_: Request, exc: SpecError) -> JSONResponse:
        return JSONResponse({"detail": "spec has errors", "issues": [i.to_dict() for i in exc.issues]}, status_code=422)

    @app.middleware("http")
    async def request_id(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", rid):
            rid = uuid.uuid4().hex
        started = time.perf_counter()
        response = await call_next(request)
        route = getattr(request.scope.get("route"), "path", "unmatched")
        counters[f'studio_http_requests_total{{route="{route}",status="{response.status_code}"}}'] += 1
        counters[f'studio_http_request_ms_sum{{route="{route}"}}'] += int((time.perf_counter() - started) * 1000)
        response.headers["X-Request-ID"] = rid
        return response

    def caller(
        authorization: str | None = Header(default=None),
        x_user_id: str | None = Header(default=None),
        x_user_roles: str | None = Header(default=None),
    ) -> Caller:
        if settings.service_token:
            presented = (authorization or "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(presented.encode(), settings.service_token.encode()):
                raise HTTPException(status_code=401, detail="missing or invalid service token")
        roles = frozenset(r.strip().lower() for r in (x_user_roles or "").split(",") if r.strip())
        unknown = roles - ROLES
        if unknown:
            raise HTTPException(status_code=400, detail=f"unknown role(s): {', '.join(sorted(unknown))}")
        return Caller(user=(x_user_id or "anonymous")[:80], roles=roles)

    def need(c: Caller, role: str) -> None:
        if role not in c.roles:
            raise HTTPException(status_code=403, detail=f"needs the {role} role")

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/studio")

    @app.get("/studio", include_in_schema=False)
    def studio() -> HTMLResponse:
        return HTMLResponse(
            _PAGE.read_text(encoding="utf-8"),
            headers={
                "Content-Security-Policy": "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:"
            },
        )

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "agents": len(registry.agents())}

    @app.get("/metrics")
    def metrics() -> Response:
        lines = [f"{k} {v}" for k, v in sorted(counters.items())]
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    @app.get("/v1/catalog")
    def catalog(_: Caller = Depends(caller)) -> dict[str, Any]:  # noqa: B008
        return {"tools": [t.describe() for t in CATALOG.values()]}

    @app.post("/v1/specs/validate")
    def validate(body: SpecIn, _: Caller = Depends(caller)) -> dict[str, Any]:  # noqa: B008
        try:
            spec, lines = load_spec(body.yaml)
        except SpecError as exc:
            return {"ok": False, "issues": [i.to_dict() for i in exc.issues]}
        analysis = analyze(spec, lines)
        return {
            "ok": analysis.ok,
            "issues": [i.to_dict() for i in analysis.issues],
            "name": spec.name,
            "version": spec.version,
            "content_hash": spec.content_hash(),
            "mermaid": to_mermaid(spec),
        }

    @app.post("/v1/specs/test")
    def test_spec(body: SpecIn, _: Caller = Depends(caller)) -> dict[str, Any]:  # noqa: B008
        spec, lines = load_spec(body.yaml)
        analysis = analyze(spec, lines)
        if not analysis.ok:
            raise SpecError(analysis.errors)
        return run_tests(spec).to_dict()

    @app.post("/v1/agents", status_code=201)
    def publish(body: SpecIn, c: Caller = Depends(caller)) -> dict[str, Any]:  # noqa: B008
        need(c, "author")
        with lock:
            record, report, created = registry.publish(body.yaml, actor=c.user, roles=c.roles, note=body.note)
        event("publish", agent=record.name, version=record.version, actor=c.user, created=created)
        return {"created": created, "version": record.to_dict(), "tests": report.to_dict()}

    @app.get("/v1/agents")
    def agents(_: Caller = Depends(caller)) -> dict[str, Any]:  # noqa: B008
        return {"agents": registry.agents(), "environments": list(ENVIRONMENTS)}

    @app.get("/v1/agents/{name}")
    def agent(name: str, _: Caller = Depends(caller)) -> dict[str, Any]:  # noqa: B008
        versions = registry.versions(name)
        if not versions:
            raise HTTPException(status_code=404, detail=f"agent {name} not found")
        return {"name": name, "environments": registry.environments(name), "versions": [v.to_dict() for v in versions]}

    @app.get("/v1/agents/{name}/versions/{version}")
    def version(name: str, version: str, _: Caller = Depends(caller)) -> dict[str, Any]:  # noqa: B008
        record = registry.version(name, version)
        return {**record.to_dict(with_spec=True), "mermaid": to_mermaid(record.spec())}

    @app.get("/v1/agents/{name}/diff")
    def diff(name: str, old: str, new: str, _: Caller = Depends(caller)) -> dict[str, Any]:  # noqa: B008
        d = registry.diff(name, old, new)
        return {**d.to_dict(), "markdown": d.markdown()}

    @app.post("/v1/agents/{name}/promote")
    def promote(name: str, body: PromoteIn, c: Caller = Depends(caller)) -> dict[str, Any]:  # noqa: B008
        with lock:
            result = registry.promote(
                name, body.version, body.env, actor=c.user, roles=c.roles, acknowledge_risk=body.acknowledge_risk
            )
        event("promote", agent=name, version=body.version, env=body.env, actor=c.user)
        return result

    @app.get("/v1/agents/{name}/audit")
    def audit(name: str, _: Caller = Depends(caller)) -> dict[str, Any]:  # noqa: B008
        return {"audit": registry.audit(name)}

    @app.post("/v1/agents/{name}/runs")
    def run(name: str, body: RunIn, c: Caller = Depends(caller)) -> dict[str, Any]:  # noqa: B008
        need(c, "operator")
        if body.env not in ENVIRONMENTS:
            raise HTTPException(status_code=400, detail=f"unknown environment {body.env!r}")
        record = registry.resolve(name, env=body.env, version=body.version)
        spec = record.spec()
        run_id = f"run-{uuid.uuid4().hex[:16]}"
        with lock:
            registry.record_run(run_id, name, record.version, body.env if not body.version else "pinned", c.user)
            try:
                result = runtime.start(spec, body.input, run_id=run_id)
            except InputError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
        counters[f'studio_runs_total{{agent="{name}",status="{result.status}"}}'] += 1
        event("run", agent=name, version=record.version, run_id=run_id, status=result.status, path=result.path)
        return result.to_dict()

    def _spec_for(run_id: str) -> Any:
        info = registry.run_record(run_id)
        return registry.version(info["name"], info["version"]).spec()

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str, c: Caller = Depends(caller)) -> dict[str, Any]:  # noqa: B008
        if not c.roles & {"operator", "approver"}:
            raise HTTPException(status_code=403, detail="needs the operator or approver role")
        return runtime.get(_spec_for(run_id), run_id).to_dict()

    @app.post("/v1/runs/{run_id}/approval")
    def approve(run_id: str, body: ApprovalIn, c: Caller = Depends(caller)) -> dict[str, Any]:  # noqa: B008
        need(c, "approver")
        spec = _spec_for(run_id)
        with lock:
            try:
                result = runtime.resume(spec, run_id, approved=body.approved, approver=c.user)
            except RunNotPaused as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        counters[f'studio_approvals_total{{decision="{"approved" if body.approved else "denied"}"}}'] += 1
        event("approval", run_id=run_id, approved=body.approved, approver=c.user)
        return result.to_dict()

    app.state.wiring = wired
    return app
