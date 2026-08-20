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

SCHEMA_VERSION = 7

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
# ones promoted to columns. Columns exist for what the gates query: lineage, family, and the
# multiple-testing/power numbers that must be recomputed before a confirmatory run.

hypotheses = Table(
    "hypotheses",
    metadata,
    Column("hypothesis_id", String(128), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column("family_id", String(128), nullable=False),
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
