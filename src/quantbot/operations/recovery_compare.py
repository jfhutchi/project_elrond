"""Offline comparison of captured evidence. No database, network or runtime access."""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Mapping, Sequence
from typing import Any

FIELDS = (
    "store_identity",
    "schema",
    "revision",
    "latest_run",
    "last_successful_cycle_at",
    "latest_market_observation",
    "latest_reconciliation",
    "latest_account_snapshot",
    "account_snapshot_content",
    "deployments",
    "qualification_rows",
    "history_counts",
    "latest_session_observation",
    "last_successful_observation",
    "ingestion_cursors",
    "ingestion_cursor_content",
    "ingestion_sources",
    "latest_ingestion_runs",
    "runtime_observations",
    "kill_switch",
    "content_sha256",
)


def _known(value: Any) -> bool:
    return value is not None and value != "UNKNOWN"


def compare_reports(
    reports: Mapping[str, dict[str, Any]], *, expected_hosts: Sequence[str]
) -> dict[str, Any]:
    """Labels are operator-supplied capture labels, never authenticated host identities.

    File IDs are local to one capture. Even matching hostnames/UUIDs/bytes in two captures
    cannot prove the same physical device, exclusivity, or legitimacy of recorded claims.
    """
    candidates: list[dict[str, Any]] = []
    coverage: dict[str, Any] = {}
    for label, report in sorted(reports.items()):
        if (
            report.get("report_version") != 1
            or not isinstance(report.get("stores"), list)
            or not isinstance(report.get("host"), dict)
        ):
            raise ValueError("UNSUPPORTED_OR_INVALID_REPORT")
        coverage[label] = {
            "host": report["host"],
            "as_of": report.get("as_of", "UNKNOWN"),
            "checkout": report.get("checkout", "UNKNOWN"),
            "collector": report.get("collector", "UNKNOWN"),
            "os_inventory": report.get("os_inventory", "UNKNOWN"),
            "candidate_scans": report.get("candidate_scans", []),
            "candidate_states": [
                {"path": item.get("path"), "evidence_state": item.get("evidence_state")}
                for item in report["stores"]
                if isinstance(item, dict)
            ],
            "report_sha256": hashlib.sha256(
                json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "retirement": "UNKNOWN",
        }
        aliases: dict[tuple[str, str], dict[str, Any]] = {}
        for store in report["stores"]:
            if not isinstance(store, dict) or not isinstance(store.get("path"), str):
                raise ValueError("INVALID_STORE_ENTRY")
            if store.get("history_counts") is not None and not isinstance(
                store["history_counts"], dict
            ):
                raise ValueError("INVALID_HISTORY_COUNTS")
            if store.get("evidence_state") != "READABLE_SNAPSHOT" or not (
                any(
                    store.get(field)
                    for field in (
                        "latest_run",
                        "deployments",
                        "latest_account_snapshot",
                        "runtime_observations",
                        "ingestion_cursors",
                        "latest_market_observation",
                        "qualification_rows",
                    )
                )
                or any((store.get("history_counts") or {}).values())
            ):
                continue
            # Collapse only proven aliases in the SAME capture with matching content hash.
            file_id, content = store.get("file_id"), store.get("content_sha256")
            key = (
                (str(file_id), str(content))
                if file_id and _known(file_id) and _known(content)
                else (store["path"], "")
            )
            if key in aliases:
                aliases[key]["aliases"].append(store["path"])
                continue
            candidate = {
                "id": f"{label}:{store['path']}",
                "capture": label,
                "physical_identity": {"capture": label, "file_id": file_id},
                "path": store["path"],
                "aliases": [],
                "evidence": {field: store.get(field) for field in FIELDS},
            }
            aliases[key] = candidate
            candidates.append(candidate)
    if len(candidates) > 200:
        raise ValueError("TOO_MANY_CANDIDATES_SPLIT_COMPARISON")
    pairs = []
    for left, right in itertools.combinations(candidates, 2):
        differing, unavailable = [], []
        for field in FIELDS:
            a, b = left["evidence"][field], right["evidence"][field]
            if not _known(a) or not _known(b):
                unavailable.append(field)
            elif a != b:
                differing.append(field)
        reasons = ["Distinct capture/file candidates; no exclusive writer or authority proof."]
        if differing:
            reasons.append("Recorded evidence differs: " + ", ".join(differing) + ".")
        else:
            reasons.append("Matching bounded evidence does not establish complete common history.")
        if "content_sha256" in differing:
            reasons.append(
                "File bytes differ; this can reflect ledger or SQLite layout differences."
            )
        if unavailable:
            reasons.append("Missing evidence prevents comparison: " + ", ".join(unavailable) + ".")
        reasons.append("No newer/configured/larger/mtime preference can establish legitimacy.")
        pairs.append(
            {
                "left": left["id"],
                "right": right["id"],
                "differing_fields": differing,
                "unavailable_fields": unavailable,
                "automatic_choice_unsafe_because": reasons,
            }
        )
    return {
        "report_version": 1,
        "ok": False,
        "command": "recovery-compare",
        "store_authority": "AMBIGUOUS" if len(candidates) > 1 else "UNKNOWN",
        "selected_store": None,
        "startup_allowed": False,
        "missing_host_captures": sorted(set(expected_hosts) - set(reports)),
        "captures": coverage,
        "candidates": candidates,
        "comparisons": pairs,
        "limitations": [
            "Reports are unauthenticated snapshots; hashes cannot establish truthful provenance.",
            "Bounded scans cannot prove global absence. Retirement requires verification.",
            "Latest/SUCCEEDED/qualified rows are claims; legitimacy needs independent evidence.",
            "Raw ingestion timestamps are comparable; deployment-status evaluates freshness.",
            "Copies need operator reconciliation; no merge or winner is generated.",
        ],
    }


def render_comparison(report: Mapping[str, Any]) -> str:
    def cell(value: Any) -> str:
        return json.dumps(value, sort_keys=True, ensure_ascii=True).replace("|", "\\|")

    lines = [
        f"Store authority: {report['store_authority']}",
        "Selected store: none. Startup allowed: false.",
        "Missing host captures: " + cell(report["missing_host_captures"]),
        "",
    ]
    for label, capture in report["captures"].items():
        lines += [f"Capture: {cell(label)}", "", "| Evidence | Value |", "|---|---|"]
        lines += [f"| {key} | {cell(value)} |" for key, value in capture.items()]
        lines.append("")
    for candidate in report["candidates"]:
        lines += [
            "Candidate: " + cell(candidate["id"]),
            "",
            "Physical identity (capture-local): " + cell(candidate["physical_identity"]),
            "Aliases: " + cell(candidate["aliases"]),
            "",
            "| Evidence | Value |",
            "|---|---|",
        ]
        lines += [f"| {key} | {cell(value)} |" for key, value in candidate["evidence"].items()]
        lines.append("")
    for pair in report["comparisons"]:
        lines += [cell(pair["left"]) + " compared with " + cell(pair["right"]), ""]
        lines += ["- " + reason for reason in pair["automatic_choice_unsafe_because"]]
        lines.append("")
    lines += ["- " + note for note in report["limitations"]]
    return "\n".join(lines) + "\n"
