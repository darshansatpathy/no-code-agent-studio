# ADR-0005: Models classify and write; they do not route or act

**Status:** accepted

## Context

A model deciding the next step or choosing tool calls is flexible, but then a spec no
longer describes the agent's behaviour, and prompt injection in the input can steer actions.

## Decision

`classify` and `generate` nodes may use a model. A classify model must return one of the
spec's labels (anything else becomes the default label); a generate model writes text into
a state field. Routing and tool calls are always decided by the spec's edges and arguments.

## Consequences

- The graph the reviewer approved is the graph that runs, with or without a model.
- Model mistakes stay inside one field: a wrong label takes a declared route, which the
  tests and limits already cover.
- Open-ended agents that choose their own tools belong in a different design; see
  multi-agent-ops-automation for one with a deterministic executor in front of the model.
