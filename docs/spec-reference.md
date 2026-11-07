# Spec reference (`studio/v1`)

A spec is one YAML document. Unknown fields anywhere are errors, so a typo is caught
rather than ignored.

```yaml
apiVersion: studio/v1
kind: Agent
metadata:
  name: refund-desk          # lowercase, digits and dashes, 2 to 49 characters
  version: 1.0.0             # semantic version; immutable once published
  description: What it does.
  owner: customer-care
  labels: {domain: retail}
spec:
  input: {...}               # what callers send
  state: {...}               # fields the agent reads and writes
  tools: [...]               # tools it may call, with approval rules and limits
  limits: {max_steps: 25, max_tool_calls: 10}
  entry: first_node
  nodes: [...]
  edges: [...]
  tests: [...]
```

## `input`

Field name to type: `string`, `number`, `integer`, `boolean`, `object`, `list`. Runs with
missing, extra or mistyped fields are rejected before anything executes. Read them in
expressions as `input.<field>`.

## `state`

```yaml
state:
  order: {type: object}
  amount: {type: number, default: 0}
```

Every field a node writes must be declared here. Expressions read state fields by name.
`input` and `args` are reserved.

## `tools`

```yaml
tools:
  - use: payments.issue_refund
    approval: "args.amount > 100"
    limits:
      amount: {max: 500}
      reason: {one_of: [damaged, not_received]}
```

- `use`: a tool from the catalog (`GET /v1/catalog` lists them with argument schemas).
- `approval`: an expression over state, `input` and `args`. When it is true, the run pauses
  before the call until a person with the approver role decides.
- `limits`: per argument, any of `min`, `max`, `max_length`, `one_of`. A call outside them is
  never sent; the node fails, or follows `on_error`.

The analyzer warns about a write tool with neither an approval rule nor limits.

## Nodes

| Type | Fields | Does |
|---|---|---|
| `classify` | `input` (expression), `output`, `labels` (label: keywords), `default`, `method` (`keywords` or `llm`) | Sets `output` to the first label whose keyword appears as a whole word, else `default`. With `llm`, a model picks from the labels; anything else becomes `default`. |
| `extract` | `input`, `fields` (state field: regex with one group) | Sets each field to the first match, or null. |
| `tool` | `tool`, `args` (argument: expression), `output`, `on_error` | Calls the tool with the spec's limits, approval rule and the run's tool budget. On failure, denial or a blocked limit, sets `error` (if declared) and goes to `on_error`; without `on_error` the run fails. |
| `set` | `values` (field: expression) | Evaluates in order; later values can use earlier ones. |
| `respond` | `template`, `output` (default `reply`) | Renders the template. |
| `generate` | `prompt`, `output`, `method` (`template` or `llm`) | Renders the prompt; with `llm`, a model rewrites it. |

## Edges

```yaml
edges:
  - {from: lookup, to: not_delivered, when: "order.status != 'delivered'"}
  - {from: lookup, to: amount}
  - {from: done, to: END}
```

Evaluated in order; the first edge whose `when` is true (or that has none) is taken. If
none matches, the run fails, which the analyzer warns about. `END` finishes the run.

## Expressions

Literals (`'text'`, numbers, `true`, `false`, `null`), names (state fields, `input`, `args`),
dotted access into objects (`order.total`), constant indexes (`items[0]`, `data['key']`),
comparisons (`== != < <= > >= in not in`), `and or not`, arithmetic on numbers
(`+ - * / %`), string concatenation with `+`, `x if cond else y`, and these functions:
`len lower upper strip contains round number min max str coalesce`.

Reading a missing key gives `null` rather than an error, so `order.refunded == null` works.
Anything else, such as calling other functions, reading attributes of objects, comprehensions,
lambdas or names starting with `_`, is rejected when the spec is validated.

## Templates

Text with `{{ expression }}` placeholders. `null` renders as nothing; whole-number floats
render with two decimals (`49.90`).

## Tests

```yaml
tests:
  - name: approved refund over 100 EUR
    input: {message: "O-5002 arrived broken"}
    approvals: [true]               # decisions given, in order, when the run pauses
    expect:
      status: completed             # completed | failed | awaiting_approval
      path: [find_order, reason, lookup, amount, refund, done_refund]
      tools: [orders.lookup_order, payments.issue_refund]   # successful calls, in order
      not_tools: []
      state: {amount: 189.0}
      reply_contains: ["189.00 EUR"]
      reply_not_contains: []
```

Each test runs against fresh simulated systems. A version with no tests, or with a failing
test, cannot be promoted past dev.
