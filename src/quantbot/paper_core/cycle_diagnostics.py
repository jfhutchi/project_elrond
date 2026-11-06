"""Bounded observations for local role logs; never persisted or consumed as authority."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

_DATA_REASONS = frozenset(
    {
        "FRESH_COMPLETE_BAR_UNAVAILABLE",
        "OPENING_HISTORY_UNAVAILABLE",
        "OPENING_WINDOW_ENDED",
        "OPENING_QUOTES_UNAVAILABLE",
        "OPENING_CONTEXT_INVALID",
        "FEATURE_INPUT_INVALID",
        "DEPENDENCY_UNAVAILABLE",
        "INVALID_FEATURES",
        "OBSERVATION_STALE",
        "NONCAUSAL_OBSERVATION",
        "CRYPTO_OBSERVATION_STALE",
        "CRYPTO_CONTIGUOUS_HISTORY_UNAVAILABLE",
        "CRYPTO_MARKET_DATA_UNAVAILABLE",
        "SOURCE_BOUNDARY_MISMATCH",
        "DISCOVERY_NO_FRESH_ELIGIBLE",
        "DISCOVERY_CATALOG_STALE",
        "DISCOVERY_RECEIPT_STALE",
    }
)
_TIME_REASONS = frozenset(
    {
        "TIME_EVIDENCE_EXPIRED",
        "TIME_EVIDENCE_MISSING",
        "TIME_ACQUISITION_RETRYABLE",
        "TIME_ACQUISITION_BUDGET",
        "TIME_PROTOCOL_INVALID",
        "TIME_DISCONTINUITY",
        "TIME_SKEW_OUT_OF_BOUNDS",
        "TIME_SAMPLES_INCOMPLETE",
        "TIME_SAMPLES_NOT_ADVANCING",
        "TIME_IDENTITY_CHANGED",
        "TIME_SAMPLES_DISAGREE",
        "TIME_PROCESS_CHANGED",
        "TIME_PROJECTION_INVALID",
        "TIME_ACCOUNT_MISMATCH",
        "TIME_SESSION_BOUNDARY",
        "TIME_CALENDAR_MISMATCH",
        "TIME_OBSERVATION_BOUNDARY",
    }
)
_SESSION_WAIT_REASONS = frozenset(
    {
        "MARKET_CLOSED",
        "TIME_SESSION_BOUNDARY",
        "TIME_OBSERVATION_BOUNDARY",
        "STOCK_SIGNAL_HORIZON_EXCEEDS_SESSION",
    }
)
_ACQUISITION_REASONS = (_TIME_REASONS - _SESSION_WAIT_REASONS) | frozenset(
    {
        "INVENTORY_ACQUISITION_RETRYABLE",
        "INVENTORY_PROTOCOL_INVALID",
        "ASSET_ELIGIBILITY_UNAVAILABLE",
        "CRYPTO_ASSET_METADATA_UNAVAILABLE",
        "CONTINUOUS_CAPITAL_UNVERIFIED",
        "ACCOUNT_RISK_ANCHOR_UNVERIFIED",
        "CONTINUOUS_ACTIVITY_CATCHUP_PENDING",
        "CONTINUOUS_ACTIVITY_PROTOCOL_INVALID",
        "CONTINUOUS_ACTIVITY_AUTHORITY_CHANGED",
        "CONTINUOUS_ACTIVITY_LINEAGE_INVALID",
        "CONTINUOUS_ANCHOR_POLICY_CHANGED",
        "CONTINUOUS_DOCUMENT_IDENTITY_INVALID",
        "CONTINUOUS_FUNDS_UNVERIFIED",
        "CONTINUOUS_POLICY_OR_ADAPTER_UNAVAILABLE",
        "CONTINUOUS_POLICY_UNAVAILABLE",
    }
)
_CRYPTO_ENTRY_REASONS = frozenset(
    {
        "CRYPTO_ACCOUNT_INELIGIBLE",
        "CRYPTO_ASSET_IDENTITY_INVALID",
        "CRYPTO_COHORT_SCOPE_INVALID",
        "CRYPTO_POLICY_NOT_COMMISSIONED",
        "CRYPTO_MODEL_PROCEDURE_NOT_VALIDATED",
        "CRYPTO_COST_AWARE_SIGNAL_NOT_QUALIFIED",
        "CRYPTO_NON_MARGINABLE_BUYING_POWER",
    }
)
_KNOWN_REASONS = (
    _DATA_REASONS
    | _TIME_REASONS
    | _ACQUISITION_REASONS
    | _SESSION_WAIT_REASONS
    | _CRYPTO_ENTRY_REASONS
    | frozenset(
        {
            "UNCLASSIFIED_REASON",
            "MARKET_CLOSED",
            "ASSET_NOT_FRACTIONABLE",
            "ASSET_NOT_TRADABLE",
            "ASSET_ELIGIBILITY_UNVERIFIED",
            "ASSET_ELIGIBILITY_STALE_OR_MISMATCHED",
            "ASSET_WHOLE_QUANTITY_REQUIRED",
            "ASSET_CAPABILITY_CHANGED",
            "ENTRY_RISK_LIMIT",
            "BUYING_POWER_RESERVE",
            "PREDICT_NET_RESERVE",
            "OPENING_SIGNAL_NOT_QUALIFIED",
            "OPENING_CONTEXT_MODEL_UNSUPPORTED",
            "ENTRY_QUOTE_TIME_UNVERIFIED",
            "INVENTORY_UNCERTIFIED",
            "INVENTORY_STALE",
            "CURRENT_PROOF_UNAVAILABLE",
            "CORE_MODEL_UNCOMMISSIONED",
            "ACTIVE_MODEL_UNCOMMISSIONED",
            "CORE_INDEPENDENT_GUARDIAN_NOT_READY",
            "CORE_MANUAL_ENTRY_PAUSE",
            "CORE_ENTRY_FENCED",
            "CORE_UNRESOLVED_SUBMISSION",
            "CORE_ENTRY_SLOT_RESERVED",
            "CORE_RUNTIME_BINDING_MISMATCH",
            "CORE_RUNTIME_EVENT_INVALID",
            "CORE_EVIDENCE_QUARANTINED",
            "CORE_PROTECTION_DEADLINE_DUE",
            "CORE_GUARDIAN_RECEIPT_EXPIRED",
            "CORE_CASH_AUTHORITY_UNAVAILABLE",
            "CORE_ENTRY_DECISION_EXPIRED",
            "CORE_ENTRY_AUTHORITY_MISMATCH",
            "CORE_FIXED_RISK_ENVELOPE",
            "CORE_SESSION_EXIT_BUFFER_DUE",
            "CORE_ENTRY_HANDOFF_MISSING",
            "CORE_ENTRY_ALREADY_REFUSED",
            "NO_ELIGIBLE_COHORT_MEMBERS",
            "SYMBOL_ALREADY_HELD",
            "SYMBOL_ALREADY_RESERVED",
            "BATCH_MEMBER_ALREADY_SELECTED",
            "SESSION_EXIT_BUFFER",
            "CONTRACT_MISMATCH",
            "PORTFOLIO_SYMBOL_CAPACITY",
            "PORTFOLIO_CURRENT_MARKS_UNAVAILABLE",
            "PORTFOLIO_MARKS_STALE_OR_MISMATCHED",
            "PORTFOLIO_INVENTORY_STALE",
            "PORTFOLIO_EXPOSURE_LIMIT",
            "PORTFOLIO_UNATTRIBUTED_POSITION",
            "SESSION_RESERVATION_LIMIT",
            "SESSION_DAILY_LOSS_HALT",
            "MANUAL_ENTRY_STOP",
            "SESSION_OPENING_UNVERIFIED",
            "SESSION_CAPITAL_UNVERIFIED",
            "SESSION_EXTERNAL_CAPITAL_FLOW",
        }
    )
)


def _safe_reason(reason: str) -> str:
    parts = reason.split(":", 2)
    if parts[0] in {"EQUITY", "CRYPTO"} and len(parts) > 1:
        return ":".join(parts[:2]) if parts[1] in _KNOWN_REASONS else "UNCLASSIFIED_REASON"
    return parts[0] if parts[0] in _KNOWN_REASONS else "UNCLASSIFIED_REASON"


def reason_codes(reasons: tuple[str, ...]) -> tuple[str, ...]:
    """Drop free text and private suffixes; keep bounded machine-readable codes."""
    return tuple(dict.fromkeys(_safe_reason(reason) for reason in reasons))[:32]


@dataclass
class TraderCycleDiagnostics:
    evaluation_evidence_ids: tuple[str, ...] = ()
    evaluation_reasons: tuple[str, ...] = ()
    last_evaluated_at: datetime | None = None
    last_observed_work_at: datetime | None = None
    session_open: datetime | None = None
    session_close: datetime | None = None
    candidate_count: int = 0
    excluded: bool = False
    submission_requests: int = 0
    acknowledgements: int = 0
    confirmed_fills: int = 0
    dispatch_failed: bool = False

    def acknowledge(self, result: Any, *, now: datetime) -> None:
        filled_quantity = result.broker_order.filled_quantity
        self.acknowledgements += 1
        if filled_quantity > 0:
            self.confirmed_fills += 1
        self.last_observed_work_at = now

    def evaluate(
        self,
        observation: Any,
        decisions: tuple[Any, ...],
        *,
        evidence_ids: tuple[str, ...],
        now: datetime,
    ) -> None:
        self.evaluation_evidence_ids = evidence_ids[:64]
        unavailable = tuple(reason for _, reason in getattr(observation, "unavailable", ()))
        market_gaps = tuple(getattr(observation, "market_gaps", ()))
        gates = tuple(reason for decision in decisions for reason in decision.gates)
        exclusions = tuple(
            reason
            for decision in decisions
            if decision.selection is not None
            for _, reasons in decision.selection.exclusions
            for reason in reasons
        )
        self.evaluation_reasons = reason_codes((*unavailable, *market_gaps, *gates, *exclusions))
        self.excluded = any(
            reason.rsplit(":", 1)[-1] not in _DATA_REASONS for reason in reason_codes(unavailable)
        ) or bool(exclusions)
        self.session_open, self.session_close = observation.session_open, observation.session_close
        self.candidate_count = sum(decision.action == "BUY" for decision in decisions)
        self.last_evaluated_at = now
        if decisions:
            self.last_observed_work_at = now

    def snapshot(
        self,
        *,
        cycle_id: str,
        now: datetime,
        waiting: bool,
        reasons: tuple[str, ...],
        failure: bool,
    ) -> dict[str, Any]:
        reported_codes = reason_codes((*self.evaluation_reasons, *reasons))
        codes = tuple(code.rsplit(":", 1)[-1] for code in reported_codes)
        outcomes = []
        if waiting:
            outcomes.append("AWAITING_EVALUATION_BOUNDARY")
        if "MARKET_CLOSED" in codes or (
            self.session_open is not None
            and self.session_close is not None
            and not (self.session_open <= now < self.session_close)
        ):
            outcomes.append("MARKET_CLOSED")
        if _SESSION_WAIT_REASONS.intersection(codes) - {"MARKET_CLOSED"}:
            outcomes.append("AWAITING_SESSION_BOUNDARY")
        if _ACQUISITION_REASONS.intersection(codes):
            outcomes.append("AUTHORITY_ACQUISITION_UNAVAILABLE")
        if _DATA_REASONS.intersection(codes):
            outcomes.append("DATA_UNAVAILABLE")
        if self.excluded:
            outcomes.append("CANDIDATES_EXCLUDED")
        if any("GUARDIAN" in code for code in codes):
            outcomes.append("AWAITING_GUARDIAN")
        if any(
            code not in _DATA_REASONS | _ACQUISITION_REASONS | _SESSION_WAIT_REASONS
            and code != "UNCLASSIFIED_REASON"
            for code in (reason.rsplit(":", 1)[-1] for reason in reason_codes(reasons))
        ) or any(
            code
            in _CRYPTO_ENTRY_REASONS
            | {
                "ENTRY_RISK_LIMIT",
                "BUYING_POWER_RESERVE",
                "INVENTORY_UNCERTIFIED",
                "INVENTORY_STALE",
                "CORE_MODEL_UNCOMMISSIONED",
            }
            for code in codes
        ):
            outcomes.append("RISK_OR_AUTHORITY_BLOCKED")
        if self.submission_requests:
            outcomes.append("SUBMISSION_REQUESTED")
        if self.acknowledgements:
            outcomes.append("EXECUTION_ACKNOWLEDGED")
        if self.confirmed_fills:
            outcomes.append("FILL_CONFIRMED")
        if failure or self.dispatch_failed:
            outcomes.append("OPERATIONAL_FAILURE")
        if not outcomes:
            outcomes.append(
                "CANDIDATE_AVAILABLE" if self.candidate_count else "NO_QUALIFYING_CANDIDATE"
            )
        return {
            "event": "PAPER_TRADER_CYCLE",
            "cycle_id": cycle_id,
            "recorded_at": now.isoformat(),
            "outcomes": outcomes,
            "reason_codes": reported_codes,
            "evaluation_evidence_ids": self.evaluation_evidence_ids,
            "last_evaluated_at": self.last_evaluated_at.isoformat()
            if self.last_evaluated_at
            else None,
            "last_observed_work_at": self.last_observed_work_at.isoformat()
            if self.last_observed_work_at
            else None,
            "candidate_count": self.candidate_count,
            "submission_requests": self.submission_requests,
            "acknowledgements": self.acknowledgements,
            "confirmed_fills": self.confirmed_fills,
        }


def emit_cycle(payload: dict[str, Any]) -> None:
    try:
        print(json.dumps(payload, separators=(",", ":")), flush=True)
    except (OSError, ValueError):
        pass  # A diagnostic output failure cannot alter trading permission.
