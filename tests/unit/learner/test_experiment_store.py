"""Real SQLite transactions with synthetic experiment/evidence, never broker state."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from importlib import import_module

import pytest
from pydantic import BaseModel

from quantbot.learner.experiment_contract import ExperimentManifest
from tests.unit.learner.test_experiment_contract import manifest_data


class Evidence(BaseModel):
    experiment_id: str
    recorded_at: datetime
    result: str


def setup(tmp_path):
    module = import_module("quantbot.learner.experiment_store")
    store = module.ExperimentStore(tmp_path / "experiment.db", create=True)
    manifest = ExperimentManifest(**manifest_data())
    store.append(manifest, idempotency_key="registration")
    return module, store, manifest


def test_unrelated_database_refused(tmp_path):
    module = import_module("quantbot.learner.experiment_store")
    path = tmp_path / "operational.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE existing(value TEXT)")
    with pytest.raises(ValueError, match="UNRELATED"):
        module.ExperimentStore(path, create=True)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("existing",)
        ]


def test_creation_explicit_and_manifest_required(tmp_path):
    module = import_module("quantbot.learner.experiment_store")
    with pytest.raises(FileNotFoundError):
        module.ExperimentStore(tmp_path / "absent.db")
    store = module.ExperimentStore(tmp_path / "new.db", create=True)
    with pytest.raises(ValueError, match="REGISTERED"):
        store.append(
            Evidence(
                experiment_id="unknown",
                recorded_at=manifest_data()["registered_at"],
                result="observation",
            ),
            idempotency_key="event",
        )


def test_repeated_event_is_idempotent(tmp_path):
    _, store, manifest = setup(tmp_path)
    event = Evidence(
        experiment_id=manifest.experiment_id,
        recorded_at=manifest.registered_at,
        result="synthetic observation",
    )
    assert store.append(event, idempotency_key="event")
    assert not store.append(event, idempotency_key="event")
    rows = store.read_as_of(manifest.experiment_id, manifest.registered_at)
    assert len(rows) == 2
    assert rows[-1].decode(Evidence) == event


def test_filtered_reads_require_explicit_index_and_preserve_bounds(tmp_path):
    _, store, manifest = setup(tmp_path)
    arguments = dict(
        experiment_id=manifest.experiment_id,
        now=manifest.registered_at,
        maximum_records=1,
        kinds=("ExperimentManifest",),
    )
    with store._transaction() as connection:
        connection.execute("DROP INDEX events_kind_time")
    with pytest.raises(ValueError, match="INDEX_NOT_COMMISSIONED"):
        store.read_as_of(**arguments)
    store.ensure_event_kind_index()
    rows = store.read_as_of(**arguments)
    assert len(rows) == 1 and rows[0].decode(ExperimentManifest) == manifest
    event = Evidence(
        experiment_id=manifest.experiment_id,
        recorded_at=manifest.registered_at,
        result="retained bounded event",
    )
    store.append(event, idempotency_key="extra")
    with pytest.raises(ValueError, match="BOUND_EXCEEDED"):
        store.read_as_of(**(arguments | {"kinds": ("ExperimentManifest", "Evidence")}))
    with pytest.raises(ValueError, match="BOUNDED_EVENT_KIND_FILTER"):
        store.read_as_of(**(arguments | {"maximum_records": None}))


def test_conflicting_evidence_retained_and_faulted(tmp_path):
    _, store, manifest = setup(tmp_path)
    event = Evidence(
        experiment_id=manifest.experiment_id, recorded_at=manifest.registered_at, result="original"
    )
    store.append(event, idempotency_key="event")
    with pytest.raises(ValueError, match="CONFLICT"):
        store.append(event.model_copy(update={"result": "different"}), idempotency_key="event")
    assert store.state(manifest.experiment_id).state == "FAULTED"
    assert (
        store.read_as_of(manifest.experiment_id, manifest.registered_at)[-1].decode(Evidence).result
        == "original"
    )
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM conflicts").fetchone()[0] == 1


def test_two_wakeups_only_one_owner(tmp_path):
    _, store, manifest = setup(tmp_path)

    def acquire(owner):
        return store.acquire_lease(
            manifest.experiment_id,
            owner=owner,
            now=manifest.registered_at,
            duration=timedelta(seconds=90),
        )

    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sum(workers.map(acquire, ["first", "second"])) == 1


def test_lease_expiry_does_not_release_dispatched_intent(tmp_path):
    _, store, manifest = setup(tmp_path)
    now = manifest.registered_at
    assert store.acquire_lease(
        manifest.experiment_id, owner="first", now=now, duration=timedelta(seconds=90)
    )
    store.enqueue_intent(manifest.experiment_id, intent_id="intent", document="synthetic")
    assert store.claim_intent("intent", owner="first", now=now) == "synthetic"
    assert store.acquire_lease(
        manifest.experiment_id,
        owner="second",
        now=now + timedelta(seconds=91),
        duration=timedelta(seconds=90),
    )
    assert store.claim_intent("intent", owner="second", now=now + timedelta(seconds=91)) is None


def test_evaluation_start_never_resets_on_delay(tmp_path):
    _, store, manifest = setup(tmp_path)
    first = store.state(manifest.experiment_id)
    next_state = store.transition(
        manifest.experiment_id,
        expected_revision=first.revision,
        target="WAITING_FOR_COVERAGE",
        reason="missing bars",
        now=manifest.registered_at + timedelta(days=1),
    )
    assert next_state.prospective_start_at == manifest.prospective_start_at
    reopened = import_module("quantbot.learner.experiment_store").ExperimentStore(store.path)
    assert (
        reopened.state(manifest.experiment_id).prospective_start_at == manifest.prospective_start_at
    )
    with pytest.raises(ValueError, match="REVISION"):
        reopened.transition(
            manifest.experiment_id,
            expected_revision=first.revision,
            target="OBSERVING",
            reason="late",
            now=manifest.registered_at,
        )


def test_consent_and_activation_are_distinct(tmp_path):
    module, store, manifest = setup(tmp_path)
    consent = module.ConsentNotice(
        experiment_id=manifest.experiment_id,
        recorded_at=manifest.registered_at,
        consent_id="chat-synthetic",
    )
    store.append(consent, idempotency_key="consent")
    state = store.state(manifest.experiment_id)
    assert state.consent_recorded
    assert state.activation_id is None
    with pytest.raises(ValueError, match="ACTIVATION"):
        store.transition(
            manifest.experiment_id,
            expected_revision=state.revision,
            target="ACTIVE_PAPER",
            reason="consent alone",
            now=manifest.registered_at,
        )


def test_as_of_read_and_hash_corruption(tmp_path):
    _, store, manifest = setup(tmp_path)
    later = Evidence(
        experiment_id=manifest.experiment_id,
        recorded_at=manifest.registered_at + timedelta(hours=1),
        result="later",
    )
    store.append(later, idempotency_key="later")
    assert len(store.read_as_of(manifest.experiment_id, manifest.registered_at)) == 1
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE events SET document='{}' WHERE idempotency_key='later'")
    with pytest.raises(ValueError, match="HASH"):
        store.read_as_of(manifest.experiment_id, manifest.registered_at + timedelta(hours=2))


def test_illegal_and_terminal_transitions_refused(tmp_path):
    _, store, manifest = setup(tmp_path)
    state = store.state(manifest.experiment_id)
    with pytest.raises(ValueError, match="TRANSITION"):
        store.transition(
            manifest.experiment_id,
            expected_revision=state.revision,
            target="COMPLETED",
            reason="no evidence",
            now=manifest.registered_at,
        )
    state = store.transition(
        manifest.experiment_id,
        expected_revision=state.revision,
        target="WAITING_FOR_COVERAGE",
        reason="wait",
        now=manifest.registered_at,
    )
    state = store.transition(
        manifest.experiment_id,
        expected_revision=state.revision,
        target="INSUFFICIENT_EVIDENCE",
        reason="bounded stop",
        now=manifest.registered_at,
    )
    with pytest.raises(ValueError, match="TRANSITION"):
        store.transition(
            manifest.experiment_id,
            expected_revision=state.revision,
            target="OBSERVING",
            reason="restart sample",
            now=manifest.registered_at,
        )


def test_query_only_reader_cannot_append_or_claim(tmp_path):
    module, store, manifest = setup(tmp_path)
    reader = module.ExperimentStore(store.path, read_only=True)
    assert reader.state(manifest.experiment_id).state == "REGISTERED"
    assert len(reader.read_as_of(manifest.experiment_id, manifest.registered_at)) == 1
    with pytest.raises(ValueError, match="READ_ONLY"):
        reader.acquire_lease(
            manifest.experiment_id,
            owner="dashboard",
            now=manifest.registered_at,
            duration=timedelta(seconds=90),
        )
    with pytest.raises(ValueError, match="READ_ONLY"):
        reader.append(
            Evidence(
                experiment_id=manifest.experiment_id,
                recorded_at=manifest.registered_at,
                result="forbidden",
            ),
            idempotency_key="forbidden",
        )
