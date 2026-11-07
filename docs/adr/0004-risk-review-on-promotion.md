# ADR-0004: A risk review on promotion, not only tests

**Status:** accepted

## Context

Spec tests show an agent does what its author expected. They do not show what was removed:
if an approval rule disappears, every existing test can still pass.

## Decision

Promotion to prod diffs the candidate against the current prod version and classifies each
change. Changes that widen what the agent may do (write tools added, approvals removed,
limits removed or raised, allowed values widened) are high risk and block the promotion
until a release manager acknowledges them. The acknowledgement is recorded in the audit log
with the findings.

## Consequences

- The example `1.2.0` that removes an approval passes all ten tests and is still stopped.
- The rules are conservative and structural; they flag, they don't prove. Medium findings
  are shown to reviewers but don't block.
