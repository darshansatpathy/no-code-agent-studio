# Contributing

## Set up

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
make check
```

`make check` runs lint, formatting, strict types, the tests with a coverage gate, and
`studio check agents`. PostgreSQL tests run when `STUDIO_TEST_DATABASE_URL` is set.

## Changing the spec language

The language is closed on purpose. For a new node type, field or expression function:

1. Add it to the models in `spec.py` with `extra="forbid"` semantics kept.
2. Teach the analyzer to catch its misuse, with a test that shows the error and its line.
3. Teach the diff whether a change to it widens what an agent can do.
4. Document it in `docs/spec-reference.md`.

## Adding a tool

Register it in `catalog.py` with a strict Pydantic argument model and the right `kind`
(`read` or `write`). Write tools are what the risk review watches, so get this right.

## Changing an example agent

Bump its version and add a new file next to the old one; published versions are immutable
here too. `studio check agents` must pass.

## Commits

Small commits with a message that says what changed and why. Update `CHANGELOG.md` under
an "Unreleased" heading.
