"""Settings from the environment, wiring, and bootstrapping specs from a folder."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from agentstudio.catalog import Systems
from agentstudio.models_io import OpenAICompatibleModel
from agentstudio.observability import event
from agentstudio.registry import ENVIRONMENTS, Registry, RegistryError, _semver
from agentstudio.runtime import Runtime
from agentstudio.spec import SpecError, load_spec


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    store: str = "memory"  # memory | sqlite | postgres
    sqlite_path: str = "studio.db"
    database_url: str | None = None
    service_token: str | None = None
    bootstrap_dir: str | None = None
    bootstrap_promote: tuple[str, ...] = ()
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        store = os.environ.get("STUDIO_STORE", "memory").strip().lower()
        if store not in {"memory", "sqlite", "postgres"}:
            raise ConfigError(f"STUDIO_STORE must be memory, sqlite or postgres (got {store!r})")
        url = os.environ.get("STUDIO_DATABASE_URL") or None
        if store == "postgres" and not url:
            raise ConfigError("STUDIO_STORE=postgres needs STUDIO_DATABASE_URL")
        promote = tuple(e.strip() for e in os.environ.get("STUDIO_BOOTSTRAP_PROMOTE", "").split(",") if e.strip())
        bad = [e for e in promote if e not in ENVIRONMENTS]
        if bad:
            raise ConfigError(f"STUDIO_BOOTSTRAP_PROMOTE has unknown environments: {', '.join(bad)}")
        return cls(
            store=store,
            sqlite_path=os.environ.get("STUDIO_SQLITE_PATH", "studio.db"),
            database_url=url,
            service_token=os.environ.get("STUDIO_SERVICE_TOKEN") or None,
            bootstrap_dir=os.environ.get("STUDIO_BOOTSTRAP_DIR") or None,
            bootstrap_promote=promote,
            log_level=os.environ.get("STUDIO_LOG_LEVEL", "INFO"),
        )


@dataclass
class Wiring:
    registry: Registry
    runtime: Runtime
    closers: list[Callable[[], None]] = field(default_factory=list)

    def close(self) -> None:
        for c in reversed(self.closers):
            c()


def build(settings: Settings | None = None) -> Wiring:
    s = settings or Settings.from_env()
    checkpointer: BaseCheckpointSaver[Any]
    closers: list[Callable[[], None]] = []
    if s.store == "postgres":
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row

        assert s.database_url is not None
        conn: Any = psycopg.connect(s.database_url, autocommit=True, prepare_threshold=0, row_factory=dict_row)
        saver = PostgresSaver(conn)
        saver.setup()
        checkpointer = saver
        registry = Registry.postgres(s.database_url)
        closers += [conn.close, registry.close]
    elif s.store == "sqlite":
        from langgraph.checkpoint.sqlite import SqliteSaver

        sconn = sqlite3.connect(s.sqlite_path, check_same_thread=False)
        checkpointer = SqliteSaver(sconn)
        registry = Registry.sqlite(s.sqlite_path)
        closers += [sconn.close, registry.close]
    else:
        checkpointer, registry = InMemorySaver(), Registry.sqlite(":memory:")
        closers.append(registry.close)
    runtime = Runtime(systems=Systems(), checkpointer=checkpointer, provider=OpenAICompatibleModel.from_env())
    wiring = Wiring(registry, runtime, closers)
    if s.bootstrap_dir:
        bootstrap(registry, Path(s.bootstrap_dir), s.bootstrap_promote)
    return wiring


def bootstrap(registry: Registry, folder: Path, promote: tuple[str, ...] = ()) -> list[str]:
    """Publish every spec in a folder (oldest version first) and promote each agent's latest version."""
    loaded: list[tuple[str, str, str]] = []
    for path in sorted(folder.rglob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        try:
            spec, _ = load_spec(text)
        except SpecError as exc:
            event("bootstrap_skipped", file=str(path), reason=str(exc)[:300])
            continue
        loaded.append((spec.name, spec.version, text))
    loaded.sort(key=lambda t: (t[0], _semver(t[1])))
    published: list[str] = []
    for name, version, text in loaded:
        try:
            _, report, created = registry.publish(text, actor="bootstrap", note="bootstrap")
            if created:
                published.append(f"{name}@{version}")
                event("bootstrap_published", agent=name, version=version, tests=f"{report.passed}/{report.total}")
        except (RegistryError, SpecError) as exc:
            event("bootstrap_skipped", agent=name, version=version, reason=str(exc)[:300])
    roles = frozenset({"author", "reviewer", "release-manager"})
    for name in sorted({n for n, _, _ in loaded}):
        versions = registry.versions(name)
        if not versions:
            continue
        latest = versions[-1].version
        for env in promote:
            if registry.environments(name).get(env) == latest:
                continue
            try:
                registry.promote(name, latest, env, actor="bootstrap", roles=roles)
            except RegistryError as exc:
                event("bootstrap_promote_failed", agent=name, version=latest, env=env, reason=str(exc)[:300])
                break
    return published
