# Threat model

The studio lets people who don't write code define agents that take real actions. The
threats are a spec doing more than its author or reviewer understood, a spec doing more
than the platform allows, and a release process that can be bypassed.

## Trust boundaries

| Boundary | Trusted | Untrusted |
|---|---|---|
| Gateway → API | the gateway that authenticated the user and set their roles | anyone else (`STUDIO_SERVICE_TOKEN`) |
| Spec → platform | the catalog of tools, the expression interpreter, the runtime | everything in a spec |
| Run input → agent | the input schema | the content of every input field |

## Threats and controls

| Threat | Example | Control | Tested by |
|---|---|---|---|
| Code execution through a spec | `when: "__import__('os').system('id')"` | Whitelisted AST, own interpreter, no `eval`; rejected at validation | `test_rejects_unsafe_or_invalid` |
| Resource exhaustion through expressions | `'a' * 10**9`, deeply nested expressions | No `**`, `*` only on numbers, string growth capped, length and node-count limits | `test_expr.py` |
| Regex denial of service | catastrophic patterns in `extract` | Pattern length cap, one group, input truncated to 10,000 characters | analyzer tests |
| A spec calling tools it shouldn't | a support agent issuing refunds | Tools must be declared; the diff flags new write tools as risks | `test_tool_checks`, risk tests |
| Side effects beyond policy | refunding 640 EUR | Argument limits checked before the call; approval rules pause the run | refund-desk tests |
| A risky change slipping through review | removing an approval rule while every test still passes | Risk review on prod promotion, explicit acknowledgement, recorded in the audit log | `test_high_risk_changes_need_acknowledgement` |
| Bypassing the release process | promoting straight to prod, untested agents in prod, overwriting a published version | Roles per environment, staging-before-prod, test gate, immutable versions | `test_promotion_rules`, `test_publish_is_immutable_and_ordered` |
| A run changing under the approver | a new version promoted while a refund waits | Runs are pinned to a version and resume on it | `test_runs_and_approvals_are_pinned_to_a_version` |
| Model output steering the agent | a ticket persuading the classifier | A model can only pick a declared label or write text; routes and tool calls stay deterministic | `test_generate_and_classify_with_a_model` |
| Infinite loops | an edge back to itself | Step limit and tool budget per run; the analyzer warns about loops | `test_step_limit_stops_loops`, `test_tool_budget` |
| Forged roles | a caller sending `X-User-Roles: release-manager` | Service token required for identity assertion; unknown roles rejected | `test_identity_checks` |
| Cross-site scripting in the studio page | a spec description containing markup | The page writes data with `textContent`; Mermaid runs with `securityLevel: strict`; CSP header | page code |
| Container escape and lateral movement | a compromised pod | Non-root, read-only root filesystem, no capabilities, no service-account token, restricted Pod Security, NetworkPolicy | manifests validated in CI |

## OWASP Top 10 for LLM applications (2025)

| Risk | Control |
|---|---|
| LLM01 Prompt injection | Models only classify into declared labels or rewrite text; they never pick routes or tool calls |
| LLM05 Improper output handling | Model output is constrained to a label set or a text field; the page renders text, not HTML |
| LLM06 Excessive agency | Declared tools only, argument limits, approval rules, budgets, risk review on promotion |
| LLM10 Unbounded consumption | Step and tool-call limits; input length limits |

## Out of scope

- Authenticating users (the gateway does that).
- The real systems behind the tools; the catalog here is a simulation.
- Exactly-once tool execution across crashes (see the README's known limits).
