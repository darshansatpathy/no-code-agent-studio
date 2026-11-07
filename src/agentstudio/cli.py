"""`studio` command line: check, validate, test, graph, diff, run, demo, serve."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from agentstudio.analyzer import analyze
from agentstudio.compiler import to_mermaid
from agentstudio.config import ConfigError, Settings, bootstrap
from agentstudio.diffing import diff
from agentstudio.registry import Registry, RegistryError
from agentstudio.runtime import InputError, Runtime
from agentstudio.spec import AgentSpec, SpecError, load_spec
from agentstudio.testing import run_tests

ALL_ROLES = frozenset({"author", "reviewer", "release-manager"})


def _load(path: str) -> tuple[AgentSpec, list[str]]:
    text = Path(path).read_text(encoding="utf-8")
    spec, lines = load_spec(text)
    analysis = analyze(spec, lines)
    return spec, [str(i) for i in analysis.issues] if not analysis.ok else [str(i) for i in analysis.warnings]


def cmd_validate(args: argparse.Namespace) -> int:
    worst = 0
    for path in args.files:
        try:
            spec, lines = load_spec(Path(path).read_text(encoding="utf-8"))
        except SpecError as exc:
            print(f"{path}: invalid")
            for i in exc.issues:
                print(f"  {i}")
            worst = 1
            continue
        analysis = analyze(spec, lines)
        print(
            f"{path}: {spec.name} {spec.version} {'ok' if analysis.ok else 'has errors'}"
            f" ({len(analysis.errors)} errors, {len(analysis.warnings)} warnings)"
        )
        for i in analysis.issues:
            print(f"  {i}")
        if not analysis.ok or (args.strict and analysis.warnings):
            worst = 1
    return worst


def cmd_test(args: argparse.Namespace) -> int:
    worst = 0
    for path in args.files:
        spec, _ = load_spec(Path(path).read_text(encoding="utf-8"))
        if not analyze(spec).ok:
            print(f"{path}: has errors; run `studio validate`")
            worst = 1
            continue
        report = run_tests(spec)
        print(f"{spec.name} {spec.version}: {report.passed}/{report.total} tests passed")
        for r in report.results:
            print(f"  {'pass' if r.passed else 'FAIL'}  {r.name}")
            for f in r.failures:
                print(f"        {f}")
        if not report.ok:
            worst = 1
    return worst


def cmd_check(args: argparse.Namespace) -> int:
    """Validate and test every spec in a folder: the CI gate for a repository of agents."""
    files = sorted(str(p) for p in Path(args.folder).rglob("*.yaml"))
    if not files:
        print(f"no specs under {args.folder}", file=sys.stderr)
        return 1
    rows, failed = [], False
    for path in files:
        try:
            spec, lines = load_spec(Path(path).read_text(encoding="utf-8"))
        except SpecError as exc:
            rows.append((path, "?", "invalid", f"{len(exc.issues)} errors", "-"))
            failed = True
            continue
        analysis = analyze(spec, lines)
        report = run_tests(spec) if analysis.ok else None
        ok = analysis.ok and report is not None and report.ok
        failed |= not ok
        rows.append(
            (
                path,
                f"{spec.name} {spec.version}",
                "ok" if analysis.ok else "errors",
                f"{len(analysis.errors)}/{len(analysis.warnings)}",
                f"{report.passed}/{report.total}" if report else "-",
            )
        )
    md = ["| Spec | Agent | Analysis | Errors / warnings | Tests |", "|---|---|---|---|---|"]
    md += [f"| {a} | {b} | {c} | {d} | {e} |" for a, b, c, d, e in rows]
    text = "\n".join(md)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("## Agent specs\n\n" + text + "\n")
    return 1 if failed else 0


def cmd_graph(args: argparse.Namespace) -> int:
    spec, _ = load_spec(Path(args.file).read_text(encoding="utf-8"))
    print(to_mermaid(spec))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    old, _ = load_spec(Path(args.old).read_text(encoding="utf-8"))
    new, _ = load_spec(Path(args.new).read_text(encoding="utf-8"))
    d = diff(old, new)
    print(json.dumps(d.to_dict(), indent=2) if args.json else d.markdown())
    return 1 if (args.fail_on_high and d.high_risk) else 0


def cmd_run(args: argparse.Namespace) -> int:
    spec, problems = _load(args.file)
    if not analyze(spec).ok:
        print("\n".join(problems), file=sys.stderr)
        return 1
    runtime = Runtime()
    try:
        result = runtime.start(spec, json.loads(args.input))
    except (InputError, json.JSONDecodeError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 2
    if result.status == "awaiting_approval" and (args.approve or args.deny):
        print(f"approval requested: {result.pending['summary'] if result.pending else ''}")
        result = runtime.resume(spec, result.run_id, approved=args.approve, approver="cli")
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        print(f"status: {result.status}")
        print(f"path:   {' -> '.join(result.path)}")
        for c in result.calls:
            print(f"call:   {c['tool']} {json.dumps(c['args'])} -> {c['status']}")
        if result.pending:
            print(f"waiting for approval: {result.pending['summary']}")
        if result.reply:
            print(f"reply:  {result.reply}")
        if result.error:
            print(f"error:  {result.error}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    folder = Path(args.folder)
    reg = Registry.sqlite()
    print("Publishing every spec in", folder)
    for item in bootstrap(reg, folder):
        print("  published", item)
    print("\nPromoting it-helpdesk 1.0.0 through dev, staging, prod")
    for env in ("dev", "staging", "prod"):
        result = reg.promote("it-helpdesk", "1.0.0", env, actor="demo", roles=ALL_ROLES)
        print(f"  {env}: 1.0.0" + (f" (tests {result['tests']})" if result["tests"] else ""))
    print("\nTrying to put 1.1.0 straight into prod:")
    try:
        reg.promote("it-helpdesk", "1.1.0", "prod", actor="demo", roles=ALL_ROLES)
    except RegistryError as exc:
        print("  refused:", exc)
    reg.promote("it-helpdesk", "1.1.0", "staging", actor="demo", roles=ALL_ROLES)
    reg.promote("it-helpdesk", "1.1.0", "prod", actor="demo", roles=ALL_ROLES)
    print("  after staging: prod is", reg.environments("it-helpdesk")["prod"])
    print("\nWhat changed from 1.0.0 to 1.1.0:")
    print(reg.diff("it-helpdesk", "1.0.0", "1.1.0").markdown())
    v11 = reg.version("it-helpdesk", "1.1.0").spec_yaml
    risky = v11.replace("version: 1.1.0", "version: 1.2.0").replace(
        '    - use: directory.reset_password\n      approval: "user.privileged == true"\n',
        "    - use: directory.reset_password\n",
    )
    reg.publish(risky, actor="demo", note="remove approval (demo of the risk gate)")
    reg.promote("it-helpdesk", "1.2.0", "staging", actor="demo", roles=ALL_ROLES)
    print("\nA 1.2.0 that drops the approval on password resets passes its tests, but prod says:")
    try:
        reg.promote("it-helpdesk", "1.2.0", "prod", actor="demo", roles=ALL_ROLES)
    except RegistryError as exc:
        print("  refused:", exc)
    print("\nRunning the prod version for a locked-out user:")
    spec = reg.resolve("it-helpdesk", env="prod").spec()
    run = Runtime().start(spec, {"user_id": "U-100", "message": "I am locked out"})
    print(f"  {run.status}: {' -> '.join(run.path)}")
    print(f"  {run.reply}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="studio", description="Declarative agents compiled to LangGraph")
    sub = p.add_subparsers(dest="command", required=True)
    v = sub.add_parser("validate", help="parse and analyze specs")
    v.add_argument("files", nargs="+")
    v.add_argument("--strict", action="store_true", help="fail on warnings too")
    t = sub.add_parser("test", help="run each spec's tests")
    t.add_argument("files", nargs="+")
    c = sub.add_parser("check", help="validate and test every spec in a folder (CI gate)")
    c.add_argument("folder", nargs="?", default="agents")
    g = sub.add_parser("graph", help="print the graph as Mermaid")
    g.add_argument("file")
    d = sub.add_parser("diff", help="compare two versions, with a risk review")
    d.add_argument("old")
    d.add_argument("new")
    d.add_argument("--json", action="store_true")
    d.add_argument("--fail-on-high", action="store_true", help="exit 1 when a high-risk change is found")
    r = sub.add_parser("run", help="run a spec once")
    r.add_argument("file")
    r.add_argument("--input", required=True, help="JSON object")
    group = r.add_mutually_exclusive_group()
    group.add_argument("--approve", action="store_true")
    group.add_argument("--deny", action="store_true")
    r.add_argument("--json", action="store_true")
    m = sub.add_parser("demo", help="publish the examples and walk through promotion")
    m.add_argument("folder", nargs="?", default="agents")
    s = sub.add_parser("serve", help="run the API and studio page")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":  # pragma: no cover - runs a server
        import uvicorn

        from agentstudio.api import create_app

        try:
            settings = Settings.from_env()
        except ConfigError as exc:
            print(f"configuration error: {exc}", file=sys.stderr)
            return 2
        uvicorn.run(create_app(settings=settings), host=args.host, port=args.port, log_level="warning")
        return 0
    handlers = {
        "validate": cmd_validate,
        "test": cmd_test,
        "check": cmd_check,
        "graph": cmd_graph,
        "diff": cmd_diff,
        "run": cmd_run,
        "demo": cmd_demo,
    }
    try:
        return handlers[args.command](args)
    except SpecError as exc:
        print("\n".join(str(i) for i in exc.issues), file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
