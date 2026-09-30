"""One frozen noise-band experiment; collection GETs are separate from measurement."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from scripts import late_day_research as ledger

RULE = "spy-noise-band14-long-first-entry-same-band-exit-v1"
CHECKS = [datetime(2000, 1, 1, 10) + timedelta(minutes=30 * i) for i in range(12)]
SIGNALS = [(t - timedelta(minutes=1)).strftime("%H:%M") for t in CHECKS]
FILLS = [(t + timedelta(minutes=1)).strftime("%H:%M") for t in CHECKS]
KEEP = set(SIGNALS + FILLS + ["09:30", "15:59"])


def choose_trade(current: dict[str, Any], previous: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(previous) != 14:
        return None
    anchor = max(ledger.positive(current["open"]), ledger.positive(current["previous_close"]))
    selected = None
    for check in sorted(current["prices"]):
        move = (
            sum(
                (
                    abs(ledger.positive(p["prices"][check]) / ledger.positive(p["open"]) - 1)
                    for p in previous
                ),
                ledger.D(0),
            )
            / 14
        )
        band = anchor * (1 + move)
        price = ledger.positive(current["prices"][check])
        if selected is not None and price <= band:
            return selected | {"exit": current["fills"][check], "exit_check": check}
        if selected is None and price > band:
            selected = {
                "entry": current["fills"][check],
                "entry_check": check,
                "band_at_entry": str(band),
            }
    return None if selected is None else selected | {"exit": current["final"], "exit_check": None}


def construct_rows(
    parent: dict[str, Any], minutes: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    base = ledger.build_rows(parent["content"] | {"minutes": minutes})
    by_day: dict[str, dict[str, Any]] = {}
    for b in minutes:
        t = ledger.timestamp(b["t"])
        by_day.setdefault(t.date().isoformat(), {})[t.strftime("%H:%M")] = b
    sessions: list[dict[str, Any] | None] = []
    for r in base:
        bars = by_day.get(r["date"], {})
        if not r["eligible"] or not KEEP <= set(bars):
            sessions.append(None)
        else:
            sessions.append(
                dict(
                    open=bars["09:30"]["o"],
                    previous_close=r["previous_close"],
                    prices={i: bars[t]["c"] for i, t in enumerate(SIGNALS)},
                    fills={i: bars[t]["o"] for i, t in enumerate(FILLS)},
                    final=bars["15:59"]["o"],
                )
            )
    candidate, reference, selected_trades = [], [], {}
    for i, r in enumerate(base):
        row = r.copy()
        previous = sessions[max(0, i - 14) : i]
        if sessions[i] is None:
            row.update(eligible=False, exclusion=r["exclusion"] or "missing_required_noise_minute")
        elif len(previous) != 14 or any(p is None for p in previous):
            row.update(eligible=False, exclusion="incomplete_14_session_lookback")
        if row["eligible"]:
            current = sessions[i]
            assert current is not None
            chosen = choose_trade(current, [p for p in previous if p is not None])
            benchmark = row | dict(
                signal_close="2",
                previous_close="1",
                entry=str(current["fills"][0]),
                exit=str(current["final"]),
            )
            row = benchmark | dict(signal_close="1", previous_close="2")
            if chosen:
                selected_trades[row["date"]] = chosen
                row.update(
                    signal_close="2",
                    previous_close="1",
                    entry=str(chosen["entry"]),
                    exit=str(chosen["exit"]),
                )
        else:
            benchmark = row.copy()
        candidate.append(row)
        reference.append(benchmark)
    return candidate, reference, selected_trades


def load_parent(path: Path) -> dict[str, Any]:
    parent = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parent, dict):
        raise ValueError("parent must be an object")
    c = parent["content"]
    if ledger.digest(c) != parent["content_hash"] or any(
        c[k] != v
        for k, v in dict(rule=ledger.RULE, start=ledger.START, end=ledger.END, feed="sip").items()
    ):
        raise ValueError("parent identity differs")
    return parent


def collect(parent_path: Path, env_file: Path, output: Path) -> None:
    import httpx

    from quantbot.operations.service import _parse_dotenv

    if output.exists():
        raise ValueError("immutable input already exists")
    parent = load_parent(parent_path)
    values = _parse_dotenv(env_file)
    if values.get("TRADING_MODE") != "PAPER":
        raise ValueError("paper configuration required")
    params: dict[str, Any] = dict(
        timeframe="1Min",
        adjustment="raw",
        feed="sip",
        limit=10000,
        start=ledger.START + "T00:00:00Z",
        end=ledger.END + "T23:59:59Z",
    )
    minutes, tokens = [], set()
    with httpx.Client(
        headers={
            "APCA-API-KEY-ID": values["ALPACA_PAPER_API_KEY"],
            "APCA-API-SECRET-KEY": values["ALPACA_PAPER_API_SECRET"],
        },
        timeout=60,
    ) as client:
        for page in range(400):
            response = client.get("https://data.alpaca.markets/v2/stocks/SPY/bars", params=params)
            response.raise_for_status()
            payload = response.json()
            for b in payload.get("bars", []):
                if ledger.timestamp(b["t"]).strftime("%H:%M") in KEEP:
                    minutes.append(b)
            token = payload.get("next_page_token")
            if not token:
                break
            if token in tokens:
                raise ValueError("repeated pagination token")
            tokens.add(token)
            params["page_token"] = token
            if page % 40 == 39:
                print(f"Collected {page + 1} pages", flush=True)
        else:
            raise ValueError("page bound reached")
    content = dict(rule=RULE, parent_input_hash=parent["content_hash"], minutes=minutes)
    ledger.write_new(
        output, dict(content=content, content_hash=ledger.digest(content), pages=page + 1)
    )
    # Validate quality only; do not construct signals or inspect strategy returns in collection.
    rows = ledger.build_rows(parent["content"] | {"minutes": minutes})
    print(
        json.dumps(
            dict(
                pages=page + 1,
                minute_count=len(minutes),
                sessions=len(rows),
                base_eligible=sum(r["eligible"] for r in rows),
            )
        )
    )


def measure(
    parent_path: Path, bundle: Path, audit_path: Path, database: Path, output: Path
) -> None:
    from sqlalchemy import select

    from quantbot.research.intraday_accounting import record_intraday_attempt
    from quantbot.research.memory import RecordKind, ResearchMemory, ResearchRecord, Verdict
    from quantbot.storage import Database, SingleWriterLock
    from quantbot.storage.schema import search_runs

    if output.exists() or output.with_suffix(".manifest.json").exists():
        raise ValueError("existing evidence; no automatic retry")
    parent = load_parent(parent_path)
    doc = json.loads(bundle.read_text(encoding="utf-8"))
    content = doc["content"]
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if (
        ledger.digest(content) != doc["content_hash"]
        or content["rule"] != RULE
        or content["parent_input_hash"] != parent["content_hash"]
        or audit.get("input_hash") != parent["content_hash"]
        or audit.get("differences") != {}
        or audit.get("library") != "exchange_calendars"
        or audit.get("expected_sessions") != len(parent["content"]["calendar"])
        or audit.get("actual_sessions") != len(parent["content"]["calendar"])
    ):
        raise ValueError("input or calendar audit identity differs")
    manifest = dict(
        rule=RULE,
        input_hash=doc["content_hash"],
        parent_hash=parent["content_hash"],
        audit_hash=ledger.digest(audit),
        costs=[5, 10],
        source_hashes={
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (Path(__file__), Path(ledger.__file__))
        },
    )
    config_hash = ledger.digest(manifest)
    identity = "intraday:" + config_hash
    with (
        SingleWriterLock(database.with_suffix(".intraday-research.lock")),
        Database(database) as db,
    ):
        with db.transaction() as s:
            if s.scalar(select(search_runs.c.search_id).where(search_runs.c.search_id == identity)):
                raise ValueError("attempt already charged")
            record_intraday_attempt(
                s,
                search_id=identity,
                configuration_hash=config_hash,
                start=date.fromisoformat(ledger.START),
                end=date.fromisoformat(ledger.END),
                now=datetime.now(UTC),
            )
        ledger.write_new(
            output.with_suffix(".manifest.json"),
            manifest | dict(search_id=identity, frozen_at=datetime.now(UTC).isoformat()),
        )
        rows, reference, selected = construct_rows(parent, content["minutes"])
        result: dict[str, Any] = dict(
            search_id=identity,
            manifest=manifest,
            status="EXPLORATORY_ONLY",
            promotion_authorized=False,
        )
        for label, cost in [("baseline", 5), ("stress", 10)]:
            account = ledger.simulate(rows, cost)
            benchmark = ledger.simulate(reference, cost)
            # The reused engine's internal comparator uses candidate-specific intervals;
            # replace it with the declared fixed10:01..15:59 benchmark and omit its HAC.
            account.pop("paired_mean_daily_difference")
            account.pop("paired_hac5_standard_error")
            account["benchmark_ending_equity"] = benchmark["ending_equity"]
            account["benchmark_net_return"] = benchmark["net_return"]
            for day, refday in zip(account["daily"], benchmark["daily"], strict=True):
                day["benchmark_equity"] = refday["equity"]
                day["benchmark_return"] = refday["strategy_return"]
            for trade in account["trades"]:
                selection = selected[trade["date"]]
                trade["entry_time"] = FILLS[selection["entry_check"]]
                trade["exit_time"] = (
                    "15:59" if selection["exit_check"] is None else FILLS[selection["exit_check"]]
                )
            recent = [d for d in account["daily"] if d["date"] >= "2025-01-01"]
            compound = 1.0
            for d in recent:
                compound *= 1 + d["strategy_return"]
            account["recent_return"] = compound - 1
            result[label] = account
        ledger.write_new(output, result)
        b = result["baseline"]
        with db.transaction() as s:
            ResearchMemory(s).record(
                ResearchRecord(
                    record_id=identity + ":result",
                    kind=RecordKind.FINDING,
                    subject="SPY noise-band14 long-only first-entry exploratory test",
                    statement=(
                        f"Frozen consumed-history noise-band test: {len(b['trades'])} trades; "
                        f"net return {b['net_return']}; recent return {b['recent_return']}. "
                        "No confirmation or promotion."
                    ),
                    verdict=Verdict.EXPLORATORY_ONLY,
                    source="noise-band-research/v1",
                    recorded_at=datetime.now(UTC),
                    evidence_hash=ledger.digest(result),
                    detail={"artifact": str(output), "search_id": identity},
                )
            )
    print(
        json.dumps(
            {
                label: {k: v for k, v in result[label].items() if k not in ("trades", "daily")}
                for label in ("baseline", "stress")
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["collect", "measure"])
    for name in ["parent", "bundle", "audit", "database", "output", "env-file"]:
        p.add_argument("--" + name, type=Path, required=name in ("parent", "bundle"))
    a = p.parse_args()
    if a.action == "collect" and a.env_file:
        collect(a.parent, a.env_file, a.bundle)
    elif a.action == "measure" and a.audit and a.database and a.output:
        measure(a.parent, a.bundle, a.audit, a.database, a.output)
    else:
        p.error("collect needs env-file; measure needs audit,database,output")
