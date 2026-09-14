"""Frozen late-day research: historical GET collection and credential-free measurement."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

START, END = "2016-01-04", "2026-08-21"
RULE = "spy-late-day-long-1531-1559-v1"
NY = ZoneInfo("America/New_York")
D = Decimal


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_new(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)


def positive(value: Any) -> Decimal:
    number = D(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError("price must be finite and positive")
    return number


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires explicit timezone")
    return parsed.astimezone(NY)


def simulate(rows: list[dict[str, Any]], cost_bps: int) -> dict[str, Any]:
    if cost_bps not in (5, 10):
        raise ValueError("only frozen cost cases permitted")
    cash = [D(500), D(500)]
    pending: list[list[tuple[int, Decimal]]] = [[], []]
    trades, daily = [], []
    peak, drawdown = D(500), D(0)
    min_cash, sleeve, gross_log = D(500), D(1), 0.0
    prior_index, prior_date = -1, ""
    cost = D(cost_bps) / D(10000)
    for row in rows:
        index = int(row["index"])
        date.fromisoformat(row["date"])
        if index <= prior_index or row["date"] <= prior_date:
            raise ValueError("sessions must be unique and chronological")
        prior_index, prior_date = index, row["date"]
        for arm in (0, 1):
            cash[arm] += sum((v for due, v in pending[arm] if due <= index), D(0))
            pending[arm] = [(due, v) for due, v in pending[arm] if due > index]
        before = [cash[a] + sum((v for _, v in pending[a]), D(0)) for a in (0, 1)]
        pnl = [D(0), D(0)]
        if row["eligible"]:
            signal, previous, entry, exit_price = [
                positive(row[k]) for k in ("signal_close", "previous_close", "entry", "exit")
            ]
            buy, sell = entry * (1 + cost), exit_price * (1 - cost)
            for arm in (0, 1):
                if arm == 0 and signal <= previous:
                    continue
                quantity = (min(before[arm] * D(".09"), cash[arm]) / buy).quantize(
                    D(".000001"), rounding=ROUND_DOWN
                )
                if quantity <= 0:
                    continue
                notional, proceeds = quantity * buy, quantity * sell
                cash[arm] -= notional
                pending[arm].append((index + 2, proceeds))
                pnl[arm] = proceeds - notional
                if arm == 0:
                    trades.append(
                        dict(
                            date=row["date"],
                            quantity=str(quantity),
                            buy_price=str(buy),
                            sell_price=str(sell),
                            notional=str(notional),
                            pnl=str(pnl[arm]),
                            equity_before=str(before[arm]),
                        )
                    )
                    sleeve *= sell / buy
                    gross_log += math.log(float(exit_price / entry))
        after = [before[a] + pnl[a] for a in (0, 1)]
        peak = max(peak, after[0])
        drawdown = max(drawdown, (peak - after[0]) / peak)
        min_cash = min(min_cash, cash[0])
        daily.append(
            dict(
                date=row["date"],
                eligible=bool(row["eligible"]),
                exclusion=row["exclusion"],
                equity=str(after[0]),
                benchmark_equity=str(after[1]),
                strategy_return=float(pnl[0] / before[0]),
                benchmark_return=float(pnl[1] / before[1]),
                unsettled_proceeds=float(sum((v for _, v in pending[0]), D(0))),
            )
        )
    ending = [cash[a] + sum((v for _, v in pending[a]), D(0)) for a in (0, 1)]
    differences = [r["strategy_return"] - r["benchmark_return"] for r in daily if r["eligible"]]
    n = len(differences)
    mean = sum(differences) / n if n else 0.0
    centered = [v - mean for v in differences]
    variance = sum(v * v for v in centered) / n if n else 0.0
    for lag in range(1, min(5, n - 1) + 1):
        variance += (
            2 * (1 - lag / 6) * sum(centered[i] * centered[i - lag] for i in range(lag, n)) / n
        )
    g = math.exp(gross_log / len(trades)) if trades else 1.0
    return dict(
        status="EXPLORATORY_ONLY",
        promotion_authorized=False,
        ending_equity=str(ending[0]),
        benchmark_ending_equity=str(ending[1]),
        net_return=float(ending[0] / D(500) - 1),
        benchmark_net_return=float(ending[1] / D(500) - 1),
        normalized_sleeve_return=float(sleeve - 1),
        sampled_max_drawdown=float(drawdown),
        trades=trades,
        daily=daily,
        eligible_sessions=n,
        exclusions=dict(Counter(r["exclusion"] for r in rows if not r["eligible"])),
        minimum_available_cash=float(min_cash),
        winning_trades=sum(D(t["pnl"]) > 0 for t in trades),
        break_even_cost_bps_per_side=10000 * (g - 1) / (g + 1),
        paired_mean_daily_difference=mean,
        paired_hac5_standard_error=math.sqrt(max(0, variance) / n) if n else None,
    )


def build_rows(content: dict[str, Any]) -> list[dict[str, Any]]:
    calendar = content["calendar"]
    dates = [row["date"] for row in calendar]
    if not dates or dates != sorted(set(dates)) or any(not START <= d <= END for d in dates):
        raise ValueError("calendar must be unique, ordered and within frozen range")
    bars: dict[tuple[str, str], dict[str, Any]] = {}
    for bar in content["minutes"]:
        stamp = timestamp(bar["t"])
        key = (stamp.date().isoformat(), stamp.strftime("%H:%M"))
        if key in bars or stamp.second or stamp.microsecond:
            raise ValueError("duplicate or unaligned minute bar")
        o, h, low, close = [positive(bar[k]) for k in ("o", "h", "l", "c")]
        if not low <= min(o, close) <= max(o, close) <= h:
            raise ValueError("invalid OHLC")
        bars[key] = bar
    daily: dict[str, dict[str, Decimal]] = {}
    for kind in ("raw", "adjusted"):
        daily[kind] = {}
        for bar in content[kind]:
            day = timestamp(bar["t"]).date().isoformat()
            if day in daily[kind]:
                raise ValueError("duplicate daily bar")
            daily[kind][day] = positive(bar["c"])
    if set(daily["raw"]) - set(dates):
        raise ValueError("daily bar missing from calendar")
    action_days = {
        a["ex_date"]
        for values in content["corporate_actions"].values()
        for a in values
        if a.get("ex_date")
    }
    result = []
    for i, session in enumerate(calendar):
        day = session["date"]
        prev = calendar[i - 1]["date"] if i else ""
        reason = ""
        if session["open"] != "09:30" or session["close"] != "16:00":
            reason = "short_or_nonstandard_session"
        elif not prev or any(d not in daily[k] for d in (prev, day) for k in daily):
            reason = "missing_daily_reference"
        elif day in action_days:
            reason = "corporate_action"
        else:
            # Adjusted daily closes are cent-rounded. Compare uncertainty intervals,
            # not exact ratios whose rounding noise looks like corporate actions.
            half_cent = D(".005")
            low_now = (daily["adjusted"][day] - half_cent) / daily["raw"][day]
            high_now = (daily["adjusted"][day] + half_cent) / daily["raw"][day]
            low_prev = (daily["adjusted"][prev] - half_cent) / daily["raw"][prev]
            high_prev = (daily["adjusted"][prev] + half_cent) / daily["raw"][prev]
            if low_now > high_prev * D("1.00001") or low_prev > high_now * D("1.00001"):
                reason = "adjustment_factor_change"
        if not reason and any((day, t) not in bars for t in ("15:29", "15:31", "15:59")):
            reason = "missing_required_minute"
        row: dict[str, Any] = dict(index=i, date=day, eligible=not reason, exclusion=reason)
        if not reason:
            row.update(
                signal_close=str(bars[day, "15:29"]["c"]),
                previous_close=str(daily["raw"][prev]),
                entry=str(bars[day, "15:31"]["o"]),
                exit=str(bars[day, "15:59"]["o"]),
            )
        result.append(row)
    return result


def collect(env_file: Path, output: Path) -> None:
    import httpx

    from quantbot.operations.service import _parse_dotenv

    if output.exists():
        raise ValueError("immutable bundle already exists")
    values = _parse_dotenv(env_file)
    if values.get("TRADING_MODE") != "PAPER":
        raise ValueError("paper configuration required")
    headers = {
        "APCA-API-KEY-ID": values["ALPACA_PAPER_API_KEY"],
        "APCA-API-SECRET-KEY": values["ALPACA_PAPER_API_SECRET"],
    }
    with httpx.Client(headers=headers, timeout=60) as client:

        def get(url: str, params: dict[str, Any]) -> dict[str, Any]:
            response = client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("expected object response")
            return payload

        response = client.get(
            "https://paper-api.alpaca.markets/v2/calendar", params={"start": START, "end": END}
        )
        response.raise_for_status()
        calendar = response.json()
        content: dict[str, Any] = dict(
            rule=RULE, start=START, end=END, feed="sip", calendar=calendar
        )
        actions = get(
            "https://data.alpaca.markets/v1/corporate-actions",
            dict(symbols="SPY", start=START, end=END, limit=1000),
        )
        if actions.get("next_page_token"):
            raise ValueError("corporate action response exceeds bound")
        content["corporate_actions"] = actions["corporate_actions"]
        pages = {}
        for label, frame, adjustment in (
            ("raw", "1Day", "raw"),
            ("adjusted", "1Day", "all"),
            ("minutes", "1Min", "raw"),
        ):
            params = dict(
                timeframe=frame,
                adjustment=adjustment,
                feed="sip",
                limit=10000,
                start=START + "T00:00:00Z",
                end=END + "T23:59:59Z",
            )
            records, tokens = [], set()
            for page in range(400):
                payload = get("https://data.alpaca.markets/v2/stocks/SPY/bars", params)
                for bar in payload.get("bars", []):
                    stamp = datetime.fromisoformat(bar["t"].replace("Z", "+00:00")).astimezone(NY)
                    if label != "minutes" or stamp.strftime("%H:%M") in ("15:29", "15:31", "15:59"):
                        records.append(bar)
                token = payload.get("next_page_token")
                if not token:
                    break
                if token in tokens:
                    raise ValueError("repeating pagination token")
                tokens.add(token)
                params["page_token"] = token
                if page % 20 == 19:
                    print(f"Collected {page + 1} {label} pages", flush=True)
            else:
                raise ValueError("historical page bound reached")
            content[label], pages[label] = records, page + 1
    rows = build_rows(content)
    document = dict(
        content=content,
        content_hash=digest(content),
        collected_at=datetime.now(UTC).isoformat(),
        pages=pages,
    )
    write_new(output, document)
    print(
        json.dumps(
            dict(
                bundle=str(output),
                sessions=len(rows),
                eligible=sum(r["eligible"] for r in rows),
                exclusions=dict(Counter(r["exclusion"] for r in rows if not r["eligible"])),
                pages=pages,
            )
        )
    )


def measure(bundle: Path, database: Path, output: Path, calendar_audit: Path) -> None:
    from sqlalchemy import select

    from quantbot.research.intraday_accounting import record_intraday_attempt
    from quantbot.research.memory import RecordKind, ResearchMemory, ResearchRecord, Verdict
    from quantbot.storage import Database, SingleWriterLock
    from quantbot.storage.schema import search_runs

    if output.exists() or output.with_suffix(".manifest.json").exists():
        raise ValueError("existing result or manifest; no automatic retry")
    doc = json.loads(bundle.read_text(encoding="utf-8"))
    content = doc["content"]
    if digest(content) != doc["content_hash"] or any(
        content[k] != v for k, v in dict(rule=RULE, start=START, end=END, feed="sip").items()
    ):
        raise ValueError("input identity differs from frozen protocol")
    audit = json.loads(calendar_audit.read_text(encoding="utf-8"))
    if (
        audit.get("input_hash") != doc["content_hash"]
        or audit.get("differences") != {}
        or audit.get("expected_sessions") != len(content["calendar"])
        or audit.get("actual_sessions") != len(content["calendar"])
        or audit.get("library") != "exchange_calendars"
    ):
        raise ValueError("independent calendar audit is missing, mismatched or failed")
    manifest = dict(
        rule=RULE,
        input_hash=doc["content_hash"],
        costs=[5, 10],
        code_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        start=START,
        end=END,
        status="EXPLORATORY_ONLY",
        calendar_audit_hash=digest(audit),
    )
    config_hash = digest(manifest)
    identity = "intraday:" + config_hash
    with (
        SingleWriterLock(database.with_suffix(".intraday-research.lock")),
        Database(database) as db,
    ):
        with db.transaction() as session:
            if session.scalar(
                select(search_runs.c.search_id).where(search_runs.c.search_id == identity)
            ):
                raise ValueError("attempt already charged; no automatic retry")
            record_intraday_attempt(
                session,
                search_id=identity,
                configuration_hash=config_hash,
                start=date.fromisoformat(START),
                end=date.fromisoformat(END),
                now=datetime.now(UTC),
            )
        write_new(
            output.with_suffix(".manifest.json"),
            manifest | dict(search_id=identity, frozen_at=datetime.now(UTC).isoformat()),
        )
        rows = build_rows(content)
        result: dict[str, Any] = dict(
            manifest=manifest,
            search_id=identity,
            baseline=simulate(rows, 5),
            stress=simulate(rows, 10),
            measured_at=datetime.now(UTC).isoformat(),
            promotion_authorized=False,
        )
        write_new(output, result)
        baseline = result["baseline"]
        with db.transaction() as session:
            ResearchMemory(session).record(
                ResearchRecord(
                    record_id=identity + ":result",
                    kind=RecordKind.FINDING,
                    subject="Late-day SPY long-only 15:31 to 15:59 exploratory test",
                    statement=(
                        f"Fixed consumed-history test: {len(baseline['trades'])} trades; "
                        f"account net return {baseline['net_return']}; "
                        f"benchmark {baseline['benchmark_net_return']}; "
                        "5bps each side. No confirmation or promotion."
                    ),
                    verdict=Verdict.EXPLORATORY_ONLY,
                    source="late-day-research/v1",
                    recorded_at=datetime.now(UTC),
                    evidence_hash=digest(result),
                    detail={"artifact": str(output), "search_id": identity},
                )
            )
    print(
        json.dumps(
            {
                k: {x: v for x, v in result[k].items() if x not in ("daily", "trades")}
                for k in ("baseline", "stress")
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("collect", "measure"))
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--calendar-audit", type=Path)
    args = parser.parse_args()
    if args.action == "collect" and args.env_file:
        collect(args.env_file, args.bundle)
    elif args.action == "measure" and args.database and args.output and args.calendar_audit:
        measure(args.bundle, args.database, args.output, args.calendar_audit)
    else:
        parser.error(
            "collection needs env-file; measurement needs database, output and calendar-audit"
        )
