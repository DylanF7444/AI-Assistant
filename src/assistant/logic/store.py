from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from assistant.logic.contracts import CompletionPolicy
from assistant.storage.database import (
    Database,
    LogicCheckpointRow,
    LogicChoiceHistoryRow,
    LogicWorkflowStateRow,
    now,
)


@dataclass(frozen=True)
class WorkflowProgress:
    run_count: int
    success_count: int
    condition_true_count: int
    recent_choice_ids: tuple[str, ...]
    should_stop: bool
    stop_reason: str | None


class LogicStateStore:
    """Stores only bounded control metadata, never arbitrary skill result bodies."""

    MAX_HISTORY = 50

    def __init__(self, database: Database) -> None:
        self.database = database
        self._lock = threading.RLock()

    def recent_choices(self, history_key: str | None) -> tuple[str, ...]:
        if not history_key:
            return ()
        with self.database.session() as session:
            row = session.get(LogicChoiceHistoryRow, history_key)
            if row is None:
                return ()
            return tuple(str(item) for item in json.loads(row.recent_ids_json))

    def checkpoint_choice(self, checkpoint_key: str) -> str | None:
        with self.database.session() as session:
            row = session.get(LogicCheckpointRow, checkpoint_key)
            return None if row is None else row.option_id

    def reserve_choice(
        self,
        *,
        checkpoint_key: str,
        option_id: str,
        history_key: str | None,
    ) -> str:
        with self._lock, self.database.session() as session:
            existing = session.get(LogicCheckpointRow, checkpoint_key)
            if existing is not None:
                return existing.option_id
            session.add(
                LogicCheckpointRow(
                    checkpoint_key=checkpoint_key,
                    option_id=option_id,
                    created_at=now(),
                )
            )
            if history_key:
                history = session.get(LogicChoiceHistoryRow, history_key)
                values = [] if history is None else list(json.loads(history.recent_ids_json))
                values = [option_id, *(item for item in values if item != option_id)][
                    : self.MAX_HISTORY
                ]
                if history is None:
                    session.add(
                        LogicChoiceHistoryRow(
                            history_key=history_key,
                            recent_ids_json=json.dumps(values),
                            updated_at=now(),
                        )
                    )
                else:
                    history.recent_ids_json = json.dumps(values)
                    history.updated_at = now()
            return option_id

    def workflow_progress(self, schedule_id: UUID, revision: int) -> WorkflowProgress:
        with self.database.session() as session:
            row = session.get(LogicWorkflowStateRow, (str(schedule_id), revision))
            if row is None:
                return WorkflowProgress(0, 0, 0, (), False, None)
            return self._progress(row)

    def record_workflow_run(
        self,
        *,
        schedule_id: UUID,
        revision: int,
        completion: CompletionPolicy,
        succeeded: bool,
        condition_met: bool,
        choice_id: str | None,
        observed_at: datetime | None = None,
    ) -> WorkflowProgress:
        with self._lock, self.database.session() as session:
            timestamp = observed_at or now()
            key = (str(schedule_id), revision)
            row = session.get(LogicWorkflowStateRow, key)
            if row is None:
                row = LogicWorkflowStateRow(
                    schedule_id=key[0],
                    schedule_revision=revision,
                    run_count=0,
                    success_count=0,
                    condition_true_count=0,
                    recent_choice_ids_json="[]",
                    stopped=False,
                    updated_at=timestamp,
                )
                session.add(row)
            row.run_count += 1
            row.success_count += int(succeeded)
            row.condition_true_count += int(condition_met)
            recent = list(json.loads(row.recent_choice_ids_json))
            if choice_id:
                recent = [choice_id, *(item for item in recent if item != choice_id)][
                    : self.MAX_HISTORY
                ]
                row.recent_choice_ids_json = json.dumps(recent)

            reason = None
            if completion.expires_at is not None and completion.expires_at <= timestamp:
                reason = "Workflow expiry was reached"
            elif completion.stop_after_condition and condition_met:
                reason = "Stop condition was met"
            elif (
                completion.stop_after_successes is not None
                and row.success_count >= completion.stop_after_successes
            ):
                reason = f"Reached {completion.stop_after_successes} successful runs"
            elif row.run_count >= completion.max_runs:
                reason = f"Reached the maximum of {completion.max_runs} runs"
            if reason:
                row.stopped = True
                row.stop_reason = reason
            row.updated_at = timestamp
            session.flush()
            return self._progress(row)

    @staticmethod
    def _progress(row: LogicWorkflowStateRow) -> WorkflowProgress:
        return WorkflowProgress(
            run_count=row.run_count,
            success_count=row.success_count,
            condition_true_count=row.condition_true_count,
            recent_choice_ids=tuple(json.loads(row.recent_choice_ids_json)),
            should_stop=row.stopped,
            stop_reason=row.stop_reason,
        )
