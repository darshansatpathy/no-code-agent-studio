# ADR-0002: A whitelisted expression language, interpreted

**Status:** accepted

## Context

Specs need conditions (`order.total > 100`), arguments and templates. Options were a
template engine, a third-party expression library, JSONLogic, or Python expressions.

## Decision

Use Python expression syntax, which authors find readable, parsed with `ast` and checked
node by node against a whitelist at validation time, then evaluated by a small interpreter.
Names resolve only to state fields, `input` and `args`; attribute access reads only from
mappings; calls are limited to a dozen pure functions.

## Alternatives considered

- **`eval` with restricted globals.** Known to be escapable.
- **Jinja2 sandbox.** Sandboxed, but much larger surface and harder to analyze for which
  fields are read.
- **JSONLogic.** Safe, but unreadable for conditions of any length.

## Consequences

- The analyzer knows exactly which fields each expression reads, so it can report a typo
  as an error with a line number.
- The language is small on purpose; extending it means adding a function to the whitelist,
  with tests.
