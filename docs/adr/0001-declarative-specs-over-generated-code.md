# ADR-0001: Declarative specs, not generated code

**Status:** accepted

## Context

"No-code" agent builders often generate Python from a canvas, or let authors paste code
into nodes. Either way the platform can no longer say what an agent is able to do without
reading the code.

## Decision

Agents are YAML documents in a closed language: six node types, ordered edges with
conditions, tools referenced by name from a catalog the platform owns, and a small
expression language. No code is ever accepted from a spec.

## Consequences

- Everything about an agent's capabilities is visible in the spec and checkable by the
  analyzer: which tools, with which limits, under which approval rules.
- Diffs between versions are structural and meaningful, which makes the risk review possible.
- Some agents cannot be expressed. New capabilities arrive as new tools or node types,
  reviewed once by the platform team, rather than as code in each spec.
