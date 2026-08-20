"""Plan, queue and close research work, with no route to the broker (#3).

Three things this orchestrator deliberately cannot do: place an order, enable live trading, or
clear the kill switch. That is not achieved by checking for it -- it is achieved by this module
having no import path to any of them, and `test_director.py` walks the import graph to prove it
rather than trusting the layering. The #14 test asserts the same boundary from the other side.

**Priority is computed, never narrated.** The score comes from expected information gain, which
comes from the power assessment, the trial budget and the novelty overlap -- all numbers this
system already holds. An experiment the data cannot resolve has expected confirmatory
information gain of **zero**, regardless of how interesting the question is, and the scorer
returns exactly that. There is no field here for a model's opinion of promisingness, because
that is the input that would quietly dominate every other one.

**`UNDERPOWERED` is terminal for the design and is not a refutation of the mechanism.** The
lifecycle keeps them as separate terminal states and forbids the transition between them, so
"we could not test this" cannot decay into "this failed" through a state machine any more than
it can through research memory.

Every transition is an append-only event carrying who moved it, why, and what the evidence and
budget looked like at the time. A state column alone cannot answer those questions, and an
autonomous loop that cannot answer them is not auditable.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from quantbot.research.power import PowerAssessment, PowerVerdict
from quantbot.storage.database import decode_utc, encode_utc
from quantbot.storage.schema import research_task_events, research_tasks

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class TaskState(StrEnum):
    PROPOSED = "PROPOSED"
    SCOUTING = "SCOUTING"
    CRITIQUE = "CRITIQUE"
    REGISTERED = "REGISTERED"
    EXPERIMENTING = "EXPERIMENTING"
    REVIEW = "REVIEW"
    #: Terminal for this design. Emphatically not a refutation of the mechanism.
    UNDERPOWERED = "UNDERPOWERED"
    REFUTED = "REFUTED"
    SURVIVED = "SURVIVED"
    PROMOTABLE = "PROMOTABLE"
    BLOCKED = "BLOCKED"


ALLOWED_TRANSITIONS: Mapping[TaskState, frozenset[TaskState]] = MappingProxyType(
    {
        TaskState.PROPOSED: frozenset(
            {TaskState.SCOUTING, TaskState.CRITIQUE, TaskState.BLOCKED, TaskState.UNDERPOWERED}
        ),
        TaskState.SCOUTING: frozenset(
            {TaskState.CRITIQUE, TaskState.BLOCKED, TaskState.UNDERPOWERED, TaskState.REFUTED}
        ),
        TaskState.CRITIQUE: frozenset(
            {
                TaskState.REGISTERED,
                TaskState.PROPOSED,
                TaskState.BLOCKED,
                TaskState.UNDERPOWERED,
                TaskState.REFUTED,
            }
        ),
        TaskState.REGISTERED: frozenset(
            {TaskState.EXPERIMENTING, TaskState.BLOCKED, TaskState.UNDERPOWERED}
        ),
        TaskState.EXPERIMENTING: frozenset(
            {TaskState.REVIEW, TaskState.BLOCKED, TaskState.UNDERPOWERED}
        ),
        TaskState.REVIEW: frozenset(
            {
                TaskState.SURVIVED,
                TaskState.REFUTED,
                TaskState.UNDERPOWERED,
                TaskState.BLOCKED,
            }
        ),
        TaskState.SURVIVED: frozenset({TaskState.PROMOTABLE, TaskState.BLOCKED}),
        # Terminal. UNDERPOWERED deliberately cannot become REFUTED: a question the data could
        # not resolve was never tested, and a state machine must not launder that into a
        # finding any more than research memory may.
        TaskState.UNDERPOWERED: frozenset(),
        TaskState.REFUTED: frozenset(),
        TaskState.PROMOTABLE: frozenset({TaskState.BLOCKED}),
        # A blocked task resumes where the operator says, never onward by itself.
        TaskState.BLOCKED: frozenset(
            {TaskState.PROPOSED, TaskState.CRITIQUE, TaskState.REGISTERED, TaskState.REVIEW}
        ),
    }
)

TERMINAL = frozenset({TaskState.UNDERPOWERED, TaskState.REFUTED, TaskState.PROMOTABLE})
ACTIVE = frozenset(
    {
        TaskState.PROPOSED,
        TaskState.SCOUTING,
        TaskState.CRITIQUE,
        TaskState.REGISTERED,
        TaskState.EXPERIMENTING,
        TaskState.REVIEW,
    }
)


class InvalidTaskTransition(ValueError):
    """Raised when a task is moved somewhere the lifecycle does not allow."""

    def __init__(self, task_id: str, current: TaskState, target: TaskState) -> None:
        self.task_id = task_id
        self.current = current
        self.target = target
        detail = (
            " -- a question the data could not resolve was never tested"
            if current is TaskState.UNDERPOWERED
            else ""
        )
        super().__init__(
            f"{task_id}: {current.value} -> {target.value} is not a legal transition{detail}"
        )


def transition(task_id: str, current: TaskState, target: TaskState) -> TaskState:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTaskTransition(task_id, current, target)
    return target


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ResearchTask(FrozenModel):
    """One unit of research work, and where it has got to."""

    task_id: Text
    state: TaskState
    question: Text
    family_id: Text
    hypothesis_id: str | None = None
    hypothesis_version: int | None = Field(default=None, ge=1)
    parent_task_id: str | None = None
    #: Tasks that must finish before this one may start.
    depends_on: tuple[Text, ...] = ()
    attempts: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_task(self) -> Self:
        if (self.hypothesis_id is None) != (self.hypothesis_version is None):
            raise ValueError("a hypothesis reference needs both id and version")
        if self.task_id in self.depends_on:
            raise ValueError("a task cannot depend on itself")
        return self

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL


class TaskEvent(FrozenModel):
    """One transition, with everything needed to audit why it happened."""

    task_id: Text
    from_state: TaskState | None
    to_state: TaskState
    #: An operator, a model identity, or a deterministic component. Never blank.
    actor: Text
    reason: Text
    occurred_at: datetime
    #: Evidence and budget state at the moment of the move.
    detail: dict[str, str] = Field(default_factory=dict)


def expected_information_gain(
    assessment: PowerAssessment | None,
    *,
    novelty_overlap: float,
    trials_remaining: Decimal,
    trials_required: Decimal,
) -> Decimal:
    """What running this is worth, from numbers the system already computed.

    Zero when the data cannot resolve the claim, whatever the narrative interest: an
    underpowered experiment has no confirmatory information to give. Zero again when the budget
    cannot cover it, and discounted by how much the question overlaps work already done.

    There is deliberately no parameter here for a model's opinion of promisingness. It would
    dominate every other term and it is the one input with no measurement behind it.
    """
    if assessment is None:
        return Decimal("0")
    if assessment.verdict in (PowerVerdict.UNDERPOWERED, PowerVerdict.UNECONOMIC):
        return Decimal("0")
    if trials_required > trials_remaining:
        return Decimal("0")
    if not 0.0 <= novelty_overlap <= 1.0:
        raise ValueError("novelty overlap is a fraction")
    # Power in hand beyond the minimum, discounted by how much of this has been asked before.
    headroom = Decimal(assessment.observations_available) / Decimal(
        max(assessment.observations_required, 1)
    )
    return (min(headroom, Decimal("4")) * Decimal(str(1.0 - novelty_overlap))).quantize(
        Decimal("0.0001")
    )


class ResearchDirector:
    """Durable task orchestration inside one caller-owned transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def open_task(self, task: ResearchTask, *, actor: str, reason: str) -> ResearchTask:
        if task.state is not TaskState.PROPOSED:
            raise InvalidTaskTransition(task.task_id, TaskState.PROPOSED, task.state)
        if self.get(task.task_id) is not None:
            raise ValueError(f"task {task.task_id} already exists")
        self._insert(task)
        self._record(
            TaskEvent(
                task_id=task.task_id,
                from_state=None,
                to_state=task.state,
                actor=actor,
                reason=reason,
                occurred_at=task.created_at,
            )
        )
        return task

    def get(self, task_id: str) -> ResearchTask | None:
        row = (
            self._session.execute(select(research_tasks).where(research_tasks.c.task_id == task_id))
            .mappings()
            .one_or_none()
        )
        return None if row is None else ResearchTask.model_validate_json(str(row["document_json"]))

    def advance(
        self,
        task_id: str,
        target: TaskState,
        *,
        actor: str,
        reason: str,
        now: datetime,
        detail: Mapping[str, str] | None = None,
    ) -> ResearchTask:
        """Move a task, refusing an illegal transition and recording the legal one."""
        task = self.get(task_id)
        if task is None:
            raise LookupError(f"no research task {task_id}")
        if not self.dependencies_met(task):
            raise InvalidTaskTransition(task_id, task.state, target)
        transition(task_id, task.state, target)

        moved = task.model_copy(
            update={
                "state": target,
                "updated_at": now,
                "attempts": task.attempts + (1 if target is TaskState.EXPERIMENTING else 0),
            }
        )
        self._session.execute(
            update(research_tasks)
            .where(research_tasks.c.task_id == task_id)
            .values(
                state=target.value,
                updated_at=encode_utc(now),
                hypothesis_id=moved.hypothesis_id,
                hypothesis_version=moved.hypothesis_version,
                document_json=moved.model_dump_json(),
            )
        )
        self._record(
            TaskEvent(
                task_id=task_id,
                from_state=task.state,
                to_state=target,
                actor=actor,
                reason=reason,
                occurred_at=now,
                detail=dict(detail or {}),
            )
        )
        return moved

    def attach_hypothesis(self, task_id: str, hypothesis_id: str, version: int) -> ResearchTask:
        task = self.get(task_id)
        if task is None:
            raise LookupError(f"no research task {task_id}")
        attached = task.model_copy(
            update={"hypothesis_id": hypothesis_id, "hypothesis_version": version}
        )
        self._session.execute(
            update(research_tasks)
            .where(research_tasks.c.task_id == task_id)
            .values(
                hypothesis_id=hypothesis_id,
                hypothesis_version=version,
                document_json=attached.model_dump_json(),
            )
        )
        return attached

    def dependencies_met(self, task: ResearchTask) -> bool:
        for dependency in task.depends_on:
            other = self.get(dependency)
            if other is None or not other.terminal:
                return False
        return True

    def by_state(self, *states: TaskState) -> list[ResearchTask]:
        statement = select(research_tasks).order_by(research_tasks.c.updated_at)
        if states:
            statement = statement.where(
                research_tasks.c.state.in_([state.value for state in states])
            )
        return [
            ResearchTask.model_validate_json(str(row["document_json"]))
            for row in self._session.execute(statement).mappings()
        ]

    def stuck(self, *, now: datetime, older_than: timedelta) -> list[ResearchTask]:
        """Active tasks that have not moved. What the supervisor reports on."""
        cutoff = now - older_than
        return [task for task in self.by_state(*ACTIVE) if task.updated_at <= cutoff]

    def history(self, task_id: str) -> list[TaskEvent]:
        rows = self._session.execute(
            select(research_task_events)
            .where(research_task_events.c.task_id == task_id)
            .order_by(research_task_events.c.event_id)
        ).mappings()
        return [
            TaskEvent(
                task_id=str(row["task_id"]),
                from_state=None if row["from_state"] is None else TaskState(str(row["from_state"])),
                to_state=TaskState(str(row["to_state"])),
                actor=str(row["actor"]),
                reason=str(row["reason"]),
                occurred_at=decode_utc(str(row["occurred_at"])),
                detail=json.loads(str(row["detail_json"])),
            )
            for row in rows
        ]

    def _insert(self, task: ResearchTask) -> None:
        self._session.execute(
            research_tasks.insert().values(
                task_id=task.task_id,
                state=task.state.value,
                hypothesis_id=task.hypothesis_id,
                hypothesis_version=task.hypothesis_version,
                parent_task_id=task.parent_task_id,
                created_at=encode_utc(task.created_at),
                updated_at=encode_utc(task.updated_at),
                document_json=task.model_dump_json(),
            )
        )

    def _record(self, event: TaskEvent) -> None:
        self._session.execute(
            research_task_events.insert().values(
                task_id=event.task_id,
                from_state=None if event.from_state is None else event.from_state.value,
                to_state=event.to_state.value,
                actor=event.actor,
                reason=event.reason,
                occurred_at=encode_utc(event.occurred_at),
                detail_json=json.dumps(event.detail, sort_keys=True),
            )
        )


def prioritize(
    scored: Sequence[tuple[ResearchTask, Decimal]],
) -> list[tuple[ResearchTask, Decimal]]:
    """Highest expected information gain first; zero-gain work sorts to the bottom."""
    return sorted(scored, key=lambda item: (-item[1], item[0].created_at, item[0].task_id))


__all__ = [
    "ACTIVE",
    "ALLOWED_TRANSITIONS",
    "TERMINAL",
    "InvalidTaskTransition",
    "ResearchDirector",
    "ResearchTask",
    "TaskEvent",
    "TaskState",
    "expected_information_gain",
    "prioritize",
    "transition",
]
