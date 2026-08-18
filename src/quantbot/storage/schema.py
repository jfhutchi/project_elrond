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

SCHEMA_VERSION = 3

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
    # Declared: only the registrant knows how many candidates it inspected to pick this one.
    Column("search_cardinality", Integer, nullable=False),
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
    ForeignKeyConstraint(
        ["hypothesis_id", "version"],
        ["hypotheses.hypothesis_id", "hypotheses.version"],
        ondelete="CASCADE",
    ),
    CheckConstraint("end_date >= start_date", name="range_ordered"),
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
