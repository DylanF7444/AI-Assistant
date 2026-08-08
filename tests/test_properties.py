from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from assistant.router.normalization import command_text, normalize_text
from assistant.scheduling.contracts import RecurrenceTrigger, WebObservation
from assistant.scheduling.observations import matches
from assistant.scheduling.recurrence import occurrences


@given(st.text(max_size=500))
def test_text_normalization_is_idempotent(value: str):
    normalized = normalize_text(value)
    assert normalize_text(normalized) == normalized
    assert "  " not in normalized


@given(st.text(max_size=300))
def test_command_normalization_is_idempotent(value: str):
    assert command_text(command_text(value)) == command_text(value)


@settings(max_examples=75)
@given(
    interval=st.integers(min_value=1, max_value=10_000),
    frequency=st.sampled_from(("minutes", "hours")),
    offset_seconds=st.integers(min_value=-100_000, max_value=100_000),
    limit=st.integers(min_value=0, max_value=20),
)
def test_interval_occurrences_are_strictly_increasing_and_bounded(
    interval: int,
    frequency: str,
    offset_seconds: int,
    limit: int,
):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    trigger = RecurrenceTrigger(
        frequency=frequency,
        interval=interval,
        start_at=start,
        timezone="UTC",
    )
    after = start + timedelta(seconds=offset_seconds)

    values = occurrences(trigger, after, limit=limit)

    assert len(values) <= limit
    assert all(value.tzinfo is not None and value > after for value in values)
    assert all(left < right for left, right in zip(values, values[1:], strict=False))


@given(
    query=st.dictionaries(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=8),
        st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", max_size=8),
        max_size=5,
    )
)
def test_web_event_matching_ignores_query_data(query: dict[str, str]):
    from urllib.parse import urlencode

    suffix = f"?{urlencode(query)}" if query else ""
    trigger = {
        "kind": "event",
        "observation_type": "web",
        "target": "https://example.com/status",
        "events": ("changed",),
    }
    observation = WebObservation(
        observation_id="web:test",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        target=f"https://EXAMPLE.com/status{suffix}",
        event="changed",
        content_fingerprint="a" * 64,
    )

    from assistant.scheduling.contracts import EventTrigger

    assert matches(EventTrigger.model_validate(trigger), observation)
