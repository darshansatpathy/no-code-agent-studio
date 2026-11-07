# ADR-0003: Immutable versions, environments as pointers

**Status:** accepted

## Context

Editing an agent in place makes it impossible to say what ran yesterday, to roll back, or
to review a change before it reaches users.

## Decision

A published `(name, version)` is immutable and identified by a content hash; re-publishing
identical content is a no-op and different content under the same version is refused.
Versions only increase. `dev`, `staging` and `prod` are pointers to versions, moved only by
promotion. Runs record the version they started on.

## Consequences

- Rollback is promoting an earlier version, and the audit log says so.
- A run waiting for approval resumes with the version it started with.
- Authors bump versions for every change, as with any release.
