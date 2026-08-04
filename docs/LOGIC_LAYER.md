# Logic Layer

The logic layer adds small, typed control-flow operations without changing the
assistant's normal planning, policy, or execution contracts. It deliberately
does not evaluate generated Python or ask a model what to do when an automation
fires.

## User-facing primitives

- `ChooseRandom`: `core.choose` supports random and weighted selection.
- `Rotate`: the same skill supports round-robin and least-recent selection with
  optional recent-choice exclusion. Stateful strategies derive a stable history
  key automatically when the user does not name one.
- `Chance`: `core.chance` performs an immediate percentage roll and shows a
  permission-gated notification only for the triggering branch; no external
  predicate is required.
- `Delay`: `core.schedule` accepts a one-time `delay_seconds` trigger.
- `Repeat`: existing minute, hour, daily, weekly, and monthly triggers drive a
  frozen workflow.
- `IfCondition`: a typed predicate reads a whitelisted field from one check
  result.
- `StopWhen`: a completion policy stops after a true condition, a number of
  successful actions, a maximum run count, or an expiry time.
- `Retry`: one to three attempts can use fixed or bounded exponential delay.

## Planning and execution

```text
User request
  -> deterministic router or grounded planner
  -> core.choose (immediate) or core.schedule/create_logic (durable)
  -> schema validation and frozen nested skill calls
  -> existing permission, confirmation, and background-safety review
  -> scheduler lease and occurrence
  -> check -> predicate -> selection -> action -> completion update
  -> normal result, notification, audit, and schedule history
```

`LogicWorkflowDraft` is the planner-facing type. Schedule authoring converts
each `LogicCallDraft` into a background `SkillCall`, validates it through the
registered skill, records a schema/permission/risk snapshot, and derives one
autonomy policy covering every possible branch. The persisted
`LogicWorkflowAction` contains no free-form instructions.

Predicates may read `check.status`, `check.summary`, `check.items_count`, or a
path below `check.data`. Operators are `exists`, equality/inequality, numeric
comparisons, and `contains`. Selection filters may read only `label`, `value`,
or a path below `metadata`. Dynamic selection accepts only non-sensitive
`SkillResult.items`.

Notifications may interpolate `{choice.id}`, `{choice.label}`, `{choice.value}`,
`{check.status}`, `{check.summary}`, and `{check.items_count}`. Other template
fields are rejected at validation time. Dynamic values are display-only; this
version does not splice a selected value into another skill's arguments.

## Persistence and safety

The scheduler remains the owner of timing, recurrence, leases, retries,
approval, cancellation, and missed-run behavior. The logic state store adds
only bounded metadata:

- up to 50 recent IDs per choice-history key;
- one retry-stable selected ID per occurrence;
- run, success, and condition-true counters per schedule revision.

Schedule revisions receive new invocation IDs and state boundaries. Every
workflow is bounded by `max_runs`; expiry and earlier stop rules are optional.
Choice checkpoints make a retried occurrence repeat the same branch. Pausing,
deleting, Emergency Stop, permission revocation, skill drift, and background
compatibility checks keep their existing behavior.

## Development checks

Run the focused suite while changing the logic layer:

```powershell
python -m pytest tests/test_logic.py -q
python -m pytest tests/test_schedule_skill.py tests/test_scheduler_service.py tests/test_background_runner.py -q
python -m pytest tests/test_router.py tests/test_planner.py -q
```

Run the complete regression suite before release:

```powershell
python -m pytest
```
