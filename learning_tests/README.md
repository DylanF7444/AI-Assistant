# Learning Tests

This folder is a guided tour of the assistant. The production regression suite
remains in `tests/`; these tests are intentionally smaller and more explanatory.

## Setup

From the project root:

```powershell
python -m pip install -e .[test]
```

## Run the tests

Run the complete learning suite:

```powershell
python -m pytest learning_tests -v
```

Run one topic:

```powershell
python -m pytest learning_tests/test_02_routing.py -v
```

Run one test:

```powershell
python -m pytest learning_tests/test_02_routing.py::test_timer_route_converts_minutes_to_seconds -v
```

Show `print()` output while experimenting:

```powershell
python -m pytest learning_tests/test_03_skills.py -v -s
```

## Suggested order

1. `test_01_contracts.py` - immutable, validated data crossing subsystem boundaries.
2. `test_02_routing.py` - text normalization and deterministic intent selection.
3. `test_03_skills.py` - input validation and execution of isolated skills.
4. `test_04_policy.py` - action-bound confirmations and capability discovery.
5. `test_05_permissions_and_storage.py` - persisted permission scopes and redaction.
6. `test_06_events_and_responses.py` - event history and user-facing response rules.
7. `test_07_execution.py` - registry, policy, execution, audit, and idempotency together.
8. `test_08_planner.py` - constrained local-model planning with a fake provider.
9. `test_09_orchestrator.py` - a full text command through the assembled application.

## How to learn with these

Read one test, predict the result, and run it. Then change one input or assertion
and rerun only that test. Useful experiments include invalid confidence values,
different timer phrases, changed confirmation arguments, narrower filesystem
targets, sensitive response settings, and duplicate idempotency keys.

The tests use temporary directories and fake providers. They do not require a
microphone, network connection, desktop UI, Ollama, or downloaded models.
