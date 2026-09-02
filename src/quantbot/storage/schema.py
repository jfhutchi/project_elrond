"""SQLAlchemy Core schema for QuantBot's durable audit store."""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)

SCHEMA_VERSION = 24

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

schema_version = Table(
    "schema_version",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("version", Integer, nullable=False),
    CheckConstraint("id = 1", name="singleton"),
)

strategy_deployments = Table(
    "strategy_deployments",
    metadata,
    Column("strategy_id", String(128), primary_key=True),
    Column("version", String(64), primary_key=True),
    Column("git_commit", String(128), nullable=False),
    Column("configuration_hash", String(128), nullable=False),
    Column("deployment_timestamp", String(40), nullable=False),
)

runs = Table(
    "runs",
    metadata,
    Column("run_id", String(128), primary_key=True),
    Column("strategy_id", String(128), nullable=False),
    Column("strategy_version", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("started_at", String(40), nullable=False),
    Column("finished_at", String(40)),
    Column("detail_json", Text, nullable=False, server_default=text("'{}'")),
    ForeignKeyConstraint(
        ["strategy_id", "strategy_version"],
        ["strategy_deployments.strategy_id", "strategy_deployments.version"],
    ),
)
Index("ix_runs_started_at", runs.c.started_at)

bars = Table(
    "bars",
    metadata,
    Column("bar_id", Integer, primary_key=True, autoincrement=True),
    Column("symbol", String(32), nullable=False),
    Column("timestamp", String(40), nullable=False),
    Column("provider", String(64), nullable=False),
    Column("adjustment_metadata_json", Text, nullable=False),
    Column("open", Text, nullable=False),
    Column("high", Text, nullable=False),
    Column("low", Text, nullable=False),
    Column("close", Text, nullable=False),
    Column("volume", Text, nullable=False),
    Column("adjustment", Text, nullable=False),
    UniqueConstraint(
        "symbol",
        "timestamp",
        "provider",
        "adjustment_metadata_json",
        name="observation",
    ),
)
Index("ix_bars_symbol_timestamp", bars.c.symbol, bars.c.timestamp)

signals = Table(
    "signals",
    metadata,
    Column("signal_id", String(128), primary_key=True),
    Column("run_id", String(128), ForeignKey("runs.run_id"), nullable=False),
    Column("strategy_id", String(128), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("occurred_at", String(40), nullable=False),
    Column("payload_json", Text, nullable=False),
)
Index("ix_signals_run_symbol", signals.c.run_id, signals.c.symbol)

account_snapshots = Table(
    "account_snapshots",
    metadata,
    Column("snapshot_id", String(128), primary_key=True),
    Column("run_id", String(128), ForeignKey("runs.run_id")),
    Column("account_id", String(128), nullable=False),
    Column("captured_at", String(40), nullable=False),
    Column("cash", Text, nullable=False),
    Column("buying_power", Text, nullable=False),
    Column("equity", Text, nullable=False),
    Column("currency", String(16), nullable=False),
)
Index(
    "ix_account_snapshots_account_time",
    account_snapshots.c.account_id,
    account_snapshots.c.captured_at,
)

equity_snapshots = Table(
    "equity_snapshots",
    metadata,
    Column("snapshot_id", String(128), primary_key=True),
    Column(
        "account_snapshot_id",
        String(128),
        ForeignKey("account_snapshots.snapshot_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    Column("account_id", String(128), nullable=False),
    Column("captured_at", String(40), nullable=False),
    Column("equity", Text, nullable=False),
    Column("cash", Text, nullable=False),
)
Index(
    "ix_equity_snapshots_account_time",
    equity_snapshots.c.account_id,
    equity_snapshots.c.captured_at,
)

positions = Table(
    "positions",
    metadata,
    Column(
        "snapshot_id",
        String(128),
        ForeignKey("account_snapshots.snapshot_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("symbol", String(32), primary_key=True),
    Column("quantity", Text, nullable=False),
    Column("market_price", Text, nullable=False),
    Column("market_value", Text, nullable=False),
    Column("average_entry_price", Text, nullable=False),
)

order_intents = Table(
    "order_intents",
    metadata,
    Column("intent_id", String(128), primary_key=True),
    Column("client_order_id", String(128), nullable=False, unique=True),
    Column("strategy_id", String(128), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("signal_date", String(10), nullable=False),
    Column("side", String(8), nullable=False),
    Column("order_type", String(16), nullable=False),
    Column("time_in_force", String(8), nullable=False),
    Column("quantity", Text, nullable=False),
    Column("limit_price", Text),
    Column("stop_price", Text),
    Column("extended_hours", Boolean, nullable=False),
    Column("state", String(32), nullable=False),
    Column("created_at", String(40), nullable=False),
)
Index("ix_order_intents_state_created", order_intents.c.state, order_intents.c.created_at)

broker_orders = Table(
    "broker_orders",
    metadata,
    Column("broker_order_id", String(128), primary_key=True),
    Column(
        "client_order_id",
        String(128),
        ForeignKey("order_intents.client_order_id"),
        nullable=False,
        unique=True,
    ),
    Column("symbol", String(32), nullable=False),
    Column("side", String(8), nullable=False),
    Column("order_type", String(16), nullable=False),
    Column("time_in_force", String(8), nullable=False),
    Column("quantity", Text, nullable=False),
    Column("filled_quantity", Text, nullable=False),
    Column("status", String(64), nullable=False),
    Column("submitted_at", String(40), nullable=False),
    Column("filled_average_price", Text),
)
Index("ix_broker_orders_status", broker_orders.c.status)

fills = Table(
    "fills",
    metadata,
    Column("fill_id", String(128), primary_key=True),
    Column(
        "broker_order_id",
        String(128),
        ForeignKey("broker_orders.broker_order_id"),
        nullable=False,
    ),
    Column("symbol", String(32), nullable=False),
    Column("side", String(8), nullable=False),
    Column("quantity", Text, nullable=False),
    Column("price", Text, nullable=False),
    Column("occurred_at", String(40), nullable=False),
    Column("fee", Text, nullable=False),
)
Index("ix_fills_broker_time", fills.c.broker_order_id, fills.c.occurred_at)

order_events = Table(
    "order_events",
    metadata,
    Column("event_id", String(128), primary_key=True),
    Column("intent_id", String(128), ForeignKey("order_intents.intent_id")),
    Column("broker_order_id", String(128), ForeignKey("broker_orders.broker_order_id")),
    Column("event_type", String(64), nullable=False),
    Column("occurred_at", String(40), nullable=False),
    Column("detail_json", Text, nullable=False),
)
Index("ix_order_events_intent_time", order_events.c.intent_id, order_events.c.occurred_at)

reconciliation_runs = Table(
    "reconciliation_runs",
    metadata,
    Column("reconciliation_id", String(128), primary_key=True),
    Column("run_id", String(128), ForeignKey("runs.run_id")),
    Column("status", String(32), nullable=False),
    Column("started_at", String(40), nullable=False),
    Column("completed_at", String(40), nullable=False),
    Column("summary_json", Text, nullable=False),
)
Index("ix_reconciliation_runs_completed", reconciliation_runs.c.completed_at)

reconciliation_diffs = Table(
    "reconciliation_diffs",
    metadata,
    Column("diff_id", String(128), primary_key=True),
    Column(
        "reconciliation_id",
        String(128),
        ForeignKey("reconciliation_runs.reconciliation_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("category", String(64), nullable=False),
    Column("record_key", String(256), nullable=False),
    Column("expected_json", Text, nullable=False),
    Column("actual_json", Text, nullable=False),
)
Index("ix_reconciliation_diffs_run", reconciliation_diffs.c.reconciliation_id)

incidents = Table(
    "incidents",
    metadata,
    Column("incident_id", String(128), primary_key=True),
    Column("run_id", String(128), ForeignKey("runs.run_id")),
    Column("severity", String(32), nullable=False),
    Column("kind", String(64), nullable=False),
    Column("message", Text, nullable=False),
    Column("occurred_at", String(40), nullable=False),
    Column("resolved_at", String(40)),
    Column("detail_json", Text, nullable=False),
)
Index("ix_incidents_occurred", incidents.c.occurred_at)

qualification_days = Table(
    "qualification_days",
    metadata,
    Column("strategy_id", String(128), primary_key=True),
    Column("trading_date", String(10), primary_key=True),
    Column("qualified", Boolean, nullable=False),
    Column("detail_json", Text, nullable=False),
)

kill_switch_state = Table(
    "kill_switch_state",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("engaged", Boolean, nullable=False),
    Column("reason", Text, nullable=False),
    Column("updated_at", String(40), nullable=False),
    CheckConstraint("id = 1", name="singleton"),
)

# --- Research: hypothesis registry (#5) -------------------------------------------------
#
# A registration is frozen at insert. `document_json` holds the whole canonical registration
# and `content_hash` is its SHA-256, so immutability covers every field rather than only the
# ones promoted to columns. Columns exist for what the gates query: legacy single-parent
# lineage, family, and the multiple-testing/power numbers that must be recomputed before a
# confirmatory run. Componentized multi-parent lineage (#187) lives in the same frozen
# `document_json` as the exact graph and is validated by the registry. At registry scale it is
# queried through that authoritative document; duplicating it into relational rows now would
# create two histories that could drift. A later evidence index may be added additively when
# #188 needs indexed component retrieval, without changing these historical registrations.

hypotheses = Table(
    "hypotheses",
    metadata,
    Column("hypothesis_id", String(128), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("family_id", String(128), nullable=False),
    # Pre-#187 single-parent lineage. New componentized registrations leave these null and carry
    # every exact parent graph in their frozen document, avoiding a lossy "primary parent".
    Column("parent_hypothesis_id", String(128)),
    Column("parent_version", Integer),
    Column("registered_at", String(40), nullable=False),
    Column("content_hash", String(64), nullable=False, unique=True),
    # How many candidates were inspected to pick this one. Never accepted as a bare
    # self-report: `search_origin` says where the number came from, and an agent's unattested
    # claim is not one of the accepted origins (#23).
    Column("search_cardinality", Integer, nullable=False),
    # NO_DATA_DEPENDENT_SEARCH, OPERATOR_ATTESTED, or MEASURED.
    Column("search_origin", String(32), nullable=False),
    # Who attested the count. Present exactly when the origin is OPERATOR_ATTESTED, which is
    # what makes an attestation auditable rather than an unsigned assertion.
    Column("search_attested_by", String(128)),
    # Computed at registration from durable state, never declared. See research/registry.py.
    Column("cumulative_trials", Integer, nullable=False),
    Column("luck_threshold_z", Text, nullable=False),
    # The estimand fixes the units of every effect figure beside it: an annualised Sharpe, a
    # Cohen's d, a proportion, or a correlation. A single "expected_sharpe" column would be a
    # lie for four of the five. See research/power.py.
    Column("estimand", String(32), nullable=False),
    Column("expected_effect", Text, nullable=False),
    Column("power_verdict", String(32), nullable=False),
    Column("required_observations", Integer, nullable=False),
    Column("available_observations", Integer, nullable=False),
    Column("document_json", Text, nullable=False),
    ForeignKeyConstraint(
        ["parent_hypothesis_id", "parent_version"],
        ["hypotheses.hypothesis_id", "hypotheses.version"],
    ),
    CheckConstraint("version >= 1", name="version_positive"),
    CheckConstraint(
        "(search_origin = 'OPERATOR_ATTESTED') = (search_attested_by IS NOT NULL)",
        name="attestation_is_signed",
    ),
    CheckConstraint(
        "search_origin != 'NO_DATA_DEPENDENT_SEARCH' OR search_cardinality = 1",
        name="theory_searches_nothing",
    ),
    CheckConstraint(
        "(parent_hypothesis_id IS NULL) = (parent_version IS NULL)",
        name="parent_is_whole",
    ),
)
Index("ix_hypotheses_family_id", hypotheses.c.family_id)

# One contiguous range per (registration, dataset, role). The primary key enforces that
# deliberately: a claim spread over disjoint ranges hides which parts were actually inspected.
hypothesis_data_windows = Table(
    "hypothesis_data_windows",
    metadata,
    Column("hypothesis_id", String(128), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("dataset", String(128), primary_key=True),
    Column("role", String(32), primary_key=True),
    Column("start_date", String(10), nullable=False),
    Column("end_date", String(10), nullable=False),
    # RESERVED, CONSUMED, or RELEASED. Registration reserves; handing the data to an experiment
    # consumes. A reservation that is never consumed used to burn the holdout forever (#22).
    Column("state", String(16), nullable=False),
    # When a RESERVED claim stops blocking. Abandonment is the absence of events, not an event,
    # so nothing can be relied on to release a reservation explicitly -- it has to lapse.
    Column("reserved_until", String(10)),
    # When the data was actually made available to an experiment. Permanent once set.
    Column("consumed_at", String(40)),
    ForeignKeyConstraint(
        ["hypothesis_id", "version"],
        ["hypotheses.hypothesis_id", "hypotheses.version"],
        ondelete="CASCADE",
    ),
    CheckConstraint("end_date >= start_date", name="range_ordered"),
    CheckConstraint(
        "(state = 'CONSUMED') = (consumed_at IS NOT NULL)",
        name="consumption_is_dated",
    ),
)
Index(
    "ix_hypothesis_data_windows_dataset",
    hypothesis_data_windows.c.dataset,
    hypothesis_data_windows.c.start_date,
)


# --- Research: power assessments (#19) ---------------------------------------------------
#
# Append-only, and deliberately not foreign-keyed to `hypotheses`: the assessments worth
# keeping include the ones that refused a registration, which therefore has no row. Recording
# an untestable hypothesis as refuted would teach a later agent that a mechanism was tested and
# failed when it was never tested at all, so `UNDERPOWERED` is preserved as its own outcome.

power_assessments = Table(
    "power_assessments",
    metadata,
    Column("assessment_id", Integer, primary_key=True, autoincrement=True),
    Column("hypothesis_id", String(128), nullable=False),
    Column("version", Integer, nullable=False),
    # REGISTRATION or EXECUTION. The bar moves between them, so both are kept.
    Column("stage", String(16), nullable=False),
    Column("assessed_at", String(40), nullable=False),
    Column("verdict", String(32), nullable=False),
    Column("estimand", String(32), nullable=False),
    Column("cumulative_trials", Integer, nullable=False),
    Column("luck_threshold_z", Text, nullable=False),
    Column("observations_required", Integer, nullable=False),
    Column("observations_available", Integer, nullable=False),
    Column("minimum_detectable_effect", Text, nullable=False),
    # Present only on an operator override, which is the audit trail for one.
    Column("overridden_by", String(128)),
    Column("document_json", Text, nullable=False),
)
Index(
    "ix_power_assessments_hypothesis",
    power_assessments.c.hypothesis_id,
    power_assessments.c.version,
)
Index("ix_power_assessments_verdict", power_assessments.c.verdict)


# --- Research memory (#6) ------------------------------------------------------------------
#
# One table for every durable research record, discriminated by `kind`. Findings, structural
# limits and analysis defects have different meanings but the same shape: a statement, the
# evidence behind it, and when it was recorded. A generated summary is just another record and
# deleting one cannot destroy what it summarised, because the underlying records are separate
# rows rather than fields inside it.

research_records = Table(
    "research_records",
    metadata,
    Column("record_id", String(128), primary_key=True),
    # FINDING, STRUCTURAL_LIMIT, ANALYSIS_DEFECT, SUMMARY.
    Column("kind", String(32), nullable=False),
    # REFUTED, SURVIVED, UNDERPOWERED, EXPLORATORY_ONLY, LITERATURE_SUPPORTED,
    # LITERATURE_REFUTED, IMPOSSIBLE. Null for records that are not verdicts.
    Column("verdict", String(32)),
    # Free-text topic the record is about, so "what do we know about momentum" is a query.
    Column("subject", Text, nullable=False),
    Column("statement", Text, nullable=False),
    Column("hypothesis_id", String(128)),
    Column("hypothesis_version", Integer),
    # Manifest hash of the bundle that evidences this, when there is one.
    Column("evidence_hash", String(64)),
    Column("source", Text, nullable=False),
    # True when a human wrote it. Structural limits are curated on purpose: an LLM summariser
    # produces "momentum did not work" and loses the part that changes future decisions.
    Column("curated", Boolean, nullable=False),
    Column("recorded_at", String(40), nullable=False),
    Column("document_json", Text, nullable=False),
    CheckConstraint(
        "(hypothesis_id IS NULL) = (hypothesis_version IS NULL)",
        name="hypothesis_reference_is_whole",
    ),
)
Index("ix_research_records_kind", research_records.c.kind, research_records.c.recorded_at)
Index("ix_research_records_hypothesis_id", research_records.c.hypothesis_id)

# Edges between records and hypotheses. Deliberately not foreign-keyed to either: a relationship
# may point at a hypothesis that was refused registration, which is exactly the case worth
# keeping.
research_relationships = Table(
    "research_relationships",
    metadata,
    Column("source_id", String(128), primary_key=True),
    # REFUTES, SUPPORTS, UNDERPOWERED_FOR, DUPLICATES, MUTATES, CONTRADICTS, DEPENDS_ON,
    # USES_DATASET, CONSUMES_WINDOW, INVALIDATED_BY_DEFECT.
    Column("relation", String(32), primary_key=True),
    Column("target_id", String(128), primary_key=True),
    Column("recorded_at", String(40), nullable=False),
    Column("note", Text, nullable=False, server_default=text("''")),
)
Index("ix_research_relationships_target_id", research_relationships.c.target_id)

# Artifact-level claims are preregistered separately from the immutable hypothesis document.
# This keeps historical registration hashes stable while still requiring the identifying design
# to exist before protected data is consumed.  Every row is one additional charged comparison;
# renaming a component cannot mint another test because the target is its behavioural content id.
artifact_test_registrations = Table(
    "artifact_test_registrations",
    metadata,
    Column("test_id", String(64), primary_key=True),
    Column("content_hash", String(64), nullable=False, unique=True),
    Column("hypothesis_id", String(128), nullable=False),
    Column("hypothesis_version", Integer, nullable=False),
    Column("registration_hash", String(64), nullable=False),
    Column("graph_hash", String(64), nullable=False),
    Column("artifact_kind", String(16), nullable=False),
    Column("artifact_content_id", String(64), nullable=False),
    Column("trial_charge", Integer, nullable=False),
    Column("power_verdict", String(32), nullable=False),
    Column("registered_at", String(40), nullable=False),
    Column("document_json", Text, nullable=False),
    ForeignKeyConstraint(
        ["hypothesis_id", "hypothesis_version"],
        ["hypotheses.hypothesis_id", "hypotheses.version"],
    ),
    CheckConstraint("trial_charge = 1", name="artifact_test_charges_one_trial"),
)
Index(
    "ix_artifact_test_registrations_hypothesis",
    artifact_test_registrations.c.hypothesis_id,
    artifact_test_registrations.c.hypothesis_version,
)
Index(
    "ix_artifact_test_registrations_artifact",
    artifact_test_registrations.c.artifact_kind,
    artifact_test_registrations.c.artifact_content_id,
)

# A projection of manifest-backed evidence for fast component and interaction retrieval.  The
# full immutable provenance/applicability document remains authoritative.  `source_record_id`
# ties this projection to ordinary research memory so artifact evidence cannot become a second,
# disconnected account of an experiment.
artifact_evidence = Table(
    "artifact_evidence",
    metadata,
    Column("evidence_id", String(64), primary_key=True),
    Column("source_record_id", String(128), nullable=False),
    Column("test_id", String(64)),
    Column("artifact_kind", String(16), nullable=False),
    Column("artifact_content_id", String(64), nullable=False),
    Column("epistemic_status", String(32), nullable=False),
    Column("verdict", String(32)),
    Column("hypothesis_id", String(128), nullable=False),
    Column("hypothesis_version", Integer, nullable=False),
    Column("manifest_hash", String(64), nullable=False),
    Column("recorded_at", String(40), nullable=False),
    Column("document_json", Text, nullable=False),
    ForeignKeyConstraint(
        ["hypothesis_id", "hypothesis_version"],
        ["hypotheses.hypothesis_id", "hypotheses.version"],
    ),
    ForeignKeyConstraint(["source_record_id"], ["research_records.record_id"]),
    ForeignKeyConstraint(["test_id"], ["artifact_test_registrations.test_id"]),
    CheckConstraint(
        "(epistemic_status = 'DIRECTLY_MEASURED') = (verdict IS NOT NULL)",
        name="direct_evidence_alone_has_verdict",
    ),
)
Index(
    "ix_artifact_evidence_artifact",
    artifact_evidence.c.artifact_kind,
    artifact_evidence.c.artifact_content_id,
    artifact_evidence.c.recorded_at,
)
Index("ix_artifact_evidence_manifest", artifact_evidence.c.manifest_hash)


# --- Budget governor (#14) -----------------------------------------------------------------
#
# Append-only spend, keyed by budget rather than by task, so a cap is a query. Statistical
# spend and compute spend share the table because they share the shape; they do not share the
# arithmetic, because CPU refills overnight and a consumed holdout never does.

budget_spend = Table(
    "budget_spend",
    metadata,
    Column("spend_id", Integer, primary_key=True, autoincrement=True),
    # e.g. "family:trend-following", "dataset:sip-us-equities-daily", "global".
    Column("budget_key", String(256), nullable=False),
    # TRIALS, WALL_SECONDS, TOKENS, REQUESTS, DOLLARS, ROUNDS.
    Column("category", String(32), nullable=False),
    Column("amount", Text, nullable=False),
    Column("task", Text, nullable=False),
    Column("recorded_at", String(40), nullable=False),
    # Present only when an operator raised a cap to allow this. Raising a statistical cap is a
    # permanent evidence decision, so it is recorded as one rather than logged.
    Column("override_by", String(128)),
    Column("note", Text, nullable=False, server_default=text("''")),
)
Index("ix_budget_spend_budget_key", budget_spend.c.budget_key, budget_spend.c.category)
Index("ix_budget_spend_override_by", budget_spend.c.override_by)


# --- Research director (#3) ----------------------------------------------------------------
#
# Task state plus an append-only event log. The log is the record: a state column alone cannot
# answer "who moved this, why, and what did the budget look like at the time", and those are
# the questions an autonomous loop has to be auditable on.

research_tasks = Table(
    "research_tasks",
    metadata,
    Column("task_id", String(128), primary_key=True),
    Column("state", String(32), nullable=False),
    Column("hypothesis_id", String(128)),
    Column("hypothesis_version", Integer),
    Column("parent_task_id", String(128)),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    Column("document_json", Text, nullable=False),
    CheckConstraint(
        "(hypothesis_id IS NULL) = (hypothesis_version IS NULL)",
        name="hypothesis_reference_is_whole",
    ),
)
Index("ix_research_tasks_state", research_tasks.c.state, research_tasks.c.updated_at)

research_task_events = Table(
    "research_task_events",
    metadata,
    Column("event_id", Integer, primary_key=True, autoincrement=True),
    Column("task_id", String(128), ForeignKey("research_tasks.task_id"), nullable=False),
    Column("from_state", String(32)),
    Column("to_state", String(32), nullable=False),
    # Who moved it: an operator, a model identity, or a deterministic component.
    Column("actor", String(128), nullable=False),
    Column("reason", Text, nullable=False),
    Column("occurred_at", String(40), nullable=False),
    Column("detail_json", Text, nullable=False, server_default=text("'{}'")),
)
Index("ix_research_task_events_task_id", research_task_events.c.task_id)


# --- Promotion ladder (#16) ------------------------------------------------------------------
#
# Two tables rather than one, and the second is the point. `promotion_state` is where a strategy
# is now; `promotion_events` is every move it ever made. A ladder that only stores the current
# stage can be walked backwards and rewritten with no trace, which for a mechanism whose whole
# purpose is "this earned its way here" would make the record worthless.
#
# Events are append-only. Nothing in `StorageRepository` updates or deletes one.
#
# The four nullable columns added in 0011 are the answer to "what claim is this strategy
# carrying, and what justified the stage it is at". They are nullable because a strategy below
# REGISTERED legitimately has no claim and one below PAPER_QUALIFIED legitimately has no
# qualifying assessment -- `PromotionState` refuses the combinations that would be dishonest.
promotion_state = Table(
    "promotion_state",
    metadata,
    Column("strategy_id", String(128), primary_key=True),
    Column("stage", String(32), nullable=False),
    Column("strategy_version", String(64), nullable=False),
    Column("configuration_hash", String(128), nullable=False),
    Column("reason", Text, nullable=False),
    Column("actor", String(128), nullable=False),
    Column("updated_at", String(40), nullable=False),
    Column("hypothesis_id", String(128)),
    Column("hypothesis_version", Integer),
    Column("objective_identity", String(128)),
    Column("evidence_identity", String(64)),
)

promotion_events = Table(
    "promotion_events",
    metadata,
    Column("event_id", Integer, primary_key=True, autoincrement=True),
    Column("strategy_id", String(128), nullable=False),
    # Null on the first event, which is the strategy entering the ladder.
    Column("from_stage", String(32)),
    Column("to_stage", String(32), nullable=False),
    # PROMOTION or DEMOTION. Stored rather than derived from the stage ranks, because the ranks
    # are code and this row has to stay readable after the ladder is reshaped.
    Column("direction", String(16), nullable=False),
    Column("strategy_version", String(64), nullable=False),
    Column("configuration_hash", String(128), nullable=False),
    Column("reason", Text, nullable=False),
    Column("actor", String(128), nullable=False),
    Column("occurred_at", String(40), nullable=False),
    # Copied onto the event as well as the state, because the state is a current value and the
    # event is the record. An audit asking "what justified PAPER_QUALIFIED on the day it was
    # granted" must not have to hope the current row still says the same thing.
    Column("hypothesis_id", String(128)),
    Column("hypothesis_version", Integer),
    Column("objective_identity", String(128)),
    Column("evidence_identity", String(64)),
    CheckConstraint("direction IN ('PROMOTION', 'DEMOTION')", name="direction_known"),
)
Index(
    "ix_promotion_events_strategy",
    promotion_events.c.strategy_id,
    promotion_events.c.occurred_at,
)


# --- Risk: what Elrond refused, so the refusing can be judged (#57) -------------------------
#
# Two tables, and the second is the point. `rejected_candidates` is one row per refusal event;
# `rejection_reasons` is one row per rule that refused it. A single entry can be refused by four
# vetoes at once, and storing those as four events would inflate the sample fourfold -- the
# manufacture of sample size that killed cycle 11's overnight result.
#
# The counterfactual outcome is deliberately absent beyond a state column: what was refused is
# known at decision time, and what would have happened is not known until the horizon elapses.
rejected_candidates = Table(
    "rejected_candidates",
    metadata,
    Column("observation_id", String(128), primary_key=True),
    # Which decision this was, independent of what any gate concluded (#57).
    Column("candidate_id", String(128), nullable=False),
    Column("strategy_id", String(128), nullable=False),
    Column("configuration_hash", String(128), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("direction", String(8), nullable=False),
    Column("observed_at", String(40), nullable=False),
    Column("session_date", String(10), nullable=False),
    Column("reference_price", Text, nullable=False),
    Column("hypothesis_id", String(128)),
    # Context this project cannot yet produce (#59, #56), nullable so absence is recorded.
    Column("market_regime_snapshot_id", String(128)),
    Column("catalyst_snapshot_id", String(128)),
    Column("anomaly_event_id", String(128)),
    Column("portfolio_snapshot_id", String(128)),
    Column("state", String(32), nullable=False),
    Column("unavailable_reason", Text, nullable=False),
)

# `rule_parameters_hash` lives here rather than on the event because it is what makes an analysis
# honest across a threshold change: outcomes recorded under different parameters are different
# datasets, and grouping on the hash stops one rule being judged on another's evidence.
rejection_reasons = Table(
    "rejection_reasons",
    metadata,
    Column("observation_id", String(128), primary_key=True),
    Column("gate_id", String(64), primary_key=True),
    Column("gate_version", Integer, nullable=False),
    Column("rule_parameters_hash", String(128), nullable=False),
    Column("detail", Text, nullable=False),
    # What the rule was *looking at*, as opposed to which rule it was (#85). Nullable because
    # rows written before v13 genuinely had no structured evidence, and a NULL that reads back
    # as LEGACY_UNRECORDED is honest where a backfilled value would not be.
    Column("evidence_kind", String(32)),
    Column("evidence_schema_version", Integer),
    Column("evidence_payload", Text),
    Column("evidence_identity", String(128)),
    ForeignKeyConstraint(
        ["observation_id"],
        ["rejected_candidates.observation_id"],
        ondelete="CASCADE",
    ),
)
Index(
    "ix_rejection_reasons_gate_id",
    rejection_reasons.c.gate_id,
    rejection_reasons.c.rule_parameters_hash,
)
# Grouping refusals by decision basis is the query #85 exists to serve.
Index("ix_rejection_reasons_evidence_identity", rejection_reasons.c.evidence_identity)


# --- Capacity: how much capital the edge survives, if there is one (#51) ----------------------
#
# The ladder existed and nothing fed it, so the milestone gate received `None` and reported the
# question as permanently unassessed. This is the artifact it reads instead.
#
# `UNASSESSED` here means the production artifact is absent or its inputs were incomplete. It
# does not mean issue #51 is open, and the distinction is the whole point: a gate that reads an
# issue number can be opened by closing an issue.
capacity_assessments = Table(
    "capacity_assessments",
    metadata,
    # Content-addressed over the verdict, the ladder and the inputs that produced it.
    Column("assessment_id", String(128), primary_key=True),
    Column("strategy_id", String(128), nullable=False),
    Column("as_of", String(10), nullable=False),
    # The frozen input artifact. Without it a verdict cannot be traced to the allocation,
    # turnover and liquidity it rested on.
    Column("inputs_identity", String(128)),
    Column("objective_identity", String(128), nullable=False),
    Column("cost_model_identity", String(128), nullable=False),
    Column("verdict", String(32), nullable=False),
    Column("breakeven_usd", Text),
    Column("first_failing_rung_usd", Text),
    # Which instrument binds, because "the strategy exhausts at $2m" tells an operator nothing
    # they can act on and "XLU binds at $2m" tells them to change the roster.
    Column("binding_symbol", String(32)),
    Column("binding_constraint", Text),
    # Inputs that rested on a versioned project default rather than a measurement. Empty is a
    # strong claim and is only true when every input was observed.
    Column("modelled_inputs", Text, nullable=False),
    Column("blocking", Text, nullable=False),
    Column("rung_count", Integer, nullable=False),
)
Index(
    "ix_capacity_assessments_strategy",
    capacity_assessments.c.strategy_id,
    capacity_assessments.c.as_of,
)


# --- Discovery: what woke the research system up (#56) ----------------------------------------
#
# Two tables, and the split is the point. `market_anomaly_events` is the raw observation: a
# symbol, a session, an expected against an observed, under a named detector and feed. It is
# fixed the moment it is measured.
#
# `anomaly_dependence_contexts` is what one frozen correlation matrix said about that
# observation -- which other symbols were moving with it. Re-run the matrix over a different
# window and the grouping legitimately changes while nothing about the observation does.
#
# Folding the second into the first broke content-addressing before it reached storage: the
# grouped copy carried a different `group_key` under the same `event_id`, so keying this table
# on it would have deduplicated two different payloads into whichever arrived first.
#
# No enrichment, no catalyst, no score. An anomaly is a routing signal for research and has no
# broker authority, so nothing here references an order, an intent or a side.
market_anomaly_events = Table(
    "market_anomaly_events",
    metadata,
    # Content-addressed over the observation and the detector that produced it, excluding the
    # wall clock: a replayed session must not mint a second event for one piece of information.
    Column("event_id", String(128), primary_key=True),
    Column("symbol", String(32), nullable=False),
    Column("session_date", String(10), nullable=False),
    Column("segment", String(24), nullable=False),
    Column("observed_at", String(40), nullable=False),
    Column("detector_identity", String(128), nullable=False),
    # Feed semantics on the row itself, so a venue feed can never be read downstream as
    # full-market volume (#56).
    Column("feed", String(24), nullable=False),
    Column("volume_coverage", String(32), nullable=False),
    Column("cadence", String(24), nullable=False),
    # The exact baseline window, so "the same sample" is a property of the row rather than a
    # claim about it -- the same rule #84 established for the dependence snapshot.
    Column("baseline_first_session", String(10), nullable=False),
    Column("baseline_last_session", String(10), nullable=False),
    Column("baseline_session_count", Integer, nullable=False),
    Column("observed_volume", Text, nullable=False),
    Column("expected_volume", Text),
    Column("volume_robust_z", Text),
    Column("session_return", Text, nullable=False),
    Column("trailing_sigma", Text),
    Column("triggers", String(128), nullable=False),
    # Per family, because one verdict described a valid price observation as unusable whenever
    # the volume family could not run on the same feed.
    Column("family_quality", String(255), nullable=False),
)
Index(
    "ix_market_anomaly_events_session",
    market_anomaly_events.c.session_date,
    market_anomaly_events.c.symbol,
)
Index("ix_market_anomaly_events_detector", market_anomaly_events.c.detector_identity)

anomaly_dependence_contexts = Table(
    "anomaly_dependence_contexts",
    metadata,
    Column("context_id", String(128), primary_key=True),
    Column("anomaly_event_id", String(128), nullable=False),
    Column("dependence_snapshot_identity", String(128), nullable=False),
    # Null means this matrix measured the symbol and placed it alone, or could not measure it.
    # The row's existence is what separates that from "no matrix ran".
    Column("group_key", String(128)),
    Column("method_version", Integer, nullable=False),
    ForeignKeyConstraint(
        ["anomaly_event_id"],
        ["market_anomaly_events.event_id"],
        ondelete="CASCADE",
    ),
    UniqueConstraint(
        "anomaly_event_id",
        "dependence_snapshot_identity",
        name="one_context_per_event_and_snapshot",
    ),
)
Index(
    "ix_anomaly_dependence_contexts_group",
    anomaly_dependence_contexts.c.dependence_snapshot_identity,
    anomaly_dependence_contexts.c.group_key,
)


# --- Instruments: one economic thing, across the names providers give it (#83 part A) ---------
#
# A ticker is a label a venue currently uses, not an identity. Keying records on that string
# splits one instrument's history in two when it renames, and merges two histories into one when
# a symbol is recycled.
#
# `instrument_id` is minted once and never recomputed -- deliberately not content-addressed,
# because an identity hashed over its attributes changes when an attribute changes, which is
# exactly what a durable identity must not do.
#
# A resolver is a source, never the key. An instrument nobody could resolve is still recordable,
# which is the case that matters: a resolver outage must not stop a decision being attributed.
instruments = Table(
    "instruments",
    metadata,
    Column("instrument_id", String(64), primary_key=True),
    Column("asset_class", String(16), nullable=False),
    # For display only. Never a key -- that is the whole point of this table.
    Column("current_symbol", String(32), nullable=False),
    # RESOLVED / AMBIGUOUS / UNRESOLVED. Ambiguity is conflicting evidence and unresolved is an
    # absence of it; collapsing them into a null would lose the difference.
    Column("status", String(16), nullable=False),
    Column("figi", String(32)),
    Column("composite_figi", String(32)),
    Column("share_class_figi", String(32)),
    # Kept separately attributable: a CIK asserted by a market-data vendor and one read from
    # EDGAR are different claims and must not share a field silently.
    Column("sec_cik", String(16)),
    Column("sec_cik_source", String(64)),
    Column("exchange_mic", String(8)),
    Column("ambiguous_candidates", Text, nullable=False),
    Column("resolver", String(64)),
    Column("resolver_version", String(32)),
    Column("resolved_at", String(40)),
    Column("source_refs", Text, nullable=False),
)
Index("ix_instruments_current_symbol", instruments.c.current_symbol)

# Historical, not current. An event recorded under an old symbol stays interpretable because the
# question is always asked with a date.
instrument_symbols = Table(
    "instrument_symbols",
    metadata,
    Column("instrument_id", String(64), primary_key=True),
    Column("provider", String(64), primary_key=True),
    Column("symbol", String(32), primary_key=True),
    Column("valid_from", String(10), primary_key=True),
    Column("valid_to", String(10)),
    ForeignKeyConstraint(["instrument_id"], ["instruments.instrument_id"], ondelete="CASCADE"),
)
Index(
    "ix_instrument_symbols_lookup",
    instrument_symbols.c.provider,
    instrument_symbols.c.symbol,
    instrument_symbols.c.valid_from,
)


# --- Counterfactual: what a refusal cost, once its horizon elapsed (#57 phase 2) --------------
#
# Append-only, and keyed so it cannot become an amendment. The refusal in `rejected_candidates`
# is what was known at decision time and stays that way forever; hindsight arrives here as a new
# row that references it.
#
# `(observation_id, policy_identity)` is unique: one hypothetical execution policy has one answer
# per refusal. A second policy is a second dataset and gets its own row, because pooling outcomes
# computed under different execution assumptions defeats the question #57 exists to ask.
#
# Nothing here carries a quantity, a client order id or a side. A hypothetical outcome must not
# be mistakable for an order, and the schema is where that stops being a convention.
counterfactual_outcomes = Table(
    "counterfactual_outcomes",
    metadata,
    Column("outcome_id", String(128), primary_key=True),
    Column("observation_id", String(128), nullable=False),
    Column("policy_identity", String(128), nullable=False),
    # COUNTERFACTUAL_UNAVAILABLE (not yet), _UNEVALUABLE (never), _EVALUATED (measured).
    Column("state", String(40), nullable=False),
    Column("horizon_session", String(10)),
    Column("evaluated_at", String(40)),
    # Only ever populated on EVALUATED. A number beside a non-evaluated state gets quoted.
    Column("net_excess_bps", Text),
    Column("gross_return_bps", Text),
    Column("benchmark_return_bps", Text),
    # Why a terminal outcome is terminal. Without it, never is indistinguishable from not yet.
    Column("unevaluable_reason", String(40)),
    Column("detail", Text, nullable=False),
    ForeignKeyConstraint(
        ["observation_id"],
        ["rejected_candidates.observation_id"],
        ondelete="CASCADE",
    ),
    UniqueConstraint(
        "observation_id",
        "policy_identity",
        name="one_outcome_per_refusal_and_policy",
    ),
)
Index(
    "ix_counterfactual_outcomes_policy",
    counterfactual_outcomes.c.policy_identity,
    counterfactual_outcomes.c.state,
)


# --- Regime: what kind of market a decision happened in (#59) ---------------------------------
#
# Context, never authority. A regime may group an outcome; it may never permit one, and
# `quantbot.risk.regime` is structurally unable to import an execution path.
#
# Bound to observations through a relation rather than by copying the id onto them, for the
# reason #94 established for dependence: the snapshot is one versioned definition's reading of a
# session, and revising that definition must mint a new snapshot rather than redefine the
# observation it described.
market_regime_snapshots = Table(
    "market_regime_snapshots",
    metadata,
    # Content-addressed over the reading and the method that produced it, so a redefinition
    # cannot relabel observations already grouped under the old one.
    Column("snapshot_id", String(128), primary_key=True),
    Column("as_of_session", String(10), nullable=False),
    Column("benchmark_symbol", String(32), nullable=False),
    Column("source", String(64), nullable=False),
    Column("method_version", Integer, nullable=False),
    Column("trend", String(16), nullable=False),
    Column("volatility", String(16), nullable=False),
    Column("breadth", String(16), nullable=False),
    Column("realised_volatility", Text),
    Column("participation", Text),
    # Which axes could not be computed, and why. An unexplained gap is indistinguishable from an
    # ordinary reading once it is in a dataset.
    Column("unavailable", Text, nullable=False),
    Column("source_snapshot_ids", Text, nullable=False),
)
Index(
    "ix_market_regime_snapshots_session",
    market_regime_snapshots.c.as_of_session,
    market_regime_snapshots.c.method_version,
)

anomaly_regime_contexts = Table(
    "anomaly_regime_contexts",
    metadata,
    Column("anomaly_event_id", String(128), primary_key=True),
    Column("market_regime_snapshot_id", String(128), primary_key=True),
    ForeignKeyConstraint(
        ["anomaly_event_id"],
        ["market_anomaly_events.event_id"],
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["market_regime_snapshot_id"],
        ["market_regime_snapshots.snapshot_id"],
        ondelete="CASCADE",
    ),
)


# --- Research: durable search records (#23, #11) ---------------------------------------------
#
# The origin of a trial count, made durable rather than declared. `SearchOrigin.MEASURED` was
# unreachable while nothing emitted search telemetry: accepting it on trust would have reopened
# the laundering path one enum value across from where #23 closed it, so it was refused outright.
#
# `SubprocessWorker` now emits a disclosed cardinality, so a producer exists and the origin can
# be derived from a record instead of asserted by whoever registers.
#
# Append-only. A search that happened cannot un-happen, and a row that could be revised downward
# would be exactly the understatement the multiple-testing budget exists to prevent.
literature_searches = Table(
    "literature_searches",
    metadata,
    Column("search_id", String(128), primary_key=True),
    Column("connector", String(64), nullable=False),
    Column("query", Text, nullable=False),
    # RESULTS / NO_RESULTS / OUTAGE. Three states rather than a nullable result set, because
    # "we looked and found nothing" and "we could not look" are different facts about the
    # world and collapsing them is how an outage reads as evidence of absence.
    Column("outcome", String(16), nullable=False),
    Column("attempted_at", String(40), nullable=False),
    Column("total_matched", Integer, nullable=False),
    #: The sources returned, with the identifier and content hash of each, so a citation can be
    #: traced back to the bytes that were actually acquired.
    Column("results_json", Text, nullable=False, server_default=text("'[]'")),
    Column("detail", Text, nullable=False, server_default=text("''")),
    CheckConstraint("outcome IN ('RESULTS', 'NO_RESULTS', 'OUTAGE')", name="known_search_outcome"),
    CheckConstraint("total_matched >= 0", name="matched_not_negative"),
    # An outage found nothing because it never looked; recording a match count against one
    # would be inventing a number nobody received.
    CheckConstraint("outcome != 'OUTAGE' OR total_matched = 0", name="an_outage_matched_nothing"),
)
Index("ix_literature_searches_connector", literature_searches.c.connector)
Index("ix_literature_searches_attempted", literature_searches.c.attempted_at)

search_runs = Table(
    "search_runs",
    metadata,
    Column("search_id", String(128), primary_key=True),
    # Who did the searching: a worker name@version, or a generator, or an operator.
    Column("actor", String(128), nullable=False),
    Column("actor_version", String(64), nullable=False),
    # Everything evaluated, including what was discarded. Not the count that was kept.
    Column("candidates_evaluated", Integer, nullable=False),
    # The data it searched over, so a count can be attributed to the window it spent.
    Column("dataset", String(128), nullable=False),
    Column("start_date", String(10), nullable=False),
    Column("end_date", String(10), nullable=False),
    Column("data_role", String(32), nullable=False),
    Column("started_at", String(40), nullable=False),
    Column("finished_at", String(40), nullable=False),
    # Hash of the configuration that produced the search, so two runs are distinguishable.
    Column("configuration_hash", String(128), nullable=False),
    Column("detail_json", Text, nullable=False, server_default=text("'{}'")),
    CheckConstraint("candidates_evaluated >= 1", name="searched_something"),
    CheckConstraint("end_date >= start_date", name="search_range_ordered"),
)
Index("ix_search_runs_dataset", search_runs.c.dataset, search_runs.c.start_date)
Index("ix_search_runs_actor", search_runs.c.actor)


veto_policy_versions = Table(
    "veto_policy_versions",
    metadata,
    Column("identity", String(128), primary_key=True),
    Column("version", Integer, nullable=False),
    # NULL only for version 1, the shipped default.
    Column("supersedes", String(128)),
    # OPERATOR or PROJECT_DEFAULT. Never written from a proposal (#72).
    Column("authority", String(32), nullable=False),
    Column("applied_by", String(128), nullable=False),
    Column("applied_at", String(40), nullable=False),
    Column("reason", Text, nullable=False),
    # The whole policy body, so a version is read rather than replayed from its diffs.
    Column("body", Text, nullable=False),
    # The derived diff, for a reader. Authority lives in `body`.
    Column("changes", Text, nullable=False),
    ForeignKeyConstraint(["supersedes"], ["veto_policy_versions.identity"], ondelete="RESTRICT"),
    UniqueConstraint("version", name="one_row_per_version_ordinal"),
    # The chain cannot fork. `apply_proposal` refuses a stale proposal, but that check lives in
    # one process; two processes can both read version 3 and both write a successor to it. Only
    # the database sees that race.
    UniqueConstraint("supersedes", name="one_successor_per_version"),
    CheckConstraint("version >= 1", name="versions_start_at_one"),
    CheckConstraint(
        "(version = 1) = (supersedes IS NULL)", name="only_the_first_has_no_predecessor"
    ),
)
Index("ix_veto_policy_versions_applied_at", veto_policy_versions.c.applied_at)


catalyst_assessments = Table(
    "catalyst_assessments",
    metadata,
    Column("assessment_id", String(128), primary_key=True),
    Column("anomaly_event_id", String(128), nullable=False),
    Column("observed_at", String(40), nullable=False),
    Column("lookback_seconds", Integer, nullable=False),
    # CATALYST_FOUND, NO_PUBLIC_CATALYST_FOUND (a finding), CATALYST_UNKNOWN (nobody looked).
    Column("outcome", String(32), nullable=False),
    # The denominator of a negative finding: who was asked, and who actually answered.
    Column("sources_consulted", Text, nullable=False),
    Column("unhealthy_sources", Text, nullable=False),
    Column("catalyst_ids", Text, nullable=False),
    # Counted, never dropped: the world explained the move, just not before it.
    Column("published_after_event", Integer, nullable=False),
    Column("unplaceable", Integer, nullable=False),
    Column("detail", Text, nullable=False),
    ForeignKeyConstraint(
        ["anomaly_event_id"], ["market_anomaly_events.event_id"], ondelete="CASCADE"
    ),
    UniqueConstraint(
        "anomaly_event_id", "lookback_seconds", name="one_assessment_per_event_and_window"
    ),
    CheckConstraint("lookback_seconds > 0", name="a_window_has_width"),
    CheckConstraint("published_after_event >= 0", name="counts_are_not_negative"),
    CheckConstraint("unplaceable >= 0", name="unplaceable_is_not_negative"),
    # An outage is not silence. Enforced by the database and not only by the model that writes
    # to it, because this is the confusion that would turn provider incidents into discoveries.
    CheckConstraint(
        "NOT (outcome = 'NO_PUBLIC_CATALYST_FOUND' AND unhealthy_sources <> '')",
        name="an_outage_is_not_a_negative_finding",
    ),
    CheckConstraint(
        "NOT (outcome = 'NO_PUBLIC_CATALYST_FOUND' AND unplaceable > 0)",
        name="an_unplaceable_item_is_not_silence",
    ),
    CheckConstraint(
        "(outcome = 'CATALYST_FOUND') = (catalyst_ids <> '')",
        name="a_positive_finding_names_what_it_found",
    ),
)
Index("ix_catalyst_assessments_event", catalyst_assessments.c.anomaly_event_id)
Index("ix_catalyst_assessments_outcome", catalyst_assessments.c.outcome)

session_observations = Table(
    "session_observations",
    metadata,
    # Content-addressed from the observed instant: a restart cannot mint a second id for a pass
    # that already happened.
    Column("observation_id", String(128), primary_key=True),
    Column("observed_at", String(40), nullable=False),
    Column("market_open", Integer, nullable=False),
    # HEALTHY, DEGRADED (a fault), HALTED (a deliberate stop -- not the same thing). Reading a
    # kill switch as a fault is how somebody clears one to make an alert go away.
    Column("outcome", String(32), nullable=False),
    Column("reasons", Text, nullable=False),
    Column("market_data_age_seconds", String(40), nullable=True),
    Column("detail", Text, nullable=False),
    CheckConstraint(
        "outcome IN ('HEALTHY', 'DEGRADED', 'HALTED')", name="an_outcome_is_one_of_three"
    ),
    CheckConstraint("market_open IN (0, 1)", name="market_open_is_a_flag"),
    # A row claiming health while carrying reasons, or claiming a fault while naming none, makes
    # every later reading of this table ambiguous. Enforced by the database, not only by the
    # model that writes to it.
    CheckConstraint("(outcome = 'HEALTHY') = (reasons = '')", name="health_and_reasons_agree"),
)
Index("ix_session_observations_observed_at", session_observations.c.observed_at)
Index("ix_session_observations_outcome", session_observations.c.outcome)

position_protection_cycles = Table(
    "position_protection_cycles",
    metadata,
    Column("cycle_id", String(128), primary_key=True),
    Column("observed_at", String(40), nullable=False),
    Column("completed_at", String(40), nullable=False),
    Column("strategy_id", String(128), nullable=False),
    Column("strategy_version", String(64), nullable=False),
    Column("configuration_hash", String(128), nullable=False),
    Column("account_id", String(128), nullable=True),
    Column("policy_version", String(64), nullable=False),
    Column("policy_hash", String(128), nullable=False),
    Column("outcome", String(32), nullable=False),
    Column("reason", String(128), nullable=False),
    Column("position_count", Integer, nullable=True),
    Column("evaluated_count", Integer, nullable=False),
    Column("unprotected_count", Integer, nullable=False),
    Column("exit_required_count", Integer, nullable=False),
    Column("detail", Text, nullable=False),
    ForeignKeyConstraint(
        ["strategy_id", "strategy_version"],
        ["strategy_deployments.strategy_id", "strategy_deployments.version"],
    ),
    CheckConstraint(
        "outcome IN ('HEALTHY', 'ACTION_REQUIRED', 'DEGRADED', 'INCIDENT')",
        name="outcome_is_known",
    ),
    CheckConstraint(
        "(outcome = 'HEALTHY') = (reason = '')",
        name="healthy_cycle_has_no_reason",
    ),
    CheckConstraint(
        "position_count IS NULL OR position_count >= 0",
        name="position_count_is_nonnegative",
    ),
    CheckConstraint("evaluated_count >= 0", name="evaluated_count_is_nonnegative"),
    CheckConstraint("unprotected_count >= 0", name="unprotected_count_is_nonnegative"),
    CheckConstraint(
        "exit_required_count >= 0",
        name="exit_required_count_is_nonnegative",
    ),
    CheckConstraint(
        "position_count IS NULL OR evaluated_count <= position_count",
        name="evaluations_do_not_exceed_positions",
    ),
    CheckConstraint(
        "unprotected_count <= evaluated_count",
        name="unprotected_does_not_exceed_evaluated",
    ),
    CheckConstraint(
        "exit_required_count <= evaluated_count",
        name="exits_do_not_exceed_evaluated",
    ),
)
Index("ix_position_protection_cycles_observed_at", position_protection_cycles.c.observed_at)
Index("ix_position_protection_cycles_outcome", position_protection_cycles.c.outcome)

position_protection_evaluations = Table(
    "position_protection_evaluations",
    metadata,
    Column("evaluation_id", String(128), primary_key=True),
    Column(
        "cycle_id",
        String(128),
        ForeignKey("position_protection_cycles.cycle_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("account_id", String(128), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("quantity", Text, nullable=False),
    Column("average_entry_price", Text, nullable=False),
    Column("price", Text, nullable=True),
    Column("price_as_of", String(40), nullable=True),
    Column("price_source", String(64), nullable=False),
    Column("strategy_id", String(128), nullable=False),
    Column("strategy_version", String(64), nullable=False),
    Column("configuration_hash", String(128), nullable=False),
    Column("policy_version", String(64), nullable=False),
    Column("policy_hash", String(128), nullable=False),
    Column("reconciled", Integer, nullable=False),
    Column("outcome", String(32), nullable=False),
    Column("rule", String(32), nullable=False),
    Column("reason", Text, nullable=False),
    Column("move_bps", Text, nullable=True),
    Column("protected", Integer, nullable=False),
    Column("evaluated_at", String(40), nullable=False),
    Column("detail", Text, nullable=False),
    ForeignKeyConstraint(
        ["strategy_id", "strategy_version"],
        ["strategy_deployments.strategy_id", "strategy_deployments.version"],
    ),
    UniqueConstraint("cycle_id", "symbol", name="one_evaluation_per_position"),
    CheckConstraint("reconciled IN (0, 1)", name="reconciled_is_a_flag"),
    CheckConstraint("protected IN (0, 1)", name="protected_is_a_flag"),
    CheckConstraint(
        "outcome IN ('NO_ACTION', 'EXIT_REQUIRED', 'BLOCKED_STATE', 'INCIDENT')",
        name="outcome_is_known",
    ),
    CheckConstraint(
        "rule IN ('HARD_STOP', 'ACCOUNT_DRAWDOWN', 'NONE')",
        name="rule_is_known",
    ),
)
Index(
    "ix_position_protection_evaluations_cycle",
    position_protection_evaluations.c.cycle_id,
)
Index(
    "ix_position_protection_evaluations_symbol_time",
    position_protection_evaluations.c.symbol,
    position_protection_evaluations.c.evaluated_at,
)

position_protection_actions = Table(
    "position_protection_actions",
    metadata,
    Column("action_id", String(128), primary_key=True),
    Column(
        "trigger_evaluation_id",
        String(128),
        ForeignKey("position_protection_evaluations.evaluation_id"),
        nullable=False,
        unique=True,
    ),
    Column("account_id", String(128), nullable=False),
    Column("symbol", String(32), nullable=False),
    Column("side", String(8), nullable=False),
    Column("quantity", Text, nullable=False),
    Column(
        "intent_id",
        String(128),
        ForeignKey("order_intents.intent_id"),
        nullable=False,
        unique=True,
    ),
    Column("client_order_id", String(128), nullable=False, unique=True),
    Column("status", String(32), nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    Column("completed_at", String(40), nullable=True),
    Column(
        "flat_confirmation_snapshot_id",
        String(128),
        ForeignKey("account_snapshots.snapshot_id"),
        nullable=True,
    ),
    Column("flat_confirmed_at", String(40), nullable=True),
    Column("last_error", Text, nullable=False),
    Column("detail", Text, nullable=False),
    CheckConstraint("side IN ('BUY', 'SELL')", name="side_is_known"),
    CheckConstraint(
        "status IN ('PENDING', 'WORKING', 'UNCERTAIN', 'FILLED_UNVERIFIED', 'COMPLETED', 'FAILED')",
        name="status_is_known",
    ),
    CheckConstraint(
        "(status = 'COMPLETED') = (completed_at IS NOT NULL)",
        name="completion_time_matches_status",
    ),
    CheckConstraint(
        "(flat_confirmation_snapshot_id IS NULL) = (flat_confirmed_at IS NULL)",
        name="flat_confirmation_fields_match",
    ),
    CheckConstraint(
        "(status = 'COMPLETED') = (flat_confirmation_snapshot_id IS NOT NULL)",
        name="completion_has_flat_confirmation",
    ),
)
Index(
    "uq_position_protection_actions_active_symbol",
    position_protection_actions.c.account_id,
    position_protection_actions.c.symbol,
    unique=True,
    sqlite_where=text(
        "status IN ('PENDING', 'WORKING', 'UNCERTAIN', 'FILLED_UNVERIFIED', 'FAILED')"
    ),
)
Index(
    "ix_position_protection_actions_status_time",
    position_protection_actions.c.status,
    position_protection_actions.c.updated_at,
)

position_protection_action_evaluations = Table(
    "position_protection_action_evaluations",
    metadata,
    Column(
        "action_id",
        String(128),
        ForeignKey("position_protection_actions.action_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "evaluation_id",
        String(128),
        ForeignKey("position_protection_evaluations.evaluation_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("linked_at", String(40), nullable=False),
)
Index(
    "ix_position_protection_action_evaluations_evaluation",
    position_protection_action_evaluations.c.evaluation_id,
)
