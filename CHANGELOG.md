# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-07

### Added

- `studio/v1` spec language: six node types, ordered conditional edges, declared tools with
  approval rules and argument limits, input and state schemas, run limits, tests.
- YAML loading with line-numbered errors; static analysis of structure, graph shape, data
  flow, tool contracts, expressions and tests.
- A whitelisted expression and template language with its own interpreter.
- Compiler to LangGraph state graphs, Mermaid output, runtime with approval pauses,
  checkpoints in memory, SQLite or PostgreSQL.
- Registry with immutable versions, dev/staging/prod environments, promotion gates, rollback,
  audit log, and runs pinned to versions.
- Structural diff between versions with a risk review that blocks high-risk prod promotions
  until acknowledged.
- FastAPI service with roles, a no-build studio page, and the `studio` CLI.
- Example agents: IT helpdesk 1.0.0 and 1.1.0, refund desk 1.0.0.
- Kubernetes manifests (kustomize base, dev and prod overlays), CI deploy to kind, image
  release to GitHub Container Registry on tags.
- CI on Python 3.11 to 3.13 with PostgreSQL, CodeQL, pip-audit, Dependabot.
